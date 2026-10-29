"""RC drill: owner API -> real local restic -> loss -> offline CLI -> held app.

Only fixture data and a pytest-owned ciphertext repository. No live services, S3,
helper installation, hold removal or authority release. See the Backups guide.
"""
import asyncio
import dataclasses
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from helpers import pause_supervisor, write_db
from test_backup_server import restic_binary  # noqa: F401 (shared pinned, scratch-only fixture)
from test_changes import git, repo

from acc.server import app as app_module
from acc.server.models import AuthSession, Device, Invite, LaunchSpec, Project, Session, Setting, User, VaultKey
from acc.server.vault import Cipher

REPO = Path(__file__).resolve().parents[1]
VAULT_CANARY = 'synthetic-vault-value-do-not-emit'
EXCLUDED_CANARY = 'synthetic-owner-login-not-in-server-backup'


def readonly(database):
    return sqlite3.connect(f'file:{database}?mode=ro&immutable=1', uri=True)


def restore_cli(kit, target_id, generation, repository, destination, binary, prepare):
    argv = [sys.executable, '-m', 'acc.backup_server', 'restore', '--kit', str(kit),
            '--target-id', target_id, '--backup-id', generation['backup_id'],
            '--snapshot-id', generation['snapshot_id'], '--repository', str(repository),
            '--destination', str(destination), '--restic', str(binary)]
    if prepare:
        argv.append('--prepare-replacement')
    return subprocess.run(argv, cwd=REPO, env={**os.environ, 'PYTHONPATH': str(REPO / 'src')},
                          capture_output=True, text=True, timeout=180)


