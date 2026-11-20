"""GHSA-h4mw-j7qp-8mwm defence in depth: only canonical UUIDs reach Claude CLI/SDK argv (SDK pinned at 0.1.50)."""
from dataclasses import replace
from pathlib import Path

import pytest

from acc.broker.explain import arguments
from acc.claude_session_id import canonical_session_id
from acc.server.compiler import CompileError, LaunchInputs, build_args
from acc.session_relay.adapters.base import ReviewedSpec
from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter
from acc.session_relay.adapters.claude_switch import terminal_resume_command
from acc.session_relay.protocol import Binding, ErrorCode, RelayError

VALID = '3f2b8c1e-7a4d-4e5f-9b6a-0c1d2e3f4a5b'
REFUSED = ('--mcp-config=/tmp/x.json', '-' + VALID[1:], 'not-a-uuid', VALID.upper(), '{' + VALID + '}',
           'urn:uuid:' + VALID, VALID.replace('-', ''), '', None)


def spec(tmp_path, native, **options):
    (tmp_path / 'settings.json').write_text('{"disableAllHooks": false, "hooks": {}}')
    return ReviewedSpec(Binding('binding', 'installation', 'node', 'fixture', str(tmp_path), 'claude', native, 'a' * 64),
                        'run', dict(session_id='session', cli_path='/nonexecuting/claude', guard_intact=True,
                                    settings_path=str(tmp_path / 'settings.json'), prior_owner_exit_verified=True, **options))


def test_helper_accepts_only_canonical_uuid():
    assert canonical_session_id(VALID)
    assert not any(canonical_session_id(value) for value in REFUSED)


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_chat', [False, True])
@pytest.mark.parametrize('native', REFUSED)
async def test_start_refuses_before_factory(tmp_path, native, initial_chat):
    calls = []
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=None, client_factory=calls.append)
    with pytest.raises(RelayError) as refused:
        await adapter.start(spec(tmp_path, native, initial_chat=initial_chat))
    assert refused.value.code is ErrorCode.INTEGRITY_FAILURE
    assert adapter.owner is None and calls == []


@pytest.mark.parametrize('initial_chat', [False, True])
@pytest.mark.parametrize('native', REFUSED)
def test_options_refuse_before_any_sdk_value(tmp_path, native, initial_chat):
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=None)
    adapter.spec = spec(tmp_path, native, initial_chat=initial_chat)
    with pytest.raises(RelayError) as refused:
        adapter.options()
    assert refused.value.code is ErrorCode.INTEGRITY_FAILURE


def test_valid_uuid_passes_for_resume_and_initial_session_id(tmp_path):
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=None)
    adapter.spec = spec(tmp_path, VALID)
    options = adapter.options()
    assert options['resume'] == VALID and 'session-id' not in options.get('extra_args', {})
    adapter.spec = spec(tmp_path, VALID, initial_chat=True)
    options = adapter.options()
    assert options['extra_args']['session-id'] == VALID and 'resume' not in options


def test_terminal_resume_command_refuses_flag_shaped_id(tmp_path):
    good = spec(tmp_path, VALID)
    assert terminal_resume_command(good)[1:3] == ('--resume', VALID)
    for native in REFUSED:
        with pytest.raises(RelayError):
            terminal_resume_command(replace(good, binding=replace(good.binding, native_conversation=native)))


@pytest.mark.parametrize('resume', [False, True])
def test_cli_launch_args_refuse_flag_shaped_id(resume):
    inputs = LaunchInputs(provider='claude')
    args = build_args(inputs, run_dir='/r', tmux_name='t', native_id=VALID, resume=resume, files={})
    assert args[args.index('--resume' if resume else '--session-id') + 1] == VALID
    for native in ('--mcp-config=/tmp/x.json', 'not-a-uuid', VALID.upper()):
        with pytest.raises(CompileError):
            build_args(inputs, run_dir='/r', tmux_name='t', native_id=native, resume=resume, files={})


def test_explanation_args_refuse_flag_shaped_id():
    assert VALID in arguments('claude', '', Path('/r'), VALID)
    with pytest.raises(ValueError):
        arguments('claude', '', Path('/r'), '--mcp-config=/tmp/x.json')
