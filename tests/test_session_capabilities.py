"""Capability ceilings checked at dispatch, file access, install and publication.

No public traffic, real installers, Caddy or provider accounts are used.
"""
import json
import time

import pytest
from helpers import wait_for, write_db
from sqlalchemy import select
from test_team import grant, lead, request

from acc.server import child_caps, session_caps
from acc.server.compiler import LaunchInputs, launch_digest
from acc.server.models import LaunchSpec, PortReservation, Project, PublishRequest, Session, ToolRequest
from acc.session_capabilities import Refused, normalize


def family(api, project, parent, child=None):
    """Persist server-bound grants; production dispatch is exercised separately."""
    with write_db(api.app.state.db) as db:
        p = db.get(Project, project['id'])
        root = Session(id='capsroot', project=p, project_id=p.id, provider='claude', name='Root',
            tmux_name='capsroot', owner='@api', run_as=p.account, desired_state='running',
            launch_spec=LaunchSpec(provider='claude', inputs=LaunchInputs(provider='claude', model='sonnet').as_dict(),
                                   files={}, digest='root'))
        session_caps.attach(db, root, parent)
        db.add(root)
        db.flush()
        worker = Session(id='capschild', project=p, project_id=p.id, provider='claude', name='Child',
            tmux_name='capschild', owner='@api', run_as=p.account, desired_state='running', parent_session_id=root.id,
            launch_spec=LaunchSpec(provider='claude', inputs=LaunchInputs(provider='claude', model='sonnet').as_dict(),
                files={child_caps.FILE: json.dumps(child_caps.proof(db, root))}, digest='child'))
        worker.launch_spec.digest = launch_digest(worker.launch_spec.inputs, worker.launch_spec.files)
        session_caps.attach(db, worker, child)
        db.add(worker)
        db.commit()
    return root.id, worker.id


def runtime_file(api, sid):
    with api.app.state.db() as db:
        row = db.get(Session, sid)
        return json.dumps({'effective': session_caps.effective(db, row),
                           'parent': row.parent_session_id, 'model': row.launch_spec.inputs.get('model')})


@pytest.mark.parametrize('dimension,parent,asked,expected', [
    ('folders', ['workspace'], ['workspace', '/private'], ['workspace']),
    ('network', ['api.example.com'], ['api.example.com', 'other.example.com'], ['api.example.com']),
    ('installs', ['npm'], ['npm', 'apt'], ['npm']),
    ('ports', [8100], [8100, 8101], [8100]),
    ('providers', ['claude'], ['claude', 'codex'], ['claude']),
    ('models', ['claude:sonnet'], ['claude:sonnet', 'claude:opus'], ['claude:sonnet']),
    ('expires_at', 2000000000, 2100000000, 2000000000),
])
async def test_requested_effective_and_parent_narrowing(api, project, dimension, parent, asked, expected):
    root, child = family(api, project, {dimension: parent}, {dimension: asked})
    with write_db(api.app.state.db) as db:
        row = db.get(Session, child)
        assert row.cap_requested[dimension] == normalize({dimension: asked})[dimension]
        assert row.cap_effective[dimension] == expected
        assert session_caps.effective(db, row)[dimension] == expected
        smaller = int(time.time()) - 1 if dimension == 'expires_at' else []
        session_caps.narrow(db, db.get(Session, root), {dimension: smaller}, '@api')
        db.commit()
        assert session_caps.effective(db, row)[dimension] == smaller
        # The old process cannot keep a broader cached generation alive.
        with pytest.raises(Refused, match='limited by capsroot'):
            session_caps.check_execution(db, row)


@pytest.mark.parametrize('dimension,restriction', [('folders', ['workspace']), ('network', ['api.example.com']), ('installs', ['npm']), ('ports', [8100])])
async def test_native_broker_refuses_unenforceable_sandbox_before_process_creation(api, project, env, monkeypatch, dimension, restriction):
    from acc.broker import ops
    _, child = family(api, project, {dimension: restriction})
    calls = []
    monkeypatch.setattr(ops, 'tmux', lambda *_a, **_k: calls.append('tmux'))
    with pytest.raises(ops.OpError, match=f'limited by capsroot: native executor cannot enforce {dimension}'):
        ops.launch(env['broker'], {'acc_id': child, 'tmux_name': 'acc-' + child, 'provider': 'claude',
            'files': {session_caps.FILE: runtime_file(api, child)}})
    assert calls == []


