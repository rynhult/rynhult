"""Both L1 directions using the real SDK and one artificial native transcript.

The fixture TUI implements the broker owner seam, not a production PTY host.
Native vendor idle/child-process and documented-hook qualification remain separate.
"""
import asyncio
import json
from dataclasses import replace

import pytest
from test_session_relay_claude_agent import operation, spec, until

from acc.session_relay.adapters.base import (
    CloseMode,
    ExitState,
    RuntimeAttestation,
    SwitchEvidence,
    UnavailableAdapter,
)
from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter
from acc.session_relay.adapters.claude_switch import ClaudeL1Switch, LaunchedClaudeOwner, terminal_resume_command
from acc.session_relay.protocol import ErrorCode, RelayError


class FakeTerminalOwner(UnavailableAdapter):
    """Test broker owner: structured fake observations, verified wait(), intact settings."""

    async def start(self, reviewed):
        self.spec = reviewed
        self.busy = False
        self.pending = False
        self.process = await asyncio.create_subprocess_exec(
            *terminal_resume_command(reviewed), cwd=reviewed.binding.workspace,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        ready = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 5))
        assert ready == {'type': 'terminal_ready', 'native': reviewed.binding.native_conversation}
        return RuntimeAttestation(reviewed.binding.binding_id, reviewed.run_id,
                                  reviewed.binding.native_conversation, f'fixture:{self.process.pid}')

    async def send(self, text):
        self.process.stdin.write((text + '\n').encode())
        await self.process.stdin.drain()
        hook = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 5))
        self.pending = hook.get('hook_event_name') == 'PermissionRequest'
        self.busy = text == 'wait-fixture'
        return hook

    async def prepare_switch(self):
        return SwitchEvidence(not self.busy and not self.pending,
                              ExitState.EXITED if self.process.returncode is not None else ExitState.RUNNING,
                              self.spec.binding.native_conversation, self.spec.binding.launch_policy_digest)

    async def stop_owner(self):
        if self.busy or self.pending:
            raise RelayError(ErrorCode.BUSY)
        await self.close(CloseMode.STOP)
        return await self.prepare_switch()

    async def close(self, mode):
        if mode is CloseMode.STOP and self.process.returncode is None:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), 5)


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


async def setup_switch(tmp_path, initial):
    pytest.importorskip('claude_agent_sdk')
    reviewed = spec(tmp_path)
    events = []
    launched = []
    async def publish(event):
        events.append(event)
    async def chat(target):
        owner = ClaudeAgentAdapter(policy=lambda *_: True, publish=publish)
        result = LaunchedClaudeOwner(owner, await owner.start(target))
        launched.append(result)
        return result
    async def terminal(target):
        owner = FakeTerminalOwner()
        result = LaunchedClaudeOwner(owner, await owner.start(target))
        launched.append(result)
        return result
    async def authorize(source, target):
        assert source.binding == target.binding
        return True  # fixture for the hub's claimed CAS/fence/input-drain grant
    first = await (chat if initial == 'chat' else terminal)(reviewed)
    switch = ClaudeL1Switch(spec=reviewed, view=initial, owner=first.owner,
                           launch_chat=chat, launch_terminal=terminal, authorize=authorize)
    return switch, launched, events


async def send(switch, text):
    if switch.view == 'terminal':
        return await switch.owner.send(text)
    op = replace(operation(text=text), run_id=switch.spec.run_id, operation_id='op-' + text)
    await switch.owner.submit(op)
    if text != 'wait-fixture':
        await until(lambda: not switch.owner.active)


async def cleanup(launched):
    for result in reversed(launched):
        await result.owner.close(CloseMode.STOP)


