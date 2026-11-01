"""SS-1..SS-5: independent review fixture proofs, inverted to safe outcomes."""
import json
from types import SimpleNamespace

import pytest

from acc.server import auth
from acc.server import security_settings as policy
from acc.server.compiler import LaunchInputs
from acc.server.models import AuditLog, AuthSession, Project, SecurityFolderPolicy, Setting


async def verified(api):
    from test_admin_features import owner_browser
    headers = await owner_browser(api)
    with api.app.state.db() as db:
        db.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = auth.now()
        db.commit()
    return headers


@pytest.mark.parametrize('human', [False, True])
async def test_ss1_legacy_writer_cannot_mutate_inherited_policy(api, project, human):
    if human:
        from test_admin_features import owner_browser
        headers = await owner_browser(api)  # deliberately no step-up
    else:
        headers = {}
    response = await api.put('/api/tools/policy', headers=headers,
        json={'scope': 'project', 'target': str(project['id']), 'required': False})
    assert response.status_code in (403, 410), response.text
    with api.app.state.db() as db:
        assert policy.resolve(db, project_id=project['id'])['switches']['tool_install_check'] is True
        assert policy.revision(db) == 0


@pytest.mark.parametrize('inputs', [
    LaunchInputs(provider='claude', permission_mode='bypassPermissions'),
    LaunchInputs(provider='codex', sandbox='danger-full-access'),
    LaunchInputs(provider='codex', approval='never'),
    LaunchInputs(provider='cursor', options={'force': True}),
    LaunchInputs(provider='claude', extra_args=('--dangerously-skip-permissions',)),
])
def test_ss2_on_is_an_upper_bound(api, project, inputs):
    with api.app.state.db() as db:
        with pytest.raises(ValueError, match='Security'):
            policy.launch_inputs(inputs, policy.resolve(db, project_id=project['id']))


@pytest.mark.parametrize('options', [False, True])
async def test_ss2_direct_and_legacy_options_cannot_bypass(api, project, options):
    body = {'project_id': project['id'], 'provider': 'claude', 'name': 'Bounded permissions', 'confirm_dangerous': True}
    body.update({'options': {'permission_mode': 'bypassPermissions'}} if options else {'permission_mode': 'bypassPermissions'})
    response = await api.post('/api/sessions', json=body)
    assert response.status_code == 409, response.text
    assert 'Security' in response.text


def test_ss2_unsafe_old_generation_cannot_resume(api, project):
    with api.app.state.db() as db:
        frozen = {'security': policy.resolve(db, project_id=project['id']), 'custom': []}
        session = SimpleNamespace(project_id=project['id'], owner=project['owner'], host='local', run_as=project['account'],
            launch_spec=SimpleNamespace(inputs={'provider': 'claude', 'permission_mode': 'bypassPermissions'},
                                        files={'guardrails.json': json.dumps(frozen)}))
        with pytest.raises(policy.Conflict, match='Security'):
            policy.check_generation(db, session)


@pytest.mark.parametrize('published', [False, True])
def test_ss3_off_on_and_roundtrip_require_fresh_ack(api, project, published):
    with api.app.state.db() as db:
        scope = policy.account_scope(db, 'local', project['account'])
        policy.save(db, scope, {'allowed_folders': False}, actor='casey', expected_revision=0)
        db.merge(SecurityFolderPolicy(key=policy.scope_key(scope), machine_id='local', account=project['account'],
            roots=[], revision=1, applied_revision=1, state='applied', detail={'allowed_folders': False}))
        db.flush()
        assert policy.resolve(db, project_id=project['id'])['enforcement']['allowed_folders'] == 'applied'
        policy.save(db, scope, {'allowed_folders': True}, actor='casey', expected_revision=1)
        effective = policy.resolve(db, project_id=project['id'])
        assert effective['enforcement']['allowed_folders'] == 'pending'
        with pytest.raises(ValueError, match='pending'):
            policy.launch_inputs(LaunchInputs(provider='claude'), effective)
        if published:
            row = db.get(SecurityFolderPolicy, policy.scope_key(scope))
            row.state, row.revision = 'pending', 2
            row.detail = {'allowed_folders': True, 'operation_id': 'a' * 32}
            db.flush()
            with pytest.raises(policy.Conflict, match='pending'):
                policy.save(db, scope, {'allowed_folders': False}, actor='casey', expected_revision=2)
            assert db.get(SecurityFolderPolicy, policy.scope_key(scope)).state == 'pending'
            assert policy.resolve(db, project_id=project['id'])['enforcement']['allowed_folders'] == 'pending'
        else:
            policy.save(db, scope, {'allowed_folders': False}, actor='casey', expected_revision=2)
            assert policy.resolve(db, project_id=project['id'])['enforcement']['allowed_folders'] == 'applied'


