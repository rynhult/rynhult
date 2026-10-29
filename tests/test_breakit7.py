"""Break-it 7: only fixture-owned links and databases; never live agent tools."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from helpers import pause_supervisor, wait_for, write_db
from test_auth import live  # noqa: F401
from test_multihost import StandInNode, keypair

from acc.server.models import Node


@pytest.mark.parametrize('immediate', [False, True])
async def test_authenticated_link_consumes_reconnect_window_without_a_recorded_disconnect(api, env, live, immediate):  # noqa: F811
    await pause_supervisor(api)
    added = (await api.post('/api/nodes', json={'name': 'restart-pc'})).json()
    key, public = keypair()
    enrolled = await api.post('/api/nodes/enroll', json={'name': 'restart-pc', 'code': added['code'], 'public_key': public})
    assert enrolled.status_code == 200
    node_id = enrolled.json()['node_id']
    before = datetime.now(timezone.utc) - timedelta(days=1)
    with write_db(api.app.state.db) as s:
        node = s.get(Node, node_id)
        node.last_reconnected_at = before
        node.last_seen = before
        node.last_downtime_seconds = 42
        node.offline_since = None  # controller stopped before recording the disconnect
        node.downtime_until_reconnect = immediate
        node.downtime_policy = {'timezone': 'UTC', 'windows': [
            {'days': list(range(7)), 'start': '00:00', 'until_reconnect': True}]}
        s.commit()
    node = StandInNode(env['settings'].broker_socket, key, node_id)
    task = asyncio.create_task(node.run(live))
    try:
        await wait_for(lambda: hasattr(node, 'ws'))
        async def online():
            row = next(n for n in (await api.get('/api/nodes')).json() if n['name'] == 'restart-pc')
            return row if row['connected'] else None
        row = await wait_for(online)
        assert row['planned_downtime']['active'] is False
        assert row['downtime_until_reconnect'] is False
        with api.app.state.db() as s:
            assert s.get(Node, node_id).last_reconnected_at > before
            assert s.get(Node, node_id).last_downtime_seconds is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
