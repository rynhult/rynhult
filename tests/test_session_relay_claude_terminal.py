"""Owned fake native process; explicit fixture draft/drain and tree-exit evidence."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_session_relay_claude_agent import spec

from acc.session_relay.adapters.base import CloseMode, ExitState, Watermark
from acc.session_relay.adapters.claude_switch import ClaudeL1Switch, LaunchedClaudeOwner, terminal_resume_command
from acc.session_relay.adapters.claude_terminal import ClaudeTerminalOwner
from acc.session_relay.protocol import RelayError


async def owner(tmp_path):
    reviewed = spec(tmp_path)
    process = await asyncio.create_subprocess_exec(*terminal_resume_command(reviewed),
        cwd=tmp_path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    assert json.loads(await process.stdout.readline())['type'] == 'terminal_ready'
    state = SimpleNamespace(current=True, watermark=Watermark(1, 0, 0), tree=True)
    async def authorize(_):
        return state.current
    async def boundary(_):
        return state.watermark
    async def drain(*_):
        return state.watermark
    async def tree_exited(process):
        return process.returncode is not None and state.tree
    result = ClaudeTerminalOwner(spec=reviewed, process=process,
        host=SimpleNamespace(drain=drain), grant=SimpleNamespace(epoch=1),
        boundary=boundary, authorize=authorize, tree_exited=tree_exited)
    return result, state


def hook(owner, event, **fields):
    owner.observe_hook({'session_id': owner.spec.binding.native_conversation,
                        'hook_event_name': event, **fields})


@pytest.mark.parametrize('blocker', ['draft', 'tool', 'background', 'decision', 'unknown'])
async def test_stop_hook_alone_cannot_prove_terminal_idle(tmp_path, blocker):
    tui, state = await owner(tmp_path)
    try:
        hook(tui, 'Stop')
        assert (await tui.prepare_switch()).idle
        if blocker == 'draft':
            state.watermark = Watermark(1, 1, 0)
        elif blocker == 'tool':
            hook(tui, 'PreToolUse', tool_use_id='tool')
        elif blocker == 'background':
            hook(tui, 'TaskStarted', task_id='task')
        elif blocker == 'decision':
            hook(tui, 'PermissionRequest', tool_use_id='tool')
        else:
            hook(tui, 'UnmappedNativeEvent')
        if not tui.failed:
            hook(tui, 'Stop')
        assert not (await tui.prepare_switch()).idle
        with pytest.raises(RelayError):
            await tui.stop_owner()
        assert tui.process.returncode is None
    finally:
        await tui.close(CloseMode.STOP)


@pytest.mark.parametrize('boundary', ['drain', 'tree'])
async def test_revocation_and_unknown_tree_never_prove_safe_exit(tmp_path, boundary):
    tui, state = await owner(tmp_path)
    try:
        hook(tui, 'Stop')
        if boundary == 'drain':
            async def revoke(*_):
                state.current = False
                return Watermark(1, 0, 0)
            tui.host.drain = revoke
            with pytest.raises(RelayError):
                await tui.stop_owner()
            assert tui.process.returncode is None
        else:
            state.tree = False
            result = await tui.stop_owner()
            assert result.exit is ExitState.UNKNOWN
            assert tui.process.returncode is not None
    finally:
        state.current = True
        await tui.close(CloseMode.STOP)


async def test_terminal_owned_process_exits_before_same_id_sdk_resume_and_back(tmp_path):
    pytest.importorskip('claude_agent_sdk')
    from acc.session_relay.adapters.base import RuntimeAttestation
    from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter
    tui, state = await owner(tmp_path)
    hook(tui, 'Stop')
    launched = [tui]
    async def authorize(*_):
        return state.current
    async def chat(target):
        adapter = ClaudeAgentAdapter(policy=lambda *_: state.current, publish=lambda _: asyncio.sleep(0))
        launched.append(adapter)
        return LaunchedClaudeOwner(adapter, await adapter.start(target))
    async def terminal(target):
        result, _ = await owner(tmp_path)
        result.spec = target
        hook(result, 'Stop')
        launched.append(result)
        return LaunchedClaudeOwner(result, RuntimeAttestation(target.binding.binding_id,
            target.run_id, target.binding.native_conversation, f'fixture:{result.process.pid}'))
    switch = ClaudeL1Switch(spec=tui.spec, view='terminal', owner=tui,
        launch_chat=chat, launch_terminal=terminal, authorize=authorize)
    try:
        await switch.select('chat', replace(tui.spec, run_id='next-chat'))
        assert tui.process.returncode is not None
        sdk = switch.owner
        await switch.select('terminal', replace(switch.spec, run_id='next-terminal'))
        assert sdk.process.returncode is not None
        assert switch.spec.binding.native_conversation == 'fixture-conversation'
        assert not switch.recovery_required
    finally:
        for item in reversed(launched):
            await item.close(CloseMode.STOP)


async def test_terminal_drain_reauthenticates_current_hub_stop_fence(tmp_path):
    from relay_helpers import setup

    from acc.server.db import write_in_thread
    from acc.server.session_relay import authority
    from acc.server.session_relay.hub import seal_for_stop
    fixture = setup(tmp_path)
    tui, state = await owner(tmp_path)
    async def authorize(_):
        def check():
            with fixture.ctx.db() as db:
                authority.current(fixture.ctx, db, fixture.resolve, 'relay', action='control')
            return True
        return await write_in_thread(check)
    async def stop_during_drain(*_):
        await write_in_thread(lambda: seal_for_stop(fixture.ctx, 'relay'))
        return Watermark(1, 0, 0)
    tui.authorize = authorize
    tui.host.drain = stop_during_drain
    hook(tui, 'Stop')
    try:
        with pytest.raises(RelayError):
            await tui.stop_owner()
        assert tui.process.returncode is None
    finally:
        async def cleanup_authorize(_):
            return True
        tui.authorize = cleanup_authorize
        await tui.close(CloseMode.STOP)
        fixture.engine.dispose()


def test_reviewed_launch_policy_is_immutable_across_switch_awaits(tmp_path):
    reviewed = spec(tmp_path)
    source = {'guard_intact': True, 'nested': {'args': ['original']}}
    reviewed = replace(reviewed, spec=source)
    source['nested']['args'][0] = 'changed'
    assert reviewed.spec['nested']['args'] == ('original',)
    with pytest.raises(TypeError):
        reviewed.spec['guard_intact'] = False


async def test_session_end_is_observational_and_new_work_during_stop_cannot_claim_idle(tmp_path, monkeypatch):
    tui, state = await owner(tmp_path)
    try:
        hook(tui, 'SessionEnd')
        assert not tui.idle and not tui.failed
        hook(tui, 'Stop')
        terminate = tui.process.terminate
        def work_during_stop():
            hook(tui, 'TaskStarted', task_id='late-background')
            terminate()
        monkeypatch.setattr(tui.process, 'terminate', work_during_stop)
        result = await tui.stop_owner()
        assert result.exit is ExitState.EXITED and result.idle is False
    finally:
        await tui.close(CloseMode.STOP)


async def test_completed_denial_retires_only_its_exact_native_tool(tmp_path):
    tui, _ = await owner(tmp_path)
    try:
        hook(tui, 'PreToolUse', tool_use_id='denied-tool')
        hook(tui, 'PreToolUse', tool_use_id='another-tool')
        hook(tui, 'Stop')
        tui.confirm_denial('denied-tool')
        assert tui.tools == {'another-tool'}
        assert not (await tui.prepare_switch()).idle
        tui.confirm_denial('unrelated-tool')
        assert tui.tools == {'another-tool'}
        hook(tui, 'PostToolUse', tool_use_id='another-tool')
        assert (await tui.prepare_switch()).idle
        hook(tui, 'TaskStarted', task_id='background')
        hook(tui, 'Stop')
        tui.confirm_denial('denied-tool')
        assert not (await tui.prepare_switch()).idle
    finally:
        await tui.close(CloseMode.STOP)