@pytest.mark.parametrize('exempt_project', [True, False])
async def test_ss4_inspector_does_not_supply_project_subject(api, project, exempt_project):
    from test_admin_features import owner_browser
    headers = await owner_browser(api)
    with api.app.state.db() as db:
        db.get(Project, project['id']).owner = 'alex'
        db.merge(Setting(key='tools.person.' + ('alex' if exempt_project else 'casey'), value={'required': False}))
        db.commit()
    response = await api.get('/api/security/effective', headers=headers, params={'project_id': project['id']})
    assert response.status_code == 200, response.text
    assert response.json()['switches']['tool_install_check'] is (not exempt_project)


async def test_ss4_shared_account_reports_variation(api, project):
    from test_admin_features import owner_browser
    headers = await owner_browser(api)
    with api.app.state.db() as db:
        p = db.get(Project, project['id'])
        p.owner = 'alex'
        db.add(Project(name='Other', slug='other', workspace_path='/fixture/other', host='local', account=p.account, owner='blair'))
        db.merge(Setting(key='tools.person.alex', value={'required': False}))
        db.commit()
    response = await api.get('/api/security/effective', headers=headers, params={'machine': 'local', 'account': project['account']})
    assert response.status_code == 200, response.text
    assert response.json()['switches']['tool_install_check'] is None
    assert response.json()['sources']['tool_install_check'] == {'kind': 'varies'}


async def test_ss5_denied_token_is_safely_audited(api):
    response = await api.post('/api/security/changes/preview', json={'scope': {'kind': 'instance'}, 'revision': 0})
    assert response.status_code == 403
    with api.app.state.db() as db:
        rows = db.query(AuditLog).filter(AuditLog.action == 'security.policy.refused').all()
        assert len(rows) == 1
        assert rows[0].detail['operation'] == 'preview'
        assert 'authorization' not in json.dumps(rows[0].detail).lower()
        assert policy.revision(db) == 0


async def test_brief_installation_token_cannot_enumerate_security(api, project):
    with api.app.state.db() as db:
        policy.save(db, {'kind': 'project', 'project_id': project['id']}, {'secrets': False}, actor='casey', expected_revision=0)
        db.commit()
    for path in ['/api/security/scopes', '/api/security/effective', f"/api/security/effective?project_id={project['id']}"]:
        assert (await api.get(path)).status_code == 403


