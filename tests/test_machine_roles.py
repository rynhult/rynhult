"""Machine policy: defaults, durable outages, and quiet disconnected passes."""
from datetime import datetime, timedelta, timezone

import pytest
from isolated_settings import isolated_settings
from sqlalchemy import select

from acc.server.db import make_engine, make_sessionmaker, migrate
from acc.server.models import AttentionItem, AuditLog, Node


@pytest.mark.parametrize('changes,due', [
    ({}, True),
    ({'idle_since': 29}, False),
    ({'last_output_at': 1}, False),
    ({'activity_at': 1}, False),
    ({'observed_state': 'active'}, False),
    ({'observed_state': 'waiting_input'}, False),
    ({'observed_state': 'starting'}, False),
    ({'desired_state': 'stopped'}, False),
    ({'management': 'external'}, False),
    ({'native_id': None}, False),
    ({'idle_since': None}, False),
    ({'role': 'always_on'}, False),
    ({'status': 'revoked'}, False),
    ({'idle_sleep_minutes': None}, False),
])
def test_sleep_requires_resumable_confirmed_idle_workstation(changes, due):
    from types import SimpleNamespace

    from acc.server.machine_presence import sleep_due
    at = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    node = dict(role='workstation', status='active', idle_sleep_minutes=30)
    session = dict(observed_state='idle', desired_state='running', management='acc', native_id='native',
                   idle_since=at - timedelta(minutes=30), last_output_at=None, activity_at=None)
    for key, value in changes.items():
        if key in node:
            node[key] = value
        else:
            session[key] = at - timedelta(minutes=value) if isinstance(value, int) else value
    assert sleep_due(SimpleNamespace(**session), SimpleNamespace(**node), at) is due


def test_idle_clock_survives_polling_but_resets_after_work(database, env):
    from acc.server.models import LaunchSpec, Project, Session
    from acc.server.supervisor import Supervisor
    supervisor = Supervisor(env['settings'], database, None)
    with database() as db:
        session = Session(id='clock', project=Project(slug='demo', name='Demo', workspace_path='/tmp/demo', account='agent'),
                          provider='copilot', name='Clock', tmux_name='clock', run_as='agent',
                          launch_spec=LaunchSpec(provider='copilot', inputs={}, files={}, digest='test'),
                          observed_state='active')
        db.add(session)
        db.flush()
        supervisor._set_state(db, session, 'idle', 'copilot_hook')
        first = session.idle_since
        assert first is not None
        supervisor._set_state(db, session, 'idle', 'tmux')
        assert session.idle_since == first
        supervisor._set_state(db, session, 'active', 'copilot_hook')
        assert session.idle_since is None
        supervisor._set_state(db, session, 'idle', 'copilot_hook')
        assert session.idle_since >= first


async def test_sleep_policy_persists_and_host_names_idle_sessions(api):
    from helpers import pause_supervisor

    from acc.server.models import LaunchSpec, Project, Session
    await pause_supervisor(api)
    await api.post('/api/nodes', json={'name': 'demo-desk', 'role': 'workstation'})
    assert (await api.get('/api/nodes')).json()[0].get('idle_sleep_minutes', 'missing') is None
    response = await api.post('/api/nodes/demo-desk/policy', json={
        'role': 'workstation', 'idle_sleep_minutes': 30})
    assert response.status_code == 200, response.text
    assert response.json()['idle_sleep_minutes'] == 30
    for invalid in (0, -1, 10081, 1.5):
        assert (await api.post('/api/nodes/demo-desk/policy', json={
            'role': 'workstation', 'idle_sleep_minutes': invalid})).status_code == 422
    with api.app.state.db() as db:
        project = Project(slug='sleep-demo', name='Demo', host='demo-desk', workspace_path='/tmp/demo', account='agent')
        for name, state in [('Idle work', 'idle'), ('Busy work', 'active'), ('Needs Alex', 'waiting_input')]:
            db.add(Session(id=name, project=project, provider='copilot', name=name, tmux_name=name,
                           run_as='agent', desired_state='running', observed_state=state,
                           launch_spec=LaunchSpec(provider='copilot', inputs={}, files={}, digest='test')))
        db.commit()
    node = (await api.get('/api/nodes')).json()[0]
    assert node['sleep']['running_sessions'] == 3
    assert node['sleep']['idle_sessions'] == [{'id': 'Idle work', 'name': 'Idle work'}]
    assert (await api.post('/api/nodes/demo-desk/policy', json={
        'role': 'workstation', 'idle_sleep_minutes': None})).json()['idle_sleep_minutes'] is None
    with api.app.state.db() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'node.policy')).detail['idle_sleep_minutes'] == 30