async def test_folder_ceiling_refuses_container_without_workspace(api, project, env):
    from acc.broker import ops
    root, child = family(api, project, {'folders': ['workspace']}, {'folders': ['workspace', '/private']})
    with write_db(api.app.state.db) as db:
        session_caps.narrow(db, db.get(Session, root), {'folders': []}, '@api')
        db.commit()
    with pytest.raises(ops.OpError, match='folders permission refused'):
        ops.launch(env['broker'], {'acc_id': child, 'tmux_name': 'acc-' + child, 'provider': 'claude',
            'files': {session_caps.FILE: runtime_file(api, child)}, 'container': {'generation': 1, 'policy_id': 'test'}})


async def test_gateway_rejects_child_destinations_before_dns_and_parent_narrowing(api, project):
    from acc.container_egress import Denied, Policy, connect
    root, child = family(api, project, {'network': ['api.example.com']},
                         {'network': ['api.example.com', 'other.example.com']})
    looked_up, dialed = [], []

    def resolve(host, _port):
        looked_up.append(host)
        return ['8.8.8.8']

    def dial(address, port):
        dialed.append((address, port))
        return 'connected'

    def policy():
        caps = json.loads(runtime_file(api, child))['effective']
        return Policy(allow_hosts=tuple(caps['network']), parent=root)

    with pytest.raises(Denied, match='limited by capsroot'):
        connect(policy(), 'other.example.com:443', resolve=resolve, dial=dial, local=lambda: [])
    assert looked_up == dialed == []
    assert connect(policy(), 'api.example.com:443', resolve=resolve, dial=dial, local=lambda: []) == 'connected'
    with write_db(api.app.state.db) as db:
        session_caps.narrow(db, db.get(Session, root), {'network': []}, '@api')
        db.commit()
    with pytest.raises(Denied, match='limited by capsroot'):
        connect(policy(), 'api.example.com:443', resolve=resolve, dial=dial, local=lambda: [])
    assert len(dialed) == 1


async def test_install_cannot_exceed_parent_or_execute_after_parent_narrowed(api, project, monkeypatch):
    from acc.server.tools import ToolRefused
    root, child = family(api, project, {'installs': ['npm']}, {'installs': ['npm', 'apt']})
    service = api.app.state.supervisor.tools
    with pytest.raises(ToolRefused, match='limited by capsroot: installs'):
        service.request_session(child, manager='apt', name='curl', reason='request outside grant')
    rid = service.request_session(child, manager='npm', name='prettier', reason='format')
    with write_db(api.app.state.db) as db:
        db.get(ToolRequest, rid).status = 'approved'
        session_caps.narrow(db, db.get(Session, root), {'installs': []}, '@api')
        db.commit()
    carried = []

    async def carry(_request):
        carried.append(True)
        return {'ok': True}

    monkeypatch.setattr(service, '_carry', carry)
    await service.execute(rid)
    with api.app.state.db() as db:
        row = db.get(ToolRequest, rid)
        assert row.status == 'failed' and 'limited by capsroot' in row.detail
    assert carried == []


async def test_publication_cannot_exceed_port_ceiling_or_execute_after_narrowing(api, project, monkeypatch):
    from acc.server.publish import PublishRefused
    root, child = family(api, project, {'ports': [8100]}, {'ports': [8100, 8101]})
    publisher = api.app.state.publisher
    with write_db(api.app.state.db) as db:
        for port in (8100, 8101):
            db.add(PortReservation(port=port, project_id=project['id'], owner='@api'))
        db.commit()
    kwargs = {'action': 'publish', 'host': 'child.example.com', 'access': 'public',
              'requested_by': 'session:' + child, 'project_id': project['id'], 'session_id': child}
    with pytest.raises(PublishRefused, match='limited by capsroot: ports'):
        publisher.request(port=8101, **kwargs)
    rid = publisher.request(port=8100, **kwargs)
    with write_db(api.app.state.db) as db:
        db.get(PublishRequest, rid).status = 'approved'
        session_caps.narrow(db, db.get(Session, root), {'ports': []}, '@api')
        db.commit()
    effects = []

    async def publish(*_args):
        effects.append(True)
        return {}

    monkeypatch.setattr(publisher, '_publish', publish)
    await publisher.execute(rid)
    with api.app.state.db() as db:
        row = db.get(PublishRequest, rid)
        assert row.status == 'failed' and 'limited by capsroot' in row.detail
    assert effects == []


