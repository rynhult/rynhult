"""Phase-one recovery contracts: failures must never produce a usable restore."""
import base64
import errno
import json
import os
import sqlite3
from pathlib import Path

import pytest
from isolated_settings import isolated_settings

from acc import backup_server as backup
from acc.server.db import migrate
from acc.server.vault import Cipher


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'main'
    settings = isolated_settings(root)
    settings.data_dir.mkdir(parents=True)
    migrate(settings.db_url)
    cipher = Cipher(settings.vault_key_path)
    cipher.key(create=True)
    settings.catalog_local_dir.mkdir()
    (settings.catalog_local_dir / 'local.json').write_text('{}')
    service = backup.Service(settings)
    service.initialize()
    return service


def test_capture_pairs_db_key_and_inventory(source, tmp_path):
    stage = source.capture()
    kit = source.kit()
    restored = tmp_path / 'replacement'
    backup.validate_and_install(stage, kit, stage.name, restored)
    assert (restored / 'data/acc.db').is_file()
    assert (restored / 'data/secrets/vault.key').read_bytes() == source.settings.vault_key_path.read_bytes()
    assert (restored / 'data/RESTORE-HOLD').is_file()
    assert not (restored / 'run').exists()
    with sqlite3.connect(restored / 'data/acc.db') as db:
        assert db.execute('pragma integrity_check').fetchone() == ('ok',)


def test_changed_key_or_db_refused(source, tmp_path):
    stage = source.capture()
    (stage / 'secrets/vault.key').write_text(base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(backup.Refused):
        backup.validate_and_install(stage, source.kit(), stage.name, tmp_path / 'replacement')
    assert not (tmp_path / 'replacement').exists()


@pytest.mark.parametrize('change', ['signature', 'replay', 'kit', 'missing'])
def test_corrupt_replayed_or_untrusted_manifest_refused(source, tmp_path, change):
    stage = source.capture()
    kit = source.kit()
    expected = stage.name
    if change == 'signature':
        doc = json.loads((stage / 'manifest.json').read_text())
        doc['manifest']['installation_id'] = 'attacker'
        (stage / 'manifest.json').write_text(json.dumps(doc))
    elif change == 'replay':
        expected = 'f' * 32
    elif change == 'kit':
        kit['public_key'] = base64.b64encode(os.urandom(32)).decode()
    else:
        kit = {}
    with pytest.raises(backup.Refused):
        backup.validate_and_install(stage, kit, expected, tmp_path / 'replacement')
    assert not (tmp_path / 'replacement').exists()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'secrets/../../escape', 'git/a/../b', 'config/run.env'])
def test_signed_hostile_path_refused(source, tmp_path, name):
    stage = source.capture()
    doc = json.loads((stage / 'manifest.json').read_text())['manifest']
    doc['files'][name] = {'size': 0, 'sha256': '0' * 64}
    source.sign(stage, doc)
    with pytest.raises(backup.Refused):
        backup.validate_and_install(stage, source.kit(), stage.name, tmp_path / 'replacement')


def test_capture_disk_full_never_publishes(source, monkeypatch):
    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, 'No space left on device')
    monkeypatch.setattr(backup, 'copy_regular', full)
    with pytest.raises(OSError):
        source.capture()
    assert not list(source.staging.glob('*/manifest.json'))


def test_capture_excludes_links_and_runtime(source):
    (source.settings.data_dir / 'remote-spool').mkdir()
    (source.settings.data_dir / 'remote-spool/secret').write_text('runtime')
    (source.settings.vault_key_path.parent / 'link').symlink_to('/etc/passwd')
    stage = source.capture()
    assert not (stage / 'secrets/link').exists()
    manifest = json.loads((stage / 'manifest.json').read_text())['manifest']
    assert 'secrets/link' in manifest['exclusions']
    assert 'remote-spool' in manifest['coverage_exclusions']


def test_signed_wrong_epoch_cannot_decrypt_database(source, tmp_path):
    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.models import VaultKey
    sessions = make_sessionmaker(make_engine(source.settings.db_url))
    cipher = Cipher(source.settings.vault_key_path)
    with sessions() as db:
        db.add(VaultKey(id='a' * 16, name='Canary', variable='CANARY', owner='owner',
                        ciphertext=cipher.seal('a' * 16, 'CANARY', 'never-log-this-canary')))
        db.commit()
    stage = source.capture()
    (stage / 'secrets/vault.key').write_text(base64.b64encode(os.urandom(32)).decode())
    m = json.loads((stage / 'manifest.json').read_text())['manifest']
    m['vault_key_epoch'] = backup.digest(stage / 'secrets/vault.key')
    m['files'] = backup.inventory(stage)
    source.sign(stage, m)
    with pytest.raises(backup.Refused, match='pairing'):
        backup.validate_and_install(stage, source.kit(), stage.name, tmp_path / 'replacement')


def test_database_transaction_waits_for_capture_barrier(source):
    import concurrent.futures

    from acc.maintenance import lease
    from acc.server.db import make_engine
    engine = make_engine(source.settings.db_url)
    def write():
        with engine.begin() as db:
            db.exec_driver_sql("create table barrier_test (id int)")
        return True
    with concurrent.futures.ThreadPoolExecutor() as pool:
        with lease(source.settings.data_dir / '.maintenance.lock'):
            task = pool.submit(write)
            import time
            time.sleep(.1)
            assert not task.done()
        assert task.result(timeout=5)
    engine.dispose()


async def test_replacement_starts_only_quarantine_app(source, tmp_path, monkeypatch):
    import dataclasses

    import httpx

    from acc.server.app import create_app
    stage = source.capture()
    dest = tmp_path / 'replacement'
    backup.validate_and_install(stage, source.kit(), stage.name, dest)
    def forbidden(*args, **kwargs):
        raise AssertionError('quarantine attempted normal startup')
    monkeypatch.setattr('acc.server.app.migrate', forbidden)
    settings = dataclasses.replace(source.settings, data_dir=dest / 'data', git_dir=dest / 'git',
                                   catalog_local_dir=dest / 'catalog-local')
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated') as client:
            result = await client.get('/api/health')
            assert result.status_code == 503
            assert result.json()['quarantine'] is True
            assert (await client.post('/api/sessions')).status_code == 503
            assert (await client.get('/api/nodes')).status_code == 503