@pytest.mark.parametrize('fresh', ['idle', 'active', 'waiting_input', 'output', 'disconnected', 'always_on', 'disabled',
                                 'screen_active', 'parse_error', 'partial_spool', 'read_error', 'kill_error',
                                 'spool_full', 'idle_without_clock', 'missing_output', 'role_changed', 'spool_backlog'])
async def test_sleep_pause_rechecks_machine_and_session_then_audits_and_resumes(api, fresh):
    import base64
    import json
    from types import SimpleNamespace

    from helpers import pause_supervisor

    from acc.server.broker_client import BrokerError
    from acc.server.models import LaunchSpec, Project, Session, SessionRun
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    at = datetime.now(timezone.utc)
    with api.app.state.db() as db:
        db.add(Node(id='sleep-node', name='demo-desk', status='active',
                    role='always_on' if fresh == 'always_on' else 'workstation',
                    idle_sleep_minutes=None if fresh == 'disabled' else 30))
        project = Project(slug='sleep-demo', name='Demo', host='demo-desk', workspace_path='/tmp/demo', account='agent')
        session = Session(id='sleep-session', project=project, provider='copilot', name='Idle work', tmux_name='sleep',
                          native_id='01234567-abcd', run_as='agent', desired_state='running', observed_state='idle',
                          idle_since=None if fresh == 'idle_without_clock' else at - timedelta(minutes=31),
                          launch_spec=LaunchSpec(provider='copilot', inputs={}, files={}, digest='test'))
        session.runs.append(SessionRun(reason='launch', verified_at=at))
        db.add(session)
        db.commit()
    supervisor.broker.hub.links['demo-desk'] = SimpleNamespace(
        closed=fresh == 'disconnected', accounts=['agent'], packages={})
    if fresh in ('spool_full', 'spool_backlog'):
        mirror = supervisor.settings.data_dir / 'remote-spool' / 'sleep-session'
        mirror.mkdir(parents=True)
        if fresh == 'spool_full':
            (mirror / 'mirror-full').touch()
        else:
            from acc.server.ingest import SPOOL_CHUNK
            (mirror / 'events.jsonl').write_bytes(b'\n' * (SPOOL_CHUNK + 1))
    async def call(method, **params):
        if method == 'read_spool':
            if fresh == 'role_changed':
                with api.app.state.db() as db:
                    db.get(Node, 'sleep-node').role = 'always_on'
                    db.commit()
            if fresh == 'read_error':
                raise BrokerError('cannot read current hooks')
            event = {'active': 'UserPromptSubmit', 'spool_full': 'UserPromptSubmit', 'spool_backlog': 'UserPromptSubmit',
                     'waiting_input': 'PermissionRequest'}.get(fresh)
            data = (json.dumps({'provider': 'copilot', 'hook': {'hook_event_name': event}}) + '\n').encode() if event else b''
            if fresh == 'parse_error':
                data = b'not-json\n'
            if fresh == 'partial_spool':
                data = b'{"provider":'
            return {'data': base64.b64encode(data).decode()}
        if method == 'list':
            return [{'tmux_name': 'sleep', 'dead': False,
                     'last_output': None if fresh == 'missing_output' else at.timestamp() if fresh == 'output'
                     else (at - timedelta(hours=1)).timestamp()}]
        if method == 'screen_prompt':
            return {'prompt': 'Allow command?' if fresh == 'waiting_input' else None, 'working': fresh == 'screen_active'}
        if method == 'kill' and fresh == 'kill_error':
            raise BrokerError('kill failed')
        if method in ('kill', 'clear_env'):
            return {}
        raise AssertionError(method)
    supervisor.broker.call = call
    try:
        await supervisor.pause_idle_workstations()
        await supervisor.pause_idle_workstations()
        with api.app.state.db() as db:
            session = db.get(Session, 'sleep-session')
            audits = list(db.scalars(select(AuditLog).where(AuditLog.action == 'session.sleep_pause')))
            if fresh not in ('idle', 'kill_error'):
                assert session.desired_state == 'running'
                assert not audits
                if fresh == 'idle_without_clock':
                    assert session.idle_since >= at
                return
            assert session.desired_state == 'stopped' and session.observed_state == 'stopped'
            assert session.sleep_paused_at is not None
            assert session.native_id == '01234567-abcd'
            assert session.runs[-1].end_reason == 'machine_sleep'
            assert len(audits) == 1
            assert audits[0].detail == {'machine': 'demo-desk', 'idle_minutes': 30}
        node = (await api.get('/api/nodes')).json()[0]
        if fresh == 'kill_error':
            assert node['sleep']['paused_sessions'][0]['stop_pending'] is True
            assert node['sleep']['running_sessions'] == 1
            assert (await api.get('/api/sessions/sleep-session')).json()['last_error'].startswith('stop not confirmed:')
            assert (await api.post('/api/sessions/sleep-session/resume')).status_code == 409
            with api.app.state.db() as db:
                await supervisor._reconcile_one(db, db.get(Session, 'sleep-session'), None)
                db.commit()
            node = (await api.get('/api/nodes')).json()[0]
        assert node['sleep']['running_sessions'] == 0
        assert node['sleep']['paused_sessions'] == [{'id': 'sleep-session', 'name': 'Idle work'}]
        assert (await api.post('/api/sessions/sleep-session/resume')).status_code == 200
        with api.app.state.db() as db:
            session = db.get(Session, 'sleep-session')
            assert session.desired_state == 'running' and session.sleep_paused_at is None
            assert session.native_id == '01234567-abcd'
    finally:
        supervisor.broker.hub.links.pop('demo-desk', None)


