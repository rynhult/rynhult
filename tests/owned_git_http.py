"""Detect and reap Git CGI processes belonging to one exclusive test root."""
import os
import signal
import sys
import time
from pathlib import Path

from owned_tmux import alive, pin_process, signal_process


def backend_processes(directory):
    """Identify CGI/helpers by executable, UID, and explicit private GIT_PROJECT_ROOT."""
    directory = Path(directory).absolute()
    found = {}
    names = {'git', 'git-http-backend', 'git-receive-pack', 'git-upload-pack', 'git-index-pack', 'git-unpack-objects'}
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            args = (proc / 'cmdline').read_bytes().split(b'\0')
            if not any(Path(os.fsdecode(arg)).name in names for arg in args[:2]):
                continue
            if proc.stat().st_uid != os.getuid():
                continue
            root = next((os.fsdecode(value.split(b'=', 1)[1]) for value in
                         (proc / 'environ').read_bytes().split(b'\0') if value.startswith(b'GIT_PROJECT_ROOT=')), None)
            if root is None or not Path(root).is_absolute() or not Path(root).is_relative_to(directory):
                continue
            fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] != 'Z':
                found[int(proc.name)] = fields[19]
        except (OSError, IndexError):
            continue
    return found


def assert_no_git_backends(directory, *, record=False):
    marker = Path(directory) / 'git-http-leaks.txt'
    leaked = backend_processes(directory)
    handles = []
    try:
        for pid, identity in leaked.items():
            fd = pin_process(pid, identity)
            if fd is not None:
                handles.append(fd)
                signal_process(fd, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while any(alive(fd) for fd in handles) and time.monotonic() < deadline:
            time.sleep(.02)
        remaining = sum(alive(fd) for fd in handles)
    finally:
        for fd in handles:
            os.close(fd)
    if leaked:
        message = f'git-http-backend leak at teardown: {sorted(leaked)}; {remaining} remain after cleanup'
        if record:
            marker.write_text(message)
        raise RuntimeError(message)
    if record and marker.exists():
        # The server may already have reaped a leak. Preserve its failure for
        # Playwright, which does not otherwise inspect server teardown errors.
        raise RuntimeError(marker.read_text())


if __name__ == '__main__':
    try:
        assert_no_git_backends(Path(sys.argv[1]), record=True)
    except RuntimeError as error:
        sys.exit(str(error))
