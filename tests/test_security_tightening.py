"""Tightening never blocks save, never relabels running generations, fences new work."""
import asyncio
import json

import pytest
from helpers import pause_supervisor, reconcile_passes, wait_for
from test_supervisor import launch, state_is

from acc.server import security_settings as policy
from acc.server.broker_client import BrokerError


@pytest.mark.parametrize('action', ['stop', 'archive'])
async def test_unconfirmed_stop_remains_older_until_machine_confirms(api, project, monkeypatch, action):
    from acc.server.models import Session
    scope = {'kind': 'project', 'project_id': project['id']}
    with api.app.state.db() as db:
        policy.save(db, scope, {'secrets': False}, actor='casey', expected_revision=0)
        db.commit()
    made = await launch(api, project)
    await wait_for(state_is(api, made['id'], 'idle'))
    with api.app.state.db() as db:
        policy.save(db, scope, {'secrets': True}, actor='casey', expected_revision=1)
        db.commit()
    supervisor = api.app.state.supervisor
    original = supervisor.broker.call

    async def unavailable(op, **kwargs):
        if op == 'kill':
            raise BrokerError('fixture: machine unavailable')
        return await original(op, **kwargs)

    await pause_supervisor(api)
    monkeypatch.setattr(supervisor.broker, 'call', unavailable)
    assert (await api.post(f"/api/sessions/{made['id']}/{action}")).status_code == 200
    with api.app.state.db() as db:
        assert db.get(Session, made['id']).last_error.startswith('stop not confirmed:')
        assert policy.older_sessions(db, scope) == [made['id']]
    if action == 'archive':
        assert (await api.post(f"/api/sessions/{made['id']}/stop")).status_code == 200
        with api.app.state.db() as db:
            assert db.get(Session, made['id']).desired_state == 'archived'
    monkeypatch.setattr(supervisor.broker, 'call', original)
    await reconcile_passes(api, 2)
    with api.app.state.db() as db:
        assert policy.older_sessions(db, scope) == []
        assert db.get(Session, made['id']).last_error is None


async def test_launch_waiting_for_layout_revalidates_after_tightening(api, project, monkeypatch):
    scope = {'kind': 'project', 'project_id': project['id']}
    with api.app.state.db() as db:
        policy.save(db, scope, {'secrets': False}, actor='casey', expected_revision=0)
        db.commit()
    supervisor = api.app.state.supervisor
    original = supervisor.layout
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(supervisor, 'layout', delayed)
    pending = asyncio.create_task(api.post('/api/sessions', json={
        'project_id': project['id'], 'provider': 'claude', 'name': 'Concurrent launch'}))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        with api.app.state.db() as db:
            policy.save(db, scope, {'secrets': True}, actor='casey', expected_revision=1)
            db.commit()
    finally:
        release.set()
    response = await pending
    assert response.status_code == 201, response.text
    assert json.loads(response.json()['launch_spec']['files']['guardrails.json'])['security']['switches']['secrets'] is True


async def test_replacement_requires_reviewed_current_provider_inputs(api, project):
    scope = {'kind': 'project', 'project_id': project['id']}
    with api.app.state.db() as db:
        policy.save(db, scope, {'provider_prompts': False}, actor='casey', expected_revision=0)
        db.commit()
    old = await launch(api, project)
    await wait_for(state_is(api, old['id'], 'idle'))
    with api.app.state.db() as db:
        policy.save(db, scope, {'provider_prompts': True}, actor='casey', expected_revision=1)
        db.commit()
    assert (await api.post(f"/api/sessions/{old['id']}/stop")).status_code == 200
    await wait_for(state_is(api, old['id'], 'stopped'))
    assert (await api.post(f"/api/sessions/{old['id']}/resume")).status_code == 409
    # Copying the old bypass inputs into a replacement cannot bypass current policy.
    refused = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
        'permission_mode': 'bypassPermissions', 'confirm_dangerous': True, 'name': 'Unsafe replay'})
    assert refused.status_code == 409, refused.text
    new = await launch(api, project)
    assert new['launch_spec']['inputs']['permission_mode'] != 'bypassPermissions'
    assert json.loads(new['launch_spec']['files']['guardrails.json'])['security']['switches']['provider_prompts'] is True


