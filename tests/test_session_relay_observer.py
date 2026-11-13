"""Existing owned process reads; no spawn or input authority on attachment."""
from types import SimpleNamespace

import pytest

from acc.broker.relay_observer import OwnedObserver
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import TerminalRun
from acc.session_relay.terminal_host import ObserverTerminalHost


async def test_revoked_observer_opens_no_stream_or_client():
    async def revoked():
        return False
    observer = OwnedObserver(None, 'acc-fixture', TerminalRun('fixture', 'run1'), revoked, lambda: True)
    with pytest.raises(p.RelayError):
        await observer.open(observer.run)
    assert not observer.tasks and not observer.writers
    await observer.close()


async def test_process_check_cannot_outlive_observer_authority():
    current = True
    async def authorize():
        return current
    def check_process():
        nonlocal current
        current = False
        return True
    observer = OwnedObserver(None, 'acc-fixture', TerminalRun('fixture', 'run1'), authorize, check_process)
    assert not await observer.current(observer.run)
    assert not observer.tasks


async def test_observer_host_never_mints_or_uses_input_even_with_private_drain_marker():
    async def unexpected(*args):
        raise AssertionError('Observer mutation must refuse before any IO/authority callback')
    run = TerminalRun('fixture', 'run1')
    host = ObserverTerminalHost(run, unexpected, unexpected, unexpected)
    marker = SimpleNamespace(run=run, epoch=1)
    host.grant = marker
    for request in (host.claim_input(marker), host.write_frames(marker, 1, b'artificial input'), host.resize(marker, None)):
        with pytest.raises(p.RelayError) as error:
            await request
        assert error.value.code is p.ErrorCode.UNSUPPORTED
    assert host.admitted == host.processed == 0
    assert host.writer is None


async def test_production_claude_terminal_composes_read_opener_without_input_grant(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from test_session_relay_claude_agent import spec

    from acc.broker import claude_owner
    reviewed = spec(tmp_path, tmux_name='acc-fixture', run_dir=str(tmp_path),
                    terminal_resume_args=['--resume', 'fixture-conversation'])
    factory = object.__new__(claude_owner.ClaudeOwnerFactory)
    factory.prepare, factory.authorize = AsyncMock(), AsyncMock(return_value=True)
    factory.config, factory.files, factory.env = SimpleNamespace(), {}, {}
    factory.guards, factory.closed = {}, False
    factory.enclosure = SimpleNamespace(cli=tmp_path / 'owned-cli', proof=lambda *args: True)
    factory.processes = []
    factory.hooks = SimpleNamespace(pending={})
    factory.runtime = SimpleNamespace(native_transcript=None)
    process = SimpleNamespace(pid=123, started='fixture-start', current=lambda: True,
                              tree_exited=AsyncMock(return_value=True), returncode=None)
    monkeypatch.setattr(claude_owner, 'TmuxProcess', lambda *args: process)
    launches = []
    def launch(config, params, **options):
        launches.append((params, options))
    monkeypatch.setattr(claude_owner.ops, 'launch', launch)
    result = await factory.terminal(reviewed)
    try:
        host = result.owner.host
        from acc.session_relay.claude_input_host import ClaudeInputHost
        assert isinstance(host, ClaudeInputHost)
        assert host.attested is False
        assert result.attestation.native_conversation == 'fixture-conversation'
        assert launches[0][0]['args'] == ['--resume', 'fixture-conversation']
        assert launches[0][1]['inherit_environment'] is False
        assert launches[0][1]['owner_executable'] == str(factory.enclosure.cli)
        assert launches[0][1]['guard_files'] == ()
        assert callable(launches[0][1]['before_owner_start'])
        assert host.opener.__self__ is factory.input_attachment
        assert host.observer.opener.__self__ is factory.observer
        assert await host.validate_input(result.owner.grant) is False  # marker is never input authority
        assert await host.validate_input(SimpleNamespace(run=host.run, epoch=1)) is False
        with pytest.raises(p.RelayError) as error:
            await host.write_frames(result.owner.grant, 1, b'artificial ungranted input')
        assert error.value.code is p.ErrorCode.STALE_AUTHORITY
        assert not factory.observer.tasks  # composition/read preference never attaches or launches another owner
    finally:
        from acc.broker.claude_native_prompt import close
        await close(factory)
        await host.close()
        await factory.input_attachment.close()
        await factory.observer.close()
