"""First-message presentation uses actual admitted turns, never a bootstrap send."""
from test_session_relay_claude_public import public_claude  # noqa: F401
from test_session_relay_ownership import SID, ownership_case  # noqa: F401
from test_session_relay_public_writer import attach, call
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.models import RelayOperation
from acc.session_relay import protocol as p


async def test_terminal_first_message_hint_requires_provider_admission(public_claude):  # noqa: F811
    from test_session_relay_claude_agent import until

    from acc.server.session_relay.hub import Hub
    c = public_claude
    c.enable()
    await attach(c.client, 'first-real-message', ['switch'])
    before = (await call(c.client, 'session.snapshot', {}))['result']
    assert before['l1']['terminalWaitingForChat'] is True
    owner = c.owned_factory.current
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'text': 'Person supplied first message',
        'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
    registered = Hub(c.ctx).register(c.resolve, SID, str(c.launch.run_id), 'first-real-message',
        'turn.submit', p.operation_digest('turn.submit', params))
    queued = (await call(c.client, 'session.snapshot', {}))['result']
    assert queued['l1']['terminalWaitingForChat'] is True
    assert c.owned_factory.current is owner and not owner.active
    await c.manager.submit(c.resolve, SID, {**params, 'operationId': registered.operation_id})
    def admitted():
        with c.db() as db:
            return db.get(RelayOperation, registered.operation_id).receipt['state'] == 'accepted'
    await until(admitted)
    after = (await call(c.client, 'session.snapshot', {}))['result']
    assert after['l1']['terminalWaitingForChat'] is False
    assert before['runId'] == after['runId'] and c.owned_factory.current is owner
    assert [receipt['operationId'] for receipt in after['receipts'] if receipt['method'] == 'turn.submit'] == [registered.operation_id]