async def test_helper_started_by_older_lead_gets_current_policy(api, project, env):
    from test_team import lead, request
    scope = {'kind': 'project', 'project_id': project['id']}
    with api.app.state.db() as db:
        policy.save(db, scope, {'secrets': False}, actor='casey', expected_revision=0)
        db.commit()
    parent = await lead(api, project, env)
    with api.app.state.db() as db:
        policy.save(db, scope, {'secrets': True}, actor='casey', expected_revision=1)
        db.commit()
    reply = await request(api, env, parent, 'start', name='Current helper', agent='claude', task='Bounded work')
    assert reply['ok'], reply
    child = (await api.get('/api/sessions/' + reply['result']['id'])).json()
    assert json.loads(child['launch_spec']['files']['guardrails.json'])['security']['switches']['secrets'] is True
    assert child['security_older_settings'] is False


async def test_schedule_created_before_tightening_launches_current_policy(api, project):
    from test_admin_features import owner_browser
    from test_schedules import payload
    headers = await owner_browser(api)
    scope = {'kind': 'project', 'project_id': project['id']}
    with api.app.state.db() as db:
        policy.save(db, scope, {'secrets': False}, actor='casey', expected_revision=0)
        db.commit()
    made = await api.post('/api/schedules', headers=headers, json=payload(project))
    assert made.status_code == 201, made.text
    with api.app.state.db() as db:
        policy.save(db, scope, {'secrets': True}, actor='casey', expected_revision=1)
        db.commit()
    started = await api.post(f"/api/schedules/{made.json()['id']}/run", headers=headers)
    assert started.status_code == 201, started.text

    async def published():
        rows = (await api.get('/api/schedules', headers=headers)).json()
        return next((run['session_id'] for row in rows for run in row['runs'] if run['session_id']), None)

    session = (await api.get('/api/sessions/' + await wait_for(published))).json()
    assert json.loads(session['launch_spec']['files']['guardrails.json'])['security']['switches']['secrets'] is True


@pytest.mark.parametrize('binding', ['local', 'placement', 'team_host'])
async def test_t1_account_inventory_uses_frozen_host_binding(api, project, binding):
    from acc.server.models import Session
    made = await launch(api, project)
    with api.app.state.db() as db:
        session = db.get(Session, made['id'])
        frozen = json.loads(session.launch_spec.files['guardrails.json'])
        frozen['security']['switches']['secrets'] = False
        session.launch_spec.files = {**session.launch_spec.files, 'guardrails.json': json.dumps(frozen)}
        machine = 'local' if binding == 'local' else 'away'
        if binding == 'placement':
            session.placement.host = machine
        elif binding == 'team_host':
            session.placement = None
            session.team_host = machine
        db.commit()
        scope = {'kind': 'account', 'machine': machine, 'account': session.run_as}
        assert [s.id for s in policy.affected(db, scope)] == [session.id]
        assert policy.older_sessions(db, scope) == [session.id]
        assert not policy.affected(db, {**scope, 'machine': 'other-machine'})
        assert not policy.affected(db, {**scope, 'account': 'other-account'})


@pytest.mark.parametrize('inputs', [{'permission_mode': 'bypassPermissions'}, {'extra_args': ['--unsafe']},
    {'external': True, 'unit': 'fixture.service'}])
async def test_t4_legacy_saved_input_refusal_is_reported_as_older(api, project, inputs):
    from acc.server.models import Session
    made = await launch(api, project)
    with api.app.state.db() as db:
        session = db.get(Session, made['id'])
        frozen = json.loads(session.launch_spec.files['guardrails.json'])
        frozen.pop('security')
        session.launch_spec.files = {**session.launch_spec.files, 'guardrails.json': json.dumps(frozen)}
        session.launch_spec.inputs = {**session.launch_spec.inputs, **inputs}
        db.commit()
        with pytest.raises(policy.Conflict):
            policy.check_generation(db, session)
        assert policy.on_older_settings(db, session)
        assert policy.older_sessions(db, {'kind': 'instance'}) == [session.id]


async def test_t6_project_delete_clears_confirmed_gone_stop_with_audit(api, project):
    from acc.server.models import AuditLog, Session
    made = await launch(api, project)
    assert (await api.post(f"/api/sessions/{made['id']}/stop")).status_code == 200
    await pause_supervisor(api)
    with api.app.state.db() as db:
        db.get(Session, made['id']).last_error = 'stop not confirmed: crash after kill'
        db.commit()
    deleted = await api.post(f"/api/projects/{project['id']}/delete", json={'confirm': project['slug']})
    assert deleted.status_code == 200, deleted.text
    with api.app.state.db() as db:
        assert db.get(Session, made['id']).last_error is None
        record = db.query(AuditLog).filter_by(action='security.session.stop_marker_cleared', target=made['id']).one()
        assert record.detail['reason'] == 'project_delete_checked'
    # Existing detached tombstones from older installs also have a safe retry:
    # actual broker acknowledgement, no reconstructed workspace and no un-archive.
    with api.app.state.db() as db:
        db.get(Session, made['id']).last_error = 'stop not confirmed: legacy tombstone'
        db.commit()
    retried = await api.post(f"/api/sessions/{made['id']}/stop")
    assert retried.status_code == 200, retried.text
    with api.app.state.db() as db:
        assert db.get(Session, made['id']).last_error is None
        assert db.get(Session, made['id']).desired_state == 'archived'
        records = db.query(AuditLog).filter_by(action='security.session.stop_marker_cleared', target=made['id']).all()
        assert {row.detail['reason'] for row in records} == {'project_delete_checked', 'detached_stop_confirmed'}


