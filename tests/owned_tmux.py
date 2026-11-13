"""Linux test-process ownership, shared by pytest and Playwright's outer teardown."""
import atexit
import json
import os
import select
import signal
import subprocess
import time
from pathlib import Path

# Tests sometimes replace subprocess.run; teardown must still execute the real command.
_run = subprocess.run


def start_time(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def process_snapshot(root):
    processes = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
            if fields[0] != 'Z':
                processes[int(path.parent.name)] = (int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            continue
    owned = {root} if root in processes else set()
    while True:
        children = {pid for pid, (parent, _) in processes.items() if parent in owned}
        if children <= owned:
            return {pid: processes[pid][1] for pid in owned}
        owned |= children


def live_processes(root):
    return set(process_snapshot(root))


def pin_process(pid, identity):
    """A pidfd cannot signal a recycled PID, even after the identity check."""
    if identity is None:
        return None
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return None
    if start_time(pid) != identity:
        os.close(fd)
        return None
    return fd


def alive(fd):
    import select
    return not select.select([fd], [], [], 0)[0]


def signal_process(fd, sig):
    try:
        signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass


def socket_path(socket):
    return Path(os.environ.get('TMUX_TMPDIR', '/tmp')) / f'tmux-{os.getuid()}' / socket


def socket_identity(socket):
    try:
        info = socket_path(socket).lstat()
        return (info.st_dev, info.st_ino)
    except FileNotFoundError:
        return None


def owns_server(server, token):
    try:
        return ('ACC_TEST_CLEANUP_OWNER=' + token).encode() in Path(f'/proc/{server}/environ').read_bytes().split(b'\0')
    except OSError:
        return False


def unlink_socket(socket, identity):
    # A replaced directory entry belongs to someone else, even when its name matches.
    if identity is not None and socket_identity(socket) == tuple(identity):
        socket_path(socket).unlink(missing_ok=True)


def cleanup(socket, *, token=None, identity=None):
    entry = socket_identity(socket)
    result = _run(['tmux', '-L', socket, 'display-message', '-p', '#{pid}'],
                  capture_output=True, text=True, timeout=3)
    if result.returncode:
        unlink_socket(socket, identity if token is not None else entry)
        return
    server = int(result.stdout.strip())
    identities = process_snapshot(server)
    handles = {}
    try:
        for pid, identity in identities.items():
            fd = pin_process(pid, identity)
            if fd is not None:
                handles[pid] = fd
        if server not in handles or not alive(handles[server]):
            return  # no longer the server whose descendants were enumerated
        if token is not None and not owns_server(server, token):
            return  # the socket was reused by another run/Owner
        identity = entry
        # Stop children while their parent can still reap them. Hold pidfds through
        # both signals: a child PID may be reused during the grace period.
        children = [fd for pid, fd in handles.items() if pid != server]
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for fd in children:
                signal_process(fd, sig)
            deadline = time.monotonic() + .5
            while time.monotonic() < deadline and any(alive(fd) for fd in children):
                time.sleep(.02)
        # Ask tmux to remove its socket normally. Keep the pinned fallback for a
        # wedged server, and never issue a command against a replaced entry.
        if alive(handles[server]) and socket_identity(socket) == identity:
            _run(['tmux', '-L', socket, 'kill-server'], capture_output=True, timeout=3)
        if alive(handles[server]):
            signal_process(handles[server], signal.SIGTERM)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(alive(fd) for fd in handles.values()):
            time.sleep(.02)
        remaining = sum(alive(fd) for fd in handles.values())
        if not alive(handles[server]):
            unlink_socket(socket, identity)
        if remaining:
            raise RuntimeError(f'test cleanup left {remaining} owned processes')
    finally:
        for fd in handles.values():
            os.close(fd)


class Owner:
    """An out-of-process safety net also runs when the test parent is interrupted or killed."""
    def __init__(self, journal=None):
        import sys
        import uuid
        self.token = uuid.uuid4().hex
        self.journal = journal
        atexit.register(self.close)
        # Future test children inherit the run tag. It is an ownership marker,
        # not a secret or a boundary against hostile code sharing this uid.
        os.environ['ACC_TEST_CLEANUP_OWNER'] = self.token
        self.done = journal / (self.token + '.done') if journal is not None else None
        self.command = [sys.executable, str(Path(__file__).resolve()), '--watch', self.token]
        if self.done is not None:
            self.command.append(str(self.done))
        self.process = None
        self.sockets = set()

    def register(self, socket):
        if socket not in self.sockets:
            if self.process is None:
                self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, text=True, start_new_session=True)
            self.process.stdin.write(socket + '\n')
            self.process.stdin.flush()
            self.sockets.add(socket)
            if self.journal is not None:
                self.journal.mkdir(parents=True, exist_ok=True)
                pending = self.journal / (self.token + '.tmp')
                pending.write_text(json.dumps({'token': self.token, 'sockets': sorted(self.sockets)}))
                pending.replace(self.journal / (self.token + '.json'))

    def close(self):
        if self.process is None:
            atexit.unregister(self.close)
            return
        if not self.process.stdin.closed:
            self.process.stdin.close()
        result = self.process.wait(timeout=30)
        atexit.unregister(self.close)
        if result:
            raise RuntimeError('owned tmux cleanup failed')


if __name__ == '__main__':
    import json
    import sys
    if sys.argv[1] == '--watch':
        # Poll while the parent is alive: remember authenticated socket inodes so
        # even a server killed before EOF cannot leave a stale filesystem entry.
        sockets = {}
        pending = b''
        while True:
            if select.select([sys.stdin], [], [], .05)[0]:
                chunk = os.read(sys.stdin.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    sockets.setdefault(line.decode(), None)
            for socket in sockets:
                result = _run(['tmux', '-L', socket, 'display-message', '-p', '#{pid}'],
                              capture_output=True, text=True, timeout=3)
                if not result.returncode and owns_server(int(result.stdout.strip()), sys.argv[2]):
                    sockets[socket] = socket_identity(socket)
        errors = []
        for socket, identity in sockets.items():
            try:
                cleanup(socket, token=sys.argv[2], identity=identity)
            except Exception as error:
                errors.append(f'{socket}: {error}')
        if len(sys.argv) > 3:
            Path(sys.argv[3]).write_text('\n'.join(errors))
        if errors:
            sys.exit('\n'.join(errors))
    else:
        manifest = Path(sys.argv[1]) / 'processes.json'
        if manifest.exists():
            info = json.loads(manifest.read_text())
            pid = info['pid']
            fd = pin_process(pid, info.get('start_time'))
            if fd is not None:
                try:
                    signal_process(fd, signal.SIGTERM)
                    deadline = time.monotonic() + 8
                    while alive(fd) and time.monotonic() < deadline:
                        time.sleep(.05)
                    signal_process(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
            # The server's Owner watchdog owns its tmux cleanup, including after
            # SIGKILL. A stale manifest's socket name conveys no process authority.

            token = info.get('cleanup_token')
            if token:
                done = manifest.parent / 'owned-tmux' / (token + '.done')
                deadline = time.monotonic() + 15
                while not done.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                if not done.exists():
                    sys.exit('e2e tmux watchdog did not finish cleanup')
                if done.read_text():
                    sys.exit(done.read_text())
