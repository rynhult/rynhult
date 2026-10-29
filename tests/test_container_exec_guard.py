"""The service must fail before Podman if systemd did not establish its trust boundary."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

DIGEST = __import__('hashlib').sha256(b'acc-ct-s1a2b3c4d5-g1').hexdigest()[:16]
ATTACH = f'/run/acc-ct-attach/{DIGEST}/attach.sock'
RUNTIME = f'/run/acc-ct/{DIGEST}'


@pytest.fixture(autouse=True)
def unit_runtime(monkeypatch):
    """systemd-run --setenv=XDG_RUNTIME_DIR=<the unit's runtime> (acc-containers systemd_run_argv)."""
    monkeypatch.setenv('XDG_RUNTIME_DIR', RUNTIME)


def load_guard():
    path = Path(__file__).parents[1] / 'deploy/containers/acc-container-exec'
    loader = importlib.machinery.SourceFileLoader('acc_container_exec', str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def test_guard_rejects_host_namespace_without_executing_podman():
    guard = load_guard()
    host = os.stat('/proc/self/ns/user')
    with pytest.raises(ValueError):
        guard.verify_namespace(os.getuid(), os.getgid(), (host.st_dev, host.st_ino))


@pytest.mark.parametrize('change', ['none', 'owner', 'uid-map', 'gid-map', 'uid'])
def test_guard_requires_root_owned_full_identity_namespace(monkeypatch, change):
    guard = load_guard()
    monkeypatch.setattr(guard.os, 'stat', lambda path: type('Info', (), {'st_dev': 4, 'st_ino': 5})())
    monkeypatch.setattr(guard.os, 'getuid', lambda: 1005 if change == 'uid' else 1004)
    monkeypatch.setattr(guard.os, 'getgid', lambda: 1004)

    def ioctl(fd, operation, owner, mutate):
        owner[0] = 1004 if change == 'owner' else 0

    def read(path):
        if (change == 'uid-map' and path.name == 'uid_map') or (change == 'gid-map' and path.name == 'gid_map'):
            return '0 0 65536\n'
        return '0 0 4294967295\n'

    monkeypatch.setattr(guard.fcntl, 'ioctl', ioctl)
    monkeypatch.setattr(Path, 'read_text', read)
    if change == 'none':
        guard.verify_namespace(1004, 1004, (1, 2))
    else:
        with pytest.raises(ValueError):
            guard.verify_namespace(1004, 1004, (1, 2))


def test_bootstrap_refuses_unprivileged_caller():
    guard = load_guard()
    with pytest.raises((ValueError, PermissionError)):
        guard.enter_namespace(1004, 1004, (0, 0))


@pytest.mark.parametrize('uid,gid', [(0, 1004), (1004, 0), (-1, 1004), (1004, 4294967295)])
def test_bootstrap_refuses_privileged_or_invalid_target(monkeypatch, uid, gid):
    guard = load_guard()
    monkeypatch.setattr(guard.os, 'getuid', lambda: 0)
    monkeypatch.setattr(guard.os, 'geteuid', lambda: 0)
    with pytest.raises(ValueError):
        guard.enter_namespace(uid, gid, (0, 0))


def test_drop_identity_removes_groups_and_all_saved_ids(monkeypatch):
    guard = load_guard()
    calls = []
    monkeypatch.setattr(guard.os, 'setgroups', lambda groups: calls.append(('groups', groups)))
    monkeypatch.setattr(guard.os, 'setresgid', lambda *ids: calls.append(('gid', ids)))
    monkeypatch.setattr(guard.os, 'setresuid', lambda *ids: calls.append(('uid', ids)))
    guard.drop_identity(1004, 1005)
    assert calls == [('groups', []), ('gid', (1005, 1005, 1005)), ('uid', (1004, 1004, 1004))]


@pytest.mark.parametrize('field', ['CapInh', 'CapPrm', 'CapEff', 'CapAmb', 'Groups', 'Uid', 'Gid'])
def test_guard_rejects_residual_privilege(monkeypatch, field):
    guard = load_guard()
    status = {'CapInh': '0', 'CapPrm': '0', 'CapEff': '0', 'CapAmb': '0',
              'Groups': '', 'Uid': '1004 1004 1004 1004', 'Gid': '1005 1005 1005 1005'}
    status[field] = '1'
    monkeypatch.setattr(Path, 'read_text', lambda path: '\n'.join(f'{k}: {v}' for k, v in status.items()))
    with pytest.raises(ValueError):
        guard.verify_identity(1004, 1005)


def test_guard_accepts_fully_dropped_identity(monkeypatch):
    guard = load_guard()
    monkeypatch.setattr(Path, 'read_text', lambda path: 'Uid: 1004 1004 1004 1004\nGid: 1005 1005 1005 1005\n'
                        'Groups:\nCapInh: 0\nCapPrm: 0\nCapEff: 0\nCapAmb: 0\n')
    guard.verify_identity(1004, 1005)


@pytest.mark.parametrize('wrong', ['none', 'other-unit', 'other-slice', 'symlink', 'writable', 'owner'])
def test_cgroup_delegation_is_limited_to_this_units_controls(tmp_path, monkeypatch, wrong):
    guard = load_guard()
    unit = 'acc-ct-s1a2b3c4d5-g1.service'
    relative = f'acc.slice/acc-containers.slice/acc-containers-1004.slice/{unit}'
    folder = tmp_path / relative
    folder.mkdir(parents=True)
    for name in ('cgroup.procs', 'cgroup.threads', 'cgroup.subtree_control', 'memory.max'):
        (folder / name).write_text('')
    if wrong == 'symlink':
        (folder / 'cgroup.procs').unlink()
        (folder / 'cgroup.procs').symlink_to(folder / 'memory.max')
    if wrong == 'writable':
        folder.chmod(0o777)
    monkeypatch.setattr(guard, 'CGROUP_ROOT', tmp_path)
    reported = relative.replace(unit, 'other.service') if wrong == 'other-unit' else relative
    if wrong == 'other-slice':
        reported = reported.replace('1004.slice', '1005.slice')
    monkeypatch.setattr(Path, 'read_text', lambda path: f'0::/{reported}\n')
    real_fstat = os.fstat
    def root_stat(fd):
        info = list(real_fstat(fd))
        info[4] = 1004 if wrong == 'owner' else 0
        return os.stat_result(info)
    monkeypatch.setattr(guard.os, 'fstat', root_stat)
    changed = []
    monkeypatch.setattr(guard.os, 'fchown', lambda fd, uid, gid: changed.append((Path(f'/proc/self/fd/{fd}').resolve().name, uid, gid)))
    if wrong != 'none':
        with pytest.raises((ValueError, OSError)):
            guard.delegate_cgroup(1004, 1005, unit)
        assert not changed  # validate all objects before changing any ownership
    else:
        guard.delegate_cgroup(1004, 1005, unit)
        assert changed == [('cgroup.procs', 1004, 1005), ('cgroup.threads', 1004, 1005),
                           ('cgroup.subtree_control', 1004, 1005), (unit, 1004, 1005)]


def test_mapper_writes_complete_identity_maps_through_held_directory(tmp_path):
    guard = load_guard()
    for name in ('uid_map', 'gid_map'):
        (tmp_path / name).write_bytes(b'')
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        guard._write_maps(fd)
    finally:
        os.close(fd)
    assert (tmp_path / 'uid_map').read_bytes() == b'0 0 4294967295\n'
    assert (tmp_path / 'gid_map').read_bytes() == b'0 0 4294967295\n'


@pytest.mark.parametrize('failure', ['none', 'unshare', 'mapper'])
def test_bootstrap_handshake_reaps_mapper_before_dropping_privilege(tmp_path, monkeypatch, failure):
    # Actual fork/socket/wait protocol; privileged kernel operations alone are
    # replaced. Removing the handshake or ignoring either failure must fail.
    guard = load_guard()
    host = os.stat('/proc/self/ns/user')
    marker = tmp_path / 'mapped'
    events = []
    monkeypatch.setattr(guard.os, 'getuid', lambda: 0)
    monkeypatch.setattr(guard.os, 'geteuid', lambda: 0)
    def unshare():
        events.append('unshare')
        if failure == 'unshare':
            raise PermissionError('unshare denied')
    def maps(fd):
        if failure == 'mapper':
            raise PermissionError('mapping denied')
        marker.write_text('mapped')
    def verify(uid, gid, namespace):
        assert marker.read_text() == 'mapped'
        events.append(('verified', uid))
    real_waitpid = os.waitpid
    def waitpid(pid, options):
        result = real_waitpid(pid, options)
        events.append('reaped')
        return result
    monkeypatch.setattr(guard, '_unshare_user', unshare)
    monkeypatch.setattr(guard, '_write_maps', maps)
    monkeypatch.setattr(guard, 'verify_namespace', verify)
    monkeypatch.setattr(guard, 'drop_identity', lambda uid, gid: events.append('dropped'))
    monkeypatch.setattr(guard, 'verify_identity', lambda uid, gid: events.append('identity'))
    monkeypatch.setattr(guard.os, 'waitpid', waitpid)
    if failure != 'none':
        with pytest.raises((OSError, ValueError)):
            guard.enter_namespace(1004, 1005, (host.st_dev, host.st_ino))
        assert events == ['unshare', 'reaped']
        assert not marker.exists()
    else:
        guard.enter_namespace(1004, 1005, (host.st_dev, host.st_ino))
        assert events == ['unshare', 'reaped', ('verified', 0), 'dropped', ('verified', 1004), 'identity']


@pytest.mark.parametrize('failure', ['delegation', 'namespace', 'identity'])
def test_guard_never_executes_podman_after_bootstrap_failure(monkeypatch, failure):
    guard = load_guard()
    monkeypatch.setattr(guard.sys, 'argv', ['guard', '1004', '1005', '1', '2',
                        'acc-ct-s1a2b3c4d5-g1.service', ATTACH, '--', '/usr/bin/podman', 'run'])
    def fail(*args):
        raise ValueError('setup failed')
    monkeypatch.setattr(guard, 'delegate_cgroup', fail if failure == 'delegation' else lambda *args: None)
    monkeypatch.setattr(guard, 'enter_namespace', fail)
    executed = []
    monkeypatch.setattr(guard.os, 'execv', lambda *args: executed.append(args))
    assert guard.main() == 126
    assert not executed


def test_guard_executes_only_fixed_podman_after_successful_setup(monkeypatch):
    guard = load_guard()
    command = ['/usr/bin/podman', 'run', '--rootfs', '/held/rootfs', 'literal-$HOME']
    monkeypatch.setattr(guard.sys, 'argv', ['guard', '1004', '1005', '1', '2',
                        'acc-ct-s1a2b3c4d5-g1.service', ATTACH, '--', *command])
    events = []
    monkeypatch.setattr(guard, 'delegate_cgroup', lambda *args: events.append(('delegate', args)))
    monkeypatch.setattr(guard, 'enter_namespace', lambda *args: events.append(('enter', args)))
    monkeypatch.setattr(guard, 'listen', lambda *args: events.append(('listen', args)) or 'listener')
    monkeypatch.setattr(guard, 'start_relay', lambda *args: events.append(('relay', args)))
    monkeypatch.setattr(guard.os, 'execv', lambda *args: events.append(('exec', args)))
    guard.main()
    # The relay binds and forks only after the identity drop, as the account; then the fixed Podman runs.
    assert events == [('delegate', (1004, 1005, 'acc-ct-s1a2b3c4d5-g1.service')),
                      ('enter', (1004, 1005, (1, 2))), ('listen', (ATTACH, 1004)), ('relay', ('listener', RUNTIME, 1004)),
                      ('exec', ('/usr/bin/podman', command))]


@pytest.mark.parametrize('command', [[], ['/bin/sh'], ['/usr/bin/env', '/usr/bin/podman']])
def test_guard_rejects_other_commands_before_root_setup(monkeypatch, command):
    guard = load_guard()
    monkeypatch.setattr(guard.sys, 'argv', ['guard', '1004', '1005', '1', '2',
                        'acc-ct-s1a2b3c4d5-g1.service', ATTACH, '--', *command])
    def unexpected(*args):
        pytest.fail('invalid command reached privileged setup or execution')
    monkeypatch.setattr(guard, 'delegate_cgroup', unexpected)
    monkeypatch.setattr(guard, 'enter_namespace', unexpected)
    monkeypatch.setattr(guard.os, 'execv', unexpected)
    assert guard.main() == 126


# ---- docs/reviews/2026-10-24-security-review-ct05-sd255.md ------------------------------------------------------------

@pytest.mark.parametrize('index,value', [(1, '+1004'), (1, ' 1004'), (2, '1_005'), (1, '\u0661\u0660\u0660\u0664'),
                                         (3, '-1'), (4, '0002')])
def test_sd255_guard_accepts_only_canonical_decimal_arguments(monkeypatch, index, value):
    guard = load_guard()
    argv = ['guard', '1004', '1005', '1', '2', 'acc-ct-s1a2b3c4d5-g1.service', ATTACH, '--', '/usr/bin/podman', 'run']
    argv[index] = value
    monkeypatch.setattr(guard.sys, 'argv', argv)
    reached = []
    monkeypatch.setattr(guard, 'delegate_cgroup', lambda *args: reached.append('delegate'))
    monkeypatch.setattr(guard, 'enter_namespace', lambda *args: reached.append('enter'))
    monkeypatch.setattr(guard.os, 'execv', lambda *args: reached.append('exec'))
    assert guard.main() == 126
    assert not reached


@pytest.mark.parametrize('error', [TypeError('unexpected'), MemoryError(), KeyboardInterrupt()])
def test_sd255_guard_reports_every_bootstrap_failure(monkeypatch, capsys, error):
    guard = load_guard()
    monkeypatch.setattr(guard.sys, 'argv', ['guard', '1004', '1005', '1', '2',
                        'acc-ct-s1a2b3c4d5-g1.service', ATTACH, '--', '/usr/bin/podman', 'run'])
    monkeypatch.setattr(guard, 'delegate_cgroup', lambda *args: None)
    def fail(*args):
        raise error
    monkeypatch.setattr(guard, 'enter_namespace', fail)
    executed = []
    monkeypatch.setattr(guard.os, 'execv', lambda *args: executed.append(args))
    try:
        code = guard.main()
    except BaseException as escaped:  # KeyboardInterrupt would otherwise stop pytest itself
        pytest.fail(f'bootstrap error escaped the guard: {escaped!r}')
    assert code == 126 and not executed
    assert json.loads(capsys.readouterr().err)['code'] == 'unsafe_runtime_namespace'


# ---- the terminal relay (docs/decisions/drafts/container-attach-path.md, option A) ------------------------------------

def frames(conn, count):
    import struct
    out, data = [], b''
    while len(out) < count:
        while len(data) < 5 or len(data) < 5 + struct.unpack('>cI', data[:5])[1]:
            chunk = conn.recv(65536)
            assert chunk, out
            data += chunk
        kind, length = struct.unpack('>cI', data[:5])
        out.append((kind, data[5:5 + length]))
        data = data[5 + length:]
    return out


def fake_conmon(tmp_path):
    """conmon's side in a runroot like the unit's: a SEQPACKET attach socket and the ctl FIFO, in the bundle folder."""
    import socket
    userdata = tmp_path / 'rt/run/overlay-containers' / ('c' * 64) / 'userdata'
    userdata.mkdir(parents=True)
    os.mkfifo(userdata / 'ctl')
    ctl = os.open(userdata / 'ctl', os.O_RDONLY | os.O_NONBLOCK)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    folder = os.open(userdata, os.O_PATH | os.O_DIRECTORY)
    try:
        server.bind(f'/proc/self/fd/{folder}/attach')  # longer than a socket address, as in production
    finally:
        os.close(folder)
    server.listen(1)
    return tmp_path / 'rt', server, ctl


def test_relay_carries_terminal_bytes_and_resizes_to_conmon_until_the_container_ends(tmp_path):
    import json
    import socket
    import threading
    guard = load_guard()
    runtime, server, ctl = fake_conmon(tmp_path)
    ours, relayed = socket.socketpair()
    done = threading.Thread(target=guard.serve, args=(relayed, str(runtime), 5))
    done.start()
    conmon, _ = server.accept()
    conmon.send(b'\x02hello')                 # stdout
    conmon.send(b'\x01ignored')               # not an output stream
    conmon.send(b'\x03warn')                  # stderr
    assert frames(ours, 2) == [(b'd', b'hello'), (b'd', b'warn')]
    big = b'k' * 10000
    ours.sendall(guard.frame(b'd', b'ls\n') + guard.frame(b'd', big))
    received = [conmon.recv(8193)]
    while sum(map(len, received)) < 3 + len(big):
        received.append(conmon.recv(8193))
    assert received[0] == b'ls\n' and b''.join(received[1:]) == big
    assert max(map(len, received)) <= guard.CONMON_CHUNK  # conmon would truncate a larger packet
    ours.sendall(guard.frame(b'r', json.dumps({'rows': 0, 'cols': 80}).encode())
                 + guard.frame(b'r', json.dumps({'rows': 40, 'cols': 120}).encode()))
    deadline = __import__('time').monotonic() + 5
    written = b''
    while not written and __import__('time').monotonic() < deadline:
        try:
            written = os.read(ctl, 64)
        except BlockingIOError:
            pass
    assert written == b'1 40 120\n'  # Podman's resize message; the invalid size was dropped
    conmon.close()                    # the container ended
    assert frames(ours, 1) == [(b'x', b'')]
    done.join(5)
    assert not done.is_alive()
    os.close(ctl)


def test_relay_ends_on_an_unknown_frame_and_keeps_the_container(tmp_path):
    import socket
    import threading
    guard = load_guard()
    runtime, server, ctl = fake_conmon(tmp_path)
    ours, relayed = socket.socketpair()
    done = threading.Thread(target=guard.serve, args=(relayed, str(runtime), 5))
    done.start()
    conmon, _ = server.accept()
    ours.sendall(guard.frame(b'q', b'?'))
    done.join(5)
    assert not done.is_alive()
    conmon.settimeout(1)
    assert conmon.recv(10) == b''  # only the relay's connection closed
    os.close(ctl)


def test_relay_waits_for_exactly_one_container(tmp_path):
    guard = load_guard()
    with pytest.raises(OSError):
        guard.conmon_endpoint(str(tmp_path), __import__('time').monotonic())
    runtime, server, ctl = fake_conmon(tmp_path)
    assert guard.conmon_endpoint(str(runtime), 0).endswith('/userdata')
    other = runtime / 'run/overlay-containers' / ('d' * 64) / 'userdata'
    other.mkdir(parents=True)
    import socket
    extra = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    folder = os.open(other, os.O_PATH | os.O_DIRECTORY)
    extra.bind(f'/proc/self/fd/{folder}/attach')
    os.close(folder)
    with pytest.raises(OSError):
        guard.conmon_endpoint(str(runtime), __import__('time').monotonic() + 5)
    os.close(ctl)


def test_relay_serves_only_the_account(monkeypatch):
    import socket
    import struct
    guard = load_guard()
    served = []

    class Client:
        def __init__(self, uid):
            self.uid = uid

        def getsockopt(self, *args):
            return struct.pack('3i', 99, self.uid, self.uid)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Listener:
        clients = [Client(1005), Client(1004)]

        def accept(self):
            if not self.clients:
                raise SystemExit
            return self.clients.pop(0), None

    monkeypatch.setattr(guard, 'serve', lambda client, runtime: served.append(client.uid))
    with pytest.raises(SystemExit):
        guard.relay(Listener(), '/run/acc-ct/x', 1004)
    assert served == [1004]
    assert socket.SO_PEERCRED


@pytest.mark.parametrize('attach,runtime', [
    ('/run/acc-ct-attach/0123456789abcdef/attach.sock', RUNTIME),            # another unit's socket
    (ATTACH, '/run/acc-ct/0123456789abcdef'),                                # another unit's runtime
    (ATTACH.replace('/run/', '/tmp/'), RUNTIME),
    (ATTACH + '/../x', RUNTIME),
    (ATTACH, None),
])
def test_attach_socket_and_runtime_must_be_this_units(attach, runtime):
    guard = load_guard()
    with pytest.raises(ValueError):
        guard.attach_paths('acc-ct-s1a2b3c4d5-g1.service', attach, runtime)
    assert guard.attach_paths('acc-ct-s1a2b3c4d5-g1.service', ATTACH, RUNTIME) == (ATTACH, RUNTIME)


def test_guard_refuses_a_foreign_attach_socket_before_root_setup(monkeypatch):
    guard = load_guard()
    monkeypatch.setattr(guard.sys, 'argv', ['guard', '1004', '1005', '1', '2', 'acc-ct-s1a2b3c4d5-g1.service',
                                            '/run/acc-ct-attach/0123456789abcdef/attach.sock', '--', '/usr/bin/podman'])
    def unexpected(*args):
        pytest.fail('a foreign attach socket reached privileged setup')
    monkeypatch.setattr(guard, 'delegate_cgroup', unexpected)
    monkeypatch.setattr(guard, 'enter_namespace', unexpected)
    monkeypatch.setattr(guard.os, 'execv', unexpected)
    assert guard.main() == 126


def test_relay_listens_only_in_the_accounts_private_folder(tmp_path):
    import stat
    guard = load_guard()
    import tempfile
    folder = Path(tempfile.mkdtemp(prefix='ct-', dir='/tmp'))
    try:
        folder.chmod(0o755)
        with pytest.raises(ValueError):
            guard.listen(str(folder / 'attach.sock'), os.getuid())
        folder.chmod(0o700)
        with pytest.raises(ValueError):
            guard.listen(str(folder / 'attach.sock'), os.getuid() + 1)
        listener = guard.listen(str(folder / 'attach.sock'), os.getuid())
        assert stat.S_IMODE((folder / 'attach.sock').stat().st_mode) == 0o600
        with pytest.raises(OSError):  # never takes over an existing socket
            guard.listen(str(folder / 'attach.sock'), os.getuid())
        listener.close()
        link = folder / 'link'
        link.symlink_to(folder)
        with pytest.raises(ValueError):
            guard.listen(str(link / 'other.sock'), os.getuid())
    finally:
        for item in folder.iterdir():
            item.unlink()
        folder.rmdir()


def test_a_hostile_resize_never_ends_the_relay(tmp_path):
    """Review A-3: a deeply nested or oversized resize payload is dropped; the terminal keeps working."""
    import json
    import socket
    import threading
    guard = load_guard()
    runtime, server, ctl = fake_conmon(tmp_path)
    ours, relayed = socket.socketpair()
    done = threading.Thread(target=guard.serve, args=(relayed, str(runtime), 5), daemon=True)
    done.start()
    conmon, _ = server.accept()
    ours.sendall(guard.frame(b'r', b'[' * 60000) + guard.frame(b'r', b'{"rows": 30, "cols": 90' + b' ' * 100 + b'}')
                 + guard.frame(b'r', b'\xff') + guard.frame(b'r', json.dumps({'rows': 40, 'cols': 120}).encode())
                 + guard.frame(b'd', b'still here'))
    conmon.settimeout(5)
    assert conmon.recv(100) == b'still here'
    assert done.is_alive()
    assert os.read(ctl, 64) == b'1 40 120\n'  # only the valid, small one
    conmon.close()
    done.join(5)
    os.close(ctl)


def test_any_error_serving_one_client_never_ends_the_relay(monkeypatch):
    """Review A-3: whatever one connection raises (RecursionError included), the next one is served."""
    import struct
    guard = load_guard()
    served = []

    class Client:
        def __init__(self, error):
            self.error = error

        def getsockopt(self, *args):
            return struct.pack('3i', 99, 1004, 1004)

        def settimeout(self, value):
            pass

        def sendall(self, data):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Listener:
        clients = [Client(RecursionError()), Client(MemoryError()), Client(None)]

        def accept(self):
            if not self.clients:
                raise SystemExit
            return self.clients.pop(0), None

    def serve(client, runtime):
        if client.error:
            raise client.error
        served.append(client)
    monkeypatch.setattr(guard, 'serve', serve)
    with pytest.raises(SystemExit):
        guard.relay(Listener(), '/run/acc-ct/x', 1004)
    assert len(served) == 1


def test_a_client_that_stops_reading_is_dropped(tmp_path):
    """Review A-5: a pane that stops reading must not hold the terminal: the relay's sends time out."""
    import socket
    import threading
    import time
    guard = load_guard()
    runtime, server, ctl = fake_conmon(tmp_path)
    ours, relayed = socket.socketpair()
    result = []

    def run():
        try:
            guard.serve(relayed, str(runtime), 5, send_timeout=0.5)
        except OSError as error:
            result.append(error)
        else:
            result.append(None)
    done = threading.Thread(target=run, daemon=True)
    done.start()
    conmon, _ = server.accept()
    conmon.settimeout(0.2)

    def flood():
        try:
            while True:
                conmon.send(b'\x02' + b'o' * 4000)
        except OSError:
            pass
    threading.Thread(target=flood, daemon=True).start()
    started = time.monotonic()
    done.join(10)
    assert not done.is_alive() and time.monotonic() - started < 10
    assert isinstance(result[0], TimeoutError)
    os.close(ctl)