@pytest.fixture
def database(tmp_path):
    url = f"sqlite:///{tmp_path / 'roles.db'}"
    migrate(url)
    engine = make_engine(url)
    yield make_sessionmaker(engine)
    engine.dispose()


@pytest.mark.parametrize('info,role', [({'os': 'linux'}, 'always_on'), ({'os': 'linux', 'wsl': 'False'}, 'always_on'), ({'os': 'darwin'}, 'workstation'),
    ({'os': 'windows'}, 'workstation'), ({'os': 'linux', 'wsl': True}, 'workstation'),
    ({'os': 'linux', 'release': 'microsoft-standard-WSL2'}, 'workstation')])
def test_os_defaults(info, role):
    from acc.server.machine_presence import default_role
    assert default_role(info) == role


def test_offline_threshold_snooze_reconnect_and_audit(database):
    from acc.server.machine_presence import evaluate, transition
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with database() as db:
        node = Node(id='a', name='cloud', status='active', role='always_on', last_seen=start)
        db.add(node)
        db.flush()
        transition(db, node, False, start)
        evaluate(db, node, start + timedelta(minutes=9))
        assert db.scalar(select(AttentionItem)) is None
        evaluate(db, node, start + timedelta(minutes=10))
        db.flush()
        item = db.scalar(select(AttentionItem))
        assert item.kind == 'machine_offline' and item.resolved_at is None
        node.snoozed_until = start + timedelta(hours=1)
        evaluate(db, node, start + timedelta(minutes=11))
        assert item.snoozed_until == node.snoozed_until
        transition(db, node, True, start + timedelta(minutes=20))
        assert item.resolved_at is not None
        assert node.last_downtime_seconds == 1200
        assert node.offline_since is None
        db.flush()
        actions = list(db.scalars(select(AuditLog.action)))
        assert actions == ['node.offline', 'node.offline_alert', 'node.reconnect']


