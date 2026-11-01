"""Round 2: account subjects and narrower frozen generations remain inspectable/usable."""
import json
from types import SimpleNamespace

import pytest

from acc.server import security_settings as policy
from acc.server.compiler import LaunchInputs
from acc.server.models import Node, Project, Setting, User


@pytest.mark.parametrize('shared,overlay,expected', [(False, None, False), (True, None, None), (True, True, True)])
async def test_remote_account_without_projects_retains_person_bindings(api, project, shared, overlay, expected):
    from test_admin_features import owner_browser
    headers = await owner_browser(api)
    with api.app.state.db() as db:
        db.add(Node(id='a' * 32, name='review-node', status='active', info={'accounts': [project['account']]}))
        db.merge(Setting(key='tools.person.casey', value={'required': False}))
        if shared:
            db.add(User(username='alex', role='member', account=project['account'], password_hash='unused', totp_secret='unused'))
        if overlay is not None:
            policy.save(db, policy.account_scope(db, 'review-node', project['account']),
                        {'tool_install_check': overlay}, actor='casey', expected_revision=0)
        db.commit()
    response = await api.get('/api/security/effective', headers=headers,
                             params={'machine': 'review-node', 'account': project['account']})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['switches']['tool_install_check'] is expected
    if expected is None:
        assert result['sources']['tool_install_check'] == {'kind': 'varies'}
    else:
        assert not result.get('varying_switches')


@pytest.mark.parametrize('machine', ['local', 'review-node'])
async def test_nullable_legacy_owner_keeps_its_subject_semantics(api, project, machine):
    from test_admin_features import owner_browser
    headers = await owner_browser(api)
    with api.app.state.db() as db:
        if machine != 'local':
            db.add(Node(id='a' * 32, name=machine, status='active', info={'accounts': [project['account']]}))
        db.add(Project(name='Legacy ownerless', slug='legacy-ownerless', workspace_path='/fixture/legacy',
                       host=machine, account=project['account'], owner=None))
        db.merge(Setting(key='tools.person.casey', value={'required': False}))
        db.commit()
    response = await api.get('/api/security/effective', headers=headers,
                             params={'machine': machine, 'account': project['account']})
    assert response.status_code == 200, response.text
    # The ownerless project's default is On; Casey's historical exception is Off.
    assert response.json()['switches']['tool_install_check'] is None
    assert response.json()['sources']['tool_install_check'] == {'kind': 'varies'}


@pytest.mark.parametrize('provider', ['antigravity', 'claude', 'codex'])
def test_protected_frozen_generation_remains_narrower_after_loosening(api, project, provider):
    with api.app.state.db() as db:
        frozen = policy.resolve(db, project_id=project['id'])
        inputs = LaunchInputs(provider=provider).as_dict()
        files = {'guardrails.json': json.dumps({'security': frozen, 'custom': []})}
        session = SimpleNamespace(project_id=project['id'], owner=project['owner'], host='local', run_as=project['account'],
                                  launch_spec=SimpleNamespace(inputs=inputs, files=files))
        policy.save(db, {'kind': 'project', 'project_id': project['id']}, {'provider_prompts': False},
                    actor='casey', expected_revision=0)
        policy.check_generation(db, session)
        assert session.launch_spec.inputs == inputs
        assert json.loads(files['guardrails.json'])['security']['switches']['provider_prompts'] is True


@pytest.mark.parametrize('provider', ['antigravity', 'claude', 'codex'])
async def test_real_protected_resume_after_loosening(api, project, provider):
    from helpers import wait_for
    from test_supervisor import launch, state_is
    session = await launch(api, project, provider=provider)
    await wait_for(state_is(api, session['id'], 'idle'))
    assert (await api.post('/api/sessions/' + session['id'] + '/stop')).status_code == 200
    await wait_for(state_is(api, session['id'], 'stopped'))
    before = (await api.get('/api/sessions/' + session['id'])).json()['launch_spec']
    with api.app.state.db() as db:
        policy.save(db, {'kind': 'project', 'project_id': project['id']}, {'provider_prompts': False},
                    actor='casey', expected_revision=0)
        db.commit()
    resumed = await api.post('/api/sessions/' + session['id'] + '/resume')
    assert resumed.status_code == 200, resumed.text
    after = await wait_for(state_is(api, session['id'], 'idle'))
    assert after['launch_spec'] == before


@pytest.mark.parametrize('provider', ['antigravity', 'claude', 'codex'])
def test_prompt_tightening_still_refuses_weaker_frozen_generation(api, project, provider):
    with api.app.state.db() as db:
        scope = {'kind': 'project', 'project_id': project['id']}
        policy.save(db, scope, {'provider_prompts': False}, actor='casey', expected_revision=0)
        frozen = policy.resolve(db, project_id=project['id'])
        inputs = LaunchInputs(provider=provider)
        if provider != 'antigravity':
            inputs = policy.launch_inputs(inputs, frozen)
        session = SimpleNamespace(project_id=project['id'], owner=project['owner'], host='local', run_as=project['account'],
            launch_spec=SimpleNamespace(inputs=inputs.as_dict(), files={'guardrails.json': json.dumps({'security': frozen, 'custom': []})}))
        policy.save(db, scope, {'provider_prompts': True}, actor='casey', expected_revision=1)
        with pytest.raises(policy.Conflict, match='Security'):
            policy.check_generation(db, session)


@pytest.mark.parametrize('unsafe', ['raw', 'bypass', 'folders'])
def test_loosened_policy_does_not_bypass_frozen_admission_checks(api, project, unsafe):
    from acc.server.models import SecurityFolderPolicy
    with api.app.state.db() as db:
        frozen = policy.resolve(db, project_id=project['id'])
        inputs = LaunchInputs(provider='claude',
            permission_mode='bypassPermissions' if unsafe == 'bypass' else 'default',
            extra_args=('--anything',) if unsafe == 'raw' else ())
        session = SimpleNamespace(project_id=project['id'], owner=project['owner'], host='local', run_as=project['account'],
            launch_spec=SimpleNamespace(inputs=inputs.as_dict(), files={'guardrails.json': json.dumps({'security': frozen, 'custom': []})}))
        policy.save(db, {'kind': 'project', 'project_id': project['id']}, {'provider_prompts': False},
                    actor='casey', expected_revision=0)
        if unsafe == 'folders':
            scope = policy.account_scope(db, 'local', project['account'])
            db.add(SecurityFolderPolicy(key=policy.scope_key(scope), machine_id='local', account=project['account'],
                roots=[], revision=1, applied_revision=0, state='pending', detail={'allowed_folders': True}))
            db.flush()
        with pytest.raises(policy.Conflict, match='Security'):
            policy.check_generation(db, session)