async def test_ss2_member_cannot_confirm_away_owner_policy(api, project, member_accounts):
    from test_admin_features import owner_browser
    from test_users import browser, csrf, join
    headers = await owner_browser(api)
    invitation = await api.post('/api/users/invites', headers=headers, json={'role': 'member', 'account': 'dana-agent'})
    assert invitation.status_code == 201, invitation.text
    async with browser(api) as member:
        assert (await join(member, invitation.json()['link'], 'alex')).status_code == 200
        with api.app.state.db() as db:
            p = db.get(Project, project['id'])
            p.owner, p.account = 'alex', 'dana-agent'
            db.commit()
        denied = await member.post('/api/security/changes/preview', headers=csrf(member),
            json={'scope': {'kind': 'project', 'project_id': project['id']}, 'revision': 0})
        assert denied.status_code == 403
        launched = await member.post('/api/sessions', headers=csrf(member), json={
            'project_id': project['id'], 'provider': 'claude', 'name': 'Member bypass',
            'permission_mode': 'bypassPermissions', 'confirm_dangerous': True})
        assert launched.status_code == 409, launched.text
        with api.app.state.db() as db:
            assert db.query(AuditLog).filter(AuditLog.action == 'security.policy.refused', AuditLog.actor == 'alex').count() == 1


async def test_ss1_retirement_preserves_saved_exceptions_and_review(api, project):
    headers = await verified(api)
    with api.app.state.db() as db:
        db.merge(Setting(key=f"tools.project.{project['id']}", value={'required': False}))
        db.commit()
    preview = await api.post('/api/security/changes/preview', headers=headers,
        json={'scope': {'kind': 'project', 'project_id': project['id']}, 'overrides': {'tool_install_check': True}, 'revision': 0})
    assert preview.status_code == 200, preview.text
    retired = await api.put('/api/tools/policy', headers=headers,
        json={'scope': 'project', 'target': str(project['id']), 'required': True})
    assert retired.status_code == 410
    with api.app.state.db() as db:
        assert policy.resolve(db, project_id=project['id'])['switches']['tool_install_check'] is False
        assert policy.revision(db) == 0
    change = preview.json()
    applied = await api.post(f"/api/security/changes/{change['id']}/apply", headers=headers, json={'digest': change['digest']})
    assert applied.status_code == 200, applied.text
    assert (await api.put('/api/tools/policy', headers=headers,
        json={'scope': 'project', 'target': str(project['id']), 'required': False})).status_code == 410
    with api.app.state.db() as db:
        assert policy.resolve(db, project_id=project['id'])['switches']['tool_install_check'] is True


@pytest.mark.parametrize('state', ['pending_policy', 'pending', 'failed', 'unavailable'])
def test_ss3_non_ack_states_never_claim_applied(api, project, state):
    with api.app.state.db() as db:
        scope = policy.account_scope(db, 'local', project['account'])
        db.add(SecurityFolderPolicy(key=policy.scope_key(scope), machine_id='local', account=project['account'],
            roots=[], revision=2, applied_revision=1, state=state, detail={'allowed_folders': True}))
        db.flush()
        assert policy.resolve(db, project_id=project['id'])['enforcement']['allowed_folders'] == 'pending'


async def test_ss5_negative_outcomes_survive_rollback_without_private_payload(api, project):
    from datetime import timedelta

    from acc.server.models import SecurityChange, utcnow
    headers = await verified(api)
    body = {'scope': {'kind': 'project', 'project_id': project['id']}, 'overrides': {'secrets': False}, 'revision': 0}
    stale = await api.post('/api/security/changes/preview', headers=headers, json={**body, 'revision': 123})
    assert stale.status_code == 409
    change = (await api.post('/api/security/changes/preview', headers=headers, json=body)).json()
    with api.app.state.db() as db:
        db.get(SecurityChange, change['id']).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    expired = await api.post(f"/api/security/changes/{change['id']}/apply", headers=headers, json={'digest': change['digest']})
    assert expired.status_code == 409
    private = await api.post('/api/security/changes/preview', headers=headers,
        json={**body, 'scope': {'kind': 'secret-rejected-marker', 'private': 'never-log-this-marker'}})
    assert private.status_code == 422
    with api.app.state.db() as db:
        assert policy.revision(db) == 0
        events = db.query(AuditLog).filter(AuditLog.action.in_(['security.policy.conflict', 'security.policy.refused'])).all()
        assert len(events) == 3
        assert {e.detail['reason'] for e in events} == {'stale_revision', 'expired', 'invalid_input'}
        encoded = json.dumps([e.detail for e in events])
        assert 'never-log-this-marker' not in encoded and 'secret-rejected-marker' not in encoded
        assert change['id'] not in encoded


