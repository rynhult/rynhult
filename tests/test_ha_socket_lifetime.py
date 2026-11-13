"""HA demotion cancels owned socket work without canceling the ASGI server task."""
import asyncio
from types import SimpleNamespace

import pytest

from acc.server.db import make_engine, migrate
from acc.server.ha import Authority, Fenced
from acc.server.ha_runtime import Gate


async def test_demotion_joins_socket_child_and_releases_admitted_activity(tmp_path):
    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    authority = Authority(engine, 'main-a')
    authority.promote(expected_epoch=0)
    runtime = SimpleNamespace(sockets=set())
    entered = asyncio.Event()
    frames = []
    async def app(scope, receive, send):
        authority.check()
        assert asyncio.current_task() in runtime.sockets
        entered.set()
        await asyncio.Event().wait()
    async def receive():
        return {'type': 'websocket.connect'}
    async def send(frame):
        frames.append(frame)
    scope = {'type': 'websocket', 'path': '/api/nodes/connect', 'scheme': 'ws',
             'headers': [], 'server': ('fixture', 80), 'query_string': b''}
    server_task = asyncio.create_task(Gate(app, authority, runtime)(scope, receive, send))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert len(runtime.sockets) == authority.activities == 1
        assert server_task not in runtime.sockets
        authority.accepting = False
        for child in runtime.sockets:
            child.cancel()
        await asyncio.wait_for(server_task, 3)
        assert frames == [{'type': 'websocket.close', 'code': 1012}]
        assert not server_task.cancelled() and not server_task.cancelling()
        assert not runtime.sockets and authority.activities == 0
        with pytest.raises(Fenced):
            authority.check()
    finally:
        server_task.cancel()
        await asyncio.gather(server_task, return_exceptions=True)
        authority.release()
        engine.dispose()