@pytest.mark.parametrize('dimension,allowed,provider,model', [
    ('providers', ['claude'], 'codex', 'sonnet'),
    ('models', ['claude:sonnet'], 'claude', 'opus'),
    ('expires_at', 1, 'claude', 'sonnet'),
])
async def test_broker_checks_provider_model_and_time_before_dispatch(api, project, env, dimension, allowed, provider, model):
    from acc.broker import ops
    _, child = family(api, project, {dimension: allowed})
    proof = json.loads(runtime_file(api, child))
    proof['model'] = model
    with pytest.raises(ops.OpError, match='limited by capsroot'):
        ops.launch(env['broker'], {'acc_id': child, 'tmux_name': 'acc-' + child, 'provider': provider,
                                  'files': {session_caps.FILE: json.dumps(proof)}})


@pytest.mark.parametrize('dimension,limit', [
    ('folders', []), ('network', []), ('installs', []), ('ports', []),
    ('providers', []), ('models', []), ('expires_at', 1),
])
async def test_live_child_stops_at_reconcile_after_each_parent_dimension_narrows(api, project, env, dimension, limit):
    root = await lead(api, project, env)
    child = await request(api, env, root, 'start', name='A', agent='claude', profile=None, task='work')
    assert child['ok'], child
    sid = child['result']['id']
    with write_db(api.app.state.db) as db:
        session_caps.narrow(db, db.get(Session, root['id']), {dimension: limit}, '@api')
        db.commit()

    async def stopped():
        row = (await api.get(f'/api/sessions/{sid}')).json()
        return row['desired_state'] == 'stopped' and row['observed_state'] in ('stopped', 'exited')

    await wait_for(stopped)
    reply = await api.get(f'/api/sessions/{sid}/permissions')
    assert reply.status_code == 200 and reply.json()['effective'][dimension] == limit


async def test_explanation_subrun_has_parent_proof_and_inherits_caps(api, project, env):
    from test_changes import repo
    from test_supervisor import launch
    repo(env['workspaces'] / 'demo')
    source = await launch(api, project, permissions={'providers': ['claude', 'codex']})
    (env['workspaces'] / 'demo' / 'app.py').write_text('one\nTWO\n')
    reply = await api.post(f"/api/sessions/{source['id']}/explanation", json={'provider': 'codex', 'model': 'test'})
    assert reply.status_code == 200, reply.text
    with api.app.state.db() as db:
        worker = db.get(Session, reply.json()['helper_id'])
        assert worker.parent_session_id == source['id']
        assert worker.parent_chain == [source['id']] and worker.root_owner == '@api'
        assert session_caps.effective(db, worker)['providers'] == ['claude', 'codex']
        assert child_caps.FILE in worker.launch_spec.files and session_caps.FILE in worker.launch_spec.files


async def test_explanation_subrun_cannot_change_to_provider_outside_parent_set(api, project, env):
    from test_changes import repo
    from test_supervisor import launch
    repo(env['workspaces'] / 'demo')
    source = await launch(api, project, permissions={'providers': ['claude']})
    (env['workspaces'] / 'demo' / 'app.py').write_text('one\nTWO\n')
    reply = await api.post(f"/api/sessions/{source['id']}/explanation", json={'provider': 'codex', 'model': 'test'})
    assert reply.status_code == 409 and f"limited by {source['id']}" in reply.text
    with api.app.state.db() as db:
        assert list(db.scalars(select(Session.id))) == [source['id']]


async def test_v1_managed_launch_records_requested_set_but_refuses_outside_parent_provider(api, project, env):
    from test_changes import repo
    repo(env['workspaces'] / 'demo')
    body = {'project_id': project['id'], 'provider': 'claude', 'name': 'Caps lead', 'worktree': True,
            'orchestrator': grant(agents=[{'agent': 'claude', 'profile': None}, {'agent': 'codex', 'profile': None}]),
            'permissions': {'providers': ['claude']}}
    made = await api.post('/api/sessions', json=body)
    assert made.status_code == 201, made.text
    root = made.json()
    denied = await request(api, env, root, 'start', name='Outside', agent='codex', profile=None, task='work',
                           permissions={'providers': ['claude', 'codex']})
    assert not denied['ok'] and f"limited by {root['id']}" in denied['error']
    good = await request(api, env, root, 'start', name='Inside', agent='claude', profile=None, task='work',
                         permissions={'providers': ['claude', 'codex']})
    assert good['ok'], good
    result = (await api.get(f"/api/sessions/{good['result']['id']}/permissions")).json()
    assert result['requested']['providers'] == ['claude', 'codex']
    assert result['effective']['providers'] == ['claude']


async def test_permission_mutation_cannot_be_used_by_installation_token(api, project):
    _, child = family(api, project, {'ports': [8100]})
    response = await api.patch(f'/api/sessions/{child}/permissions', json={'permissions': {'ports': []}})
    assert response.status_code == 403
    with write_db(api.app.state.db) as db:
        with pytest.raises(Refused, match='only narrow'):
            session_caps.narrow(db, db.get(Session, child), {'ports': [8100, 8101]}, 'session:capsroot')