async def test_ss5_reauth_denial_is_audited(api):
    from test_admin_features import owner_browser
    headers = await owner_browser(api)
    response = await api.post('/api/security/changes/preview', headers=headers,
        json={'scope': {'kind': 'instance'}, 'revision': 0})
    assert response.status_code == 403
    with api.app.state.db() as db:
        assert db.query(AuditLog).filter(AuditLog.action == 'security.policy.refused', AuditLog.actor == 'casey').count() == 1


async def test_ss2_profile_cannot_widen_policy(api, project):
    catalog = api.app.state.supervisor.catalog
    catalog.profiles['fullstack']['claude'] = {'permission_mode': 'bypassPermissions'}
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
        'name': 'Profile bypass', 'profile': 'fullstack', 'confirm_dangerous': True})
    assert response.status_code == 409, response.text
    assert 'Security' in response.text


def test_ss2_frozen_argv_is_checked_and_prepare_stays_readonly(api, project):
    from acc.server.compiler import CompileError, build_args
    with api.app.state.db() as db:
        effective = policy.resolve(db, project_id=project['id'])
    with pytest.raises(CompileError, match='Security'):
        build_args(LaunchInputs(provider='codex', approval='never'), run_dir='/fixture/run', tmux_name='fixture',
                   native_id=None, resume=False, files={'guardrails.json': json.dumps({'security': effective})})
    effective['switches']['provider_prompts'] = False
    assert policy.launch_inputs(LaunchInputs(provider='claude', permission_mode='plan'), effective).permission_mode == 'plan'
    with pytest.raises(ValueError, match='Security'):
        policy.launch_inputs(LaunchInputs(provider='claude', permission_mode='plan', extra_args=('--permission-mode=bypassPermissions',)), effective)


async def test_ss5_tightening_applies_and_replay_has_committed_outcome(api, project):
    headers = await verified(api)
    with api.app.state.db() as db:
        policy.save(db, {'kind': 'project', 'project_id': project['id']}, {'secrets': False}, actor='casey', expected_revision=0)
        db.commit()
    made = await api.post('/api/sessions', headers=headers,
        json={'project_id': project['id'], 'provider': 'claude', 'name': 'Active protection fixture'})
    assert made.status_code == 201, made.text
    preview = (await api.post('/api/security/changes/preview', headers=headers,
        json={'scope': {'kind': 'project', 'project_id': project['id']}, 'overrides': {'secrets': True}, 'revision': 1})).json()
    # A new generation may publish between preview and apply. It must appear in the
    # applied outcome even though it was not in the preview's inventory.
    raced = await api.post('/api/sessions', headers=headers,
        json={'project_id': project['id'], 'provider': 'claude', 'name': 'Between preview and apply'})
    assert raced.status_code == 201, raced.text
    refused = await api.post(f"/api/security/changes/{preview['id']}/apply", headers=headers, json={'digest': preview['digest']})
    assert preview['older_sessions'] == [made.json()['id']]
    assert refused.status_code == 200, refused.text
    assert refused.json()['older_sessions'] == sorted([made.json()['id'], raced.json()['id']])
    assert (await api.get('/api/sessions/' + raced.json()['id'], headers=headers)).json()['security_older_settings'] is True
    current = await api.get('/api/security/effective', headers=headers, params={'project_id': project['id']})
    assert current.json()['older_sessions'] == refused.json()['older_sessions']
    fresh = await api.post('/api/sessions', headers=headers, json={'project_id': project['id'], 'provider': 'claude', 'name': 'Current policy'})
    assert fresh.status_code == 201
    assert (await api.get('/api/sessions/' + fresh.json()['id'], headers=headers)).json()['security_older_settings'] is False
    with api.app.state.db() as db:
        assert policy.revision(db) == 2
        assert policy.resolve(db, project_id=project['id'])['switches']['secrets'] is True
        assert db.query(AuditLog).filter(AuditLog.action == 'security.policy.conflict').count() == 0
    # A loosening can apply to a new generation; reusing its preview is refused/audited.
    preview = (await api.post('/api/security/changes/preview', headers=headers,
        json={'scope': {'kind': 'instance'}, 'overrides': {'risk_labels': False}, 'revision': 2})).json()
    path, body = f"/api/security/changes/{preview['id']}/apply", {'digest': preview['digest']}
    assert (await api.post(path, headers=headers, json=body)).status_code == 200
    assert (await api.post(path, headers=headers, json=body)).status_code == 409
    with api.app.state.db() as db:
        assert db.query(AuditLog).filter(AuditLog.action == 'security.policy.conflict').count() == 1