@pytest.fixture(scope='session')
def restic_binary(tmp_path_factory):
    """Pinned official release in pytest scratch only, with embedded upstream SHA-256."""
    import importlib.machinery
    import importlib.util
    helper = Path(__file__).resolve().parents[1] / 'deploy/packages/acc-packages'
    loader = importlib.machinery.SourceFileLoader('backup_packages', str(helper))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader('backup_packages', loader))
    loader.exec_module(module)
    target = tmp_path_factory.mktemp('restic')
    spec = {**module.CATALOG['restic'], 'dest': str(target / 'restic'), 'root': str(target / 'versions')}
    try:
        module.install_restic(spec)
    except OSError as error:
        pytest.skip(f'offline: pinned restic download unavailable ({type(error).__name__})')
    return target / 'restic'


def test_real_restic_offline_recovery_and_wrong_password(source, restic_binary, tmp_path):
    target = source.add_local_target(tmp_path / 'encrypted', binary=restic_binary)
    result = source.back_up(target['id'], binary=restic_binary)
    assert result['verified_at'] >= result['captured_at']
    kit = source.kit()
    assert kit['targets'][0]['repository_id']
    restored = tmp_path / 'replacement'
    backup.restore(kit, target['id'], result['backup_id'], restored, binary=restic_binary)
    assert (restored / 'data/RESTORE-HOLD').is_file()
    assert (restored / 'data/secrets/vault.key').read_bytes() == source.settings.vault_key_path.read_bytes()
    # Entire payload (including DB and manifest) is ciphertext on the target.
    for path in (tmp_path / 'encrypted').rglob('*'):
        if path.is_file():
            assert source.settings.vault_key_path.read_bytes().strip() not in path.read_bytes()
    kit['targets'][0]['password'] = 'wrong'
    with pytest.raises(backup.Refused):
        backup.restore(kit, target['id'], result['backup_id'], tmp_path / 'wrong', binary=restic_binary)
    assert not (tmp_path / 'wrong').exists()


