"""Disposable GnuPG homes journaled before startup for crashed-worker cleanup."""
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from owned_tmux import alive, pin_process, signal_process

_run = subprocess.run


def agent_processes(homes):
    """Match exact private homedirs and our UID, never the user's default agent."""
    found = {}
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            args = (proc / 'cmdline').read_bytes().split(b'\0')
            if (Path(os.fsdecode(args[0])).name != 'gpg-agent' or b'--homedir' not in args
                    or os.fsdecode(args[args.index(b'--homedir') + 1]) not in homes):
                continue
            fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            if proc.stat().st_uid == os.getuid() and fields[0] != 'Z':
                found[int(proc.name)] = fields[19]
        except (OSError, IndexError, ValueError):
            continue
    return found


def stop_agents(home):
    """Stop every GnuPG daemon, then wait; pin fallback signals against PID reuse."""
    handles = []
    try:
        for pid, identity in agent_processes({str(home)}).items():
            fd = pin_process(pid, identity)
            if fd is not None:
                handles.append(fd)
        result = _run(['/usr/bin/gpgconf', '--homedir', str(home), '--kill', 'all'],
                      capture_output=True, timeout=10)
        deadline = time.monotonic() + 3
        while any(alive(fd) for fd in handles) and time.monotonic() < deadline:
            time.sleep(.02)
    finally:
        for fd in handles:
            if alive(fd):
                signal_process(fd, signal.SIGKILL)
            os.close(fd)
    if result.returncode:
        raise RuntimeError(f'gpgconf cleanup failed for {home}: {result.stderr.decode(errors="replace")}')


def new_home(directory):
    # Short sockets regardless of worktree/basetemp length; durable ownership
    # survives os._exit in an xdist worker. Register before any gpg invocation.
    journal = directory / 'owned-gpg'
    journal.mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix='acc-gpg-test-', dir='/tmp'))
    (journal / f'{uuid.uuid4().hex}.home').write_text(str(home))
    return home


def assert_no_gpg_agents(directory):
    homes = {entry.read_text() for entry in directory.glob('**/owned-gpg/*.home')}
    leaked = agent_processes(homes)
    errors = []
    for home in homes:
        if not Path(home).exists() and not leaked:
            continue
        try:
            stop_agents(Path(home))
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            errors.append(str(error))
        # Only registered directories created by this run are removable.
        shutil.rmtree(home, ignore_errors=True)
    if leaked or errors:
        pytest.fail(f'gpg-agent leak at session end: {sorted(leaked)}; cleanup errors: {errors}', pytrace=False)


@pytest.fixture(scope='session', autouse=True)
def no_leftover_gpg_at_session_end(tmp_path_factory):
    directory = tmp_path_factory.getbasetemp()
    yield
    assert_no_gpg_agents(directory)


@pytest.fixture(scope='session')
def gpg_home(tmp_path_factory, no_leftover_gpg_at_session_end):
    homes = []

    def make():
        home = new_home(tmp_path_factory.getbasetemp())
        homes.append(home)
        return home

    yield make
    errors = []
    for home in homes:
        try:
            stop_agents(home)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            errors.append(str(error))
    if errors:
        pytest.fail('; '.join(errors), pytrace=False)
