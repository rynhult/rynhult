"""Isolated root updater host acceptance and rollback regression checks."""
import os
from types import SimpleNamespace

import pytest

from acc.update_helper import NodeTransaction, Transaction


@pytest.mark.parametrize('mutation', ['remove', 'replace'])
@pytest.mark.parametrize('manager', ['dnf', 'zypper', 'pacman'])
@pytest.mark.parametrize('cls', [Transaction, NodeTransaction])
def test_recorded_manager_used_for_install_and_rollback(tmp_path, manager, cls, mutation, monkeypatch):
    record = tmp_path / 'acceptance'
    record.write_text(manager + '\n')
    record.chmod(0o644)
    commands = []

    def run(argv, **kwargs):
        if argv[1:2] == ['-c']:
            return SimpleNamespace(returncode=0, stdout=manager.encode())
        commands.append(argv)
        if len(commands) == 1:
            record.unlink()
            if mutation == 'replace':
                record.write_text('not-the-accepted-manager')
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    class Local(cls):
        def backup(self):
            self.saved = tmp_path / 'saved'
            self.saved.mkdir()
            (self.saved / 'acc.db').write_bytes(b'before')

        def download(self):
            self.stage.mkdir()
            tree = self.stage / 'new-tree'
            tree.mkdir()
            (tree / 'new').touch()
            return tree

        def health(self):
            pytest.fail('health after failed installation')

    job = Local(tmp_path / 'updates', tmp_path / 'app', tmp_path / 'data', '99.0.0',
                {'distro_acceptance': str(record)}, run=run)
    job.source.mkdir()
    (job.source / 'old').touch()
    job.data.mkdir()
    (job.data / 'acc.db').write_bytes(b'before')
    # Emulate root ownership of this one fixture inode on unprivileged CI.
    real_fstat = os.fstat
    inode = record.stat().st_ino
    def fstat(fd):
        info = real_fstat(fd)
        if info.st_ino == inode:
            fields = list(info)
            fields[4] = 0
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(os, 'fstat', fstat)
    real_open = os.open
    reads = []
    def opened(path, *args, **kwargs):
        if str(path) == str(record):
            reads.append(path)
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', opened)
    result = job.apply()
    assert len(reads) == 1
    assert not result['ok'] and result['rolled_back'], result
    assert (job.source / 'old').exists()
    assert not (job.source / 'new').exists()
    installs = [cmd for cmd in commands if cmd[0] == 'bash']
    assert len(installs) == 2
    assert all(cmd[-1] == '--unqualified-distro' for cmd in installs)


@pytest.mark.parametrize('mode', ['missing', 'writable', 'symlink', 'changed', 'invalid'])
def test_acceptance_cannot_authorize_another_host(tmp_path, mode):
    record = tmp_path / 'record'
    if mode != 'missing':
        record.write_text('invalid' if mode == 'invalid' else 'dnf')
        record.chmod(0o666 if mode == 'writable' else 0o644)
    if mode == 'symlink':
        link = tmp_path / 'link'
        link.symlink_to(record)
        record = link
    job = Transaction(tmp_path, tmp_path / 'app', tmp_path / 'data', '99.0.0',
                      {'distro_acceptance': str(record)},
                      run=lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b'zypper'))
    if mode == 'missing':
        assert job.distro_flags() == []
    else:
        with pytest.raises((ValueError, OSError)):
            job.distro_flags()


def test_preflight_refuses_before_backup_or_download(tmp_path):
    source = tmp_path / 'app'
    source.mkdir()
    job = Transaction(tmp_path / 'updates', source, tmp_path / 'data', '99.0.0',
                      {'distro_acceptance': str(tmp_path / 'missing')},
                      run=lambda *a, **kw: SimpleNamespace(returncode=1))
    job.backup = lambda: pytest.fail('backup before host acceptance')
    job.download = lambda: pytest.fail('download before host acceptance')
    result = job.apply()
    assert not result['ok'] and 'Update refused' in result['detail']
    assert not result['steps']


@pytest.mark.parametrize('installer', ['setup.sh', 'node/install.sh'])
@pytest.mark.parametrize('manager', ['apt-get', 'dnf', 'zypper', 'pacman', None])
@pytest.mark.parametrize('accepted', [False, True])
def test_installer_check_with_fake_managers(tmp_path, installer, manager, accepted):
    import shutil
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    deploy = tmp_path / 'deploy'
    (deploy / 'lib').mkdir(parents=True)
    (deploy / 'node').mkdir()
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name in ['dirname', 'id', 'uname', 'bash', 'env', 'sed']:
        (bindir / name).symlink_to(shutil.which(name))
    for name in ['systemctl', *([manager] if manager else [])]:
        tool = bindir / name
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o755)
    for name in ['lib/acc-env.sh', 'lib/pkg.sh', 'setup.sh', 'node/install.sh']:
        script = (repo / 'deploy' / name).read_text()
        script = script.replace('/usr/sbin:/usr/bin:/sbin:/bin', str(bindir))
        script = script.replace('/run/systemd/system', str(tmp_path))
        (deploy / name).write_text(script)
    args = ['bash', str(deploy / installer), '--check']
    if accepted:
        args.append('--unqualified-distro')
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) == (manager == 'apt-get' or (manager is not None and accepted)), result.stderr
    assert not (tmp_path / 'app').exists()


