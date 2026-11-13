# ruff: noqa: F811
"""Terminal lost answers use the same proof rules without reacquiring input."""
import pytest
from test_session_relay_native_ingress import native_case  # noqa: F401
from test_session_relay_public_interrupts import mutate, native_calls
from test_session_relay_public_writer import call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_terminal_grants import stream_case  # noqa: F401
from test_session_relay_terminal_interrupts import terminal_interrupt_case  # noqa: F401


@pytest.mark.parametrize('loss', ['native_answer', 'broker_reply'])
async def test_terminal_lost_answer_is_observed_without_any_resend(terminal_interrupt_case, loss):
    c = terminal_interrupt_case
    stream = c.launch.terminal_grants.record
    if loss == 'native_answer':
        original = c.owners[0].request
        async def lost(method, params):
            value = await original(method, params)
            if method == 'turn/interrupt':
                raise ConnectionError('Artificial lost Terminal native answer')
            return value
        c.owners[0].request = lost
    else:
        original = c.launch.peer.call
        async def lost(method, params):
            value = await original(method, params)
            if method == 'dispatch' and c.launch.grants[params['grant']].operation.method == 'turn.interrupt':
                raise ConnectionError('Artificial lost Terminal broker reply after journal settlement')
            return value
        c.launch.peer.call = lost
    params, identifier = await mutate(c.client, c, 'turn.interrupt', {
        'turnId': c.active, 'expectedControlRevision': 2}, 'terminal-lost')
    first = await call(c.client, 'turn.interrupt', {**params, 'operationId': identifier})
    assert first['result']['state'] == 'uncertain', first
    assert await call(c.client, 'turn.interrupt', {**params, 'operationId': identifier}) == first
    observed = (await call(c.client, 'operation.get', {'operationId': identifier}))['result']
    assert observed['state'] == ('uncertain' if loss == 'native_answer' else 'accepted')
    assert observed['actor'] == first['result']['actor'] and observed['payloadDigest'] == first['result']['payloadDigest']
    assert (await call(c.client, 'operation.get', {'operationId': identifier}))['result'] == observed
    assert len(native_calls(c)) == 1 and c.host.writes == [] and len(c.host.claims) == 1
    assert c.launch.terminal_grants.record is stream and not c.launch.grants
