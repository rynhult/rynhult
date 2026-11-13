"""Request lifetime, shutdown, and scoped leak detection for Git CGI children."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from owned_git_http import assert_no_git_backends, backend_processes
from test_auth import live  # noqa: F401

from acc.server.gitbackup import HttpBackend


@pytest.fixture
def backend_script(tmp_path):
    script = tmp_path / 'git-http-backend'
    script.write_text('''
import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['GIT_PROJECT_ROOT'])
if sys.argv[-1] == '--helper':
    (root / 'helper').write_text(str(os.getpid()))
    time.sleep(120)
    sys.exit()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, __file__, '--helper'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not (root / 'helper').exists():
    time.sleep(.01)
(root / 'pids').write_text(json.dumps([os.getpid(), child.pid]))
if sys.argv[-1] == '--success':
    print('Content-Type: text/plain\\n\\nok', flush=True)
else:
    time.sleep(120)
''')
    return script


async def ready(root):
    async with asyncio.timeout(5):
        while not (root / 'pids').exists():
            await asyncio.sleep(.01)
    return json.loads((root / 'pids').read_text())


@pytest.mark.parametrize('ending', ['success', 'timeout', 'oversize', 'disconnect', 'cancel', 'shutdown'])
async def test_request_reaps_entire_group(tmp_path, backend_script, ending):
    root = tmp_path / 'git'
    root.mkdir()
    backend = HttpBackend(root, 1000, 1, command=(sys.executable, str(backend_script),
                          '--success' if ending == 'success' else '--hold'))

    async def body():
        await ready(root)
        if ending == 'oversize':
            yield b'x' * 1001
        elif ending == 'disconnect':
            raise RuntimeError('client disconnected')
        elif ending != 'success':
            await asyncio.Event().wait()  # deadline must include a stalled request read

    request = asyncio.create_task(backend.run('POST', '/1.git/git-receive-pack', '', {}, body()))
    pids = await ready(root)
    if ending == 'cancel':
        request.cancel()
    elif ending == 'shutdown':
        await backend.close()
    if ending in ('cancel', 'shutdown'):
        with pytest.raises(asyncio.CancelledError):
            await request
    elif ending == 'disconnect':
        with pytest.raises(RuntimeError, match='client disconnected'):
            await request
    else:
        status, _, _ = await asyncio.wait_for(request, 5)
        assert status == {'success': 200, 'oversize': 413, 'timeout': 504}[ending]
    assert not backend_processes(root), pids
    await backend.close()
    assert (await backend.run('GET', '', '', {}, body()))[0] == 503


async def test_guard_fails_cleans_and_preserves_other_roots(tmp_path, backend_script):
    ours, other = tmp_path / 'ours', tmp_path / 'other'
    ours.mkdir()
    other.mkdir()
    processes = []
    try:
        for root in (ours, other):
            processes.append(subprocess.Popen([sys.executable, str(backend_script), '--hold'],
                env=os.environ | {'GIT_PROJECT_ROOT': str(root)}, start_new_session=True))
            await ready(root)
        assert len(backend_processes(ours)) == 2
        with pytest.raises(RuntimeError, match='git-http-backend leak at teardown'):
            assert_no_git_backends(ours)
        assert not backend_processes(ours)
        assert len(backend_processes(other)) == 2
    finally:
        for root in (ours, other):
            try:
                assert_no_git_backends(root)
            except RuntimeError:
                pass
        for process in processes:
            process.wait(timeout=5)


def test_global_guard_cli_reports_leak(tmp_path, backend_script):
    root = tmp_path / 'git'
    root.mkdir()
    process = subprocess.Popen([sys.executable, str(backend_script), '--hold'],
        env=os.environ | {'GIT_PROJECT_ROOT': str(root)}, start_new_session=True)
    try:
        import time
        deadline = time.monotonic() + 5
        while not (root / 'pids').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (root / 'pids').exists()
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('owned_git_http.py')), str(root)],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 1
        assert 'git-http-backend leak at teardown' in result.stderr
        assert not backend_processes(root)
        # A server-side guard may have cleaned a leak before global teardown.
        again = subprocess.run([sys.executable, str(Path(__file__).with_name('owned_git_http.py')), str(root)],
                               capture_output=True, text=True, timeout=10)
        assert again.returncode == 1
        assert 'git-http-backend leak at teardown' in again.stderr
    finally:
        try:
            assert_no_git_backends(root)
        except RuntimeError:
            pass
        process.wait(timeout=5)


async def test_disconnect_after_complete_upload_stops_backend(api, env, project, live, backend_script, monkeypatch):  # noqa: F811
    from urllib.parse import urlsplit

    from acc.server import gitbackup
    from acc.server.models import Project

    root = env['settings'].git_dir
    pid = project['id']
    with api.app.state.db() as db:
        owner = gitbackup.identity(db.get(Project, pid))
    await asyncio.to_thread(gitbackup.ensure_repo, root, pid, owner)
    original = HttpBackend._locked_run

    async def held(self, *args):
        self.command = (sys.executable, str(backend_script), '--hold')
        self.timeout = 5  # keep fixture teardown bounded even if disconnect handling regresses
        return await original(self, *args)

    monkeypatch.setattr(HttpBackend, '_locked_run', held)
    token = api.app.state.supervisor.backup_tokens.mint(pid)
    address = urlsplit(live)
    _, writer = await asyncio.open_connection(address.hostname, address.port)
    try:
        writer.write((f'POST /api/git/{pid}.git/git-receive-pack HTTP/1.1\r\n'
                      f'Host: {address.netloc}\r\nAuthorization: Bearer {token}\r\n'
                      'Content-Type: application/x-git-receive-pack-request\r\nContent-Length: 0\r\n\r\n').encode())
        await writer.drain()
        await ready(root)
        assert backend_processes(root)
        writer.close()
        await writer.wait_closed()
        async with asyncio.timeout(5):
            while backend_processes(root):
                await asyncio.sleep(.02)
    finally:
        writer.close()
        await writer.wait_closed()
        try:
            assert_no_git_backends(root)
        except RuntimeError:
            pass


async def test_router_shutdown_cancels_active_request(api, tmp_path, backend_script, monkeypatch):
    from acc.server.api import backups

    instances = []
    original = HttpBackend.__init__

    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(HttpBackend, '__init__', capture)
    router = backups.router(api.app.state.ctx)
    backend = instances[0]
    backend.git_dir = tmp_path
    backend.command = (sys.executable, str(backend_script), '--hold')

    async def body():
        yield b''

    async with router.lifespan_context(api.app):
        request = asyncio.create_task(backend.run('POST', '/1.git/git-receive-pack', '', {}, body()))
        await ready(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not backend_processes(tmp_path)


async def test_cancel_during_spawn_still_reaps_child(tmp_path, backend_script, monkeypatch):
    backend = HttpBackend(tmp_path, 1000, 5, command=(sys.executable, str(backend_script), '--hold'))
    spawned, release = asyncio.Event(), asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def delayed(*args, **kwargs):
        process = await original(*args, **kwargs)
        await ready(tmp_path)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed)

    async def body():
        yield b''

    request = asyncio.create_task(backend.run('POST', '/1.git/git-receive-pack', '', {}, body()))
    await asyncio.wait_for(spawned.wait(), 5)
    request.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, 5)
    assert not backend_processes(tmp_path)


def test_controller_guard_after_worker_crash(tmp_path, backend_script):
    base = tmp_path / 'nested'
    case = tmp_path / 'test_crash.py'
    case.write_text('''
import os, subprocess, sys, time

def test_crash(tmp_path_factory):
    root = tmp_path_factory.getbasetemp()
    subprocess.Popen([sys.executable, SCRIPT, '--hold'],
        env=os.environ | {'GIT_PROJECT_ROOT': str(root)}, start_new_session=True)
    deadline = time.monotonic() + 5
    while not (root / 'pids').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert (root / 'pids').exists()
    os._exit(23)
'''.replace('SCRIPT', repr(str(backend_script))))
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-n', '1', '--max-worker-restart=0',
        '-p', 'host_isolation', '-p', 'conftest', f'--basetemp={base}', str(case)], cwd=tmp_path,
        env=os.environ | {'PYTHONPATH': os.pathsep.join([str(root / 'tests'), str(root / 'src')])},
        capture_output=True, text=True, timeout=45)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'git-http-backend leak at teardown' in result.stdout + result.stderr
    assert not backend_processes(base)