@pytest.mark.parametrize('initial', ['terminal', 'chat'])
async def test_roundtrip_preserves_one_transcript_and_exits_before_resume(tmp_path, initial):
    switch, launched, events = await setup_switch(tmp_path, initial)
    try:
        for index in range(3):
            if switch.view == 'terminal':
                # L2 hook observation uses the TUI process; it starts no SDK sidecar.
                hook = await send(switch, 'fixture-hook-approval')
                assert hook['hook_event_name'] == 'PermissionRequest'
                assert len(records(tmp_path / '.fixture-processes.jsonl')) == index * 2 + 1
                switch.owner.pending = False  # fixture person resolves the hook decision
            await send(switch, f'{switch.view}-turn-{index}')
            if index < 2:
                previous = switch.owner
                view = 'chat' if switch.view == 'terminal' else 'terminal'
                target = replace(switch.spec, run_id=f'run-{index + 1}',
                                 spec={**switch.spec.spec, 'prior_owner_exit_verified': False})
                result = await switch.select(view, target)
                assert result.native_conversation == 'fixture-conversation'
                assert result.changed and result.view == view
                assert previous.process.returncode is not None
        transcript = records(tmp_path / '.fixture-transcript.jsonl')
        expected = [f'{initial}-turn-0',
                    f'{"chat" if initial == "terminal" else "terminal"}-turn-1', f'{initial}-turn-2']
        assert [item['text'] for item in transcript] == expected
        assert {item['native'] for item in transcript} == {'fixture-conversation'}
        assert all('conversation-checkpoint' not in str(e) for e in events)
        log = records(tmp_path / '.fixture-processes.jsonl')
        assert [item['kind'] for item in log] == ['start', 'exit', 'start', 'exit', 'start']
        assert [item['mode'] for item in log if item['kind'] == 'start'] == [initial, 'chat' if initial == 'terminal' else 'terminal', initial]
        assert all(item.get('settings', switch.spec.spec['settings_path']) == switch.spec.spec['settings_path'] for item in log)
        same = await switch.select(switch.view, switch.spec)
        assert not same.changed
        assert len(records(tmp_path / '.fixture-processes.jsonl')) == len(log)
    finally:
        await cleanup(launched)


@pytest.mark.parametrize('initial', ['terminal', 'chat'])
async def test_busy_refuses_other_tab_without_stop_or_deferred_launch(tmp_path, initial):
    switch, launched, _ = await setup_switch(tmp_path, initial)
    try:
        await send(switch, 'wait-fixture')
        previous = switch.owner
        target = replace(switch.spec, run_id='next-run')
        with pytest.raises(RelayError) as error:
            await switch.select('chat' if initial == 'terminal' else 'terminal', target)
        assert error.value.code is ErrorCode.BUSY
        assert 'before switching to' in switch.inline_reason
        assert switch.owner is previous and switch.view == initial
        assert not switch.recovery_required
        assert [item['kind'] for item in records(tmp_path / '.fixture-processes.jsonl')] == ['start']
        if initial == 'chat':
            await previous.interrupt(replace(operation('turn.interrupt', turnId='op-wait-fixture'), run_id=switch.spec.run_id))
            await until(lambda: not previous.active)
        else:
            await previous.send('terminal-idle-turn')
        await asyncio.sleep(.05)
        assert len(launched) == 1  # completing work does not auto-switch
        assert previous.process.returncode is None
    finally:
        await cleanup(launched)


async def test_unknown_exit_and_changed_conversation_never_launch_target(tmp_path):
    switch, launched, _ = await setup_switch(tmp_path, 'terminal')
    try:
        target = replace(switch.spec, run_id='next-run')
        invalid = replace(target, binding=replace(target.binding, native_conversation='other'))
        with pytest.raises(RelayError):
            await switch.select('chat', invalid)
        assert len(launched) == 1
        async def unknown_stop():
            return SwitchEvidence(True, ExitState.UNKNOWN, 'fixture-conversation', switch.spec.binding.launch_policy_digest)
        switch.owner.stop_owner = unknown_stop
        with pytest.raises(RelayError):
            await switch.select('chat', target)
        assert switch.recovery_required and len(launched) == 1
        with pytest.raises(RelayError):
            await switch.select('chat', target)
        assert len(launched) == 1
    finally:
        await cleanup(launched)


@pytest.mark.parametrize('failure', ['start', 'identity', 'revoked'])
async def test_stop_success_start_failure_requires_recovery_no_restart(tmp_path, failure):
    switch, launched, _ = await setup_switch(tmp_path, 'terminal')
    try:
        original = switch.launch['chat']
        async def fail(target):
            if failure == 'start':
                raise RuntimeError('fixture start failed')
            result = await original(target)
            return replace(result, attestation=replace(result.attestation, native_conversation='wrong-native-id'))
        switch.launch['chat'] = fail
        if failure == 'revoked':
            calls = 0
            async def revoked(source, target):
                nonlocal calls
                calls += 1
                return calls <= 2  # revoke after old owner exits, before target launch
            switch.authorize = revoked
        with pytest.raises((RuntimeError, RelayError)):
            await switch.select('chat', replace(switch.spec, run_id='next-run'))
        assert switch.recovery_required and switch.view == 'terminal'
        assert launched[0].owner.process.returncode is not None
        assert switch.inline_reason == 'Session stopped — switch did not finish.'
        if failure == 'identity':
            assert launched[1].owner.process.returncode is not None
        else:
            assert len(launched) == 1
        count = len(launched)
        with pytest.raises(RelayError):
            await switch.select('chat', replace(switch.spec, run_id='next-run'))
        assert len(launched) == count
    finally:
        await cleanup(launched)