@pytest.mark.parametrize('prepare', [False, True], ids=['offline-hold', 'replacement-preparation'])
async def test_owner_backup_loss_and_quarantined_restore(api, env, tmp_path_factory, restic_binary, monkeypatch, prepare):  # noqa: F811
    if prepare and os.geteuid() == 0:
        pytest.skip('documented replacement preparation must run unprivileged')
    # Drive stages explicitly: no assumed background cadence and no live source writes during deletion.
    await pause_supervisor(api)
    settings = env['settings']
    old_root = env['home'].parent
    escrow = tmp_path_factory.mktemp('rc-recovery-escrow')
    repository = escrow / 'ciphertext'
    jobs = api.app.state.ctx.backup_jobs
    target = await asyncio.to_thread(jobs.service.add_local_target, repository, binary=restic_binary)
    cipher = Cipher(settings.vault_key_path)
    encrypted = cipher.seal('a' * 32, 'EXAMPLE_SERVICE_TOKEN', VAULT_CANARY, create=True)
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    workspace = env['workspaces'] / 'demo'
    head = repo(workspace)
    (workspace / 'uncommitted.txt').write_text(EXCLUDED_CANARY)
    login = env['home'] / '.codex/auth.json'
    login.parent.mkdir(parents=True)
    login.write_text(EXCLUDED_CANARY)
    settings.catalog_local_dir.mkdir(parents=True, exist_ok=True)
    overlay = {'items': [], 'rehearsal_note': 'synthetic catalog overlay'}
    (settings.catalog_local_dir / 'local.json').write_text(json.dumps(overlay))
    settings.run_dir.mkdir(parents=True, exist_ok=True)
    (settings.run_dir / 'excluded.log').write_text(EXCLUDED_CANARY)
    settings.data_dir.joinpath('secrets/drill-canary').write_text('synthetic-main-secret')
    with write_db(api.app.state.db) as db:
        owner = User(username='alex', role='owner', password_hash='synthetic-disabled-password', totp_secret='SYNTHETIC')
        db.add(owner)
        db.flush()
        for index in range(2):
            project = Project(slug=f'drill-{index}', name=f'Drill {index}', owner='alex', account='agent',
                              workspace_path=str(workspace), backup_identity=f'drill-{index}@2026-10-27')
            db.add(project)
            db.flush()
            db.add(Session(id=str(index + 1) * 12, project_id=project.id, provider='codex', name=f'Recorded {index}',
                           tmux_name=f'acc-drill-{index}', run_as='agent', owner='alex',
                           desired_state='stopped', observed_state='stopped',
                           launch_spec=LaunchSpec(provider='codex', inputs={'provider': 'codex'}, files={}, digest='d' * 64)))
            if index == 0:
                project_id = project.id
        for index, (kind, status) in enumerate([('browser', 'active'), ('browser', 'revoked'), ('native', 'active'), ('native', 'revoked')]):
            db.add(Device(id=str(index + 1) * 32, person='alex', kind=kind, status=status, label=f'Device {index}',
                          fingerprint=str(index + 1) * 64, serial=str(index + 1), expires_at=expiry,
                          certificate_pem='synthetic-public-certificate'))
        db.add(Setting(key='budget.alex', value={'paused': True, 'hours': 8}, updated_by='alex'))
        db.add(VaultKey(id='a' * 32, name='Synthetic service', variable='EXAMPLE_SERVICE_TOKEN',
                        ciphertext=encrypted, owner='alex'))
        db.add(AuthSession(token_hash='b' * 64, csrf='c' * 64, user_id=owner.id, expires_at=expiry))
        db.add(Invite(token_hash='e' * 64, created_by='alex', expires_at=expiry))
        db.commit()
    bare = settings.git_dir / f'{project_id}.git'
    git(workspace, 'clone', '--bare', '.', str(bare))
    git(bare, 'config', 'acc.project', 'drill-0@2026-10-27')
    vault_key = settings.vault_key_path.read_bytes()
    api_token = settings.api_token_path.read_bytes()
    exported = await api.post('/api/host/backups/kit', json={'confirmed': True})
    assert exported.status_code == 200
    kit = exported.json()
    kit_file = escrow / 'kit.json'
    kit_file.write_text(json.dumps(kit))
    kit_file.chmod(0o600)
    capture = await api.post(f"/api/host/backups/{target['id']}/run")
    assert capture.status_code == 202
    await asyncio.to_thread(jobs.work, binary=restic_binary)
    state = (await api.get('/api/host/backups')).json()['targets'][0]
    assert state['last_job']['state'] == 'complete'
    generation = {'backup_id': state['backup_id'], 'snapshot_id': state['snapshot_id']}
    # The owner keeps these outside the main. Never write the actual kit to artifacts or test output.
    (escrow / 'independent-record.json').write_text(json.dumps({**generation, 'fingerprint': kit['public_key_fingerprint']}))
    # Revocation after the capture cannot retroactively modify it. Quarantine must contain that rollback.
    with write_db(api.app.state.db) as db:
        db.get(Device, '1' * 32).status = 'revoked'
        db.commit()
    await api.aclose()
    api.app.state.db.kw['bind'].dispose()
    assert all(path.is_relative_to(old_root) for path in
               (settings.data_dir, settings.catalog_local_dir, settings.git_dir, settings.run_dir, env['workspaces']))
    shutil.rmtree(old_root)
    assert not old_root.exists()
    # Simulate an owner-exported repository at a different location, without editing the saved kit.
    exported_repo = escrow / 'exported-ciphertext'
    repository.rename(exported_repo)
    for path in exported_repo.rglob('*'):
        if path.is_file():
            raw = path.read_bytes()
            assert VAULT_CANARY.encode() not in raw and vault_key.strip() not in raw
    bad_kit = json.loads(kit_file.read_text())
    bad_kit['targets'][0]['password'] = 'incorrect-synthetic-password'
    bad_file = escrow / 'wrong-kit.json'
    bad_file.write_text(json.dumps(bad_kit))
    bad_file.chmod(0o600)
    rejected = await asyncio.to_thread(restore_cli, bad_file, target['id'], generation, exported_repo,
                                     escrow / 'rejected', restic_binary, prepare)
    assert rejected.returncode != 0
    assert not (escrow / 'rejected').exists()
    assert 'incorrect-synthetic-password' not in rejected.stdout + rejected.stderr
    destination = escrow / 'replacement'
    recovered = await asyncio.to_thread(restore_cli, kit_file, target['id'], generation, exported_repo,
                                      destination, restic_binary, prepare)
    assert recovered.returncode == 0, recovered.stderr
    report = json.loads(recovered.stdout)
    assert report['quarantine'] is True and report['backup_id'] == generation['backup_id']
    assert kit['targets'][0]['password'] not in recovered.stdout + recovered.stderr
    assert (destination / 'data/RESTORE-HOLD').is_file()
    assert (destination / 'data/secrets/vault.key').read_bytes() == vault_key
    assert (destination / 'data/secrets/vault.key').stat().st_mode & 0o077 == 0
    assert (destination / 'data/secrets/drill-canary').read_text() == 'synthetic-main-secret'
    assert not (destination / 'run').exists() and not (destination / 'workspaces').exists()
    assert not list(destination.rglob('auth.json'))
    assert not list(escrow.glob('.acc-recovery-*')) and not list(escrow.glob('.restore-*'))
    with readonly(destination / 'data/acc.db') as db:
        assert db.execute('pragma integrity_check').fetchone() == ('ok',)
        assert db.execute('pragma foreign_key_check').fetchall() == []
        for table, expected in [('project', 2), ('session', 2), ('device', 4), ('vault_key', 1), ('user', 1)]:
            assert db.execute(f'select count(*) from {table}').fetchone()[0] == expected
        assert db.execute('select count(*) from auth_session').fetchone()[0] == 0
        assert db.execute('select count(*) from invite').fetchone()[0] == 0
        assert json.loads(db.execute("select value from setting where key='budget.alex'").fetchone()[0]) == {'paused': True, 'hours': 8}
        devices = dict(db.execute('select id,status from device'))
        assert devices['2' * 32] == devices['4' * 32] == 'revoked'
        assert devices['1' * 32] == devices['3' * 32] == ('revoked' if prepare else 'active')
        restored_cipher = Cipher(destination / 'data/secrets/vault.key')
        row = db.execute('select id,variable,ciphertext from vault_key').fetchone()
        assert restored_cipher.open(*row) == VAULT_CANARY
        assert db.execute("select count(*) from audit_log where action='backup.restore'").fetchone()[0] >= 1
    if prepare:
        assert (destination / 'plan.json').is_file()
        assert (destination / 'data/REPLACEMENT-ID').is_file()
        assert not (destination / 'data/secrets/api_token').exists()
    else:
        assert (destination / 'data/secrets/api_token').read_bytes() == api_token
    assert json.loads((destination / 'catalog-local/local.json').read_text()) == overlay
    assert git(destination / f'git/{project_id}.git', 'rev-parse', 'HEAD') == head
    assert git(destination / f'git/{project_id}.git', 'show', 'HEAD:app.py') == 'one\ntwo'
    # A restored main starts, but cannot migrate, authenticate, connect to nodes, launch, or publish.
    def forbidden(*args, **kwargs):
        pytest.fail('held app attempted ordinary startup')
    monkeypatch.setattr(app_module, 'migrate', forbidden)
    monkeypatch.setattr(app_module, 'make_engine', forbidden)
    monkeypatch.setattr(app_module, 'Supervisor', forbidden)
    restored_settings = dataclasses.replace(settings, data_dir=destination / 'data', git_dir=destination / 'git',
                                            catalog_local_dir=destination / 'catalog-local')
    held = app_module.create_app(restored_settings)
    assert not hasattr(held.state, 'supervisor')
    async with held.router.lifespan_context(held):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=held), base_url='http://recovery.example.lan') as client:
            for path in ('/', '/api/health', '/api/projects', '/api/sessions', '/api/nodes', '/api/devices'):
                response = await client.get(path)
                assert response.status_code == 503 and response.json()['quarantine'] is True
            assert (await client.post('/api/auth/login', json={})).status_code == 503
    assert (destination / 'data/RESTORE-HOLD').is_file()
