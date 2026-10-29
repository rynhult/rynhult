"""Busy-server capture and read-only deployment probes; temporary data only."""
import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from test_backup_server import source  # noqa: F401

from acc import backup_server as backup
from acc.maintenance import lease


def test_readback_compares_the_captured_catalog_not_uncaptured_builtin_bytes(source, tmp_path, monkeypatch):  # noqa: F811
    stage = source.capture()
    installed = tmp_path / 'installed-catalog'
    shutil.copytree(source.settings.catalog_dir, installed)
    # Same valid items/profiles, different built-in bytes outside the backup's coverage.
    with (installed / 'manifest.toml').open('a') as out:
        out.write('\n# refreshed installed catalog\n')
    original = backup.validate_catalog
    monkeypatch.setattr(backup, 'validate_catalog', lambda stage, catalog_root=None:
                        original(stage, catalog_root or installed))
    assert backup.validate_catalog(stage) != json.loads((stage / 'manifest.json').read_bytes())['manifest']['catalog_revision']
    backup.validate_and_install(stage, source.kit(), stage.name, tmp_path / 'restored')
    assert (tmp_path / 'restored/data/RESTORE-HOLD').exists()


def test_dry_run_ignores_scheduler_lease_without_taking_any_lock(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('aliases_busy', root / 'deploy/lib/public-aliases.py')
    aliases = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aliases)
    with lease(tmp_path / 'srv/acc/data/backup-service/.worker'):
        def forbidden(*args, **kwargs):
            raise AssertionError('dry-run acquired a lease')
        monkeypatch.setattr(aliases.fcntl, 'flock', forbidden)
        monkeypatch.setattr(aliases.fcntl, 'lockf', forbidden)
        assert aliases.capture_running(tmp_path) is False