def test_archive_rejects_links_traversal_and_oversize(tmp_path):
    import io
    import tarfile
    for name, kind in [('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                       ('secrets/link', tarfile.SYMTYPE), ('secrets/device', tarfile.CHRTYPE)]:
        archive = tmp_path / 'payload.tar'
        with tarfile.open(archive, 'w') as out:
            item = tarfile.TarInfo(name)
            item.type = kind
            out.addfile(item, io.BytesIO())
        with pytest.raises(backup.Refused):
            backup.unpack(archive, tmp_path / 'unpacked')
        assert not (tmp_path / 'escape').exists()


async def test_owner_routes_export_kit_audit_and_members_are_excluded(api, env, member_accounts):
    from test_auth import cookie_client, do_setup
    from test_users import browser, csrf, join

    from acc.server.models import AuditLog
    await do_setup(api, env)
    owner = cookie_client(api)
    response = await owner.get('/api/host/backups')
    assert response.status_code == 200
    assert response.json()['protected'] is False
    assert 'password' not in response.text
    assert (await owner.post('/api/host/backups/kit', json={'confirmed': False}, headers=csrf(owner))).status_code == 422
    kit = await owner.post('/api/host/backups/kit', json={'confirmed': True}, headers=csrf(owner))
    assert kit.status_code == 200
    assert kit.headers['cache-control'] == 'no-store'
    assert 'attachment' in kit.headers['content-disposition']
    with api.app.state.db() as db:
        audit = db.query(AuditLog).filter_by(action='backup.kit_export').one()
        assert kit.json()['public_key'] not in json.dumps(audit.detail)
    link = (await owner.post('/api/users/invites', json={'account': 'dana-agent'}, headers=csrf(owner))).json()['link']
    async with browser(api) as member:
        await join(member, link, 'alex')
        for method, path, body in [('GET', '', None), ('POST', '/kit', {'confirmed': True}),
                                  ('POST', '/unknown/run', {}), ('POST', '/unknown/prune', {}),
                                  ('POST', '/unknown/enable', {'confirmed': True}), ('POST', '/targets', {'node_id': 'a' * 32}),
                                  ('GET', '/unknown/setup', None), ('POST', '/unknown/check', {}),
                                  ('GET', '/unknown/snapshots', None), ('POST', '/unknown/verify', {'confirmed': True, 'backup_id': 'b' * 32}), ('POST', '/drill', {})]:
            result = await member.request(method, '/api/host/backups' + path, json=body, headers=csrf(member))
            assert result.status_code == 403, result.text
        assert not any(i['kind'] == 'backup' for i in (await member.get('/api/attention')).json())


async def test_jobs_are_durable_deduplicated_and_interrupted_jobs_fail_closed(api, env):
    from acc.server.backup_jobs import BackupJobs
    jobs = BackupJobs(env['settings'], api.app.state.db)
    jobs.service.initialize()
    # A configured target; transport isn't touched when queuing durable intent.
    backup.private_write(jobs.service.root / 'targets.json', backup.canonical([
        {'id': 'a' * 32, 'kind': 'local', 'repository': '/mounted/example', 'enabled': False, 'created_at': 1}]))
    first = jobs.request('a' * 32, 'capture', 'owner')
    assert jobs.request('a' * 32, 'capture', 'owner')['id'] == first['id']
    again = BackupJobs(env['settings'], api.app.state.db)
    assert again.pending()[0]['id'] == first['id']
    again.store({**first, 'state': 'running'})
    again.reconcile()
    assert again.pending() == []
    assert again.state()['targets'][0]['last_job']['state'] == 'failed'


async def test_quarterly_reminder_is_owner_only_and_deduplicated(api, env):
    from acc.server.backup_jobs import BackupJobs
    from acc.server.models import AttentionItem
    jobs = BackupJobs(env['settings'], api.app.state.db)
    jobs.service.initialize()
    backup.private_write(jobs.service.root / 'targets.json', backup.canonical([
        {'id': 'a' * 32, 'kind': 'local', 'repository': '/mounted/example', 'enabled': True, 'created_at': 1}]))
    jobs.remind(now=100 * 86400)
    jobs.remind(now=100 * 86400)
    with api.app.state.db() as db:
        assert db.query(AttentionItem).filter_by(dedupe_key='backup:quarterly').count() == 1
        assert db.query(AttentionItem).filter_by(dedupe_key='backup:stale:' + 'a' * 32).count() == 1
    jobs.record_drill('owner', 'a' * 32, 'b' * 32)
    with api.app.state.db() as db:
        assert db.query(AttentionItem).filter_by(dedupe_key='backup:quarterly').one().resolved_at is not None


def test_missing_overlay_vault_reference_refuses_capture(source):
    (source.settings.catalog_local_dir / 'local.json').write_text(json.dumps({'mcps': [
        {'id': 'example', 'name': 'Example', 'command': 'example', 'env': {'TOKEN': 'vault:aaaaaaaaaaaaaaaa'}}]}))
    with pytest.raises(backup.Refused, match='catalog'):
        source.capture()


def test_update_marker_blocks_capture(source):
    (source.settings.data_dir / '.update-in-progress').touch()
    with pytest.raises(backup.Refused, match='update'):
        source.capture()
    assert not list(source.staging.glob('*/manifest.json'))


def test_staging_budget_includes_failed_uploads(source, monkeypatch):
    monkeypatch.setattr(backup, 'MAX_BYTES', 100)
    with pytest.raises(backup.Refused):
        source.capture()
    assert not list(source.staging.glob('*/manifest.json'))


def test_restored_git_never_executes_hooks_or_config(source, tmp_path):
    import subprocess
    gitdir = source.settings.git_dir
    repo = gitdir / 'orphaned/old.git'
    subprocess.run(['git', 'init', '--bare', str(repo)], check=True, capture_output=True)
    (repo / 'hooks/post-update').write_text('#!/bin/sh\ntouch ' + str(tmp_path / 'executed') + '\n')
    (repo / 'hooks/post-update').chmod(0o755)
    subprocess.run(['git', '--git-dir', str(repo), 'config', 'acc.project', 'example@2026-10-22'], check=True)
    subprocess.run(['git', '--git-dir', str(repo), 'config', 'core.hooksPath', str(tmp_path)], check=True)
    stage = source.capture()
    restored = tmp_path / 'restored'
    backup.validate_and_install(stage, source.kit(), stage.name, restored)
    assert not (tmp_path / 'executed').exists()
    assert (restored / 'git/orphaned/old.git/hooks/update').read_text().startswith('#!/bin/sh')
    assert not (restored / 'git/orphaned/old.git/hooks/post-update').exists()
    assert 'hooksPath' not in (restored / 'git/orphaned/old.git/config').read_text()
    assert 'example@2026-10-22' in (restored / 'git/orphaned/old.git/config').read_text()


def test_vault_creation_joins_maintenance_barrier(source, tmp_path):
    import concurrent.futures
    import time

    from acc.maintenance import lease
    key = source.settings.vault_key_path.with_name('new-vault.key')
    with concurrent.futures.ThreadPoolExecutor() as pool:
        with lease(source.settings.data_dir / '.maintenance.lock'):
            task = pool.submit(Cipher(key).key, create=True)
            time.sleep(.1)
            assert not task.done()
        assert len(task.result(timeout=5)) == 32


async def test_cancelled_receive_does_not_leak_capture_lock(tmp_path):
    import asyncio

    from acc.maintenance import async_lease, lease
    lock = tmp_path / 'git.lock'
    entered = asyncio.Event()
    async def waiting():
        entered.set()
        async with async_lease(lock):
            pass
    with lease(lock):
        task = asyncio.create_task(waiting())
        await entered.wait()
        await asyncio.sleep(.05)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with lease(lock, timeout=.2):
        pass


async def test_one_capture_uploads_independently_to_multiple_targets(api, env, restic_binary, tmp_path_factory):
    from acc.server.backup_jobs import BackupJobs
    jobs = BackupJobs(env['settings'], api.app.state.db)
    targets_root = tmp_path_factory.mktemp('multi-target')
    a = jobs.service.add_local_target(targets_root / 'first-encrypted', binary=restic_binary)
    b = jobs.service.add_local_target(targets_root / 'second-encrypted', binary=restic_binary)
    jobs.request(a['id'], 'capture', 'owner')
    jobs.request(b['id'], 'capture', 'owner')
    import asyncio
    await asyncio.to_thread(jobs.work, binary=restic_binary)
    values = jobs.state()['targets']
    assert {v['last_job']['state'] for v in values} == {'complete'}
    assert len({v['backup_id'] for v in values}) == 1
    assert not list(jobs.service.staging.glob('*/manifest.json'))


def test_capture_checkpoint_uses_versioned_database_settings(source):
    stage = source.capture()
    with sqlite3.connect(stage / 'acc.db') as db:
        row = db.execute("select value from setting where key='backup.capture_checkpoint'").fetchone()
        assert row and json.loads(row[0])['id'] == stage.name
        assert db.execute("select 1 from sqlite_master where name='acc_capture_checkpoint'").fetchone() is None


async def test_recovery_revokes_browser_authority_and_audits_without_secrets(api, env, tmp_path):
    from test_auth import do_setup
    await do_setup(api, env)
    with sqlite3.connect(env['settings'].data_dir / 'acc.db') as db:
        assert db.execute('select count(*) from auth_session').fetchone()[0] > 0
    service = backup.Service(env['settings'])
    stage = service.capture()
    backup.validate_and_install(stage, service.kit(), stage.name, tmp_path / 'recovered')
    with sqlite3.connect(tmp_path / 'recovered/data/acc.db') as db:
        assert db.execute('select count(*) from auth_session').fetchone()[0] == 0
        row = db.execute("select detail from audit_log where action='backup.restore'").fetchone()
        assert json.loads(row[0]) == {'backup_id': stage.name, 'quarantine': True}


def test_repeated_upload_of_same_capture_is_idempotent(source, restic_binary, tmp_path):
    target = source.add_local_target(tmp_path / 'encrypted', binary=restic_binary)
    stage = source.capture()
    a = source.back_up(target['id'], binary=restic_binary, stage=stage)
    b = source.back_up(target['id'], binary=restic_binary, stage=stage)
    assert a['snapshot_id'] == b['snapshot_id']
    backend = backup.LocalTarget(target['repository'], source.root / 'keys' / target['id'], restic_binary)
    assert len(json.loads(backend.command('snapshots', '--json', '--tag', stage.name))) == 1


def test_archive_size_limit_is_checked_before_extracting_any_file(tmp_path, monkeypatch):
    import tarfile
    monkeypatch.setattr(backup, 'MAX_BYTES', 100)
    archive = tmp_path / 'oversize.tar'
    info = tarfile.TarInfo('secrets/oversize')
    info.size = 101
    archive.write_bytes(info.tobuf(format=tarfile.USTAR_FORMAT) + b'\0' * 1024)
    with pytest.raises(backup.Refused, match='limits'):
        backup.unpack(archive, tmp_path / 'unpacked')
    assert not (tmp_path / 'unpacked').exists()


def test_capture_does_not_log_decrypted_vault_values(source, tmp_path, caplog):
    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.models import VaultKey
    canary = 'backup-canary-do-not-log-97531'
    engine = make_engine(source.settings.db_url)
    with make_sessionmaker(engine)() as db:
        db.add(VaultKey(id='c' * 16, name='Canary', variable='CANARY', owner='owner',
                        ciphertext=Cipher(source.settings.vault_key_path).seal('c' * 16, 'CANARY', canary)))
        db.commit()
    engine.dispose()
    stage = source.capture()
    backup.validate_and_install(stage, source.kit(), stage.name, tmp_path / 'restored')
    assert canary not in caplog.text
    with sqlite3.connect(tmp_path / 'restored/data/acc.db') as db:
        assert canary not in json.dumps(db.execute('select detail from audit_log').fetchall())
        assert canary not in json.dumps(db.execute('select payload from event').fetchall())


async def test_owner_capture_kit_schedule_and_prune_use_real_repository(api, env, restic_binary, tmp_path_factory):
    """Owner API actions reach durable jobs and real restic; prune preserves the verified recovery point."""
    import asyncio

    from acc.server.models import AuditLog
    jobs = api.app.state.ctx.backup_jobs
    repository = tmp_path_factory.mktemp('owner-repository') / 'ciphertext'
    target = jobs.service.add_local_target(repository, binary=restic_binary)
    requested = await api.post(f"/api/host/backups/{target['id']}/run")
    assert requested.status_code == 202
    await asyncio.to_thread(jobs.work, binary=restic_binary)
    target_state = (await api.get('/api/host/backups')).json()['targets'][0]
    assert target_state['last_job']['state'] == 'complete'
    kit = await api.post('/api/host/backups/kit', json={'confirmed': True})
    assert kit.status_code == 200
    enabled = await api.post(f"/api/host/backups/{target['id']}/enable", json={'confirmed': True})
    assert enabled.status_code == 200 and enabled.json()['targets'][0]['enabled']
    prune = await api.post(f"/api/host/backups/{target['id']}/prune", json={'confirmed': True})
    assert prune.status_code == 202
    await asyncio.to_thread(jobs.work, binary=restic_binary)
    assert jobs.state()['targets'][0]['last_job']['state'] == 'complete'
    destination = repository.parent / 'restored-after-prune'
    backup.restore(kit.json(), target['id'], target_state['backup_id'], destination, binary=restic_binary)
    assert (destination / 'data/RESTORE-HOLD').exists()
    with api.app.state.db() as db:
        entries = db.query(AuditLog).filter(AuditLog.action.startswith('backup.')).all()
        assert {'backup.request', 'backup.kit_export', 'backup.enable', 'backup.result'} <= {entry.action for entry in entries}
        assert kit.json()['targets'][0]['password'] not in json.dumps([entry.detail for entry in entries])


def test_updater_cannot_begin_while_capture_is_copying_git(source, monkeypatch):
    """Updater/source replacement must not mix a captured DB version with a later build identity."""
    import concurrent.futures
    import threading

    from acc.maintenance import update_hold
    copying, release, updating = threading.Event(), threading.Event(), threading.Event()
    original = backup.copy_tree
    def paused(src, *args, **kwargs):
        if src == source.settings.git_dir:
            copying.set()
            assert release.wait(5)
        return original(src, *args, **kwargs)
    def update():
        with update_hold(source.settings.data_dir):
            updating.set()
    monkeypatch.setattr(backup, 'copy_tree', paused)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        capture = pool.submit(source.capture)
        assert copying.wait(5)
        transaction = pool.submit(update)
        try:
            assert not updating.wait(.1)
        finally:
            release.set()
        assert capture.result(timeout=5).is_dir()
        transaction.result(timeout=5)
        assert updating.is_set()


def test_restart_with_current_schema_does_not_take_migration_exclusive_lease(source, monkeypatch):
    """A normal restart must not deadlock an active transaction waiting for the same event loop."""
    from acc.maintenance import lease
    from acc.server import db as database
    monkeypatch.setattr(database, 'lease', lambda path, **kw: lease(path, **{**kw, 'timeout': min(kw.get('timeout', .1), .1)}))
    engine = database.make_engine(source.settings.db_url)
    try:
        with engine.connect() as live:
            current = live.exec_driver_sql('select version_num from alembic_version').scalar()
            database.migrate(source.settings.db_url)
            assert live.exec_driver_sql('select version_num from alembic_version').scalar() == current
    finally:
        engine.dispose()


def test_group_exchange_and_private_recovery_capture_restore_keep_separate_identities(source, tmp_path):
    from test_gitbackup import git, make_repo

    from acc import git_exchange
    from acc.server import gitbackup, group_recovery
    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.models import Group, Placement, Project

    engine = make_engine(source.settings.db_url)
    db = make_sessionmaker(engine)
    with db() as s:
        s.add(Group(id='a' * 32, name='Example group', created_by='alex'))
        s.flush()
        project = Project(group_id='a' * 32, owner=None, name='Example project', slug='group-example', account='', workspace_path='')
        s.add(project)
        s.commit()
        pid, identity = project.id, 'a' * 32 + ':' + gitbackup.identity(project)
        s.add(Placement(project_id=pid, person='alex', host='local', account='alex-agent', path='/example'))
        s.commit()
    repo = make_repo(tmp_path / 'committed')
    sha = git(repo, 'rev-parse', 'HEAD').stdout.strip()
    exchanged = git_exchange.initialize(source.settings.git_dir / 'exchange' / f'{pid}.git', identity)
    git_exchange.publish(exchanged, git_exchange.export(repo / '.git', sha), 'refs/heads/people/example/session', '0' * 40, sha)
    snapshot = group_recovery.snapshot(exchanged, source.settings.git_dir / 'group-backups' / str(pid), {'group_id': 'a' * 32})
    from test_gitbackup import commit

    from acc.server import auth
    private_sha = commit(repo, 'private.txt', 'private-recovery-canary')
    private_person = auth.digest('alex')[:32]
    private_repo = git_exchange.initialize(tmp_path / 'private', identity + ':' + private_person)
    git_exchange.publish(private_repo, git_exchange.export(repo / '.git', private_sha), 'refs/heads/private/session', '0' * 40, private_sha)
    private_snapshot = group_recovery.snapshot(private_repo, source.settings.git_dir / 'private-groups' / str(pid) / private_person, {})
    stage = source.capture()
    inventory = json.loads((stage / 'manifest.json').read_text())['manifest']['repositories']
    matching = [r for r in inventory if r['path'] == f'git/exchange/{pid}.git']
    assert matching[0]['project_id'] == pid
    assert matching[0]['recoverable_orphan'] is False
    private_entry = next(r for r in inventory if r['scope'] == 'placement-private')
    assert private_entry['project_id'] == pid and private_entry['recoverable_orphan'] is False
    restored = tmp_path / 'replacement'
    backup.validate_and_install(stage, source.kit(), stage.name, restored)
    saved = restored / 'git/group-backups' / str(pid) / snapshot / 'repo.git'
    assert git_exchange.read_file(saved, sha, 'README.md') == b'hello\n'
    with pytest.raises(ValueError):
        git_exchange.read_file(saved, private_sha, 'private.txt')
    private_saved = restored / 'git/private-groups' / str(pid) / private_person / private_snapshot / 'repo.git'
    assert git_exchange.read_file(private_saved, private_sha, 'private.txt') == b'private-recovery-canary'
    engine.dispose()


async def test_capture_barrier_never_waits_on_event_loop(source, monkeypatch):
    """Contended acquisition raises without ever sleeping on the loop, regardless of CPU load."""
    import asyncio
    import threading
    import time

    from acc import maintenance
    from acc.server.db import MaintenanceBusy, make_engine
    engine = make_engine(source.settings.db_url)
    held, release = threading.Event(), threading.Event()
    original_sleep = time.sleep
    def guarded_sleep(delay):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            original_sleep(delay)
        else:
            pytest.fail('maintenance slept on the event loop')
    monkeypatch.setattr(maintenance.time, 'sleep', guarded_sleep)
    def hold():
        with maintenance.lease(source.settings.data_dir / '.maintenance.lock'):
            held.set()
            release.wait(5)
    task = asyncio.create_task(asyncio.to_thread(hold))
    assert await asyncio.to_thread(held.wait, 2)
    try:
        with pytest.raises(MaintenanceBusy):
            with engine.begin() as db:
                db.exec_driver_sql('select 1')
    finally:
        release.set()
        await task
        engine.dispose()


def test_node_target_starts_unverified_and_prune_is_unavailable(source):
    target = source.add_node_target('a' * 32, 'archive-node')
    assert target['kind'] == 'node'
    assert target['enabled'] is False
    assert target['repository_id'] is None
    assert 'password' not in json.dumps(target)
    commands = source.target_setup(target['id'])
    assert '--append-only' in commands
    assert 'vault.key' not in commands
    assert 'private-repos' in commands
    with pytest.raises(backup.Refused, match='setup'):
        source.back_up(target['id'])


def test_offline_node_kit_can_use_owner_exported_ciphertext(source, restic_binary, tmp_path):
    target = source.add_local_target(tmp_path / 'encrypted-export', binary=restic_binary)
    result = source.back_up(target['id'], binary=restic_binary)
    kit = source.kit()
    kit['targets'][0]['kind'] = 'node'
    kit['targets'][0]['repository'] = 'enrolled:archive-node'
    backup.restore(kit, target['id'], result['backup_id'], tmp_path / 'restored',
                   binary=restic_binary, repository=tmp_path / 'encrypted-export')
    assert (tmp_path / 'restored/data/RESTORE-HOLD').is_file()

@pytest.fixture(scope='session')
def rest_server_binary(tmp_path_factory):
    import hashlib
    import platform
    import tarfile
    import urllib.request

    from acc.backup_targets import CHECKSUMS, VERSION
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]
    root = tmp_path_factory.mktemp('receiver')
    archive = root / 'receiver.tar.gz'
    urllib.request.urlretrieve(f'https://github.com/restic/rest-server/releases/download/v{VERSION}/rest-server_{VERSION}_linux_{arch}.tar.gz', archive)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == CHECKSUMS[arch]
    with tarfile.open(archive) as tar:
        member = tar.getmember(f'rest-server_{VERSION}_linux_{arch}/rest-server')
        binary = root / 'rest-server'
        binary.write_bytes(tar.extractfile(member).read())
    binary.chmod(0o700)
    return binary


@pytest.fixture
def receiver(source, rest_server_binary, tmp_path, request):
    import socket
    import subprocess
    import time
    from contextlib import contextmanager
    target = source.add_node_target('a' * 32, 'archive-node')
    root = tmp_path / 'receiver'
    root.mkdir()
    credential = (source.root / 'transport' / target['id']).read_bytes()
    import hashlib
    auth = tmp_path / 'writers'
    auth.write_text(target['id'] + ':{SHA}' + base64.b64encode(hashlib.sha1(credential).digest()).decode() + '\n')
    # The test suite alone starts a temporary receiver on a kernel-selected port.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([str(rest_server_binary), '--path', str(root), '--listen', f'127.0.0.1:{port}',
                                '--htpasswd-file', str(auth), '--private-repos', '--max-size', '10485760',
                                *([] if getattr(request, 'param', '') == 'mutable' else ['--append-only'])],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.1):
                break
        except OSError:
            time.sleep(.05)
    else:
        process.terminate()
        process.wait()
        pytest.fail('temporary receiver did not start')
    @contextmanager
    def transport(t):
        yield f"http://127.0.0.1:{port}/{t['id']}/"
    source.transport = transport
    try:
        yield target, root
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_real_append_only_node_capture_restore_and_history_survive(source, receiver, restic_binary, tmp_path):
    target, root = receiver
    target = source.check_target(target['id'], binary=restic_binary)
    assert target['setup_verified_at']
    assert target['max_size_bytes'] == 20 * 2**30
    assert source.target(target['id'])['max_size_bytes'] == 20 * 2**30
    assert '--max-size 21474836480' in source.target_setup(target['id'])
    first = source.back_up(target['id'], binary=restic_binary)
    second = source.back_up(target['id'], binary=restic_binary)
    assert first['backup_id'] != second['backup_id']
    with source.backend(target, restic_binary) as backend:
        snapshots = json.loads(backend.command('snapshots', '--json', '--tag', first['backup_id']))
        assert len(snapshots) == 1
        # The compromised main possesses both writer credentials and encryption key.
        # It still cannot delete historical snapshots.
        with pytest.raises(backup.Refused):
            backend.command('forget', snapshots[0]['id'], '--prune')
        backup.restore(source.kit(), target['id'], first['backup_id'], tmp_path / 'restored',
                       binary=restic_binary, backend=backend)
    assert (tmp_path / 'restored/data/RESTORE-HOLD').is_file()
    for path in root.rglob('*'):
        if path.is_file():
            assert source.settings.vault_key_path.read_bytes().strip() not in path.read_bytes()


