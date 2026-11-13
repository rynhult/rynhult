"""Managed Bash qualification cannot bypass unique native IDs or file ask policy."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from acc.broker.claude_hooks import ClaudeHookServer
from acc.broker.claude_tool_binding import prompt_digest
from acc.session_relay import protocol as p


def setup(tmp_path, tool='Bash', *, managed=True, observer=True, policy=False):
    hooks = ClaudeHookServer(tmp_path / 'hooks', SimpleNamespace(), AsyncMock(return_value=True),
        AsyncMock(), AsyncMock(), allow_policy=policy, accept_pid=lambda pid:pid==__import__('os').getpid())
    native = {'hook_event_name': 'PreToolUse', 'session_id': 'native', 'prompt_id': 'prompt',
        'tool_use_id': 'tool-one', 'tool_name': tool, 'tool_input': {'command': 'echo fixture'}}
    hooks.tool_bindings.observe(native)
    request = {**native, 'hook_event_name': 'PermissionRequest'}
    request.pop('tool_use_id')
    pending = {'promptDigest': prompt_digest(request), 'boundToolId': hooks.tool_bindings.bind(request),
        'promptId': 'prompt', 'toolName': tool, 'managed_bash_allow': managed,
        'allow_application': (lambda: 'exact-native-proof') if observer else None}
    return hooks, pending, native


@pytest.mark.parametrize(('tool', 'managed', 'observer', 'policy', 'allowed'), [
    ('Bash', True, True, False, True), ('Bash', False, True, False, False),
    ('Bash', True, False, False, False), ('Read', True, True, False, False),
    ('Bash', False, False, True, True),
])
def test_bound_managed_bash_and_conservative_native_policy_both_hold(tmp_path, tool, managed, observer, policy, allowed):
    hooks, pending, _ = setup(tmp_path, tool, managed=managed, observer=observer, policy=policy)
    hooks.pending['decision'] = pending
    assert hooks.allow_permitted(pending) is allowed


async def test_managed_bash_collision_during_verifier_refuses_without_consuming(tmp_path):
    hooks, pending, native = setup(tmp_path)
    pending.update(future=asyncio.get_running_loop().create_future(), digest=p.sha256_digest(b'action'),
        claimed=False, deadline=time.monotonic() + 15)
    hooks.pending['decision'] = pending
    async def verify():
        hooks.tool_bindings.observe({**native, 'tool_use_id': 'tool-two'})
        return True
    with pytest.raises(p.RelayError) as refused:
        await hooks.answer('decision', pending['digest'], 'allow_once', verify)
    assert refused.value.code is p.ErrorCode.INPUT_CONFLICT
    assert not pending['claimed'] and not pending['future'].done() and not hooks.applications


@pytest.mark.parametrize('fresh', [True, False])
async def test_managed_bash_binding_and_reserved_answer_both_checked_at_native_delivery(tmp_path, fresh):
    from test_session_relay_claude_tool_binding import payload

    from acc.session_relay.claude_hook import exchange

    ready = asyncio.Event()
    cards = []
    async def publish(card):
        cards.append(card)
        ready.set()
    hooks = ClaudeHookServer(tmp_path / 'hooks', SimpleNamespace(run_id='run',
        binding=SimpleNamespace(native_conversation='native', workspace=str(tmp_path))), AsyncMock(return_value=True),
        publish, AsyncMock(), allow_policy=False,
        prepare_application=lambda *_: {'managed_bash_allow': True, 'allow_once': lambda: None}, accept_pid=lambda pid:pid==__import__('os').getpid())
    await hooks.start()
    channel = hooks.directory / 'owner-channel.json'
    await asyncio.to_thread(exchange, channel, payload(hook_event_name='PreToolUse', tool_use_id='tool-one'))
    task = asyncio.create_task(asyncio.to_thread(exchange, channel, payload(hook_event_name='PermissionRequest')))
    calls = 0
    async def verify():
        nonlocal calls
        calls += 1
        return calls == 1 or fresh
    try:
        await asyncio.wait_for(ready.wait(), 3)
        card = cards[0]
        assert card['choices'] == ['accept', 'decline']
        answer = asyncio.create_task(hooks.answer(card['decisionId'], card['actionDigest'], 'allow_once', verify))
        assert await asyncio.wait_for(task, 4) == {'behavior': 'allow' if fresh else 'deny'}
        assert calls == 2
        assert bool(hooks.tool_bindings.allowed) is fresh
        assert await asyncio.wait_for(answer, 5) == {'outcome': 'unknown'}
    finally:
        await hooks.close()
        await asyncio.gather(task, return_exceptions=True)