def test_overlay_revision_still_rejects_changed_captured_bytes(source, tmp_path):  # noqa: F811
    stage = source.capture()
    # Even with a trusted re-signed inventory, an inconsistent overlay revision is refused.
    (stage / 'catalog/local.json').write_text('{"profiles": {}}')
    manifest = json.loads((stage / 'manifest.json').read_bytes())['manifest']
    manifest['files'] = backup.inventory(stage)
    source.sign(stage, manifest)
    with pytest.raises(backup.Refused, match='saved catalog doesn.t match its signed list'):
        backup.validate_and_install(stage, source.kit(), stage.name, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_legacy_manifest_retains_its_catalog_check(source, tmp_path):  # noqa: F811
    stage = source.capture()
    manifest = json.loads((stage / 'manifest.json').read_bytes())['manifest']
    manifest.pop('catalog_overlay_revision', None)
    manifest['catalog_revision'] = 'wrong'
    source.sign(stage, manifest)
    with pytest.raises(backup.Refused, match='catalog doesn.t match its signed list'):
        backup.validate_and_install(stage, source.kit(), stage.name, tmp_path / 'restored')


def test_current_manifest_still_rejects_missing_vault_references(source, tmp_path):  # noqa: F811
    stage = source.capture()
    (stage / 'catalog/local.json').write_text(json.dumps({'mcps': [
        {'id': 'fixture', 'command': 'fixture', 'env': {'TOKEN': 'vault:' + 'a' * 16}}]}))
    manifest = json.loads((stage / 'manifest.json').read_bytes())['manifest']
    manifest['files'] = backup.inventory(stage)
    manifest['catalog_overlay_revision'] = backup.catalog_overlay_revision(manifest['files'])
    source.sign(stage, manifest)
    with pytest.raises(backup.Refused, match='vault key that no longer exists'):
        backup.validate_and_install(stage, source.kit(), stage.name, tmp_path / 'restored')


def test_worker_only_dry_run_is_not_a_capture_refusal(tmp_path):
    import subprocess
    root = Path(__file__).resolve().parents[1]
    (tmp_path / 'etc/acc').mkdir(parents=True)
    with lease(tmp_path / 'srv/acc/data/backup-service/.worker'):
        result = subprocess.run(['bash', str(root / 'deploy/install.sh'), '--dry-run', '--alias-root', str(tmp_path)],
                                capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_matching_manifest_still_restores(source, tmp_path):  # noqa: F811
    stage = source.capture()
    manifest = json.loads((stage / 'manifest.json').read_bytes())['manifest']
    manifest.pop('catalog_overlay_revision')
    source.sign(stage, manifest)
    backup.validate_and_install(stage, source.kit(), stage.name, tmp_path / 'restored')
    assert (tmp_path / 'restored/data/RESTORE-HOLD').exists()


@pytest.mark.parametrize('change', ['strip', 'forge', 'catalog_bytes', 'old_manifest_new_payload'])
def test_unsigned_downgrade_or_tampering_is_refused(source, tmp_path, change):  # noqa: F811
    stage = source.capture()
    doc = json.loads((stage / 'manifest.json').read_bytes())
    if change == 'strip':  # make a current manifest look legacy without re-signing it
        doc['manifest'].pop('catalog_overlay_revision')
    elif change == 'forge':
        doc['manifest']['catalog_overlay_revision'] = '0' * 64
    elif change == 'catalog_bytes':
        (stage / 'catalog/local.json').write_text('{"profiles": {}}')
    if change == 'old_manifest_new_payload':
        newer = source.capture()
        shutil.copy(stage / 'manifest.json', newer / 'manifest.json')
        stage = newer
    else:
        (stage / 'manifest.json').write_text(json.dumps(doc))
    with pytest.raises(backup.Refused):
        backup.validate_and_install(stage, source.kit(), doc['manifest']['backup_id'], tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_capture_restores_while_a_session_edit_and_git_push_finish(source, tmp_path, monkeypatch):  # noqa: F811
    """DB and git have separate capture instants: ordinary writes between them must still restore cleanly."""
    import concurrent.futures
    import sqlite3
    import subprocess
    import threading

    from helpers import write_db

    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.gitbackup import git_lock
    from acc.server.models import LaunchSpec, Project, Session

    sessions = make_sessionmaker(make_engine(source.settings.db_url))
    with write_db(sessions) as db:
        project = Project(slug='busy-demo', name='Busy demo', workspace_path='/example/demo', account='agent',
                          backup_identity='busy-demo')
        db.add(Session(id='busy-session', name='Before capture', tmux_name='acc-busy', provider='claude',
                       project=project, run_as='agent', desired_state='running', observed_state='idle',
                       launch_spec=LaunchSpec(provider='claude', inputs={}, files={}, digest='example')))
        db.commit()
        pid = project.id
    bare = source.settings.git_dir / f'{pid}.git'
    work = tmp_path / 'pusher'

    def git(*args):
        return subprocess.run(['git', '-c', 'user.name=Alex Example', '-c', 'user.email=alex@example.com', *map(str, args)],
                              check=True, capture_output=True, text=True, timeout=15).stdout.strip()

    git('init', '--bare', bare)
    git('--git-dir', bare, 'config', 'acc.project', 'busy-demo')
    git('init', '-b', 'main', work)
    (work / 'README.md').write_text('First version\n')
    git('-C', work, 'add', '.')
    git('-C', work, 'commit', '-m', 'First version')
    git('-C', work, 'push', bare, 'main')
    copied_catalog = threading.Event()
    copy = backup.copy_tree

    def copying(src, *args, **kwargs):
        result = copy(src, *args, **kwargs)
        if src == source.settings.catalog_local_dir:
            copied_catalog.set()
        return result

    monkeypatch.setattr(backup, 'copy_tree', copying)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        # Simulate receive-pack owning its real serialization lease, while capture snapshots the session database.
        with lease(git_lock(source.settings.git_dir)):
            capturing = pool.submit(source.capture)
            assert copied_catalog.wait(10), 'capture did not reach the git-copy boundary'
            with write_db(sessions) as db:
                db.get(Session, 'busy-session').name = 'Edited during capture'
                db.commit()
            (work / 'README.md').write_text('Second version\n')
            git('-C', work, 'commit', '-am', 'Second version')
            git('-C', work, 'push', bare, 'main')
            pushed = git('-C', work, 'rev-parse', 'HEAD')
        stage = capturing.result(timeout=30)
    restored = tmp_path / 'restored'
    backup.validate_and_install(stage, source.kit(), stage.name, restored)
    with sqlite3.connect(restored / 'data/acc.db') as db:
        assert db.execute("select name from session where id='busy-session'").fetchone() == ('Before capture',)
        assert db.execute('pragma integrity_check').fetchone() == ('ok',)
    assert git('--git-dir', restored / f'git/{pid}.git', 'rev-parse', 'refs/heads/main') == pushed
    assert (restored / 'data/RESTORE-HOLD').exists()
