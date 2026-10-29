"""Linux test-process ownership, shared by pytest and Playwright's outer teardown."""
import os
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


def cleanup(socket, *, token=None):
    result = _run(['tmux', '-L', socket, 'display-message', '-p', '#{pid}'],
                  capture_output=True, text=True, timeout=3)
    if result.returncode:
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
        if token is not None:
            try:
                environment = Path(f'/proc/{server}/environ').read_bytes().split(b'\0')
            except OSError:
                return  # no proof this is our server
            if ('ACC_TEST_CLEANUP_OWNER=' + token).encode() not in environment:
                return  # the socket was reused by another run/Owner
        # Stop children while their parent can still reap them. Hold pidfds through
        # both signals: a child PID may be reused during the grace period.
        children = [fd for pid, fd in handles.items() if pid != server]
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for fd in children:
                signal_process(fd, sig)
            deadline = time.monotonic() + .5
            while time.monotonic() < deadline and any(alive(fd) for fd in children):
                time.sleep(.02)
        # Never resolve the socket name again: it could now belong to another run.
        signal_process(handles[server], signal.SIGTERM)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(alive(fd) for fd in handles.values()):
            time.sleep(.02)
        remaining = sum(alive(fd) for fd in handles.values())
        if remaining:
            raise RuntimeError(f'test cleanup left {remaining} owned processes')
    finally:
        for fd in handles.values():
            os.close(fd)


class Owner:
    """An out-of-process safety net also runs when the test parent is interrupted or killed."""
    def __init__(self):
        import sys
        import uuid
        self.token = uuid.uuid4().hex
        # Future test children inherit the run tag. It is an ownership marker,
        # not a secret or a boundary against hostile code sharing this uid.
        os.environ['ACC_TEST_CLEANUP_OWNER'] = self.token
        self.command = [sys.executable, str(Path(__file__).resolve()), '--watch', self.token]
        self.process = None
        self.sockets = set()

    def register(self, socket):
        if socket not in self.sockets:
            if self.process is None:
                self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, text=True, start_new_session=True)
            self.process.stdin.write(socket + '\n')
            self.process.stdin.flush()
            self.sockets.add(socket)

    def close(self):
        if self.process is None:
            return
        self.process.stdin.close()
        if self.process.wait(timeout=30):
            raise RuntimeError('owned tmux cleanup failed')


if __name__ == '__main__':
    import json
    import sys
    if sys.argv[1] == '--watch':
        sockets = [line.strip() for line in sys.stdin]
        for socket in sockets:
            cleanup(socket, token=sys.argv[2])
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