def test_workstation_hour_is_neutral_and_logs_only_transitions(database, caplog):
    from acc.server.machine_presence import evaluate, transition
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with database() as db:
        node = Node(id='w', name='desk', status='active', role='workstation', last_seen=start)
        db.add(node)
        db.flush()
        with caplog.at_level('INFO'):
            for seconds in range(0, 3601, 6):
                transition(db, node, False, start + timedelta(seconds=seconds))
                evaluate(db, node, start + timedelta(seconds=seconds))
            transition(db, node, True, start + timedelta(hours=1))
        db.flush()
        assert db.scalar(select(AttentionItem)) is None
        assert len([r for r in caplog.records if 'desk' in r.message]) == 2
        assert node.last_downtime_seconds == 3600


async def test_disconnected_passes_never_contact_brokers_or_spools(database, tmp_path):
    from acc.server.broker_client import Brokers
    from acc.server.models import LaunchSpec, Project, Session
    from acc.server.nodes import NodeHub
    from acc.server.supervisor import Supervisor
    hub = NodeHub()
    broker = Brokers(tmp_path / 'absent.sock', None, 'agent', 'services', hub)
    supervisor = Supervisor(isolated_settings(tmp_path, data_dir=tmp_path, run_dir=tmp_path), database, broker)
    with database() as db:
        project = Project(slug='remote', name='Remote', workspace_path='/tmp/demo', host='desk', account='agent')
        db.add(project)
        db.flush()
        db.add(Session(id='s', project_id=project.id, provider='claude', name='Work', tmux_name='s',
                       launch_spec=LaunchSpec(provider='claude', inputs={}, files={}, digest='test'),
                       run_as='agent', desired_state='running', observed_state='idle'))
        db.commit()
    calls = []
    async def call(method, **params):
        calls.append((method, params.get('host')))
        return []
    broker.call = call
    for _ in range(3):
        await supervisor.reconcile()
        await supervisor.fetch_remote_spools()
    assert all(host == 'local' for _, host in calls)


async def test_role_settings_are_owner_controlled_and_survive_os_reports(api):
    from test_multihost import keypair
    response = await api.post('/api/nodes', json={'name': 'home-server', 'role': 'always_on'})
    assert response.status_code == 201
    added = response.json()
    _, public = keypair()
    assert (await api.post('/api/nodes/enroll', json={'name': 'home-server', 'code': added['code'],
        'public_key': public, 'info': {'os': 'darwin'}})).status_code == 200
    rows = (await api.get('/api/nodes')).json()
    assert rows[0]['role'] == 'always_on'
    changed = await api.post('/api/nodes/home-server/policy', json={'role': 'workstation', 'offline_threshold_seconds': 120, 'snooze_minutes': 60})
    assert changed.status_code == 200, changed.text
    assert changed.json()['role'] == 'workstation'
    assert changed.json()['snoozed_until']
    assert (await api.post('/api/nodes/home-server/policy', json={'role': 'invalid'})).status_code == 422


async def test_check_machines_reports_offline_without_dialing_and_rate_limits(api):
    await api.post('/api/nodes', json={'name': 'desk'})
    result = await api.post('/api/nodes/check')
    assert result.status_code == 200, result.text
    rows = result.json()['machines']
    assert next(row for row in rows if row['name'] == 'desk')['state'] == 'offline'
    assert next(row for row in rows if row['name'] == 'local')['state'] == 'responding'
    assert (await api.post('/api/nodes/check')).status_code == 429


async def test_machine_check_refreshes_snapshot_and_version(api):
    response = await api.post('/api/nodes/check')
    assert response.status_code == 200
    local = response.json()['machines'][0]
    assert local['latency_ms'] >= 0
    assert local['info']['version']
    assert local['snapshot']['memory']['total'] > 0
    assert (await api.get('/api/nodes/check')).json()['machines'][0]['snapshot_available']