async def test_node_setup_routes_validate_enrollment_and_refuse_remote_prune(api, env):
    from test_auth import cookie_client, do_setup
    from test_users import csrf

    from acc.server.models import AuditLog, Node
    await do_setup(api, env)
    owner = cookie_client(api)
    enrolled = await owner.post('/api/nodes', json={'name':'archive-node'}, headers=csrf(owner))
    assert enrolled.status_code == 201
    node_id = enrolled.json()['id']
    with api.app.state.db() as db:
        node = db.get(Node, node_id)
        node.status, node.info = 'active', {'os':'linux'}
        db.commit()
    invalid = await owner.post('/api/host/backups/targets', json={'node_id': 'b' * 32}, headers=csrf(owner))
    assert invalid.status_code == 409
    response = await owner.post('/api/host/backups/targets', json={'node_id': node_id}, headers=csrf(owner))
    assert response.status_code == 201, response.text
    target = response.json()
    commands = await owner.get(f"/api/host/backups/{target['id']}/setup")
    assert commands.status_code == 200
    assert commands.headers['cache-control'] == 'no-store'
    assert '--append-only' in commands.json()['commands']
    assert (await owner.post(f"/api/host/backups/{target['id']}/prune", json={'confirmed': True}, headers=csrf(owner))).status_code == 409
    assert (await owner.post(f"/api/host/backups/{target['id']}/run", headers=csrf(owner))).status_code == 409
    with api.app.state.db() as db:
        audit = db.query(AuditLog).filter_by(action='backup.target_add').one()
        assert audit.target == target['id']
        assert 'password' not in json.dumps(audit.detail)


