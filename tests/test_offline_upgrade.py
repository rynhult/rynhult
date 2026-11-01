"""Signed local RC upgrades use the normal updater transaction, never release discovery."""
import datetime
import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from acc import bundle


@pytest.fixture
def release(tmp_path, monkeypatch):
    from acc import offline_upgrade as offline
    tree = tmp_path / 'candidate'
    tree.mkdir()
    (tree / 'pyproject.toml').write_text('[project]\nversion = "0.15.0-rc.2"\n')
    archive = bundle.build(tree, tmp_path / 'release')
    (archive.parent / 'SHA256SUMS').write_text(f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n')
    (archive.parent / 'SHA256SUMS.asc').write_bytes(b'test signature')
    monkeypatch.setattr(offline, 'verify', lambda sums, signature: {'status': 'verified'})
    source = tmp_path / 'app'
    source.mkdir()
    (source / 'pyproject.toml').write_text('[project]\nversion = "0.15.0-rc.1"\n')
    (source / '.acc-build').write_text('a' * 64)
    data = tmp_path / 'data'
    (data / 'secrets').mkdir(parents=True)
    (data / 'secrets/vault.key').write_bytes(b'private fixture')
    with sqlite3.connect(data / 'acc.db') as db:
        db.execute('create table marker(value)')
        db.execute('insert into marker values (1)')
    return offline, archive, source, data


def job_for(release, failure=None):
    offline, archive, source, data = release
    events = []
    def run(argv, *, env, **kwargs):
        events.append(argv)
        if argv[0] == 'fixture-backup':
            saved = Path(env['ACC_BACKUP_DIR']) / datetime.date.today().isoformat()
            saved.mkdir()
            shutil.copyfile(data / 'acc.db', saved / 'acc.db')
            shutil.copytree(data / 'secrets', saved / 'secrets')
        return type('Result', (), {'returncode': 0})()
    class Local(offline.OfflineTransaction):
        def install(self, path):
            if bundle.version_of(path).endswith('rc.2'):
                with sqlite3.connect(data / 'acc.db') as db:
                    db.execute('update marker set value=2')
                if failure == 'install':
                    raise ValueError('fixture install failed')
        def health(self):
            if failure == 'health' and self.target.endswith('rc.2'):
                raise ValueError('fixture health failed')
    return Local(source.parent / 'updates', source, data, '0.15.0-rc.2',
                 {'backup_command': 'fixture-backup'}, archive=archive, run=run), events


@pytest.mark.parametrize('failure', [None, 'install', 'health'])
def test_offline_rc_upgrade_and_automatic_rollback(release, failure):
    job, events = job_for(release, failure)
    result = job.apply()
    assert result['ok'] is (failure is None), result
    assert events[0][0] == 'fixture-backup'
    assert bundle.version_of(job.source) == ('0.15.0-rc.1' if failure else '0.15.0-rc.2')
    with sqlite3.connect(job.data / 'acc.db') as db:
        assert db.execute('select value from marker').fetchone()[0] == (1 if failure else 2)
    assert (job.data / 'secrets/vault.key').read_bytes() == b'private fixture'
    if failure:
        assert result['rolled_back'] is True
    else:
        assert (job.root / 'offline' / f'{job.id}.json').is_file()


@pytest.mark.parametrize('problem', ['signature', 'tampered', 'downgrade', 'postgres', 'git'])
def test_offline_refuses_before_source_swap(release, problem, monkeypatch):
    offline, archive, source, data = release
    job, _ = job_for(release)
    if problem == 'signature':
        def invalid(*args):
            raise ValueError('invalid signature')
        monkeypatch.setattr(offline, 'verify', invalid)
    elif problem == 'tampered':
        archive.write_bytes(archive.read_bytes() + b'tampered')
    elif problem == 'downgrade':
        (source / 'pyproject.toml').write_text('[project]\nversion = "0.15.0"\n')
    elif problem == 'postgres':
        monkeypatch.setenv('ACC_DATABASE_URL', 'postgresql://example.invalid/test')
    elif problem == 'git':
        (source / '.git').mkdir()
    result = job.apply()
    assert not result['ok']
    assert not job.activated and not job.install_started
    assert (source / '.acc-build').read_text() == 'a' * 64


def test_explicit_rollback_reuses_retained_source_and_database(release):
    offline, _, source, data = release
    job, _ = job_for(release)
    assert job.apply()['ok']
    result = offline.rollback(job.root, source, data, job.id, {'backup_command': 'fixture-backup'}, transaction_class=type(job), run=job.run)
    assert result['rolled_back'], result
    assert bundle.version_of(source) == '0.15.0-rc.1'
    with sqlite3.connect(data / 'acc.db') as db:
        assert db.execute('select value from marker').fetchone()[0] == 1
    with pytest.raises(ValueError):
        offline.rollback(job.root, source, data, job.id, {'backup_command': 'fixture-backup'}, transaction_class=type(job), run=job.run)


def test_real_signature_authenticates_offline_rc(release, release_signer, monkeypatch):
    from acc.release_signatures import verify
    offline, archive, _, _ = release
    sums = (archive.parent / 'SHA256SUMS').read_bytes()
    (archive.parent / 'SHA256SUMS.asc').write_bytes(release_signer.sign(sums))
    monkeypatch.setattr(offline, 'verify', verify)
    job, _ = job_for(release)
    assert job.apply()['ok']
    assert job.state['signature']['primary'] == release_signer.fingerprint


def test_checksum_uses_exact_bytes_whose_signature_was_verified(release, monkeypatch):
    offline, archive, _, _ = release
    original_sums = (archive.parent / 'SHA256SUMS').read_bytes()
    def verify_and_replace(sums, signature):
        assert sums == original_sums
        (archive.parent / 'SHA256SUMS').write_text(f'{"0" * 64}  {archive.name}\n')
        return {'status': 'verified'}
    monkeypatch.setattr(offline, 'verify', verify_and_replace)
    job, _ = job_for(release)
    assert job.apply()['ok']


def test_unsafe_archive_stops_before_activation(release):
    import tarfile
    _, archive, _, _ = release
    with tarfile.open(archive, 'w:gz') as tar:
        link = tarfile.TarInfo('acc-0.15.0-rc.2/escape')
        link.type, link.linkname = tarfile.SYMTYPE, '/outside'
        tar.addfile(link)
    (archive.parent / 'SHA256SUMS').write_text(f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n')
    job, _ = job_for(release)
    result = job.apply()
    assert not result['ok'] and not job.activated


def test_rollback_backup_failure_preserves_current_install(release):
    offline, _, source, data = release
    job, _ = job_for(release)
    assert job.apply()['ok']
    def failed_backup(*args, **kwargs):
        return type('Result', (), {'returncode': 1})()
    with pytest.raises(ValueError, match='failed'):
        offline.rollback(job.root, source, data, job.id, {'backup_command': 'fixture-backup'},
                         transaction_class=type(job), run=failed_backup)
    assert bundle.version_of(source) == '0.15.0-rc.2'
    with sqlite3.connect(data / 'acc.db') as db:
        assert db.execute('select value from marker').fetchone()[0] == 2


def test_rollback_refuses_another_current_build(release):
    offline, _, source, data = release
    job, _ = job_for(release)
    assert job.apply()['ok']
    (source / '.acc-build').write_text('b' * 64)
    with pytest.raises(ValueError, match='match'):
        offline.rollback(job.root, source, data, job.id, {}, transaction_class=type(job),
                         run=lambda *a, **k: pytest.fail('must refuse before backup'))


def test_manual_cli_requires_explicit_confirmation_without_touching_host(monkeypatch):
    from acc import offline_upgrade
    monkeypatch.setattr(offline_upgrade.os, 'geteuid', lambda: 1)
    with pytest.raises(SystemExit) as error:
        offline_upgrade.main(['--bundle', '/unused/acc-0.15.0-rc.2.tar.gz', '--version', '0.15.0-rc.2'])
    assert error.value.code == 2


def test_vm_rehearsal_embedded_python_compiles():
    import ast
    import re
    script = (Path(__file__).resolve().parents[1] / 'deploy/test/upgrade-rc1.sh').read_text()
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY", script, re.S)
    assert len(blocks) == 4
    for block in blocks:
        ast.parse(block)
    assert 'set -euo pipefail' in script
    assert '--offline' in script and '--rollback' in script
    assert 'verify(sums,' in script


def test_installed_backend_and_backup_encryption_are_used(tmp_path, monkeypatch):
    from acc import offline_upgrade
    from acc.env import Environment
    # configure_environment dual-writes ACC_/RYNHULT_. Keep all mutations in an
    # isolated mapping, so later Settings tests cannot inherit a preferred alias.
    environment = Environment({'ACC_DATABASE_URL': 'ignored', 'ACC_BACKUP_PASSPHRASE_FILE': 'ignored'})
    monkeypatch.setattr(offline_upgrade, 'environment', environment)
    settings, backup = tmp_path / 'accd', tmp_path / 'backup'
    settings.write_text('ACC_DATABASE_URL=sqlite:///stale\nRYNHULT_DATABASE_URL="postgresql://example.invalid/test"\n')
    backup.write_text('ACC_BACKUP_PASSPHRASE_FILE="/private/fixture-passphrase"\n')
    offline_upgrade.configure_environment(settings, backup, tmp_path / 'data')
    assert environment['ACC_DATABASE_URL'] == 'postgresql://example.invalid/test'
    assert environment['ACC_BACKUP_PASSPHRASE_FILE'] == '/private/fixture-passphrase'
    assert environment['RYNHULT_DATABASE_URL'] == environment['ACC_DATABASE_URL']
    assert environment['RYNHULT_BACKUP_PASSPHRASE_FILE'] == environment['ACC_BACKUP_PASSPHRASE_FILE']


def test_custom_data_directory_is_refused_before_backup(tmp_path):
    from acc import offline_upgrade
    settings = tmp_path / 'accd'
    settings.write_text('RYNHULT_DATA_DIR=/different/data\n')
    with pytest.raises(ValueError, match='standard data directory'):
        offline_upgrade.configure_environment(settings, tmp_path / 'missing', tmp_path / 'data')