async def test_t6_machine_gone_requires_verified_owner_removed_node_and_exact_confirmation(api, project):
    from test_security_folders import verified

    from acc.server.models import AuditLog, Node, Session
    made = await launch(api, project)
    assert (await api.post(f"/api/sessions/{made['id']}/stop")).status_code == 200
    await pause_supervisor(api)
    headers = await verified(api)
    with api.app.state.db() as db:
        session = db.get(Session, made['id'])
        session.placement.host = 'retired-node'
        session.last_error = 'stop not confirmed: unreachable'
        db.add(Node(id='d'*32, name='retired-node', status='active', info={'accounts': [session.run_as]}))
        db.commit()
    path = f"/api/security/sessions/{made['id']}/machine-gone"
    assert (await api.post(path, json={'confirm_machine': 'retired-node'})).status_code == 403
    assert (await api.post(path, headers=headers, json={'confirm_machine': 'wrong'})).status_code == 422
    assert (await api.post(path, headers=headers, json={'confirm_machine': 'retired-node'})).status_code == 409
    removed = await api.post('/api/nodes/retired-node/remove', headers=headers)
    assert removed.status_code == 200, removed.text
    with api.app.state.db() as db:
        assert db.get(Session, made['id']).last_error.startswith('stop not confirmed:')
    from datetime import timedelta

    from acc.server import auth
    from acc.server.models import AuthSession
    with api.app.state.db() as db:
        db.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = auth.now() - timedelta(minutes=6)
        db.commit()
    assert (await api.post(path, headers=headers, json={'confirm_machine': 'retired-node'})).status_code == 403
    headers = await verified(api)
    response = await api.post(path, headers=headers, json={'confirm_machine': 'retired-node'})
    assert response.status_code == 200, response.text
    assert response.json()['stop_proven'] is False
    with api.app.state.db() as db:
        assert db.get(Session, made['id']).last_error is None
        record = db.query(AuditLog).filter_by(action='security.session.stop_marker_cleared', target=made['id']).one()
        assert record.detail['reason'] == 'owner_confirmed_machine_gone'


async def test_t7_explanation_helper_reconcile_clears_stop_even_without_cached_result(api, project):
    from acc.server.models import Session
    made = await launch(api, project)
    assert (await api.post(f"/api/sessions/{made['id']}/stop")).status_code == 200
    await pause_supervisor(api)
    with api.app.state.db() as db:
        session = db.get(Session, made['id'])
        session.launch_spec.files = {**session.launch_spec.files, 'explanation.json': '{}'}
        session.last_error = 'stop not confirmed: interrupted acknowledgement'
        db.commit()
    await reconcile_passes(api, 1)
    with api.app.state.db() as db:
        assert db.get(Session, made['id']).last_error is None


async def test_t8_archived_group_generation_is_not_unarchived_by_reconcile(api, monkeypatch):
    from types import SimpleNamespace

    from acc.server import group_board
    monkeypatch.setattr(group_board, 'authorized', lambda *a, **k: False)
    session = SimpleNamespace(desired_state='archived', project=SimpleNamespace(group_id='gone'),
        launch_spec=SimpleNamespace(files={}), runs=[], container=False, team_stop_pending=True,
        multirun_stop_pending=False, last_error='stop not confirmed: pending')
    with api.app.state.db() as db:
        await api.app.state.supervisor._reconcile_workspace(db, session, None)
    assert session.desired_state == 'archived'
    assert session.last_error is None


async def test_v3_observe_only_sessions_never_offer_security_stop(api, project):
    from acc.server.models import Session
    made = await launch(api, project)
    with api.app.state.db() as db:
        row = db.get(Session, made['id'])
        row.management = 'external'
        row.launch_spec.inputs = {'external': True, 'unit': 'fixture.service'}
        db.commit()
        assert policy.older_sessions(db, {'kind': 'instance'}) == []
        assert not policy.on_older_settings(db, row)