def test_node_schedule_requires_kit_with_verified_repository_identity(source):
    from acc.server.backup_jobs import BackupJobs
    from acc.server.db import make_engine, make_sessionmaker
    jobs = BackupJobs(source.settings, make_sessionmaker(make_engine(source.settings.db_url)))
    target = source.add_node_target('a' * 32, 'archive-node')
    from acc.server.models import Node
    with jobs.db() as db:
        db.add(Node(id='a' * 32, name='archive-node', status='active'))
        db.commit()
    backup.private_write(source.root / 'kit-exported', backup.canonical({'targets':[target['id']]}))
    target.update(repository_id='b' * 64, setup_verified_at=1)
    backup.private_write(source.root / 'targets.json', backup.canonical([target]))
    backup.private_write(source.root / f"result-{target['id']}.json", b'{}')
    with pytest.raises(backup.Refused, match='kit'):
        jobs.enable(target['id'], 'owner')


async def test_real_node_link_carries_only_ciphertext_receiver_traffic(source, receiver, restic_binary, tmp_path, monkeypatch):
    import asyncio
    from contextlib import contextmanager
    from urllib.parse import urlsplit

    from acc import backup_targets
    from acc.node.client import Relay
    from acc.server.nodes import NodeLink
    target, root = receiver
    with source.transport(target) as url:
        port = urlsplit(url).port
    binding = tmp_path / 'binding.json'
    root.chmod(0o700)
    from acc.backup_receiver_check import identity
    binding.write_text(json.dumps({'target_id': target['id'], 'installation_id': source.kit()['installation_id'], 'port': port, **identity(root, os.getuid())}))
    binding.chmod(0o600)
    monkeypatch.setattr(backup_targets, 'BINDING', binding)
    original = backup_targets.receiver_binding
    monkeypatch.setattr(backup_targets, 'receiver_binding', lambda *a: original(*a, owner=os.getuid(), data=root, receiver_uid=os.getuid()))
    class Socket:
        async def send(self, data):
            link.receive(data)
    relay = Relay(Socket(), tmp_path / 'no-broker-sockets')
    # A slow receiver can finish draining after the HTTP client has reconnected.
    original_drain = relay._drain
    async def delayed_drain(channel):
        await asyncio.sleep(.05)
        await original_drain(channel)
    monkeypatch.setattr(relay, '_drain', delayed_drain)
    link = NodeLink('a' * 32, 'archive-node', relay.handle, {'capabilities':['backup-v1']}, [])
    @contextmanager
    def transport(t):
        with backup_targets.node_transport(link, t, source.kit()['installation_id']) as url:
            yield url
    source.transport = transport
    try:
        await asyncio.to_thread(source.check_target, target['id'], binary=restic_binary)
        result = await asyncio.to_thread(source.back_up, target['id'], binary=restic_binary)
        assert result['backup_id']
        assert not list((tmp_path / 'no-broker-sockets').glob('*'))
        assert not link._channels
    finally:
        link.close()
        relay.close_all()


