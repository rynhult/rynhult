"""Cleanup must collect the server and its children, including stubborn fake CLIs."""
import os
import signal
import subprocess
import time
import uuid

import pytest


@pytest.mark.parametrize('exit_kind', ['normal', 'failure', 'interrupt'])
def test_cleanup_leaves_no_owned_processes(tmp_path, exit_kind):
    from owned_tmux import cleanup, live_processes

    socket = 'acc-test-clean-' + uuid.uuid4().hex[:8]
    stubborn = tmp_path / 'fake-cli.py'
    stubborn.write_text('import signal, time\nsignal.signal(signal.SIGHUP, signal.SIG_IGN)\n'
                        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(1)\n')
    subprocess.run(['tmux', '-L', socket, 'new-session', '-d', f'python3 {stubborn}'], check=True)
    pid = int(subprocess.check_output(['tmux', '-L', socket, 'display-message', '-p', '#{pid}']))
    time.sleep(.1)  # allow the child to install its signal handlers
    owned = live_processes(pid)
    assert len(owned) >= 2
    try:
        try:
            if exit_kind == 'failure':
                raise AssertionError('test failed')
            if exit_kind == 'interrupt':
                raise KeyboardInterrupt
        finally:
            cleanup(socket)
    except (AssertionError, KeyboardInterrupt):
        pass
    try:
        assert sum(1 for child in owned if live_processes(child)) == 0
    finally:  # keep the red regression safe too
        for child in owned:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize(("termination", "ignore_sigint"), [
    pytest.param("kill", False, id="kill"),
    pytest.param("group_interrupt", False, id="group_interrupt"),
    pytest.param("group_interrupt", True, id="group_interrupt_ignored"),
])
def test_owner_cleans_after_parent_is_killed(tmp_path, termination, ignore_sigint):
    import sys
    from pathlib import Path

    from owned_tmux import live_processes

    identity = tmp_path / 'pid'
    code = '''
import os, signal, subprocess, sys
from pathlib import Path
from owned_tmux import Owner
# Background runners can pass SIG_IGN through exec; readiness must include SIGINT handling.
signal.signal(signal.SIGINT, signal.default_int_handler)
owner = Owner()
socket = 'acc-test-orphan-' + str(os.getpid())
owner.register(socket)
subprocess.run(['tmux', '-L', socket, 'new-session', '-d', 'sleep 120'], check=True)
pid = subprocess.check_output(['tmux', '-L', socket, 'display-message', '-p', '#{pid}'])
ready = Path(sys.argv[1])
pending = ready.with_suffix('.tmp')
pending.write_bytes(pid)
pending.replace(ready)
signal.pause()
'''
    command = [sys.executable, '-c', code, str(identity)]
    if ignore_sigint:
        command = [sys.executable, '-c', '''
import os, signal, sys
signal.signal(signal.SIGINT, signal.SIG_IGN)
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
''', *command[1:]]
    child = subprocess.Popen(command,
                             env=os.environ | {'PYTHONPATH': str(Path(__file__).parent)}, start_new_session=True)
    owned = set()
    try:
        deadline = time.monotonic() + 5
        while not identity.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        owned = live_processes(int(identity.read_text()))
        assert len(owned) >= 2
        if termination == 'group_interrupt':
            os.killpg(child.pid, signal.SIGINT)
        else:
            child.kill()
        child.wait(timeout=3)
        deadline = time.monotonic() + 5
        while any(live_processes(pid) for pid in owned) and time.monotonic() < deadline:
            time.sleep(.02)
        assert sum(bool(live_processes(pid)) for pid in owned) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        for pid in owned:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('failure', ['test', 'setup', 'interrupt'])
def test_pytest_fixture_collects_processes_on_unsuccessful_exit(tmp_path, failure):
    import sys
    from pathlib import Path

    from owned_tmux import live_processes

    root = Path(__file__).resolve().parents[1]
    identity = tmp_path / 'owned.pid'
    case = tmp_path / 'test_failure.py'
    case.write_text('''
import json
import subprocess
from pathlib import Path
import pytest
from acc.broker import ops
from owned_tmux import live_processes

@pytest.fixture
def launched(env):
    ops.tmux(env['broker'], 'new-session', '-d', '-s', 'owned', 'sleep 120', check=True)
    pid = subprocess.check_output(['tmux', '-L', env['broker'].tmux_socket, 'display-message', '-p', '#{pid}'])
    Path(IDENTITY).write_text(json.dumps(sorted(live_processes(int(pid)))))
    if FAILURE == 'setup':
        raise RuntimeError('deliberate setup failure')
    return env

def test_failure(launched):
    if FAILURE == 'interrupt':
        raise KeyboardInterrupt
    raise AssertionError('deliberate test failure')
'''.replace('IDENTITY', repr(str(identity))).replace('FAILURE', repr(failure)))
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '--color=no', '-p', 'host_isolation', '-p', 'conftest', str(case)],
                            cwd=tmp_path, env=os.environ | {'PYTHONPATH': os.pathsep.join([str(root / 'tests'), str(root / 'src'), *sys.path])},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode in (1, 2), result.stdout + result.stderr
    assert identity.exists(), result.stdout + result.stderr
    import json
    pids = json.loads(identity.read_text())
    assert len(pids) >= 2
    assert sum(bool(live_processes(pid)) for pid in pids) == 0, result.stdout + result.stderr


