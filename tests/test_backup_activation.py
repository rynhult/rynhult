"""Offline replacement control: temporary files only, no target or service operations."""
import json
import os
import sqlite3
import time

import pytest
from isolated_settings import isolated_settings
from test_auth import live  # noqa: F401 (fixture)

from acc import backup_server as backup
from acc.server.config import Settings
from acc.server.db import migrate
from acc.server.vault import Cipher


@pytest.fixture
def source(tmp_path):
    settings = isolated_settings(tmp_path / 'old')
    settings.data_dir.mkdir(parents=True)
    migrate(settings.db_url)
    Cipher(settings.vault_key_path).key(create=True)
    settings.catalog_local_dir.mkdir()
    (settings.catalog_local_dir / 'local.json').write_text('{}')
    service = backup.Service(settings)
    service.initialize()
    return service


@pytest.fixture
def prepared(source, tmp_path):
    from acc.backup_activation import Layout, prepare
    layout = Layout(root=tmp_path / 'replacement', policy=tmp_path / 'policy.json',
                    state=tmp_path / 'state.json', audit=tmp_path / 'audit.jsonl', lock=tmp_path / 'lock')
    stage = source.capture()
    plan = prepare(stage, source.kit(), stage.name, layout.prepared)
    return layout, plan


def approve(layout, plan, **changes):
    from acc.backup_activation import CHECKS, plan_digest
    policy = {'enabled': True, 'plan_sha256': plan_digest(plan), 'new_main_id': plan['new_main_id'],
              'installation_id': plan['installation_id'], 'backup_id': plan['backup_id'],
              'old_main_id': plan['old_main_id'], 'release_control': True,
              'fence': {'kind': 'power-off', 'reference': 'DRILL-123', 'at': time.time(),
                        'expires_at': time.time() + 900, 'restart_prevented': True,
                        'nodes_dns_helpers_isolated': True}, 'checks': dict.fromkeys(CHECKS, True)}
    policy.update(changes)
    layout.policy.write_text(json.dumps(policy))
    layout.policy.chmod(0o644)
    return policy


def controller(layout, stopped=lambda: True):
    from acc.backup_activation import Controller
    return Controller(layout, owner=os.getuid(), uid=os.getuid(), gid=os.getgid(), stopped=stopped)


def test_default_off_and_missing_fence_never_install(prepared):
    from acc.backup_activation import Refused
    layout, plan = prepared
    with pytest.raises(Refused):
        controller(layout).install()
    assert not (layout.root / 'data').exists()
    approve(layout, plan, enabled=False)
    with pytest.raises(Refused):
        controller(layout).install()
    assert not (layout.root / 'data').exists()


def test_fenced_install_recovery_and_explicit_release_are_separate(prepared):
    from acc.backup_activation import startup_blocked
    layout, plan = prepared
    approve(layout, plan)
    ctl = controller(layout)
    installed = ctl.install()
    assert installed['phase'] == 'installed'
    assert (layout.root / 'data/RESTORE-HOLD').exists()
    assert startup_blocked(layout.root / 'data', layout=layout, owner=os.getuid())
    assert ctl.activate()['phase'] == 'recovery'
    assert startup_blocked(layout.root / 'data', layout=layout, owner=os.getuid())
    assert ctl.release_control()['phase'] == 'active'
    assert not startup_blocked(layout.root / 'data', layout=layout, owner=os.getuid())
    assert ctl.release_control()['new_main_id'] == plan['new_main_id']
    events = [json.loads(line) for line in layout.audit.read_text().splitlines()]
    assert {e['action'] for e in events} >= {'install', 'activate', 'release_control'}
    assert all(e['backup_id'] == plan['backup_id'] for e in events)
    with sqlite3.connect(layout.root / 'data/acc.db') as db:
        assert db.execute("select count(*) from audit_log where action='backup.replacement_prepare'").fetchone()[0] == 1


@pytest.mark.parametrize('attack', ['dns', 'expired', 'future', 'no_restart', 'no_isolation', 'wrong_backup',
                                  'wrong_old', 'wrong_new', 'unchecked', 'writable', 'symlink', 'hash'])
def test_fencing_and_policy_refusal_is_audited(prepared, attack):
    from acc.backup_activation import Refused
    layout, plan = prepared
    policy = approve(layout, plan)
    if attack == 'dns': policy['fence']['kind'] = 'dns-moved'
    if attack == 'expired': policy['fence']['expires_at'] = time.time() - 1
    if attack == 'future': policy['fence']['at'] = time.time() + 60
    if attack == 'no_restart': policy['fence']['restart_prevented'] = False
    if attack == 'no_isolation': policy['fence']['nodes_dns_helpers_isolated'] = False
    if attack in ('wrong_backup', 'wrong_old', 'wrong_new'):
        key = {'wrong_backup': 'backup_id', 'wrong_old': 'old_main_id', 'wrong_new': 'new_main_id'}[attack]
        policy[key] = 'f' * 32
    if attack == 'unchecked': policy['checks']['authority_reviewed'] = False
    if attack == 'hash': policy['plan_sha256'] = '0' * 64
    layout.policy.write_text(json.dumps(policy))
    if attack == 'writable': layout.policy.chmod(0o666)
    if attack == 'symlink':
        layout.policy.rename(layout.policy.with_suffix('.real'))
        layout.policy.symlink_to(layout.policy.with_suffix('.real'))
    with pytest.raises(Refused): controller(layout).install()
    assert not (layout.root / 'data').exists()
    assert json.loads(layout.audit.read_text().splitlines()[-1])['result'] == 'refused'