async def test_gateway_integrations_read_the_effective_session_envelope(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from acc.broker import container_integrations as integrations
    captured = []
    entry = {'image': 'example', 'providers': ['codex'], 'credentials': 'readonly-v1',
             'egress': {'mode': 'internet-v1', 'deny_hosts': ['control.example.com'], 'deny_networks': ['9.9.9.9/32']}}
    monkeypatch.setattr(integrations, '_read_policy', lambda *_: entry)
    monkeypatch.setattr(integrations, 'snapshot', lambda *_: {})
    monkeypatch.setenv('ACC_CONTAINERS', '1')

    class Gateway:
        def __init__(self, _path, _sid, generation, _provider, _credentials, policy, **_kwargs):
            captured.append(policy)
            self.metadata = {'generation': generation}
        def __enter__(self):
            return self
        def close(self):
            pass

    monkeypatch.setattr(integrations, 'Gateway', Gateway)
    expiry = int(time.time()) + 100
    (tmp_path / session_caps.FILE).write_text(json.dumps({'effective': normalize({
        'network': ['api.example.com'], 'expires_at': expiry}), 'parent': 'capsroot'}))
    config = SimpleNamespace(container_policy=tmp_path / 'policy', run_dir=tmp_path)
    try:
        integrations.start(config, 'capschild', {'policy_id': 'test', 'generation': 1}, 'codex', tmp_path)
        assert captured[0].allow_hosts == ('api.example.com',)
        assert captured[0].expires_at == expiry and captured[0].parent == 'capsroot'
    finally:
        integrations.stop(config, 'capschild')


def test_gateway_relay_cannot_transfer_bytes_after_deadline():
    from acc.container_egress import relay
    class Stream:
        def recv(self, _size):
            pytest.fail('expired relay must not read')
        def sendall(self, _data):
            pytest.fail('expired relay must not write')
    relay(Stream(), Stream(), expires_at=1)


async def test_broker_install_barrier_rejects_disallowed_manager_and_expired_grant(env, monkeypatch):
    from acc.broker import server
    from acc.broker.server import Broker, RequestError
    broker = Broker(env['broker'])
    monkeypatch.setattr(server.os, 'geteuid', lambda: 1000)
    calls = []
    monkeypatch.setattr(server.tooling, 'execute', lambda **_kwargs: calls.append(True))
    for caps in ({'installs': []}, {'installs': ['npm'], 'expires_at': 1}):
        with pytest.raises(RequestError, match='limited by capsroot'):
            broker.tool_change({'manager': 'npm', 'name': 'prettier', 'version': None, 'operation': 'install',
                                'capabilities': {'effective': normalize(caps), 'parent': 'capsroot'}})
    assert calls == []


async def test_person_can_narrow_a_grant_through_the_verified_browser_api(api, project):
    from test_admin_features import owner_browser

    from acc.server import auth
    from acc.server.models import AuthSession
    root, child = family(api, project, {'ports': [8100, 8101]})
    headers = await owner_browser(api)
    token = api.cookies.get('acc_session')
    with write_db(api.app.state.db) as db:
        db.get(AuthSession, auth.digest(token)).reauthenticated_at = auth.now()
        db.commit()
    reply = await api.patch(f'/api/sessions/{root}/permissions', headers=headers,
                            json={'permissions': {'ports': [8100]}})
    assert reply.status_code == 200, reply.text
    assert (await api.get(f'/api/sessions/{child}/permissions')).json()['effective']['ports'] == [8100]
    widened = await api.patch(f'/api/sessions/{root}/permissions', headers=headers,
                              json={'permissions': {'ports': [8100, 8101]}})
    assert widened.status_code == 409


async def test_legacy_install_execution_refuses_caps_it_cannot_enforce(api, project, monkeypatch):
    root, child = family(api, project, {'installs': ['npm']})
    service = api.app.state.supervisor.tools
    rid = service.request_session(child, manager='npm', name='prettier', reason='format')
    with api.app.state.db() as db:
        row = db.get(ToolRequest, rid)
        db.expunge(row)
    calls = []
    monkeypatch.setattr(service.brokers, 'fresh', lambda *_args, **_kwargs: calls.append(True))
    from acc.server.tools import ToolRefused
    with pytest.raises(ToolRefused, match='qualified reviewed extractor'):
        await service._carry(row)
    assert calls == []


def test_gate_broker_refuses_expired_session_before_spawning(env, monkeypatch):
    from acc.broker import gate_ops
    folder = env['broker'].run_dir / 'gates' / ('a' * 32)
    (folder / 'checkout').mkdir(parents=True)
    monkeypatch.setattr(gate_ops, '_job_dir', lambda *_: folder)
    spawned = []
    monkeypatch.setattr(gate_ops.subprocess, 'Popen', lambda *_args, **_kwargs: spawned.append(True))
    with pytest.raises(gate_ops.OpError, match='time limit ended'):
        gate_ops.gate_start(env['broker'], 'a' * 32, ['pytest'], {'expires_at': 1})
    assert spawned == []


async def test_unbound_session_origin_cannot_create_an_unparented_root(api, project, env):
    from acc.server.supervisor_common import LaunchRefused
    root = await lead(api, project, env)
    with pytest.raises(LaunchRefused, match='authenticated parent'):
        await api.app.state.supervisor.create_session(project['id'], LaunchInputs(provider='claude'),
            'Unbound', 'session:' + root['id'], owner='@api')


async def test_container_parent_cannot_fall_back_to_native_subrun(api, project):
    from acc.server.supervisor_common import LaunchRefused
    root, _ = family(api, project, {})
    with write_db(api.app.state.db) as db:
        db.get(Session, root).container = True
        db.commit()
    with pytest.raises(LaunchRefused, match='container parent cannot launch a native child'):
        await api.app.state.supervisor.create_session(project['id'], LaunchInputs(provider='claude'),
            'Fallback', 'session:' + root, owner='@api', parent_session_id=root)


async def test_external_source_with_unknown_grant_cannot_launch_managed_subrun(api, project):
    from acc.server.supervisor_common import LaunchRefused
    supervisor = api.app.state.supervisor
    with api.app.state.db() as db:
        account = db.get(Project, project['id']).account
    source = supervisor.register_external(project['id'], 'claude', 'Observed', 'external-native',
                                          account, 'fixture-unit', '@api')
    with pytest.raises(LaunchRefused, match='external source has no qualified managed grant'):
        await supervisor.create_session(project['id'], LaunchInputs(provider='claude'), 'Unknown grant',
            'session:' + source, parent_session_id=source)


def test_person_owned_multirun_and_schedule_launch_bodies_retain_requested_sets():
    from acc.server.api.multiruns import MultiRunIn
    from acc.server.api.schemas import SessionIn
    requested = {'providers': ['claude', 'codex'], 'expires_at': 2000000000}
    multirun = MultiRunIn(project_id=1, name='Alternatives', initial_prompt='Review changes',
                         runs=[{'provider': 'claude'}, {'provider': 'codex'}], permissions=requested)
    for launch in multirun.launches():
        assert launch.permissions == normalize(requested)
    # Schedules retain the ordinary SessionIn launch document and validate it
    # again at launch. The request is never dropped by conversion to inputs.
    retained = SessionIn(project_id=1, provider='claude', name='Scheduled', permissions=requested).model_dump()
    assert SessionIn.model_validate(retained).permissions == normalize(requested)


@pytest.mark.parametrize('reason,explicit_expiry,allowed', [('expired', False, True),
                                                         ('person', False, False), ('expired', True, False)])
async def test_running_root_wrapup_does_not_reopen_launch_or_explicit_expiry(api, project, reason, explicit_expiry, allowed):
    from datetime import timedelta

    from helpers import pause_supervisor

    from acc.server.models import Team, utcnow

    await pause_supervisor(api)
    root_id, child_id = family(api, project, None)
    with write_db(api.app.state.db) as db:
        root = db.get(Session, root_id)
        root.team_id = root_id
        team = Team(id=root_id, limits={}, expires_at=utcnow() - timedelta(seconds=1), stopped=True,
                    stop_reason=reason, wrapup_until=utcnow() + timedelta(minutes=5))
        db.add(team)
        if explicit_expiry:
            root.cap_limit = {'expires_at': int(time.time()) - 1}
        db.flush()
        if allowed:
            session_caps.check_execution(db, root, wrapup=True)
        else:
            with pytest.raises(Refused, match='time limit ended'):
                session_caps.check_execution(db, root, wrapup=True)
        with pytest.raises(Refused):
            session_caps.check_execution(db, root)
        with pytest.raises(Refused):
            session_caps.admit_launch(db, root)
        with pytest.raises(Refused):
            session_caps.authorize(db, root, 'expires_at')
        with pytest.raises(Refused):
            session_caps.check_execution(db, db.get(Session, child_id), wrapup=True)
        team.wrapup_until = utcnow() - timedelta(seconds=1)
        with pytest.raises(Refused):
            session_caps.check_execution(db, root, wrapup=True)
