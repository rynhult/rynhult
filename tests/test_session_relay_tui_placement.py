"""Owned placement refuses stale authority before spawning any client."""
from types import SimpleNamespace

import pytest

from acc.broker.relay_tui import OwnedTUI
from acc.session_relay import protocol as p


def placement(tmp_path):
    executable = tmp_path / 'client'
    executable.write_bytes(b'artificial pin; never executed')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    spec = SimpleNamespace(run_id=42, spec={'session_id': 'fixture'},
                           binding=SimpleNamespace(workspace=str(workspace)))
    native = SimpleNamespace(validate=lambda: None)
    return OwnedTUI(tmp_path / 'view', None, spec, executable, native,
                    digest=p.sha256_digest(executable.read_bytes()))


async def test_revoked_reviewed_launch_creates_no_client_or_view_files(tmp_path):
    view = placement(tmp_path)
    async def revoked():
        return False
    with pytest.raises(p.RelayError) as error:
        await view.start(revoked)
    assert error.value.code is p.ErrorCode.STALE_AUTHORITY
    assert not view.root.exists()
    assert view.enclosure is None and not view.started
    assert await view.close() is False  # no invented exit receipt


async def test_pin_change_refuses_before_authority_or_process_work(tmp_path):
    view = placement(tmp_path)
    view.executable.write_bytes(b'replaced pin')
    async def unexpected():
        raise AssertionError('Changed executable must fail before authority check')
    with pytest.raises(p.RelayError) as error:
        await view.start(unexpected)
    assert error.value.code is p.ErrorCode.UNSUPPORTED
    assert not view.root.exists()


async def test_attach_without_reviewed_launch_cannot_spawn(tmp_path):
    view = placement(tmp_path)
    with pytest.raises(p.RelayError) as error:
        await view.open(SimpleNamespace(session_id='fixture', run_id=42))
    assert error.value.code is p.ErrorCode.STALE_RUN
    assert not view.root.exists()


def test_unavailable_placement_is_not_advertised_as_launchable(monkeypatch):
    from acc.broker import relay_runtime
    class UnavailableView:
        @staticmethod
        def available():
            return False
    factory = relay_runtime.RuntimeTransportFactory(SimpleNamespace(), owned_view_factory=UnavailableView)
    def unexpected(*args):
        raise AssertionError('Provider probe must not run when owned placement is unavailable')
    monkeypatch.setattr(relay_runtime.shutil, 'which', unexpected)
    assert factory.probe() == {'available': False}