def test_running_services_and_audit_failure_precede_any_install(prepared, monkeypatch):
    from acc.backup_activation import Refused
    layout, plan = prepared
    approve(layout, plan)
    with pytest.raises(Refused): controller(layout, stopped=lambda: False).install()
    assert not (layout.root / 'data').exists()
    ctl = controller(layout)
    def failed(*args): raise OSError('audit unavailable')
    monkeypatch.setattr(ctl, 'audit', failed)
    with pytest.raises(OSError): ctl.install()
    assert not (layout.root / 'data').exists()


def test_changed_prepared_data_and_existing_destination_refused(prepared):
    from acc.backup_activation import Refused
    layout, plan = prepared
    approve(layout, plan)
    (layout.prepared / 'data/acc.db').write_bytes(b'changed')
    with pytest.raises(Refused): controller(layout).install()
    assert not (layout.root / 'data').exists()


def test_startup_refuses_tampered_state_even_without_hold(prepared):
    from acc.backup_activation import startup_blocked
    layout, plan = prepared
    approve(layout, plan)
    ctl = controller(layout)
    ctl.install(); ctl.activate(); ctl.release_control()
    (layout.root / 'data/RESTORE-HOLD').unlink()
    layout.state.write_text('{"phase":"active"}')
    assert startup_blocked(layout.root / 'data', layout=layout, owner=os.getuid())


