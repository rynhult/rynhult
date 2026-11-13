"""Native background result cannot disappear on Stop or an unrelated task hook."""
import asyncio
from types import SimpleNamespace

import pytest
from test_session_relay_claude_agent import spec
from test_session_relay_claude_terminal import hook, owner

from acc.broker.claude_hooks import ClaudeHookServer
from acc.session_relay.adapters.base import CloseMode
from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter
from acc.session_relay.claude_hook import exchange
from acc.session_relay.protocol import ErrorCode, RelayError


@pytest.mark.parametrize('kind', ['terminal', 'sdk'])
async def test_background_result_blocks_stop_and_handover_without_task_started(tmp_path, kind):
    if kind == 'terminal':
        adapter, _ = await owner(tmp_path)
    else:
        adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=lambda _: asyncio.sleep(0))
        await adapter.start(spec(tmp_path))
    try:
        hook(adapter, 'PreToolUse', tool_use_id='bash-tool')
        hook(adapter, 'PostToolUse', tool_use_id='bash-tool', tool_response={'backgroundTaskId': 'native-bg'})
        hook(adapter, 'Stop')
        assert adapter.background and not (await adapter.prepare_switch()).idle
        if kind == 'terminal':
            hook(adapter, 'TaskCompleted', task_id='native-bg')  # Different native task namespace.
            assert adapter.background and not (await adapter.prepare_switch()).idle
        with pytest.raises(RelayError) as failure:
            await adapter.stop_owner()
        assert failure.value.code is ErrorCode.BUSY
    finally:
        await adapter.close(CloseMode.STOP)


@pytest.mark.parametrize('kind', ['terminal', 'sdk'])
@pytest.mark.parametrize('identifier', ['', 1, 'x' * 257])
async def test_malformed_background_result_never_establishes_idle(tmp_path, kind, identifier):
    if kind == 'terminal':
        adapter, _ = await owner(tmp_path)
    else:
        adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=lambda _: asyncio.sleep(0))
        await adapter.start(spec(tmp_path))
    try:
        hook(adapter, 'PreToolUse', tool_use_id='bash-tool')
        with pytest.raises(RelayError) as failure:
            hook(adapter, 'PostToolUse', tool_use_id='bash-tool', tool_response={'backgroundTaskId': identifier})
        assert failure.value.code is ErrorCode.INTEGRITY_FAILURE
        assert not (await adapter.prepare_switch()).idle
    finally:
        await adapter.close(CloseMode.STOP)


@pytest.mark.parametrize('background', [True, 'false', 1])
async def test_background_request_denied_before_observation_or_execution(tmp_path, background):
    observed = []
    async def authorize():
        return True
    async def observe(payload):
        observed.append(payload)
    server = ClaudeHookServer(tmp_path / 'hook', SimpleNamespace(run_id='run1',
        binding=SimpleNamespace(native_conversation='fixture-native')), authorize, observe, observe, accept_pid=lambda pid:pid==__import__('os').getpid())
    try:
        await server.start()
        payload = {'session_id': 'fixture-native', 'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash', 'tool_use_id': 'native-bash',
            'tool_input': {'command': 'fixture command never executed', 'run_in_background': background}}
        assert await asyncio.to_thread(exchange, server.directory / 'owner-channel.json', payload) == {'behavior': 'deny'}
        assert not observed and not server.pending
    finally:
        await server.close()


async def test_sdk_background_result_ledger_is_bounded(tmp_path):
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=lambda _: asyncio.sleep(0))
    await adapter.start(spec(tmp_path))
    try:
        for number in range(64):
            hook(adapter, 'PreToolUse', tool_use_id='tool')
            hook(adapter, 'PostToolUse', tool_use_id='tool', tool_response={'backgroundTaskId': f'bg-{number}'})
        assert len(adapter.background) == 64
        hook(adapter, 'PreToolUse', tool_use_id='tool')
        with pytest.raises(RelayError) as failure:
            hook(adapter, 'PostToolUse', tool_use_id='tool', tool_response={'backgroundTaskId': 'overflow'})
        assert failure.value.code is ErrorCode.LIMIT
        assert len(adapter.background) == 64
        assert not (await adapter.prepare_switch()).idle
    finally:
        await adapter.close(CloseMode.STOP)


@pytest.mark.parametrize('reviewed,background,expected', [
    (True, True, {'observed': True}), (False, True, {'behavior': 'deny'}),
    (True, 'true', {'behavior': 'deny'}), (True, 1, {'behavior': 'deny'}),
])
async def test_only_reviewed_sdk_background_reaches_observer(tmp_path, reviewed, background, expected):
    observed = []
    async def authorize():
        return True
    async def observe(payload):
        observed.append(payload)
    server = ClaudeHookServer(tmp_path / 'hook', SimpleNamespace(run_id='run1',
        binding=SimpleNamespace(native_conversation='fixture-native')), authorize, observe, observe,
        sdk_background=lambda: reviewed, accept_pid=lambda pid:pid==__import__('os').getpid())
    try:
        await server.start()
        payload = {'session_id': 'fixture-native', 'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash', 'tool_use_id': 'native-bash',
            'tool_input': {'command': 'fixture', 'run_in_background': background}}
        assert await asyncio.to_thread(exchange, server.directory / 'owner-channel.json', payload) == expected
        assert bool(observed) is (expected == {'observed': True})
        assert not server.pending
    finally:
        await server.close()


def test_sdk_background_gate_requires_exact_current_owner_run_and_binding(tmp_path):
    from dataclasses import replace

    from acc.broker.claude_owner import ClaudeOwnerFactory
    factory = ClaudeOwnerFactory.__new__(ClaudeOwnerFactory)
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=lambda _: asyncio.sleep(0))
    reviewed = spec(tmp_path)
    adapter.spec = reviewed
    factory.closed, factory.current = False, adapter
    assert factory.sdk_background(reviewed)
    assert not factory.sdk_background(replace(reviewed, run_id='other-run'))
    assert not factory.sdk_background(replace(reviewed, binding=replace(reviewed.binding, workspace='/other')))
    adapter.closing = True
    assert not factory.sdk_background(reviewed)
    adapter.closing, adapter.failure = False, True
    assert not factory.sdk_background(reviewed)
    adapter.failure, factory.closed = False, True
    assert not factory.sdk_background(reviewed)
    factory.closed, factory.current = False, SimpleNamespace(spec=reviewed)
    assert not factory.sdk_background(reviewed)