def test_migration_backfills_existing_os_defaults(tmp_path):
    import json
    import sqlite3

    from alembic import command
    from alembic.script import ScriptDirectory

    from acc.server.db import alembic_config
    path = tmp_path / 'legacy.db'
    config = alembic_config(f'sqlite:///{path}')
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1
    command.upgrade(config, '0041')
    with sqlite3.connect(path) as connection:
        for name, info in [('cloud', {'os': 'linux'}), ('mac', {'os': 'darwin'}), ('pc', {'os': 'windows'}),
                           ('wsl', {'os': 'linux', 'release': 'microsoft-WSL2'}),
                           ('wsl2', {'os': 'linux', 'wsl': '2'})]:
            connection.execute('INSERT INTO node (id, name, status, info, created_at) VALUES (?, ?, ?, ?, ?)',
                               (name, name, 'active', json.dumps(info), '2026-01-01'))
        connection.execute("INSERT INTO node (id, name, status, info, created_at) VALUES "
                           "('pending', 'pending', 'pending', '{}', '2026-01-01')")
    command.upgrade(config, 'head')
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT name, role, offline_threshold_seconds FROM node ORDER BY name').fetchall() == [
            ('cloud', 'always_on', 600), ('mac', 'workstation', 600), ('pc', 'workstation', 600),
            ('pending', None, 600), ('wsl', 'workstation', 600), ('wsl2', 'workstation', 600)]
    command.downgrade(config, '0041')
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT version_num FROM alembic_version').fetchone() == ('0041',)
        assert connection.execute('SELECT count(*) FROM node').fetchone() == (6,)
        assert 'role' not in {row[1] for row in connection.execute('PRAGMA table_info(node)')}
        assert 'snoozed_until' not in {row[1] for row in connection.execute('PRAGMA table_info(attention_item)')}
    command.upgrade(config, 'head')
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT role FROM node WHERE name='mac'").fetchone() == ('workstation',)


async def test_policy_rejects_out_of_bounds_thresholds_and_snoozes(api):
    await api.post('/api/nodes', json={'name': 'cloud'})
    for field, values in [('offline_threshold_seconds', [-1, 0, 59, 604801, 10**100, 60.5, 'NaN', 'Infinity']),
                          ('snooze_minutes', [-1, 43201, 10**100, 0.5])]:
        for value in values:
            response = await api.post('/api/nodes/cloud/policy', json={'role': 'always_on', field: value})
            assert response.status_code == 422, (field, value, response.text)
    for threshold, snooze in [(60, 0), (604800, 43200)]:
        response = await api.post('/api/nodes/cloud/policy', json={
            'role': 'always_on', 'offline_threshold_seconds': threshold, 'snooze_minutes': snooze})
        assert response.status_code == 200, response.text
        assert response.json()['offline_threshold_seconds'] == threshold


async def test_node_hello_cannot_spoof_owner_policy_or_last_seen(api, live):  # noqa: F811
    import base64
    import json

    import websockets
    from helpers import pause_supervisor, wait_for
    from test_multihost import enroll

    await pause_supervisor(api)
    key, node_id = await enroll(api)
    assert (await api.post('/api/nodes/pc/policy', json={
        'role': 'always_on', 'offline_threshold_seconds': 120, 'snooze_minutes': 60})).status_code == 200
    start = datetime.now(timezone.utc)
    forged = {'public_git_backup': True, 'role': 'workstation', 'offline_threshold_seconds': 0, 'snoozed_until': '2099-01-01T00:00:00Z',
              'last_seen': '2099-01-01T00:00:00Z', 'offline_since': None, 'offline_alert': False}
    async with websockets.connect(f'{live}/api/nodes/connect') as ws:
        challenge = json.loads(await ws.recv())
        signature = key.sign(base64.b64decode(challenge['nonce']) + challenge['primary'].encode())
        await ws.send(json.dumps({**forged, 't': 'hello', 'node_id': node_id,
                                  'signature': base64.b64encode(signature).decode(),
                                  'info': {'os': 'darwin', **forged}, 'accounts': ['agent']}))
        assert json.loads(await ws.recv()) == {'t': 'ready'}
        await ws.send(json.dumps({'t': 'ping', **forged}))
        assert json.loads(await ws.recv()) == {'t': 'pong'}
        with api.app.state.db() as db:
            node = db.get(Node, node_id)
            assert node.public_git_backup is False
            assert node.role == 'always_on' and node.offline_threshold_seconds == 120
            assert node.snoozed_until < start + timedelta(hours=2)
            assert start <= node.last_seen <= datetime.now(timezone.utc)
            assert node.offline_since is None
            assert not (set(forged) & node.info.keys())

    async def offline():
        return not (await api.get('/api/nodes')).json()[0]['connected']
    await wait_for(offline)
    with api.app.state.db() as db:
        assert db.get(Node, node_id).offline_since >= start