async def test_frozen_policy_change_refuses_before_stopping_owner(tmp_path):
    switch, launched, _ = await setup_switch(tmp_path, 'terminal')
    try:
        changed = replace(switch.spec, run_id='next-run', spec={**switch.spec.spec, 'model': 'different'})
        with pytest.raises(RelayError) as error:
            await switch.select('chat', changed)
        assert error.value.code is ErrorCode.POLICY_REFUSED
        assert not switch.recovery_required and len(launched) == 1
        assert switch.owner.process.returncode is None
    finally:
        await cleanup(launched)


@pytest.mark.parametrize('boundary', ['idle', 'launch'])
async def test_authority_loss_during_idle_or_launch_await_never_completes_switch(tmp_path, boundary):
    """Pure owner seam: runs even when the optional SDK is unavailable."""
    reviewed = spec(tmp_path)
    revoked = False
    launched = []
    class Owner(UnavailableAdapter):
        stopped = False
        async def prepare_switch(self):
            nonlocal revoked
            await asyncio.sleep(0)
            if boundary == 'idle':
                revoked = True
            return SwitchEvidence(True, ExitState.RUNNING, reviewed.binding.native_conversation, reviewed.binding.launch_policy_digest)
        async def stop_owner(self):
            self.stopped = True
            return SwitchEvidence(True, ExitState.EXITED, reviewed.binding.native_conversation, reviewed.binding.launch_policy_digest)
        async def close(self, mode):
            assert mode is CloseMode.STOP
            self.stopped = True
    original = Owner()
    async def launch(target):
        nonlocal revoked
        await asyncio.sleep(0)
        owner = Owner()
        launched.append(owner)
        revoked = True
        return LaunchedClaudeOwner(owner, RuntimeAttestation(target.binding.binding_id,
            target.run_id, target.binding.native_conversation, 'fixture-process'))
    async def authorize(source, target):
        return not revoked
    switch = ClaudeL1Switch(spec=reviewed, view='terminal', owner=original,
                           launch_chat=launch, launch_terminal=launch, authorize=authorize)
    with pytest.raises(RelayError) as error:
        await switch.select('chat', replace(reviewed, run_id='next-run'))
    assert error.value.code is ErrorCode.POLICY_REFUSED
    assert switch.view == 'terminal'
    if boundary == 'idle':
        assert not original.stopped and launched == []
        assert not switch.recovery_required
    else:
        assert original.stopped and len(launched) == 1 and launched[0].stopped
        assert switch.recovery_required


@pytest.mark.parametrize('changed_authority', [False, True], ids=['target-unavailable', 'authority-ended-after-preflight'])
async def test_target_preflight_refuses_before_stop_and_rechecks_authority(tmp_path, changed_authority):
    switch, launched, _ = await setup_switch(tmp_path, 'chat')
    original = switch.owner
    allowed = True
    async def authorize(*_):
        return allowed
    async def preflight(view, target):
        nonlocal allowed
        assert view == 'terminal' and target.run_id == 'next-run'
        assert original.process.returncode is None
        if changed_authority:
            allowed = False
        else:
            raise RelayError(ErrorCode.UNSUPPORTED)
    switch.authorize, switch.preflight = authorize, preflight
    try:
        target = replace(switch.spec, run_id='next-run')
        with pytest.raises(RelayError) as failure:
            await switch.select('terminal', target)
        assert failure.value.code is (ErrorCode.POLICY_REFUSED if changed_authority else ErrorCode.UNSUPPORTED)
        assert switch.owner is original and switch.view == 'chat'
        assert not switch.recovery_required and original.process.returncode is None
        assert len(launched) == 1
    finally:
        await cleanup(launched)