def test_explicit_remote_verify_restores_historical_generation_in_disposable_scratch(source, receiver, restic_binary):
    target, root = receiver
    source.check_target(target['id'], binary=restic_binary)
    result = source.back_up(target['id'], binary=restic_binary)
    verified = source.verify(target['id'], result['backup_id'], binary=restic_binary)
    assert verified['backup_id'] == result['backup_id']
    assert verified['restore_tested_at'] >= result['verified_at']
    assert not list(source.root.glob('verify-*'))
    with pytest.raises(backup.Refused):
        source.verify(target['id'], 'f' * 32, binary=restic_binary)


async def test_async_api_reports_capture_barrier_as_retryable(api, env):
    import asyncio
    import threading

    from test_auth import cookie_client, do_setup

    from acc.maintenance import lease
    await do_setup(api, env)
    owner = cookie_client(api)
    # Authentication already finished before capture starts; model that race.
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: None
    held, release = threading.Event(), threading.Event()
    def hold():
        with lease(env['settings'].data_dir / '.maintenance.lock'):
            held.set()
            release.wait(5)
    task = asyncio.create_task(asyncio.to_thread(hold))
    await asyncio.to_thread(held.wait, 2)
    try:
        result = await owner.get('/api/projects/999/knowledge')
        assert result.status_code == 503
        assert result.headers['retry-after'] == '1'
    finally:
        release.set()
        await task


