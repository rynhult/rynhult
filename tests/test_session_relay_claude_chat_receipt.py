"""Original Chat receipt survives delayed publication after L1 handover."""
import asyncio

from test_session_relay_claude_agent import until
from test_session_relay_ownership import SID, ownership_case, select_owner  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.models import RelayOperation, RelaySession
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


async def test_post_handover_chat_late_receipt_never_redispatches_original(ownership_case):  # noqa: F811
    c = ownership_case
    await select_owner(c, 'terminal', 'receipt-source')
    await select_owner(c, 'chat', 'receipt-target')
    adapter = c.owned_factory.current
    entered, release = asyncio.Event(), asyncio.Event()
    publish = adapter.publish
    query = adapter.client.query
    queries = []
    async def counted(*args, **kwargs):
        queries.append(True)
        return await query(*args, **kwargs)
    async def held(event):
        if event['kind'] == 'operation.accepted':
            entered.set()
            await release.wait()
        return await publish(event)
    adapter.client.query, adapter.publish = counted, held
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'text': 'post-handover receipt fixture',
        'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
    registration = Hub(c.ctx).register(c.resolve, SID, str(c.launch.run_id), 'held-chat-receipt',
        'turn.submit', p.operation_digest('turn.submit', params))
    payload = {**params, 'operationId': registration.operation_id}
    try:
        original = await c.manager.submit(c.resolve, SID, payload)
        await asyncio.wait_for(entered.wait(), 5)
        assert original.state is p.ReceiptState.UNCERTAIN
        assert (await c.manager.submit(c.resolve, SID, payload)).state is p.ReceiptState.UNCERTAIN
        assert len(queries) == 1
        release.set()
        def finished():
            with c.db() as db:
                op = db.get(RelayOperation, original.operation_id)
                return op.receipt['state'] == 'accepted' and db.get(RelaySession, SID).active_operation_id is None
        await until(finished)
        settled = await c.manager.submit(c.resolve, SID, payload)
        assert settled.state is p.ReceiptState.ACCEPTED
        assert (settled.operation_id, settled.actor, settled.run_id, settled.payload_digest) == (
            original.operation_id, original.actor, original.run_id, original.payload_digest)
        assert len(queries) == 1
    finally:
        release.set()