def test_global_teardown_does_not_signal_a_reused_pid(tmp_path):
    import json
    import sys
    from pathlib import Path

    unrelated = subprocess.Popen(['sleep', '120'])
    try:
        (tmp_path / 'processes.json').write_text(json.dumps({
            'pid': unrelated.pid, 'start_time': 'stale-identity', 'socket': 'acc-test-missing-' + uuid.uuid4().hex[:8]}))
        subprocess.run([sys.executable, str(Path(__file__).with_name('owned_tmux.py')), str(tmp_path)],
                       check=True, timeout=15)
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)


@pytest.mark.parametrize("worker_exit", [False, True])
def test_session_teardown_detects_fake_agents_without_the_env_fixture(tmp_path, worker_exit):
    import json
    import sys
    from pathlib import Path

    from owned_tmux import alive, pin_process, signal_process

    root = Path(__file__).resolve().parents[1]
    identity = tmp_path / "agent.json"
    outside = tmp_path / "fake-codex"
    outside.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(120)\n")
    outside.chmod(0o700)
    unrelated = subprocess.Popen([str(outside)], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    case = tmp_path / "test_leak.py"
    case.write_text('''
import json, os, subprocess
from pathlib import Path
from owned_tmux import start_time

def test_leak(tmp_path):
    agent = tmp_path / "bin/fake-codex"
    agent.parent.mkdir()
    agent.write_text("#!/usr/bin/env python3\\nimport time\\ntime.sleep(120)\\n")
    agent.chmod(0o700)
    child = subprocess.Popen([str(agent)], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path(IDENTITY).write_text(json.dumps({"pid": child.pid, "start_time": start_time(child.pid)}))
    if WORKER_EXIT:
        os._exit(7)
'''.replace("IDENTITY", repr(str(identity))).replace("WORKER_EXIT", repr(worker_exit)))
    handle = None
    try:
        # The checkout-root conftest is only the host guard, not the leak-check plugin.
        # Resolve -p conftest from tests via PYTHONPATH, not from the child's cwd.
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "host_isolation", "-p", "conftest",
             "--basetemp", str(tmp_path / "nested"),
             *(["-n", "1", "--max-worker-restart=0"] if worker_exit else []), str(case)],
            cwd=tmp_path, env=os.environ | {"PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "tests"), *sys.path])},
            capture_output=True, text=True, timeout=60)
        assert identity.exists(), result.stdout + result.stderr
        info = json.loads(identity.read_text())
        handle = pin_process(info["pid"], info["start_time"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert "fake CLI" in result.stdout + result.stderr
        assert handle is None or not alive(handle), "the leak check reported but did not stop its own agent"
        assert unrelated.poll() is None, "the session check signalled a fake agent outside its own base"
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)
        if identity.exists() and handle is None:
            info = json.loads(identity.read_text())
            handle = pin_process(info["pid"], info["start_time"])
        if handle is not None:
            try:
                signal_process(handle, signal.SIGKILL)
            finally:
                os.close(handle)