def test_offline_session_and_project_expose_neutral_presence(database):
    from acc.server.api.schemas import project_out, session_out
    from acc.server.models import LaunchSpec, Project, Session
    with database() as db:
        db.add(Node(id='n', name='desk', status='active', role='workstation', offline_since=datetime.now(timezone.utc)))
        project = Project(slug='demo', name='Demo', host='desk', workspace_path='/tmp/demo', account='agent')
        session = Session(id='s', project=project, provider='claude', name='Work', tmux_name='s', run_as='agent',
                          launch_spec=LaunchSpec(provider='claude', inputs={}, files={}, digest='test'))
        db.add(session)
        db.flush()
        assert project_out(project)['machine_offline'] is True
        assert session_out(session)['machine_offline'] is True


from test_auth import live  # noqa: F401


async def test_fake_link_reconnect_resolves_outage_and_manual_roundtrip(api, env, live):  # noqa: F811
    import asyncio

    from helpers import pause_supervisor, wait_for, write_db
    from test_multihost import StandInNode, enroll

    await pause_supervisor(api)
    key, node_id = await enroll(api)
    node = StandInNode(env['settings'].broker_socket, key, node_id)
    task = asyncio.create_task(node.run(live))
    async def online():
        return (await api.get('/api/nodes')).json()[0]['connected']
    try:
        await wait_for(online)
        result = (await api.post('/api/nodes/check')).json()
        remote = next(row for row in result['machines'] if row['name'] == 'pc')
        assert remote['state'] == 'responding' and remote['latency_ms'] >= 0
        assert remote['info']['version'] and remote['snapshot']['memory']['total'] > 0
        await node.ws.close()
        await task
        async def offline():
            return not await online()
        await wait_for(offline)
        with write_db(api.app.state.db) as db:
            row = db.get(Node, node_id)
            assert row.offline_since
            row.offline_since = datetime.now(timezone.utc) - timedelta(minutes=11)
            db.commit()
        supervisor = api.app.state.supervisor
        await supervisor.evaluate_machines()
        items = (await api.get('/api/attention')).json()
        alert = next(item for item in items if item['kind'] == 'machine_offline')
        assert alert['resolved_at'] is None
        assert (await api.post('/api/nodes/pc/policy', json={'role': 'always_on', 'snooze_minutes': 60})).status_code == 200
        assert not any(item['kind'] == 'machine_offline' for item in (await api.get('/api/attention')).json())
        supervisor._host_backoff['pc'] = (3, asyncio.get_running_loop().time() + 3600)
        task = asyncio.create_task(node.run(live))
        await wait_for(online)
        assert 'pc' not in supervisor._host_backoff
        history = (await api.get('/api/attention?scope=all')).json()
        resolved = next(item for item in history if item['id'] == alert['id'])
        assert resolved['resolved_at'] and 'seconds offline' in resolved['detail']
        rows = (await api.get('/api/nodes')).json()
        assert rows[0]['last_downtime_seconds'] >= 660
        with api.app.state.db() as db:
            actions = list(db.scalars(select(AuditLog.action).where(AuditLog.target == 'pc')))
            assert {'node.offline', 'node.reconnect', 'node.offline_alert', 'node.policy'} <= set(actions)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_offline_machine_requests_are_neutral_conflicts(tmp_path):
    from acc.server.api.context import Context
    from acc.server.broker_client import Brokers
    from acc.server.nodes import NodeHub
    broker = Brokers(tmp_path / 'broker.sock', None, 'agent', 'services', NodeHub())
    with pytest.raises(Exception) as error:
        broker.get('agent', 'desk')
    response = Context.refused(error.value)
    assert response.status_code == 409
    assert str(response.detail).startswith('Machine offline')