async def test_ss2_scheduled_launch_cannot_use_a_frozen_bypass(api, project):
    from helpers import wait_for
    from test_schedules import payload
    headers = await verified(api)
    body = payload(project)
    body['launch'].update(permission_mode='bypassPermissions', confirm_dangerous=True)
    made = await api.post('/api/schedules', headers=headers, json=body)
    assert made.status_code == 201, made.text
    schedule = made.json()['id']
    run = await api.post(f'/api/schedules/{schedule}/run', headers=headers)
    assert run.status_code == 201, run.text
    async def failed():
        rows = (await api.get('/api/schedules', headers=headers)).json()
        return next((r for row in rows if row['id'] == schedule for r in row['runs'] if r['status'] == 'failed'), None)
    outcome = await wait_for(failed)
    assert 'Security' in outcome['reason']
    assert outcome['session_id'] is None


@pytest.mark.parametrize('exception', [False, True])
async def test_ss2_helper_permission_requires_owner_exception(api, project, env, exception):
    from test_changes import repo
    from test_team import grant, request
    repo(env['workspaces'] / 'demo')
    if exception:
        with api.app.state.db() as db:
            policy.save(db, {'kind': 'project', 'project_id': project['id']}, {'provider_prompts': False}, actor='casey', expected_revision=0)
            db.commit()
    made = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Bounded lead',
        'worktree': True, 'confirm_dangerous': True,
        'orchestrator': grant(agents=[{'agent': 'claude', 'permission_mode': 'bypassPermissions'}])})
    assert made.status_code == 201, made.text
    reply = await request(api, env, made.json(), 'start', name='Helper', agent='claude', task='Bounded work')
    assert reply['ok'] is exception, reply
    if not exception:
        assert 'Security' in reply['error']


def test_ss3_switch_cannot_race_an_inflight_ack(api, project):
    with api.app.state.db() as db:
        scope = policy.account_scope(db, 'local', project['account'])
        db.add(SecurityFolderPolicy(key=policy.scope_key(scope), machine_id='local', account=project['account'],
            roots=[], revision=2, applied_revision=1, state='pending', detail={'allowed_folders': True}))
        db.flush()
        with pytest.raises(policy.Conflict, match='pending'):
            policy.save(db, scope, {'allowed_folders': False}, actor='casey', expected_revision=0)
        assert policy.revision(db) == 0
        assert policy.resolve(db, project_id=project['id'])['switches']['allowed_folders'] is True


async def test_ss2_resume_refuses_old_bypass_before_acceptance(api, project, env):
    from helpers import wait_for
    from test_supervisor import launch, state_is

    from acc.server.models import Session
    session = await launch(api, project)
    await wait_for(state_is(api, session['id'], 'idle'))
    assert (await api.post(f"/api/sessions/{session['id']}/stop")).status_code == 200
    await wait_for(state_is(api, session['id'], 'stopped'))
    with api.app.state.db() as db:
        row = db.get(Session, session['id'])
        row.launch_spec.inputs = {**row.launch_spec.inputs, 'permission_mode': 'bypassPermissions'}
        db.commit()
    response = await api.post(f"/api/sessions/{session['id']}/resume")
    assert response.status_code == 409, response.text
    assert 'Security' in response.text
    with api.app.state.db() as db:
        assert db.get(Session, session['id']).desired_state == 'stopped'


