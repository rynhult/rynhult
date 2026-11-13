"""Exact private hook races, without SDK, vendor input or provider acceptance claims."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from acc.broker.claude_hooks import ClaudeHookServer
from acc.session_relay import protocol as p
from acc.session_relay.claude_hook import exchange


@pytest.fixture
async def hook_case(tmp_path):
    cards = []
    published = asyncio.Event()
    async def authorize():
        return True
    async def observe(payload):
        pass
    async def publish(payload):
        cards.append(payload)
        published.set()
    spec = SimpleNamespace(run_id='run1', binding=SimpleNamespace(native_conversation='fixture-native'))
    server = ClaudeHookServer(tmp_path / 'hook', spec, authorize, publish, observe, accept_pid=lambda pid:pid==__import__('os').getpid())
    await server.start()
    await asyncio.to_thread(exchange, server.directory / 'owner-channel.json', {
        'session_id':'fixture-native','prompt_id':'prompt-1','hook_event_name':'PreToolUse',
        'tool_use_id':'tool-1','tool_name':'Bash','tool_input':{'command':'echo artificial'}})
    task = asyncio.create_task(asyncio.to_thread(exchange, server.directory / 'owner-channel.json', {
        'session_id': 'fixture-native', 'prompt_id':'prompt-1', 'hook_event_name': 'PermissionRequest',
        'tool_name': 'Bash', 'tool_input': {'command': 'echo artificial'}}))
    await asyncio.wait_for(published.wait(), 5)
    yield server, cards[0], task
    await server.close()
    await asyncio.gather(task, return_exceptions=True)


async def test_two_verified_answers_consume_exact_hook_only_once(hook_case):
    server, card, task = hook_case
    barrier = asyncio.Event()
    entered = 0
    async def verify():
        nonlocal entered
        entered += 1
        if entered == 2:
            barrier.set()
        await barrier.wait()
        return True
    results = await asyncio.wait_for(asyncio.gather(*(
        server.answer(card['decisionId'], card['actionDigest'], effect, verify)
        for effect in ('allow_once', 'deny')), return_exceptions=True), 5)
    assert results.count({'outcome': 'unknown'}) == 1
    errors = [item for item in results if isinstance(item, p.RelayError)]
    assert len(errors) == 1 and errors[0].code is p.ErrorCode.DECISION_RESOLVED
    assert await asyncio.wait_for(task, 5) in ({'behavior': 'allow'}, {'behavior': 'deny'})
    assert not server.pending


@pytest.mark.parametrize('invalid', ['digest', 'effect', 'revoked', 'expired', 'expire_during_verify'])
async def test_invalid_hook_answer_cannot_consume_pending_choice(hook_case, invalid):
    server, card, task = hook_case
    pending = server.pending[card['decisionId']]
    async def verify():
        if invalid == 'expire_during_verify':
            pending['deadline'] = time.monotonic() - 1
        return invalid != 'revoked'
    if invalid == 'expired':
        pending['deadline'] = time.monotonic() - 1
    with pytest.raises(p.RelayError):
        await server.answer(card['decisionId'],
            p.sha256_digest(b'wrong') if invalid == 'digest' else card['actionDigest'],
            'allow_always' if invalid == 'effect' else 'allow_once', verify)
    assert not pending['claimed'] and not pending['future'].done()
    await server.close()
    assert await asyncio.wait_for(task, 5) == {'behavior': 'deny'}


async def test_expiry_during_final_native_authority_await_sends_deny(hook_case, monkeypatch):
    from acc.broker import claude_hooks

    server, card, task = hook_case
    deadline = server.pending[card['decisionId']]['deadline']
    clock = SimpleNamespace(monotonic=time.monotonic, time_ns=time.time_ns)
    monkeypatch.setattr(claude_hooks, 'time', clock)
    async def authorize():
        clock.monotonic = lambda: deadline + 1
        return True
    async def verify():
        return True
    server.authorize = authorize
    assert await server.answer(card['decisionId'], card['actionDigest'], 'allow_once', verify) == {'outcome': 'unknown'}
    assert await asyncio.wait_for(task, 5) == {'behavior': 'deny'}