async def test_machine_check_refuses_members_and_audits_owner(api, monkeypatch):
    from acc.server.api.context import Principal
    ctx = api.app.state.ctx
    original = ctx.principal
    monkeypatch.setattr(ctx, 'principal', lambda request: Principal('blair', 'member', 'agent'))
    assert (await api.post('/api/nodes/check')).status_code == 403
    assert (await api.get('/api/nodes/check')).status_code == 403
    assert (await api.post('/api/nodes/desk/policy', json={'role': 'workstation'})).status_code == 403
    monkeypatch.setattr(ctx, 'principal', original)
    assert (await api.post('/api/nodes/check')).status_code == 200
    with api.app.state.db() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'node.check'))
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'node.check_result'))


async def test_offline_alert_uses_existing_owner_push_path(api):
    from helpers import pause_supervisor, write_db

    from acc.server.machine_presence import evaluate, transition
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    supervisor.push = object()  # delivery transport is replaced; alert selection remains real
    sent = []
    async def notify(message, **audience):
        sent.append((message, audience))
    supervisor.notify = notify
    await supervisor.push_attention()  # establish cursor before the outage
    with write_db(api.app.state.db) as db:
        node = Node(id='cloud', name='demo-cloud', status='active', role='always_on')
        db.add(node)
        db.flush()
        start = datetime.now(timezone.utc) - timedelta(minutes=11)
        transition(db, node, False, start)
        evaluate(db, node)
        db.commit()
    await supervisor.push_attention()
    assert len(sent) == 1
    assert sent[0][0]['tag'] == 'machine_offline:demo-cloud'
    assert sent[0][1] == {'everyone': False}
    await supervisor.push_attention()
    assert len(sent) == 1


async def test_manual_check_reports_live_but_unresponsive_link(api, env, live):  # noqa: F811
    import asyncio

    from helpers import pause_supervisor, wait_for
    from test_multihost import StandInNode, enroll
    await pause_supervisor(api)
    key, node_id = await enroll(api)
    class SilentNode(StandInNode):
        async def frame(self, ws, data):
            pass  # the authenticated link stays up, but never acknowledges a broker channel
    node = SilentNode(env['settings'].broker_socket, key, node_id)
    task = asyncio.create_task(node.run(live))
    async def online():
        return (await api.get('/api/nodes')).json()[0]['connected']
    try:
        await wait_for(online)
        result = await asyncio.wait_for(api.post('/api/nodes/check'), 8)
        assert result.status_code == 200
        remote = next(row for row in result.json()['machines'] if row['name'] == 'pc')
        assert remote['state'] == 'unresponsive'
        assert await online()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_threshold_change_rechecks_existing_alert(database):
    from acc.server.machine_presence import evaluate, transition
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with database() as db:
        node = Node(id='a', name='cloud', status='active', role='always_on')
        db.add(node)
        db.flush()
        transition(db, node, False, start)
        evaluate(db, node, start + timedelta(minutes=11))
        db.flush()
        item = db.scalar(select(AttentionItem))
        node.offline_threshold_seconds = 1200
        evaluate(db, node, start + timedelta(minutes=11))
        assert item.resolved_at is not None


async def test_primary_restart_does_not_count_unobserved_time_as_node_downtime(database, tmp_path):
    from acc.server.broker_client import Brokers
    from acc.server.nodes import NodeHub
    from acc.server.supervisor import Supervisor
    started = datetime.now(timezone.utc)
    with database() as db:
        db.add(Node(id='cloud', name='cloud', status='active', role='always_on',
                    last_seen=started - timedelta(days=1)))
        db.commit()
    supervisor = Supervisor(isolated_settings(tmp_path, data_dir=tmp_path, run_dir=tmp_path), database,
        Brokers(tmp_path / 'absent.sock', None, 'agent', 'services', NodeHub()))
    await supervisor.evaluate_machines()
    with database() as db:
        assert db.scalar(select(AttentionItem)) is None
        assert db.get(Node, 'cloud').offline_since >= started
