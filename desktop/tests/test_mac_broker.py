"""Mac activation adapter contracts; real sockets, no launchd or privilege changes."""
import importlib.util
import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def broker():
    source = ROOT / 'desktop/mac_broker.py'
    assert source.exists(), 'macOS launchd broker entry point missing'
    spec = importlib.util.spec_from_file_location('mac_broker', source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_activation_uses_exact_unix_listener_and_does_not_rebind(tmp_path, monkeypatch):
    module = broker()
    path = tmp_path / 'broker.sock'
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(4)
    fd = os.dup(listener.fileno())
    monkeypatch.setattr(module, 'launch_fds', lambda: [fd])
    # Root-only metadata cannot be created here; exercise descriptor/path separately.
    monkeypatch.setattr(module, 'check_socket_path', lambda value, owner_uid: None)
    with module.activated_listener(path, 501) as active:
        assert active.getsockname() == str(path)
        assert active.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
        client = socket.socket(socket.AF_UNIX)
        client.connect(str(path))
        connection, _ = active.accept()
        connection.close()
        client.close()
    listener.close()
    assert path.exists(), 'adapter must never unlink/rebind a launchd-owned socket'
    with pytest.raises(OSError):
        os.fstat(fd)


def test_activation_refuses_multiple_descriptors_and_closes_them(tmp_path, monkeypatch):
    module = broker()
    pair = socket.socketpair()
    fds = [os.dup(s.fileno()) for s in pair]
    monkeypatch.setattr(module, 'launch_fds', lambda: fds)
    with pytest.raises(ValueError, match='one'):
        module.activated_listener(tmp_path / 'socket', 501)
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    for sock in pair:
        sock.close()


def test_activation_rejects_a_connected_socket_instead_of_a_listener(tmp_path, monkeypatch):
    module = broker()
    pair = socket.socketpair()
    fd = os.dup(pair[0].fileno())
    monkeypatch.setattr(module, 'launch_fds', lambda: [fd])
    with pytest.raises(ValueError, match='listener'):
        module.activated_listener(tmp_path / 'socket', 501)
    for sock in pair:
        sock.close()


def test_direct_launch_refused_without_mac_launchd():
    module = broker()
    if sys.platform != 'darwin':
        with pytest.raises(ValueError, match='macOS'):
            module.launch_fds()


def test_mac_configuration_drops_inheritance_and_finds_both_homebrew_prefixes(monkeypatch):
    from types import SimpleNamespace
    module = broker()
    common = sys.modules[module.configuration.__module__]
    value = {'home': '/Library/Application Support/Rynhult Desktop/Owners/501/rynd-501',
             'run_dir': '/Library/Application Support/Rynhult Desktop/Owners/501/run',
             'projects': '/Library/Application Support/Rynhult Desktop/Owners/501/projects',
             'socket': '/Library/Application Support/Rynhult Desktop/Sockets/501/rynd-501.sock', 'agent': 'rynd-501'}
    monkeypatch.setattr(common, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(common, 'read_manifest', lambda uid: value)
    monkeypatch.setattr(common, 'validate_account', lambda val, uid: SimpleNamespace(pw_uid=50001))
    env = {'RYNHULT_DESKTOP_TOKEN': 'private', 'ACC_SESSION_ID': 'live', 'PATH': '/owner/bin', 'XPC_SERVICE_NAME': 'service'}
    monkeypatch.setattr(common, 'os', SimpleNamespace(getuid=lambda:50001, geteuid=lambda:50001, environ=env))
    config = common.configuration(501)
    assert config.allowed_uids == frozenset({501})
    assert config.run_dir == Path('/Library/Application Support/Rynhult Desktop/Owners/501/run/rynd-501')
    assert env['PATH'].endswith('/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin')
    assert env['HOME'] == value['home'] and env['SHELL'] == '/bin/sh'
    # tmux -L lands in the agent's run directory, not /private/tmp, under a label short enough for sun_path.
    assert env['TMUX_TMPDIR'] == '/Library/Application Support/Rynhult Desktop/Owners/501/run/rynd-501/tmux'
    assert config.tmux_socket == 'rd'
    assert not {'RYNHULT_DESKTOP_TOKEN', 'ACC_SESSION_ID', 'XPC_SERVICE_NAME'} & env.keys()


def test_socket_metadata_requires_exact_owner_and_private_mode(tmp_path, monkeypatch):
    module = broker()
    path = tmp_path / 'broker.sock'
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    monkeypatch.setattr(module, 'protected', lambda paths: None)
    try:
        path.chmod(0o600)
        module.check_socket_path(path, os.getuid())
        with pytest.raises(ValueError, match='owner-only'):
            module.check_socket_path(path, os.getuid() + 1)
        path.chmod(0o660)
        with pytest.raises(ValueError, match='owner-only'):
            module.check_socket_path(path, os.getuid())
    finally:
        listener.close()


async def test_shell_override_follows_persistent_tmux_server_setup(monkeypatch):
    from types import SimpleNamespace
    module = broker()
    calls = []
    monkeypatch.setattr(module.ops, 'prepare_tmux', lambda config: calls.append('persistent'))

    def configure_shell(config, *args, **kwargs):
        assert calls == ['persistent'], 'default-shell must apply to the retained tmux server'
        assert args == ('set-option', '-g', 'default-shell', '/bin/sh')
        assert kwargs == {'check': True}
        calls.append('shell')

    async def start(*args, **kwargs):
        assert calls == ['persistent', 'shell']
        raise RuntimeError('test stopped before real broker listener')

    monkeypatch.setattr(module.ops, 'tmux', configure_shell)
    monkeypatch.setattr(module, 'Broker', lambda config: SimpleNamespace(handle=object()))
    monkeypatch.setattr(module.asyncio, 'start_unix_server', start)
    with pytest.raises(RuntimeError, match='test stopped'):
        await module.serve(object(), object())