@pytest.mark.parametrize('manager,accepted,ok', [
    ('apt-get', False, True), ('dnf', False, False), ('dnf', True, True),
    ('zypper', True, True), ('pacman', True, True), (None, True, False),
])
def test_updater_preflight_with_fake_managers(tmp_path, monkeypatch, manager, accepted, ok):
    import shutil
    import subprocess
    from pathlib import Path

    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name in ['uname']:
        (bindir / name).symlink_to(shutil.which(name))
    for name in ['systemctl', *([manager] if manager else [])]:
        tool = bindir / name
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o755)

    def run(argv, **kwargs):
        argv[2] = argv[2].replace('/usr/sbin:/usr/bin:/sbin:/bin', str(bindir))
        argv[2] = argv[2].replace('/run/systemd/system', str(tmp_path))
        return subprocess.run(argv, **kwargs)

    repo = Path(__file__).resolve().parents[1]
    job = Transaction(tmp_path / 'updates', repo, tmp_path / 'data', '99.0.0', {}, run=run)
    monkeypatch.setattr(job, 'distro_flags', lambda: ['--unqualified-distro'] if accepted else [])
    if ok:
        job.preflight(repo)
    else:
        with pytest.raises(ValueError, match='Update refused'):
            job.preflight(repo)


def test_candidate_host_refusal_does_not_activate(tmp_path, monkeypatch):
    source = tmp_path / 'app'
    source.mkdir()
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    events = []
    job = Transaction(tmp_path / 'updates', source, tmp_path / 'data', '99.0.0', {},
                      run=lambda argv, **kw: SimpleNamespace(
                          returncode=int(str(candidate) in str(argv))))
    monkeypatch.setattr(job, 'distro_flags', lambda: [])
    job.backup = lambda: events.append('backup')
    job.download = lambda: candidate
    job.activate = lambda: pytest.fail('activation after candidate host refusal')
    result = job.apply()
    assert not result['ok'] and 'Update refused' in result['detail']
    assert events == ['backup']
    assert not job.activated and not job.install_started
    assert source.is_dir() and candidate.is_dir()


@pytest.mark.parametrize('phase', ['manager', 'host'])
def test_preflight_timeout_records_failure_before_hold(tmp_path, monkeypatch, phase):
    import json
    import subprocess

    from acc import update_helper

    source = tmp_path / 'app'
    source.mkdir()
    job = Transaction(tmp_path / 'updates', source, tmp_path / 'data', '99.0.0', {})
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('host-check', 30)
    monkeypatch.setattr(job, 'distro_flags', timeout if phase == 'manager' else lambda: [])
    monkeypatch.setattr(job, 'run', timeout)
    monkeypatch.setattr(update_helper, 'checkout_blocks_update', lambda *a, **kw: False)
    job.backup = lambda: pytest.fail('backup after timeout')
    result = job.apply()
    assert not result['ok'] and not result['running']
    assert json.loads((job.root / 'state.json').read_text()) == result
    assert not job.data.exists()


def test_git_checkout_refused_before_acceptance_or_shell(tmp_path, monkeypatch):
    from acc import update_helper
    from acc.updates import GIT_MESSAGE

    source = tmp_path / 'app'
    source.mkdir()
    (source / '.git').mkdir()
    job = Transaction(tmp_path / 'updates', source, tmp_path / 'data', '99.0.0', {})
    monkeypatch.setattr(update_helper, 'checkout_blocks_update', lambda *a, **kw: True)
    job.distro_flags = lambda: pytest.fail('record read before checkout refusal')
    job.run = lambda *a, **kw: pytest.fail('shell before checkout refusal')
    result = job.apply()
    assert not result['ok'] and result['detail'] == GIT_MESSAGE
    assert not job.data.exists()


def test_queued_timeout_writes_request_result(tmp_path, monkeypatch):
    import json
    import subprocess

    from acc import update_helper
    from acc.env import Environment

    root, source, data = tmp_path / 'updates', tmp_path / 'app', tmp_path / 'data'
    (root / 'requests').mkdir(parents=True)
    source.mkdir()
    (source / 'pyproject.toml').write_text('[project]\nversion="1.0.0"\n')
    request = root / 'requests' / ('a' * 32 + '.json')
    request.write_text(json.dumps({'action': 'update', 'version': '99.0.0'}))
    monkeypatch.setattr(update_helper, 'environment', Environment({
        'ACC_UPDATE_ROOT': str(root), 'ACC_UPDATE_SOURCE': str(source),
        'ACC_DATA': str(data), 'ACC_UPDATE_GROUP': '',
    }))
    def timeout(self):
        raise subprocess.TimeoutExpired('manager-check', 30)
    monkeypatch.setattr(Transaction, 'distro_flags', timeout)
    assert update_helper.main() == 0
    result = json.loads((root / 'results' / request.name).read_text())
    assert not result['ok'] and not result['running']
    assert 'timed out' in result['detail']
    assert not request.exists() and not data.exists()


def test_missing_acceptance_is_also_snapshotted(tmp_path):
    record = tmp_path / 'missing'
    job = Transaction(tmp_path / 'updates', tmp_path / 'app', tmp_path / 'data', '99.0.0',
                      {'distro_acceptance': str(record)})
    assert job.distro_flags() == []
    record.write_text('new record must not be read')
    assert job.distro_flags() == []