async def test_identity_marker_blocks_app_before_migration_without_old_hold(source, tmp_path, monkeypatch):
    import dataclasses

    import httpx

    from acc.server.app import create_app
    dest = tmp_path / 'untrusted-replacement'
    (dest / 'data').mkdir(parents=True)
    (dest / 'data/REPLACEMENT-ID').write_text('a' * 32)
    def forbidden(*args, **kwargs): raise AssertionError('ordinary startup bypassed recovery gate')
    monkeypatch.setattr('acc.server.app.migrate', forbidden)
    app = create_app(dataclasses.replace(source.settings, data_dir=dest / 'data'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated') as client:
        assert (await client.get('/api/health')).status_code == 503
        assert (await client.post('/api/sessions')).status_code == 503


@pytest.fixture
def occupied_source(source):
    import base64

    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.models import LaunchSpec, Node, Project, Session
    sessions = make_sessionmaker(make_engine(source.settings.db_url))
    key = base64.b64encode(b'synthetic-node-public-key-32bytes!').decode()
    with sessions() as db:
        db.add(Node(id='a' * 32, name='borealis', status='active', public_key=key))
        for idx, host in [(1, 'local'), (2, 'borealis')]:
            project = Project(id=idx, slug=f'project-{idx}', name='Example', host=host, account='agent', workspace_path='/example/work', compose_autostart=True)
            spec = LaunchSpec(id=idx, provider='claude', inputs={}, digest=str(idx) * 64, files={})
            db.add_all([project, spec])
            db.flush()
            db.add(Session(id=str(idx) * 32, project_id=idx, provider='claude', name='Example', tmux_name=f'example-{idx}',
                           launch_spec_id=idx, run_as='agent', native_id='original-conversation', desired_state='running',
                           observed_state='working', automation={'enabled': True}))
        db.commit()
    return source, key


def test_preparation_holds_local_and_remote_and_preserves_installation_identity(occupied_source, tmp_path):
    from acc.backup_activation import prepare
    source, _ = occupied_source
    stage = source.capture()
    dest = tmp_path / 'prepared'
    plan = prepare(stage, source.kit(), stage.name, dest)
    with sqlite3.connect(dest / 'data/acc.db') as db:
        assert db.execute('select distinct desired_state,observed_state,automation from session').fetchall() == [('archived','archived',None)]
        assert db.execute('select distinct compose_autostart,git_backup from project').fetchall() == [(0,0)]
        assert db.execute('select status from node').fetchall() == [('revoked',)]
    identity = json.loads((dest / 'data/backup-service/identity.json').read_text())
    assert identity['installation_id'] == source.kit()['installation_id']
    assert identity['execution_machine_id'] == plan['new_main_id'] != source.kit()['execution_machine_id']
    assert identity['public_key'] != source.kit()['public_key']


def test_local_resume_and_wrong_node_identity_cannot_be_preapproved(occupied_source, tmp_path):
    from acc.backup_activation import Refused, prepare
    source, _ = occupied_source
    stage = source.capture()
    with pytest.raises(Refused, match='node identity'):
        prepare(stage, source.kit(), stage.name, tmp_path / 'wrong-key', reviewed_nodes={'a' * 32: '0' * 64})
    assert not (tmp_path / 'wrong-key').exists()
    with pytest.raises(Refused, match='session release'):
        prepare(stage, source.kit(), stage.name, tmp_path / 'local', resume_sessions=['1' * 32])
    assert not (tmp_path / 'local').exists()


def test_selected_remote_resume_needs_same_reviewed_node_key(occupied_source, tmp_path):
    import base64
    import hashlib

    from acc.backup_activation import prepare, resume_permitted
    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.models import Node, Session
    source, key = occupied_source
    stage = source.capture()
    dest = tmp_path / 'selected'
    fingerprint = hashlib.sha256(base64.b64decode(key)).hexdigest()
    prepare(stage, source.kit(), stage.name, dest, reviewed_nodes={'a' * 32: fingerprint}, resume_sessions=['2' * 32])
    sessions = make_sessionmaker(make_engine(f'sqlite:///{dest / "data/acc.db"}'))
    with sessions() as db:
        assert not resume_permitted(db, db.get(Session, '1' * 32))
        assert resume_permitted(db, db.get(Session, '2' * 32))
        db.get(Node, 'a' * 32).status = 'revoked'
        db.flush()
        assert not resume_permitted(db, db.get(Session, '2' * 32))


def test_authenticated_repository_restore_can_prepare_activation_without_installing(source, tmp_path, monkeypatch):
    target_id, repository_id, snapshot_id = 'a' * 32, 'b' * 64, 'c' * 64
    kit = source.kit()
    kit['targets'] = [{'id': target_id, 'password': 'synthetic-repository-key', 'repository_id': repository_id}]
    stage = source.capture()
    doc = json.loads((stage / 'manifest.json').read_text())['manifest']
    doc['targets'] = {target_id: {'repository_id': repository_id, 'key_epoch': 1}}
    source.sign(stage, doc)
    class Backend:
        def repository_id(self): return repository_id
        def command(self, *args): return json.dumps([{'id': snapshot_id, 'tags': [stage.name]}])
    def dump(backend, snapshot, archive):
        assert snapshot == snapshot_id
        backup.pack(stage, archive)
    monkeypatch.setattr(backup, '_bounded_dump', dump)
    dest = tmp_path / 'prepared-from-repository'
    result = backup.restore(kit, target_id, stage.name, dest, backend=Backend(), snapshot_id=snapshot_id,
                            prepare_replacement=True)
    plan = json.loads((dest / 'plan.json').read_text())
    assert plan['snapshot_id'] == snapshot_id and plan['repository_id'] == repository_id
    assert plan['backup_id'] == result['backup_id']
    assert (dest / 'data/RESTORE-HOLD').exists()


@pytest.mark.parametrize('step', ['activate', 'release_control'])
def test_fence_expiry_is_rechecked_at_each_transition(prepared, step):
    from acc.backup_activation import Refused
    layout, plan = prepared
    approve(layout, plan)
    ctl = controller(layout)
    ctl.install()
    if step == 'release_control': ctl.activate()
    policy = json.loads(layout.policy.read_text())
    policy['fence']['expires_at'] = time.time() - 1
    layout.policy.write_text(json.dumps(policy))
    with pytest.raises(Refused): getattr(ctl, step)()
    assert json.loads(layout.state.read_text())['phase'] != 'active'


def test_install_refuses_existing_data_and_partial_install_never_retries(prepared, monkeypatch):
    from acc.backup_activation import Refused
    layout, plan = prepared
    approve(layout, plan)
    (layout.root / 'data').mkdir()
    (layout.root / 'data/keep').write_text('existing')
    with pytest.raises(Refused): controller(layout).install()
    assert (layout.root / 'data/keep').read_text() == 'existing'
    (layout.root / 'data/keep').unlink(); (layout.root / 'data').rmdir()
    import acc.backup_activation as activation
    def failed(*args, **kwargs): raise OSError('disk-full-canary')
    monkeypatch.setattr(activation, '_stream', failed)
    with pytest.raises(Refused): controller(layout).install()
    assert json.loads(layout.state.read_text())['phase'] == 'installing'
    assert activation.startup_blocked(layout.root / 'data', layout=layout, owner=os.getuid())
    with pytest.raises(Refused, match='partial installation'): controller(layout).install()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink'])
def test_prepared_file_link_replacement_never_installs(prepared, kind):
    from acc.backup_activation import Refused
    layout, plan = prepared
    approve(layout, plan)
    path = layout.prepared / 'data/acc.db'
    path.rename(layout.prepared / 'database')
    if kind == 'symlink': path.symlink_to(layout.prepared / 'database')
    else: os.link(layout.prepared / 'database', path)
    with pytest.raises(Refused): controller(layout).install()
    assert not (layout.root / 'data').exists()


def test_installed_data_tampering_blocks_activation(prepared):
    from acc.backup_activation import Refused
    layout, plan = prepared
    approve(layout, plan)
    ctl = controller(layout)
    ctl.install()
    (layout.root / 'data/acc.db').write_bytes(b'changed')
    with pytest.raises(Refused, match='installed payload changed'): ctl.activate()
    assert json.loads(layout.state.read_text())['phase'] == 'installed'


async def test_fresh_manual_resume_consumes_remote_selection_and_refuses_local(occupied_source, tmp_path):
    import asyncio
    import base64
    import hashlib

    from acc.backup_activation import prepare
    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.models import Session, Setting
    from acc.server.supervisor import Supervisor
    from acc.server.supervisor_common import LaunchRefused
    source, key = occupied_source
    stage = source.capture()
    dest = tmp_path / 'selected-manual'
    prepare(stage, source.kit(), stage.name, dest,
            reviewed_nodes={'a' * 32: hashlib.sha256(base64.b64decode(key)).hexdigest()}, resume_sessions=['2' * 32])
    supervisor = object.__new__(Supervisor)
    supervisor.db = make_sessionmaker(make_engine(f'sqlite:///{dest / "data/acc.db"}'))
    supervisor._wake = asyncio.Event()
    # _resume has no broker effect: normal reconciliation still requires a fresh API request and active control.
    with pytest.raises(LaunchRefused, match='archived'):
        await supervisor._resume('1' * 32, 'owner')
    await supervisor._resume('2' * 32, 'owner')
    with supervisor.db() as db:
        restored = db.get(Session, '2' * 32)
        assert restored.desired_state == 'running' and restored.observed_state == 'recovering'
        assert restored.native_id == 'original-conversation'
        assert db.get(Setting, 'backup.replacement').value['resume_sessions'] == []
        assert db.get(Session, '1' * 32).desired_state == 'archived'


@pytest.mark.parametrize('relative', ['run/stale', 'publish/request.json', 'updates/requests/old.json', 'packages/requests/old.json'])
def test_preexisting_runtime_or_helper_intent_refuses_install(prepared, relative):
    from acc.backup_activation import Refused
    layout, plan = prepared
    approve(layout, plan)
    path = layout.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('old-intent')
    with pytest.raises(Refused, match='queues must be fresh'): controller(layout).install()
    assert path.read_text() == 'old-intent'
    assert not (layout.root / 'data').exists()


def test_stopped_service_check_uses_fixed_read_only_commands(monkeypatch):
    from types import SimpleNamespace

    from acc.backup_activation import stopped_services
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        text = b'accd.service loaded inactive dead\n' if argv[1] == 'list-units' else b'accd.service masked enabled\n'
        return SimpleNamespace(stdout=text)
    monkeypatch.setattr('acc.backup_activation.subprocess.run', run)
    assert stopped_services()
    assert all(argv[0] == '/usr/bin/systemctl' and argv[1] in ('list-units', 'list-unit-files') for argv in calls)
    monkeypatch.setattr('acc.backup_activation.subprocess.run', lambda *a, **kw: SimpleNamespace(stdout=b'accd.socket loaded active listening\n'))
    assert not stopped_services()


def test_signed_database_trigger_cannot_undo_quarantine(occupied_source, tmp_path):
    from acc.backup_activation import Refused, prepare
    source, _ = occupied_source
    with sqlite3.connect(source.settings.data_dir / 'acc.db') as db:
        db.execute("CREATE TRIGGER restart_after_quarantine AFTER UPDATE OF desired_state ON session "
                   "WHEN NEW.desired_state='archived' BEGIN UPDATE session SET desired_state='running' WHERE id=NEW.id; END")
    stage = source.capture()
    with pytest.raises(Refused, match='schema'):
        prepare(stage, source.kit(), stage.name, tmp_path / 'trigger-restore')
    assert not (tmp_path / 'trigger-restore').exists()


def test_quarantine_retains_pending_intents_without_replaying_them(occupied_source, tmp_path):
    from acc.backup_activation import prepare
    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.models import Observation, PublishRequest, Schedule, Setting, ToolRequest
    source, _ = occupied_source
    sessions = make_sessionmaker(make_engine(source.settings.db_url))
    with sessions() as db:
        db.add(PublishRequest(id='pending-publish', action='publish', host='demo.example.com', port=8100,
                              requested_by='alex', status='approved'))
        db.add(ToolRequest(id='pending-tool', owner='alex', account='agent', host='local', manager='uv', name='example',
                           operation='install', reason='example', status='approved'))
        db.add(Schedule(id='f' * 32, owner='alex', account='agent', launch={}, timing={}, timezone='UTC', enabled=True))
        db.add(Observation(key='server-backup.job.' + 'f' * 32,
                           value={'id': 'f' * 32, 'state': 'queued', 'action': 'capture'}, source='backup', observed_at=time.time()))
        db.add(Setting(key='remote_access_runtime', value={'desired_enabled': True, 'pending': True}))
        db.commit()
    stage = source.capture()
    dest = tmp_path / 'pending-held'
    prepare(stage, source.kit(), stage.name, dest)
    with sqlite3.connect(dest / 'data/acc.db') as db:
        assert db.execute('select distinct status from publish_request').fetchall() == [('failed',)]
        assert db.execute('select distinct status from tool_request').fetchall() == [('failed',)]
        assert db.execute('select enabled from schedule').fetchall() == [(0,)]
        assert db.execute("select value from setting where key='remote_access_runtime'").fetchone() is None
        assert db.execute("select count(*) from observation where key like 'server-backup.job.%'").fetchone() == (0,)
        archived = [json.loads(row[0]) for row in db.execute("select value from observation where key like 'recovery.quarantine.%'")]
        assert any(item['value'].get('state') == 'queued' and item['quarantined'] for item in archived)


@pytest.mark.parametrize('field,value', [('run_as', 'wrong-account'), ('native_id', 'invented-conversation')])
def test_remote_resume_binding_refuses_changed_account_or_conversation(occupied_source, tmp_path, field, value):
    import base64
    import hashlib

    from acc.backup_activation import prepare, resume_permitted
    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.models import Session
    source, key = occupied_source
    stage = source.capture()
    dest = tmp_path / 'binding'
    prepare(stage, source.kit(), stage.name, dest,
            reviewed_nodes={'a' * 32: hashlib.sha256(base64.b64decode(key)).hexdigest()}, resume_sessions=['2' * 32])
    sessions = make_sessionmaker(make_engine(f'sqlite:///{dest / "data/acc.db"}'))
    with sessions() as db:
        session = db.get(Session, '2' * 32)
        setattr(session, field, value)
        assert not resume_permitted(db, session)


@pytest.mark.parametrize('attack', ['disabled', 'writable_state', 'wrong_marker', 'missing_state'])
def test_active_startup_remains_fail_closed_after_control_metadata_changes(prepared, attack):
    from acc.backup_activation import startup_blocked
    layout, plan = prepared
    approve(layout, plan)
    ctl = controller(layout)
    ctl.install(); ctl.activate(); ctl.release_control()
    if attack == 'disabled':
        policy = json.loads(layout.policy.read_text()); policy['enabled'] = False
        layout.policy.write_text(json.dumps(policy))
    if attack == 'writable_state': layout.state.chmod(0o666)
    if attack == 'wrong_marker': (layout.root / 'data/REPLACEMENT-ID').write_text('f' * 32)
    if attack == 'missing_state': layout.state.unlink()
    assert startup_blocked(layout.root / 'data', layout=layout, owner=os.getuid())


def test_release_control_requires_its_own_root_approval(prepared):
    from acc.backup_activation import Refused
    layout, plan = prepared
    approve(layout, plan, release_control=False)
    ctl = controller(layout)
    ctl.install(); ctl.activate()
    with pytest.raises(Refused, match='explicit control release'): ctl.release_control()
    assert json.loads(layout.state.read_text())['phase'] == 'recovery'
    events = [json.loads(row) for row in layout.audit.read_text().splitlines()]
    assert events[-1]['result'] == 'refused'
    assert events[-1]['fence']['reference'] == 'DRILL-123'
    assert events[-1]['policy_sha256']


def test_audit_never_echoes_invalid_ids_or_payload_values(prepared):
    from acc.backup_activation import Refused
    layout, plan = prepared
    plan['backup_id'] = 'CREDENTIAL-CANARY-DO-NOT-LOG'
    (layout.prepared / 'plan.json').write_text(json.dumps(plan))
    approve(layout, plan)
    with pytest.raises(Refused): controller(layout).install()
    assert 'CREDENTIAL-CANARY' not in layout.audit.read_text()


def test_replacement_preparation_requires_recorded_snapshot_before_repository_io():
    from acc.backup_activation import Refused
    with pytest.raises(Refused, match='recorded snapshot'):
        backup.restore({}, 'a' * 32, 'b' * 32, __import__('pathlib').Path('/unused'), prepare_replacement=True)


def test_preparation_drops_restored_bearer_and_provider_tokens(source, tmp_path):
    """The old main's installation API token is owner authority without sign-in; provider tokens are old credentials."""
    from acc.backup_activation import prepare
    secrets = source.settings.data_dir / 'secrets'
    for name in ('api_token', 'setup_token', 'github_token', 'cloudflare_token'):
        (secrets / name).write_text('old-' + name)
        (secrets / name).chmod(0o600)
    stage = source.capture()
    assert {p.name for p in (stage / 'secrets').iterdir()} >= {'api_token', 'github_token', 'cloudflare_token'}
    dest = tmp_path / 'prepared'
    plan = prepare(stage, source.kit(), stage.name, dest)
    for name in ('api_token', 'setup_token', 'github_token', 'cloudflare_token'):
        assert not (dest / 'data/secrets' / name).exists()
        assert 'data/secrets/' + name not in plan['files']
    assert 'data/secrets/vault.key' in plan['files']  # restored vault data stays decryptable
    with sqlite3.connect(dest / 'data/acc.db') as db:
        detail = json.loads(db.execute("select detail from audit_log where action='backup.replacement_prepare'").fetchone()[0])
    assert detail['credentials_dropped'] == ['api_token', 'cloudflare_token', 'github_token', 'setup_token']
    # A fresh token is generated on first start; the old bearer value no longer matches.
    assert Settings(data_dir=dest / 'data').api_token() != 'old-api_token'


async def test_reconnecting_node_session_left_running_by_old_main_stays_quarantined(api, env, live, tmux_has, monkeypatch):  # noqa: F811
    """After activation a reviewed node comes back still running the old main's session: hold it, tell the owner."""
    import asyncio
    import base64
    import hashlib
    import uuid
    from types import SimpleNamespace

    import websockets
    from helpers import TOTP_AT, pause_supervisor, wait_for
    from sqlalchemy import select
    from test_auth import ORIGIN, PASSWORD, do_setup
    from test_multihost import StandInNode, _connected, keypair
    from test_supervisor import state_is

    from acc.backup_activation import quarantine
    from acc.server import auth
    from acc.server.models import AuditLog, Session, SessionRun

    secret = await do_setup(api, env)
    csrf = {'X-ACC-CSRF': api.cookies.get('acc_csrf'), **ORIGIN}
    added = (await api.post('/api/nodes', headers=csrf, json={'name': 'pc'})).json()
    key, public = keypair()
    node_id = (await api.post('/api/nodes/enroll', json={'name': 'pc', 'code': added['code'], 'public_key': public})).json()['node_id']
    link = asyncio.create_task(StandInNode(env['settings'].broker_socket, key, node_id).run(live))
    (env['workspaces'] / 'remote').mkdir()
    await wait_for(lambda: _connected(api))
    project = (await api.post('/api/projects', headers=csrf, json={'slug': 'remote', 'name': 'Remote', 'host': 'pc',
                                                                   'workspace_path': str(env['workspaces'] / 'remote')})).json()
    await api.post(f"/api/projects/{project['id']}/trust", headers=csrf)
    ids = {}
    for name in ('left-running', 'preapproved'):
        ids[name] = (await api.post('/api/sessions', headers=csrf, json={'project_id': project['id'], 'provider': 'claude',
                                                                         'name': name})).json()['id']
        await wait_for(state_is(api, ids[name], 'idle'), timeout=20)
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    with api.app.state.db() as db:
        tmux = {name: db.get(Session, acc_id).tmux_name for name, acc_id in ids.items()}

    # The old main is fenced; its node link drops while both agents keep running on the machine.
    link.cancel()
    await asyncio.gather(link, return_exceptions=True)
    await wait_for(lambda: asyncio.sleep(0, result=not supervisor.broker.hub.links.get('pc')
                                         or supervisor.broker.hub.links['pc'].closed))
    # The same quarantine preparation applies to a restored DB, with only "preapproved" selected for Resume.
    fingerprint = hashlib.sha256(base64.b64decode(public)).hexdigest()
    with sqlite3.connect(env['settings'].data_dir / 'acc.db') as db:
        quarantine(db, 'drill-backup', uuid.uuid4().hex, {node_id: fingerprint}, [ids['preapproved']])
        db.commit()

    # The reviewed node reconnects to the activated replacement.
    link = asyncio.create_task(StandInNode(env['settings'].broker_socket, key, node_id).run(live))
    try:
        await wait_for(lambda: _connected(api))
        await supervisor.reconcile()
        await supervisor.reconcile()
        with api.app.state.db() as db:
            for acc_id in ids.values():
                session = db.get(Session, acc_id)
                assert (session.desired_state, session.observed_state) == ('archived', 'archived')
                assert not list(db.scalars(select(SessionRun).where(SessionRun.session_id == acc_id,
                                                                    SessionRun.ended_at.is_(None))))  # not adopted
            noticed = list(db.scalars(select(AuditLog).where(AuditLog.action == 'session.replacement_held_running')))
            assert sorted(a.target for a in noticed) == sorted(ids.values())  # once each across both passes
        assert all(tmux_has(name) for name in tmux.values())  # not killed: still there to inspect

        # A fresh owner sign-in (restored sign-ins are gone) sees it, but cannot resume it or type into it.
        monkeypatch.setattr(auth, 'time', SimpleNamespace(time=lambda: TOTP_AT + 30, monotonic=time.monotonic))
        anon = {'Authorization': '', **ORIGIN}
        signed = await api.post('/api/auth/login', headers=anon, json={'username': 'casey', 'password': PASSWORD,
                                                                       'code': auth.totp(secret, TOTP_AT + 30)})
        assert signed.status_code == 200, signed.text
        owner = {**anon, 'X-ACC-CSRF': api.cookies.get('acc_csrf')}
        items = (await api.get('/api/attention', headers=owner)).json()
        held_items = [i for i in items if i['kind'] == 'replacement_held']
        assert {i['session_id'] for i in held_items} == set(ids.values())
        assert all('still running on pc' in i['title'] for i in held_items)
        left = ids['left-running']
        assert (await api.post(f'/api/sessions/{left}/resume', headers=owner)).status_code >= 400
        assert (await api.post(f'/api/sessions/{left}/message', headers=owner, json={'text': 'continue'})).status_code >= 400
        headers = {'Cookie': f"acc_session={api.cookies.get('acc_session')}", 'Origin': ORIGIN['Origin']}
        async with websockets.connect(f'{live}/api/sessions/{left}/tty?mode=write&take=1', additional_headers=headers) as ws:
            assert json.loads(await asyncio.wait_for(ws.recv(), 5)) == {'t': 'mode', 'write': False}
        with api.app.state.db() as db:
            assert db.get(Session, left).desired_state == 'archived'

        # Stop on that machine: owner only, freshly verified; one stop for that one pane, audited. Nothing stopped yet.
        assert all(tmux_has(name) for name in tmux.values())
        assert (await api.post(f'/api/sessions/{left}/stop-held')).status_code == 403  # the API token is not a person
        assert (await api.post(f'/api/sessions/{left}/stop-held', headers=owner)).status_code == 403  # not verified again
        assert all(tmux_has(name) for name in tmux.values())
        monkeypatch.setattr(auth, 'time', SimpleNamespace(time=lambda: TOTP_AT + 60, monotonic=time.monotonic))
        verified = await api.post('/api/auth/passkeys/reauth', headers=owner,
                                  json={'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT + 60)})
        assert verified.status_code == 200, verified.text
        stopped = await api.post(f'/api/sessions/{left}/stop-held', headers=owner)
        assert stopped.status_code == 200, stopped.text
        await wait_for(lambda: asyncio.sleep(0, result=not tmux_has(tmux['left-running'])))
        assert tmux_has(tmux['preapproved'])  # only that one session
        with api.app.state.db() as db:
            assert db.get(Session, left).desired_state == 'archived'
            outcomes = [a.detail for a in db.scalars(select(AuditLog).where(
                AuditLog.action == 'session.replacement_held_stop', AuditLog.target == left).order_by(AuditLog.id))]
            assert outcomes == [{'outcome': 'refused', 'reason': 'verification_required'}] * 2 + [{'node': 'pc', 'outcome': 'stopped'}]
        items = (await api.get('/api/attention', headers=owner)).json()
        assert [i['session_id'] for i in items if i['kind'] == 'replacement_held'] == [ids['preapproved']]

        # Only the preapproved conversation can be released, by that fresh Resume, and the audit says so.
        chosen = ids['preapproved']
        resumed = await api.post(f'/api/sessions/{chosen}/resume', headers=owner)
        assert resumed.status_code == 200, resumed.text
        with api.app.state.db() as db:
            audit = db.scalar(select(AuditLog).where(AuditLog.action == 'session.resume', AuditLog.target == chosen))
            assert audit.detail == {'replacement_exception': True, 'node': 'pc'}
        assert (await api.post(f'/api/sessions/{chosen}/stop-held', headers=owner)).status_code == 409  # no longer held
    finally:
        link.cancel()
        await asyncio.gather(link, return_exceptions=True)


def test_every_install_intent_has_a_result_even_when_already_installed(prepared):
    layout, plan = prepared
    approve(layout, plan)
    ctl = controller(layout)
    ctl.install()
    assert ctl.install()['phase'] == 'installed'
    events = [(e['action'], e['result']) for e in map(json.loads, layout.audit.read_text().splitlines())]
    assert events == [('install', 'intent'), ('install', 'complete'), ('install', 'intent'), ('install', 'unchanged')]


def test_restored_telemetry_waits_for_the_owner_instead_of_sending(source, tmp_path):
    """Approved items would be sent under the old installation's key; deletions the owner asked for still go."""
    from acc.backup_activation import prepare
    with sqlite3.connect(source.settings.data_dir / 'acc.db') as db:
        for item_id, kind, state in [('r', 'reports', 'approved'), ('u', 'usage', 'retry'), ('d', 'delete', 'approved'),
                                     ('q', 'reports', 'queued'), ('s', 'usage', 'sent')]:
            db.execute('insert into telemetry_item(id,kind,key_id,body,state,created,due,attempts,result,receipt,occurrences) '
                       "values (?,?,'key','{}',?,0,0,0,'','',1)", (item_id, kind, state))
    stage = source.capture()
    prepare(stage, source.kit(), stage.name, tmp_path / 'prepared')
    with sqlite3.connect(tmp_path / 'prepared/data/acc.db') as db:
        assert dict(db.execute('select id,state from telemetry_item')) == {
            'r': 'queued', 'u': 'discarded', 'd': 'approved', 'q': 'queued', 's': 'sent'}


def test_replacement_cli_names_the_gate_that_refused(monkeypatch, capsys):
    # About 30 fixed, secret-free reasons used to collapse into one line, and the audit recorded only "refused".
    from acc import backup_activation
    monkeypatch.setattr(backup_activation.os, 'geteuid', lambda: 1000)
    assert backup_activation.main(['status']) == 1
    error = capsys.readouterr().err
    assert "Run this as root at the machine's local console." in error and 'inspect the offline policy' in error


def test_replacement_cli_has_no_unchecked_prepare(capsys):
    # The reviewed path is backup_server restore --prepare-replacement (snapshot ID and repository binding).
    from acc import backup_activation
    with pytest.raises(SystemExit):
        backup_activation.main(['prepare', '--stage', '/x', '--kit', '/x', '--backup-id', 'x', '--destination', '/x'])
    capsys.readouterr()


def test_restore_cli_names_the_refusal_but_never_other_errors(tmp_path, capsys):
    from acc import backup_server
    kit = tmp_path / 'kit.json'
    kit.write_text('{}')
    common = ['restore', '--kit', str(kit), '--target-id', 'a' * 32, '--backup-id', 'b' * 32, '--destination', str(tmp_path / 'out')]
    assert backup_server.main([*common, '--resume-session', 'c' * 32]) == 1
    error = capsys.readouterr().err
    assert 'node/session selections require replacement preparation' in error and 'check the kit' in error
    kit.write_text('{not json /private/path')
    assert backup_server.main(common) == 1
    error = capsys.readouterr().err
    assert 'check the kit' in error and '/private/path' not in error and 'Expecting' not in error


async def test_an_unconfirmed_held_stop_says_what_to_check(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from acc import backup_activation
    from acc.server import supervisor as supervisor_module
    from acc.server.broker_client import BrokerError
    from acc.server.supervisor import LaunchRefused, Supervisor
    session = SimpleNamespace(management='acc', host='pc', run_as='agent')
    rows = []
    @contextmanager
    def db():
        yield SimpleNamespace(rollback=lambda: None, add=rows.append, commit=lambda: None)
    async def call(*a, **kw):
        raise BrokerError('kill: tmux socket /tmp/tmux-1001/default: permission denied')
    fake = SimpleNamespace(db=db, _get=lambda d, i: session, settings=None, broker=SimpleNamespace(call=call))
    monkeypatch.setattr(backup_activation, 'held', lambda d, s: True)
    monkeypatch.setattr(supervisor_module, 'connected', lambda broker, host: True)
    monkeypatch.setattr(supervisor_module, 'kill_params', lambda s, settings: {})
    with pytest.raises(LaunchRefused) as caught:
        await Supervisor.stop_held(fake, 'a' * 32, 'casey')
    assert '/tmp/tmux' not in str(caught.value) and 'pc' in str(caught.value) and 'before trying again' in str(caught.value)
    assert rows[0].detail == {'node': 'pc', 'outcome': 'failed'}


@pytest.mark.parametrize('unexpected', [False, True])
def test_restore_cli_default_binary_and_safe_backend_diagnostics(tmp_path, monkeypatch, capsys, unexpected):
    from pathlib import Path
    kit = tmp_path / 'kit.json'
    kit.write_text('{}')
    calls = []

    def refused(*args, **kwargs):
        calls.append(kwargs['binary'])
        if unexpected:
            raise RuntimeError('secret-token /private/repository')
        raise backup.Refused('recovery kit does not contain this target')

    monkeypatch.setattr(backup, 'restore', refused)
    assert backup.main(['restore', '--kit', str(kit), '--target-id', 'a' * 32,
                        '--backup-id', 'b' * 32, '--destination', str(tmp_path / 'out')]) == 1
    assert calls == [Path('/usr/local/bin/restic')]
    error = capsys.readouterr().err
    assert ('recovery kit does not contain this target' in error) is not unexpected
    assert 'operation refused' in error
    assert 'secret-token' not in error and '/private/repository' not in error


def test_replacement_preparation_accepts_historical_default_quote_spelling(source, tmp_path):
    import re

    from acc.backup_activation import prepare
    with sqlite3.connect(source.settings.data_dir / 'acc.db') as db:
        db.execute('PRAGMA writable_schema=ON')
        for table, column in [('auth_session', 'passkey_verified'), ('project', 'containers'), ('session', 'container')]:
            ddl = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
            # Flip only quote spelling, regardless of which spelling the current
            # migration path emits; no real installation or exported data needed.
            pattern = rf"({column} BOOLEAN DEFAULT )('?)([01])\2"
            def flip(match):
                literal = match[3] if match[2] else f"'{match[3]}'"
                return match[1] + literal
            changed, count = re.subn(pattern, flip, ddl)
            assert count == 1, ddl
            db.execute('UPDATE sqlite_master SET sql=? WHERE name=?', (changed, table))
    stage = source.capture()
    plan = prepare(stage, source.kit(), stage.name, tmp_path / 'prepared')
    assert plan['backup_id'] == stage.name
