"""Synthetic publication and process-ownership proofs for review four."""
# ruff: noqa: F811
import json
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path

import pytest
from test_public_snapshot import export, git, source  # noqa: F401, F811


@pytest.mark.parametrize('name', ['.env', '.env.local', 'web/.ENV.production', '.acc/artifacts/private.txt',
                                  'secrets/token', 'Docs/Reviews/new/report.md', 'HOST/new/inventory'])
@pytest.mark.parametrize('channel', ['snapshot', 'checkout-bundle', 'installed-bundle'])
def test_sr4_01_private_paths_never_publish(source, name, channel):
    from acc.bundle import build
    repo, _, output = source
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('synthetic private content absent from the external denylist')
    git(repo, 'add', '-f', name)
    git(repo, '-c', 'user.name=Example', '-c', 'user.email=alex@example.com', 'commit', '-m', 'private path')
    if channel == 'snapshot':
        result = export(source)
        assert result.returncode == 0, result.stderr
        assert not (output / name).exists()
    else:
        if channel == 'installed-bundle':
            import shutil
            shutil.rmtree(repo / '.git')
        archive = build(repo, output.parent / 'bundles', version='test')
        with tarfile.open(archive) as tar:
            assert f'acc-test/{name}' not in tar.getnames()


def test_sr4_02_stale_manifest_never_kills_another_runs_tmux(tmp_path):
    from owned_tmux import cleanup, start_time
    socket = 'acc-test-sr4-' + uuid.uuid4().hex[:12]
    subprocess.run(['tmux', '-L', socket, 'new-session', '-d', 'sleep 120'], check=True)
    server = int(subprocess.check_output(['tmux', '-L', socket, 'display-message', '-p', '#{pid}']))
    # A stale manifest PID need not match the current socket server's owning run.
    (tmp_path / 'processes.json').write_text(json.dumps({
        'pid': __import__('os').getpid(), 'start_time': 'stale', 'socket': socket}))
    try:
        subprocess.run([sys.executable, str(Path(__file__).with_name('owned_tmux.py')), str(tmp_path)],
                       check=True, timeout=15)
        assert start_time(server) is not None
        assert subprocess.run(['tmux', '-L', socket, 'has-session'], capture_output=True).returncode == 0
    finally:
        cleanup(socket)


def test_sr4_02_watchdog_cannot_clean_a_socket_reused_by_another_owner():
    from owned_tmux import Owner, cleanup
    socket = 'acc-test-sr4-owner-' + uuid.uuid4().hex[:10]
    first = Owner()
    first.register(socket)
    subprocess.run(['tmux', '-L', socket, 'new-session', '-d', 'sleep 120'], check=True)
    cleanup(socket)  # first run's server has already ended
    second = Owner()
    second.register(socket)
    subprocess.run(['tmux', '-L', socket, 'new-session', '-d', 'sleep 120'], check=True)
    try:
        first.close()  # delayed watchdog from the earlier run
        assert subprocess.run(['tmux', '-L', socket, 'has-session'], capture_output=True).returncode == 0
    finally:
        second.close()
