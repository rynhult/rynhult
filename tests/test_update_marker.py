"""Updater/old-installer compatibility with real data preflight, no host commands."""
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from acc import maintenance
from acc.update_helper import Transaction

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('fail_health', [False, True])
def test_transaction_installs_and_rolls_back_through_old_data_preflight(tmp_path, fail_health):
    source, data, updates = (tmp_path / name for name in ('app', 'data', 'updates'))
    source.mkdir()
    data.mkdir(mode=0o700)
    service_uid = 65534 if os.geteuid() == 0 else os.getuid()
    service_gid = 65534 if os.geteuid() == 0 else os.getgid()
    if os.geteuid() == 0:
        os.chown(data, service_uid, service_gid)
    with sqlite3.connect(data / 'acc.db') as db:
        db.execute('create table marker(value)')
        db.execute('insert into marker values(1)')
    if os.geteuid() == 0:
        os.chown(data / 'acc.db', service_uid, service_gid)
    # Fake setup runs the real unchanged rc.1/main data preflight. The outside
    # marker assertion reproduces this on non-root runners too, where updater
    # and service fixtures necessarily share a uid.
    setup = f'''#!/bin/bash
set -eu
/usr/bin/python3 -I "{REPO}/deploy/lib/data-paths.py" check "{data}" {service_uid}
[ -f "{tmp_path}/.update-state/.update-in-progress" ]
printf '%s\\n' "$0" >> "{tmp_path}/installs"
'''
    (source / 'deploy').mkdir()
    (source / 'deploy/setup.sh').write_text(setup)
    (source / 'generation').write_text('old')

    class Local(Transaction):
        def backup(self):
            self.saved = tmp_path / 'saved'
            self.saved.mkdir()
            shutil.copyfile(data / 'acc.db', self.saved / 'acc.db')

        def download(self):
            self.stage.mkdir()
            tree = self.stage / 'new'
            shutil.copytree(source, tree)
            (tree / 'generation').write_text('new')
            return tree

        def health(self):
            if fail_health:
                raise ValueError('fixture unhealthy')

    def run(argv, **kwargs):
        if argv[0] == 'systemctl':
            return subprocess.CompletedProcess(argv, 0)
        assert argv[:1] == ['bash']
        return subprocess.run(argv, **kwargs)

    job = Local(updates, source, data, '0.15.0-rc.2', {}, run=run)
    result = job.apply()
    assert result['ok'] is (not fail_health), result
    if fail_health:
        assert result['rolled_back'], result
    assert (source / 'generation').read_text() == ('old' if fail_health else 'new')
    assert len((tmp_path / 'installs').read_text().splitlines()) == (2 if fail_health else 1)
    assert not (data / '.update-in-progress').exists()


def test_marker_excludes_concurrent_update_and_abandoned_marker_refuses(tmp_path):
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    with maintenance.update_hold(data):
        marker = maintenance.update_marker(data)
        assert marker.exists() and marker.parent != data
        assert marker.stat().st_mode & 0o777 == 0o600
        with pytest.raises(ValueError, match='unfinished'):
            with maintenance.update_hold(data):
                pytest.fail('concurrent update entered')
    marker.touch(mode=0o600)
    with pytest.raises(ValueError, match='unfinished'):
        with maintenance.update_hold(data):
            pytest.fail('abandoned update entered')
    assert marker.exists()


def test_legacy_abandoned_or_dangling_marker_is_never_cleared(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    marker = data / '.update-in-progress'
    marker.symlink_to(tmp_path / 'missing')
    with pytest.raises(ValueError, match='unfinished'):
        with maintenance.update_hold(data):
            pytest.fail('legacy marker ignored')
    assert marker.is_symlink()


@pytest.mark.parametrize('unsafe', ['link', 'writable'])
def test_unsafe_state_directory_refuses_without_following_links(tmp_path, unsafe):
    data = tmp_path / 'data'
    data.mkdir()
    state = maintenance.update_marker(data).parent
    other = tmp_path / 'unrelated'
    other.mkdir()
    if unsafe == 'link':
        state.symlink_to(other, target_is_directory=True)
    else:
        state.mkdir(mode=0o777)
        state.chmod(0o777)
    with pytest.raises((ValueError, OSError)):
        with maintenance.update_hold(data):
            pytest.fail('unsafe state accepted')
    assert not list(other.iterdir())


def test_service_owned_legacy_signal_and_leases_pass_real_preflight(tmp_path):
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    with maintenance.update_hold(data):
        assert maintenance.update_pending(data)
        for name in ('.capture-update.lock', '.maintenance.lock', '.update-in-progress'):
            info = (data / name).lstat()
            assert info.st_uid == data.stat().st_uid
            assert info.st_nlink == 1 and info.st_mode & 0o777 == 0o600
        result = subprocess.run(['/usr/bin/python3', '-I', str(REPO / 'deploy/lib/data-paths.py'),
                                 'check', str(data), str(data.stat().st_uid)],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert maintenance.update_marker(data).parent.stat().st_mode & 0o777 == 0o710