@pytest.mark.parametrize('receiver', ['mutable'], indirect=True)
def test_mutable_receiver_never_passes_setup(source, receiver, restic_binary):
    target, root = receiver
    with pytest.raises(backup.Refused, match='append-only'):
        source.check_target(target['id'], binary=restic_binary)
    assert source.target(target['id'])['repository_id'] is None
    assert source.target(target['id'])['setup_verified_at'] is None


def test_corrupt_remote_ciphertext_does_not_advance_verified_result(source, receiver, restic_binary):
    target, root = receiver
    source.check_target(target['id'], binary=restic_binary)
    first = source.back_up(target['id'], binary=restic_binary)
    pack = next(p for p in (root / target['id'] / 'data').rglob('*') if p.is_file())
    pack.write_bytes(b'corrupt ciphertext')
    with pytest.raises(backup.Refused):
        source.back_up(target['id'], binary=restic_binary)
    assert json.loads((source.root / f"result-{target['id']}.json").read_bytes()) == first


def test_setup_commands_have_valid_shell_syntax(source):
    import subprocess
    target = source.add_node_target('a' * 32, 'archive-node')
    result = subprocess.run(['sh', '-n'], input=source.target_setup(target['id']), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


async def test_prepare_target_releases_db_barrier_before_coordinator(api, env, monkeypatch):
    from test_auth import cookie_client, do_setup
    from test_users import csrf

    from acc.maintenance import lease
    from acc.server.models import Node
    await do_setup(api, env)
    owner = cookie_client(api)
    with api.app.state.db() as db:
        db.add(Node(id='a' * 32, name='archive-node', status='active', info={'os':'linux'}))
        db.commit()
    service = api.app.state.ctx.backup_jobs.service
    original = service.add_node_target
    barrier_free = []
    def provision(*args):
        try:
            with lease(env['settings'].data_dir / '.maintenance.lock', timeout=.1):
                barrier_free.append(True)
        except TimeoutError:
            barrier_free.append(False)
        return original(*args)
    monkeypatch.setattr(service, 'add_node_target', provision)
    result = await owner.post('/api/host/backups/targets', json={'node_id':'a'*32}, headers=csrf(owner))
    assert result.status_code == 201
    assert barrier_free == [True]

def test_postgres_capture_refuses_before_touching_sqlite(source):
    from dataclasses import replace
    source.settings = replace(source.settings, database_url='postgresql+psycopg://unused/example')
    with pytest.raises(backup.Refused, match='PostgreSQL'):
        source.capture()


def test_real_append_only_receiver_recovers_killed_local_restic_lock(source, receiver, restic_binary):
    """A real restic lock survives SIGKILL; only once it is provably this main's own child is it deleted, by ID, on the
    append-only receiver."""
    import subprocess
    import time
    target, root = receiver
    target = source.check_target(target['id'], binary=restic_binary)
    audits = []
    source.lock_audit = lambda target_id, detail: audits.append(detail)
    with source.backend(target, restic_binary) as backend:
        started = time.time()
        process = subprocess.Popen([str(restic_binary), *backend.repo_args(), 'backup', '--stdin',
                                    '--stdin-filename', 'interrupted-test'], env=backend.env(),
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            lock_dir = root / target['id'] / 'locks'
            for _ in range(200):
                if list(lock_dir.glob('*')): break
                if process.poll() is not None: pytest.fail('fixture restic exited before acquiring its lock')
                time.sleep(.025)
            assert list(lock_dir.glob('*'))
            with pytest.raises(backup.RepositoryLocked) as caught:
                backend.command('check', '--read-data')
            assert any(l['pid'] == process.pid for l in caught.value.locks)
            assert not audits  # live same-host PID must not be removed
            process.kill()  # deliberately simulate abrupt death of this fixture child only
            process.wait(timeout=5)
            assert list(lock_dir.glob('*'))
            # Dead and on this host, but not a child this backend recorded: a hostname is no proof (BR-3).
            with pytest.raises(backup.RepositoryLocked):
                backend.command('check', '--read-data')
            assert list(lock_dir.glob('*')) and not audits
            # Recorded as _raw_command records its own children: removed by ID with DELETE locks/<id> (BR-2).
            backend._record_child({'pid': process.pid, 'boot': backup.boot_id(), 'started': started,
                                   'ended': time.time()})
            backend.command('check', '--read-data')
            assert not list(lock_dir.glob('*'))
            assert any(a['phase'] == 'complete' and a['removed'] for a in audits)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=65)
            process.stdin.close()


def test_real_restic_timeout_reaps_child_and_removes_receiver_lock(source, receiver, restic_binary):
    import os
    import time
    from concurrent.futures import ThreadPoolExecutor

    from acc.restic_process import run
    target, root = receiver
    target = source.check_target(target['id'], binary=restic_binary)
    reader, writer = os.pipe()
    with os.fdopen(reader, 'rb') as input_file, os.fdopen(writer, 'wb'), source.backend(target, restic_binary) as backend:
        with ThreadPoolExecutor() as pool:
            future = pool.submit(run, [str(restic_binary), *backend.repo_args(), 'backup', '--stdin'],
                                 env=backend.env(), stdin=input_file, timeout=3)
            lock_dir = root / target['id'] / 'locks'
            for _ in range(100):
                if list(lock_dir.glob('*')): break
                if future.done(): future.result()
                time.sleep(.02)
            assert list(lock_dir.glob('*'))
            with pytest.raises(TimeoutError): future.result(timeout=10)
            assert not list(lock_dir.glob('*'))


def test_capture_releases_writer_barrier_before_file_copy_and_validation(source, monkeypatch):
    """Slow catalog/secrets IO must not deny unrelated DB transactions."""
    import time

    from acc.maintenance import lease
    original = backup.copy_tree
    observations = []

    def copy(*args, **kwargs):
        before = time.monotonic()
        with lease(source.settings.data_dir / '.maintenance.lock', timeout=.05):
            observations.append(time.monotonic() - before)
        return original(*args, **kwargs)

    monkeypatch.setattr(backup, 'copy_tree', copy)
    stage = source.capture()
    assert len(observations) >= 3
    backup.validate_and_install(stage, source.kit(), stage.name, source.root.parent / 'restored')


def test_capture_snapshot_holds_barrier_but_validation_does_not(source, monkeypatch):
    from acc.maintenance import lease
    connect, validate = sqlite3.connect, backup.validate_db
    snapshots = []
    class Connection(sqlite3.Connection):
        def backup(self, destination, **kwargs):
            with pytest.raises(TimeoutError):
                with lease(source.settings.data_dir / '.maintenance.lock', timeout=0):
                    pass
            snapshots.append(True)
            return super().backup(destination, **kwargs)
    def open_db(*args, **kwargs):
        return connect(*args, factory=Connection, **kwargs)
    def validate_unlocked(*args, **kwargs):
        with lease(source.settings.data_dir / '.maintenance.lock', timeout=0):
            return validate(*args, **kwargs)
    monkeypatch.setattr(backup.sqlite3, 'connect', open_db)
    monkeypatch.setattr(backup, 'validate_db', validate_unlocked)
    source.capture()
    assert snapshots == [True]


def test_a_catalog_edit_makes_capture_wait_without_blocking_writers(source):
    """Review 2026-10-25 (L-1): a zero-wait catalog lock failed the whole capture during any catalog edit."""
    import threading
    import time

    from acc.maintenance import lease
    from acc.server.catalog_store import catalog_lock
    result = {}

    def capture():
        try:
            result['stage'] = source.capture()
        except Exception as error:  # noqa: BLE001 - reported below
            result['error'] = error

    with catalog_lock(source.settings.catalog_local_dir):  # an edit in progress
        worker = threading.Thread(target=capture)
        worker.start()
        time.sleep(.3)
        assert worker.is_alive() and not result  # waiting, not failed
        with lease(source.settings.data_dir / '.maintenance.lock', exclusive=False, timeout=0):
            pass  # database transactions aren't held off while the capture waits
    worker.join(30)
    assert 'error' not in result, result
    backup.validate_and_install(result['stage'], source.kit(), result['stage'].name, source.root.parent / 'restored')


def test_capture_holds_the_secrets_lock_from_snapshot_until_secrets_are_copied(source, monkeypatch):
    """Review 2026-10-25 (L-2): a secrets file the database refers to must not vanish between the snapshot and the copy."""
    from acc.maintenance import lease, secrets_lock
    original, held = backup.copy_tree, {}

    def copy(src, dst, budget, exclusions, prefix):
        try:
            with lease(secrets_lock(source.settings.data_dir), exclusive=False, timeout=0):
                held[prefix] = False
        except TimeoutError:
            held[prefix] = True
        return original(src, dst, budget, exclusions, prefix)

    monkeypatch.setattr(backup, 'copy_tree', copy)
    source.capture()
    assert held['secrets'] is True and held['catalog'] is False and held['git'] is False


async def test_restore_drill_keeps_the_selected_snapshot_in_state_and_audit(api, project):
    from acc.server.models import AuditLog, Setting
    jobs = api.app.state.ctx.backup_jobs
    jobs.service.initialize()
    target, generation, snapshot = 'a' * 32, 'b' * 32, 'c' * 64
    backup.private_write(jobs.service.root / 'targets.json', backup.canonical([
        {'id': target, 'kind': 'local', 'repository': '/mounted/example', 'enabled': False, 'created_at': 1}]))
    response = await api.post('/api/host/backups/drill', json={
        'confirmed': True, 'target_id': target, 'backup_id': generation, 'snapshot_id': snapshot})
    assert response.status_code == 200, response.text
    with api.app.state.db() as db:
        assert db.get(Setting, 'backup.drill').value['snapshot_id'] == snapshot
        audit = db.query(AuditLog).filter_by(action='backup.drill').one()
        assert audit.detail == {'backup_id': generation, 'snapshot_id': snapshot}
    refused = await api.post('/api/host/backups/drill', json={
        'confirmed': True, 'target_id': target, 'backup_id': generation, 'snapshot_id': 'not-a-snapshot'})
    assert refused.status_code == 422


def test_external_update_marker_blocks_capture_without_compatibility_signal(source):
    from acc.maintenance import update_hold, update_marker
    data = source.settings.data_dir
    with update_hold(data):
        legacy = data / '.update-in-progress'
        legacy.unlink()
        try:
            assert update_marker(data).is_file()
            with pytest.raises(backup.Refused, match='updating'):
                source.capture()
        finally:
            legacy.touch(mode=0o600)
    assert not list(source.staging.glob('*/manifest.json'))