async def test_ss5_failed_apply_rolls_back_and_audits_safe_outcome(api, monkeypatch):
    headers = await verified(api)
    preview = (await api.post('/api/security/changes/preview', headers=headers,
        json={'scope': {'kind': 'instance'}, 'overrides': {'secrets': False}, 'revision': 0})).json()
    original = policy.save
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('private-failure-marker')
    monkeypatch.setattr(policy, 'save', fail)
    with pytest.raises(RuntimeError, match='private-failure-marker'):
        await api.post(f"/api/security/changes/{preview['id']}/apply", headers=headers, json={'digest': preview['digest']})
    with api.app.state.db() as db:
        assert policy.revision(db) == 0
        events = db.query(AuditLog).filter(AuditLog.action == 'security.policy.failed').all()
        assert len(events) == 1
        assert 'private-failure-marker' not in json.dumps(events[0].detail)


def test_ss3_ack_is_bound_to_execution_machine_identity(api, project):
    with api.app.state.db() as db:
        scope = policy.account_scope(db, 'local', project['account'])
        db.add(SecurityFolderPolicy(key=policy.scope_key(scope), machine_id='other-machine', account=project['account'],
            roots=[], revision=1, applied_revision=1, state='applied', detail={'allowed_folders': True}))
        db.flush()
        effective = policy.resolve(db, project_id=project['id'])
        assert effective['enforcement']['allowed_folders'] == 'pending'
        assert effective['mode'] == 'custom'


async def test_ss1_active_session_cannot_get_an_unreviewed_exception(api, project):
    from test_supervisor import launch
    await launch(api, project)
    headers = await verified(api)
    response = await api.put('/api/tools/policy', headers=headers,
        json={'scope': 'project', 'target': str(project['id']), 'required': False})
    assert response.status_code == 410
    with api.app.state.db() as db:
        assert policy.revision(db) == 0
        assert policy.resolve(db, project_id=project['id'])['switches']['tool_install_check'] is True


async def test_ss5_changed_identity_and_stale_apply_are_conflicts(api, project):
    headers = await verified(api)
    stale_identity = await api.post('/api/security/changes/preview', headers=headers,
        json={'scope': {'kind': 'account', 'machine': 'local', 'account': project['account'], 'machine_id': 'old-identity'}, 'revision': 0})
    assert stale_identity.status_code == 409
    preview = (await api.post('/api/security/changes/preview', headers=headers,
        json={'scope': {'kind': 'instance'}, 'overrides': {'secrets': False}, 'revision': 0})).json()
    with api.app.state.db() as db:
        policy.save(db, {'kind': 'instance'}, {'risk_labels': False}, actor='casey', expected_revision=0)
        db.commit()
    response = await api.post(f"/api/security/changes/{preview['id']}/apply", headers=headers, json={'digest': preview['digest']})
    assert response.status_code == 409
    with api.app.state.db() as db:
        assert policy.revision(db) == 1
        events = db.query(AuditLog).filter(AuditLog.action == 'security.policy.conflict').all()
        assert {e.detail['reason'] for e in events} == {'identity_changed', 'stale_revision'}


async def test_ss5_compatibility_writer_refusal_has_safe_audit(api, project):
    response = await api.put('/api/tools/policy', json={'scope': 'project', 'target': str(project['id']), 'required': False})
    assert response.status_code == 403
    with api.app.state.db() as db:
        rows = db.query(AuditLog).filter(AuditLog.action == 'security.policy.refused').all()
        assert len(rows) == 1 and rows[0].detail['operation'] == 'legacy_tools'
