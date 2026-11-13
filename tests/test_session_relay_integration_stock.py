"""Authenticated answer application and every truthful turn outcome coexist."""
import asyncio

import pytest
from test_session_relay_public_writer import SID, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_stock_approval_frames import approval_case, key  # noqa: F401
from test_session_relay_terminal_grants import stream_case  # noqa: F401

from acc.server.models import RelayEvent, RelayOperation, RelaySession
from acc.server.session_relay.hub import Hub
from acc.server.session_relay.terminal_grants import NativeResolver, NativeSubmission
from acc.session_relay import protocol as p


@pytest.mark.parametrize('outcome', ['completed', 'interrupted', 'failed'])
async def test_stock_application_watermark_survives_turn_completion_and_queue_drain(approval_case, outcome):  # noqa: F811
    c = approval_case
    hub = Hub(c.ctx)
    run = str(c.launch.run_id)
    params = {'sessionId': SID, 'expectedRunId': run, 'text': 'Artificial active parent turn',
        'expectedConfigurationRevision': 0, 'expectedControlRevision': 2, 'ifBusy': 'queue'}
    stream = c.launch.terminal_grants.record
    stream.native = NativeSubmission(0, params)
    resolve = NativeResolver(c.launch.terminal_grants, stream, stream.native)
    active = hub.register(resolve, SID, run, 'active-stock-parent', 'turn.submit', p.operation_digest('turn.submit', params))
    stream.native.operation_id = active.operation_id
    hub.queue(resolve, SID, active.operation_id, expected_run=run, digest=p.operation_digest('turn.submit', params))
    hub.claim(resolve, SID, active.operation_id)
    assert 'result' in await call(c.client, 'terminal.input', key(c))
    ticket = c.approvals.submission
    assert (await c.approvals.reply(c.key_grant['inputGrant'], 1, c.offer['alias'], 'accept'))['state'] == 'uncertain'
    applied = {'kind': 'decision.applied', 'decisionId': 'key-question', 'operationId': ticket.operation_id,
        'choiceId': 'accept', 'actionDigest': c.digest, 'nativeRef': p.sha256_digest(b'joined exact fake application')}
    assert await c.launch.handle('publication', applied)
    assert c.launch.terminal_grants.record.processed == 1 and c.approvals.offer is None
    seen = []
    original = c.manager.drain
    async def drain(owner):
        seen.append(owner)
        return await original(owner)
    c.manager.drain = drain
    await c.launch.handle('publication', {'kind': 'turn.' + outcome, 'operationId': active.operation_id})
    await asyncio.wait_for(c.launch.drain_task, 5)
    assert seen == [c.launch]
    assert c.replies == [(7, {'decision': 'accept'})]
    with c.db() as db:
        assert db.get(RelaySession, SID).active_operation_id is None
        assert db.get(RelayOperation, ticket.operation_id).receipt['state'] == 'accepted'
        assert db.get(RelayOperation, active.operation_id).receipt['state'] == 'submitting'
        events = [row.event for row in db.query(RelayEvent).filter_by(session_id=SID)]
        assert sum(event.get('operationId') == active.operation_id and event['kind'] == 'turn.' + outcome for event in events) == 1
