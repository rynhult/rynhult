"""Scoped owner policy: explicit false, restore, consent and frozen launches."""
import json

import pytest

from acc.server.models import AuditLog, Setting


def test_registry_and_legacy_person_exception_survive_scopes(api, project):
    from acc.server import security_settings as security
    with api.app.state.db() as db:
        db.merge(Setting(key='guardrails', value={'rules': {'secrets': False}, 'custom': [{'pattern': 'private-command'}]}))
        db.merge(Setting(key='tools.person.casey', value={'required': False}))
        db.flush()
        effective = security.resolve(db, project_id=project['id'], owner='casey')
        assert effective['switches']['secrets'] is False
        assert effective['switches']['tool_install_check'] is False
        assert effective['switches']['force_push'] is True
        assert set(effective['switches']) == {'force_push', 'push_default', 'merge_pr', 'rm_outside', 'secrets',
            'custom_rules', 'allowed_folders', 'tool_install_check', 'budget_checks', 'risk_labels', 'provider_prompts'}


def test_scope_inheritance_unrestricted_restore_and_stale_revision(api, project):
    from acc.server import security_settings as security
    with api.app.state.db() as db:
        scope = {'kind': 'project', 'project_id': project['id']}
        security.save(db, {'kind': 'instance'}, {'secrets': False}, actor='casey', expected_revision=0)
        security.save(db, scope, {'force_push': False}, actor='casey', expected_revision=1)
        original = security.resolve(db, project_id=project['id'])
        assert original['switches']['secrets'] is False
        assert original['sources']['force_push']['kind'] == 'project'
        security.save(db, scope, {}, actor='casey', expected_revision=2, preset='unrestricted')
        relaxed = security.resolve(db, project_id=project['id'])
        assert relaxed['mode'] == 'custom'  # account folder protection still on
        assert not relaxed['switches']['provider_prompts']
        assert relaxed['switches']['allowed_folders']
        security.save(db, scope, {}, actor='casey', expected_revision=3, preset='restore')
        assert security.resolve(db, project_id=project['id'])['switches'] == original['switches']
        with pytest.raises(security.Conflict):
            security.save(db, scope, {}, actor='casey', expected_revision=2)


def test_project_cannot_override_account_folder_guard(api, project):
    from acc.server import security_settings as security
    with api.app.state.db() as db:
        with pytest.raises(ValueError, match='machine/account'):
            security.save(db, {'kind': 'project', 'project_id': project['id']}, {'allowed_folders': False},
                          actor='casey', expected_revision=0)


def test_resolved_guard_commands_and_custom_blocks(api, project):
    from test_guardrails import rule

    from acc.server import guardrails
    from acc.server import security_settings as security
    with api.app.state.db() as db:
        db.merge(Setting(key='guardrails', value={'custom': [{'pattern': 'private-command'}]}))
        db.flush()
        assert rule('private-command', policy=guardrails.policy(db, '/srv/ws', 'casey', project['id'])) == 'custom'
        security.save(db, {'kind': 'project', 'project_id': project['id']},
                      {'force_push': False, 'custom_rules': False}, actor='casey', expected_revision=0)
        policy = guardrails.policy(db, '/srv/ws', 'casey', project['id'])
        assert rule('git push --force origin feature', policy=policy) is None
        assert rule('private-command', policy=policy) is None
        assert rule('cat .env', policy=policy) == 'secrets'
        assert json.loads(json.dumps(policy))['security']['revision'] == 1


def test_provider_bypass_is_supported_explicitly_and_readonly_remains(api, project):
    from acc.server import security_settings as security
    from acc.server.compiler import LaunchInputs
    with api.app.state.db() as db:
        security.save(db, {'kind': 'project', 'project_id': project['id']}, {'provider_prompts': False}, actor='casey', expected_revision=0)
        policy = security.resolve(db, project_id=project['id'])
        assert security.launch_inputs(LaunchInputs(provider='codex'), policy).sandbox == 'danger-full-access'
        assert security.launch_inputs(LaunchInputs(provider='claude'), policy).permission_mode == 'bypassPermissions'
        assert security.launch_inputs(LaunchInputs(provider='codex', sandbox='read-only'), policy).sandbox == 'read-only'
        with pytest.raises(ValueError, match='support'):
            security.launch_inputs(LaunchInputs(provider='antigravity'), policy)


async def test_security_api_rejects_install_token_and_requires_recent_owner(api):
    assert (await api.post('/api/security/changes/preview', json={'scope': {'kind': 'instance'}, 'overrides': {'secrets': False}, 'revision': 0})).status_code == 403
    from test_admin_features import owner_browser
    headers = await owner_browser(api)
    response = await api.post('/api/security/changes/preview', headers=headers,
        json={'scope': {'kind': 'instance'}, 'overrides': {'secrets': False}, 'revision': 0})
    assert response.status_code == 403


async def test_security_api_preview_apply_is_bound_audited_and_one_time(api, monkeypatch):
    from test_admin_features import owner_browser

    from acc.server import auth
    from acc.server.models import AuthSession
    headers = await owner_browser(api)
    token = api.cookies.get('acc_session')
    with api.app.state.db() as db:
        db.get(AuthSession, auth.digest(token)).reauthenticated_at = auth.now()
        db.commit()
    body = {'scope': {'kind': 'instance'}, 'overrides': {'secrets': False}, 'revision': 0}
    preview = await api.post('/api/security/changes/preview', headers=headers, json=body)
    assert preview.status_code == 200, preview.text
    change = preview.json()
    applied = await api.post(f"/api/security/changes/{change['id']}/apply", headers=headers, json={'digest': change['digest']})
    assert applied.status_code == 200, applied.text
    assert applied.json()['revision'] == 1
    assert (await api.post(f"/api/security/changes/{change['id']}/apply", headers=headers, json={'digest': change['digest']})).status_code == 409
    with api.app.state.db() as db:
        assert db.query(AuditLog).filter(AuditLog.action == 'security.policy.applied').count() == 1


def test_scope_controls_budget_risk_and_account_tool_checks(api, project, monkeypatch):
    from acc.server import budgets, risk_labels, tools
    from acc.server import security_settings as security
    monkeypatch.setattr(budgets, 'status', lambda db, owner: {'claude.five_hour': {'over': True, 'action': 'pause'}})
    with api.app.state.db() as db:
        assert budgets.paused(db, 'casey', 'claude', project_id=project['id'])
        security.save(db, {'kind': 'project', 'project_id': project['id']},
            {'budget_checks': False, 'risk_labels': False, 'tool_install_check': False}, actor='casey', expected_revision=0)
        assert not budgets.paused(db, 'casey', 'claude', project_id=project['id'])
        assert not risk_labels.enabled(db, 'casey', project_id=project['id'])
        assert not tools.required(db, 'casey', project['id'])
        assert budgets.paused(db, 'casey', 'claude')  # unrelated scopes keep their cap


def test_compiler_and_saved_inputs_apply_provider_policy(api, project):
    from acc.server import guardrails
    from acc.server import security_settings as security
    from acc.server.compiler import LaunchInputs, compile_files
    with api.app.state.db() as db:
        security.save(db, {'kind': 'project', 'project_id': project['id']}, {'provider_prompts': False},
                      actor='casey', expected_revision=0)
        policy = guardrails.policy(db, '/srv/ws', 'casey', project['id'])
        compiled = compile_files(LaunchInputs(provider='claude'), '/tmp/acc-hook', '/tmp/acc-statusline', guardrails=policy)
        settings = json.loads(compiled.files['settings.json'])
        assert settings.get('skipDangerousModePermissionPrompt') is True


def test_generation_never_widens_and_refuses_weaker_frozen_resume(api, project):
    from types import SimpleNamespace

    from acc.server import guardrails
    from acc.server import security_settings as security
    with api.app.state.db() as db:
        policy = guardrails.policy(db, '/srv/ws', 'casey', project['id'])
        session = SimpleNamespace(launch_spec=SimpleNamespace(files={'guardrails.json': json.dumps(policy)}),
            project_id=project['id'], owner='casey', host='local', run_as=project['account'])
        security.save(db, {'kind': 'project', 'project_id': project['id']}, {'secrets': False}, actor='casey', expected_revision=0)
        security.check_generation(db, session)
        assert json.loads(session.launch_spec.files['guardrails.json'])['rules']['secrets'] is True
        session.launch_spec.files['guardrails.json'] = json.dumps(guardrails.policy(db, '/srv/ws', 'casey', project['id']))
        security.save(db, {'kind': 'project', 'project_id': project['id']}, {'secrets': True}, actor='casey', expected_revision=1)
        with pytest.raises(security.Conflict, match='new session'):
            security.check_generation(db, session)


async def test_legacy_settings_endpoint_cannot_skip_browser_consent(api):
    response = await api.put('/api/guardrails', json={'rules': {'secrets': False}})
    assert response.status_code == 403


@pytest.mark.parametrize('preset', ['custom', 'restore', 'default'])
async def test_tightening_applies_and_lists_only_weaker_frozen_sessions(api, project, preset):
    from test_supervisor import launch

    from acc.server import security_settings as security
    from acc.server.models import AuditLog, Session
    scope = {'kind': 'project', 'project_id': project['id']}
    strong = await launch(api, project)
    with api.app.state.db() as db:
        security.save(db, scope, {'secrets': False}, actor='casey', expected_revision=0,
                      preset='unrestricted' if preset == 'restore' else 'custom')
        db.commit()
    weak = await launch(api, project)
    with api.app.state.db() as db:
        frozen = dict(db.get(Session, weak['id']).launch_spec.files)
        security.save(db, scope, {} if preset == 'default' else {'secrets': True}, actor='casey',
                      expected_revision=1, preset='restore' if preset == 'restore' else 'custom')
        db.commit()
        assert security.revision(db) == 2
        assert security.resolve(db, project_id=project['id'])['switches']['secrets'] is True
        assert security.older_sessions(db, scope) == [weak['id']]
        assert db.get(Session, weak['id']).launch_spec.files == frozen
        security.check_generation(db, db.get(Session, strong['id']))
        with pytest.raises(security.Conflict, match='new session'):
            security.check_generation(db, db.get(Session, weak['id']))
        audit = db.query(AuditLog).filter(AuditLog.action == 'security.policy.applied').order_by(AuditLog.id.desc()).first()
        assert audit.detail['older_sessions'] == [weak['id']]
        assert audit.detail['older_session_count'] == 1
    new = await launch(api, project)
    assert json.loads(new['launch_spec']['files']['guardrails.json'])['security']['switches']['secrets'] is True


def test_account_policy_does_not_transfer_to_reenrolled_machine(api, project):
    from acc.server import security_settings as security
    from acc.server.models import Node
    with api.app.state.db() as db:
        db.add(Node(id='a'*32, name='dev-node', status='active', info={'accounts': [project['account']]}))
        db.flush()
        security.save(db, {'kind': 'account', 'machine': 'dev-node', 'account': project['account']}, {'secrets': False},
                      actor='casey', expected_revision=0)
        assert not security.resolve(db, machine='dev-node', account=project['account'])['switches']['secrets']
        db.delete(db.get(Node, 'a'*32))
        db.flush()
        db.add(Node(id='b'*32, name='dev-node', status='active', info={'accounts': [project['account']]}))
        db.flush()
        assert security.resolve(db, machine='dev-node', account=project['account'])['switches']['secrets'] is True


async def test_real_session_freezes_provider_mapping_in_launch_inputs(api, project):
    from acc.server import security_settings as security
    with api.app.state.db() as db:
        security.save(db, {'kind': 'project', 'project_id': project['id']}, {'provider_prompts': False}, actor='casey', expected_revision=0)
        db.commit()
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Reviewed bypass'})
    assert response.status_code == 201, response.text
    details = (await api.get('/api/sessions/' + response.json()['id'])).json()
    assert details['launch_spec']['inputs']['permission_mode'] == 'bypassPermissions'
    assert json.loads(details['launch_spec']['files']['guardrails.json'])['security']['revision'] == 1


def test_security_switches_do_not_relax_team_integration_authority():
    from test_guardrails import rule
    policy = {'rules': {'team': True, 'push_default': False, 'merge_pr': False}, 'custom': [], 'workspace': '/srv/ws'}
    assert rule('git push origin main', policy=policy) == 'push_default'
    assert rule('gh pr merge 7 --squash', policy=policy) == 'team'
    ordinary = {**policy, 'rules': {**policy['rules'], 'team': False}}
    assert rule('git push origin main', policy=ordinary) is None
    assert rule('gh pr merge 7 --squash', policy=ordinary) is None
    assert rule('git update-ref refs/heads/main abc', policy=policy) == 'team'


@pytest.mark.parametrize('command', ['git push --force origin feature', 'git push origin main',
    'gh pr merge 3 --squash', 'rm -rf /home/alex/outside', 'cat .env', 'npm install -g demo-tool'])
def test_unrestricted_hook_policy_disables_each_configurable_command_check(api, project, command):
    from test_guardrails import rule

    from acc.server import guardrails
    from acc.server import security_settings as security
    with api.app.state.db() as db:
        security.save(db, {'kind': 'project', 'project_id': project['id']}, {}, actor='casey', expected_revision=0,
                      preset='unrestricted')
        assert rule(command, policy=guardrails.policy(db, '/srv/ws', 'casey', project['id'])) is None


async def test_malformed_security_scope_is_rejected_as_validation_error(api):
    from test_admin_features import owner_browser

    from acc.server import auth
    from acc.server.models import AuthSession
    headers = await owner_browser(api)
    with api.app.state.db() as db:
        db.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = auth.now()
        db.commit()
    response = await api.post('/api/security/changes/preview', headers=headers,
        json={'scope': {'kind': []}, 'revision': 0})
    assert response.status_code == 422


def test_stopped_generation_does_not_resume_without_new_custom_blocks(api, project):
    from types import SimpleNamespace

    from acc.server import guardrails
    from acc.server import security_settings as security
    with api.app.state.db() as db:
        frozen = guardrails.policy(db, '/srv/ws', 'casey', project['id'])
        session = SimpleNamespace(launch_spec=SimpleNamespace(files={'guardrails.json': json.dumps(frozen)}),
            project_id=project['id'], owner='casey', host='local', run_as=project['account'])
        db.merge(Setting(key='guardrails', value={'custom': [{'pattern': 'newly-blocked'}]}))
        db.flush()
        with pytest.raises(security.Conflict, match='custom'):
            security.check_generation(db, session)


def test_unrestricted_does_not_claim_folder_enforcement_without_machine_ack(api, project):
    from acc.server import security_settings as security
    with api.app.state.db() as db:
        security.save(db, {'kind': 'account', 'machine': 'local', 'account': project['account']}, {},
                      actor='casey', expected_revision=0, preset='unrestricted')
        result = security.resolve(db, project_id=project['id'])
        assert result['policy_mode'] == 'unrestricted'
        assert result['mode'] == 'custom'
        assert result['enforcement']['allowed_folders'] == 'pending'


async def test_owner_can_resolve_account_without_creating_project_override(api, project):
    from acc.server import security_settings as security
    with api.app.state.db() as db:
        security.save(db, {'kind': 'account', 'machine': 'local', 'account': project['account']},
                      {'secrets': False}, actor='casey', expected_revision=0)
        db.commit()
    from test_admin_features import owner_browser
    headers = await owner_browser(api)
    response = await api.get('/api/security/effective', headers=headers, params={'machine': 'local', 'account': project['account']})
    assert response.status_code == 200, response.text
    assert response.json()['switches']['secrets'] is False
    assert response.json()['sources']['secrets']['kind'] == 'account'


async def test_member_legacy_read_does_not_expose_custom_command_patterns(api):
    from acc.server.api.context import Principal
    with api.app.state.db() as db:
        db.merge(Setting(key='guardrails', value={'custom': [{'pattern': 'private-owner-path', 'reason': 'private'}]}))
        db.commit()
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: Principal('alex', 'member', 'alex-agent')
    response = await api.get('/api/guardrails')
    assert response.status_code == 200
    assert response.json()['custom'] == []


def test_instance_budget_switch_resolves_alerts_without_deleting_rules(api):
    import time

    from acc.server import budgets
    from acc.server import security_settings as security
    from acc.server.models import AttentionItem, Observation
    with api.app.state.db() as db:
        db.merge(Setting(key='budget.@api', value={'rules': {'claude.five_hour': {'mode': 'cap', 'limit': 50, 'action': 'pause'}}}))
        db.merge(Observation(key='claude.rate_limits', source='fixture', observed_at=time.time(), value={'five_hour': {'used_percentage': 99, 'resets_at': time.time()+3600}}))
        db.flush()
        budgets.evaluate(db)
        assert db.query(AttentionItem).filter(AttentionItem.kind == 'budget', AttentionItem.resolved_at.is_(None)).count() == 1
        security.save(db, {'kind': 'instance'}, {'budget_checks': False}, actor='casey', expected_revision=0)
        budgets.evaluate(db)
        assert db.query(AttentionItem).filter(AttentionItem.kind == 'budget', AttentionItem.resolved_at.is_(None)).count() == 0
        assert db.get(Setting, 'budget.@api').value['rules']['claude.five_hour']['limit'] == 50


async def test_pattern_save_is_revision_checked_instance_partial_update(api):
    from test_admin_features import owner_browser

    from acc.server import auth
    from acc.server import security_settings as security
    from acc.server.models import AuthSession, SecurityPolicy
    headers = await owner_browser(api)
    with api.app.state.db() as db:
        db.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = auth.now()
        db.merge(Setting(key='tools.person.casey', value={'required': False}))
        security.save(db, {'kind': 'instance'}, {'tool_install_check': True, 'secrets': False},
                      actor='casey', expected_revision=0)
        db.commit()
    response = await api.put('/api/guardrails', headers=headers,
                             json={'revision': 1, 'custom': [{'pattern': 'blocked-command'}]})
    assert response.status_code == 200, response.text
    with api.app.state.db() as db:
        assert db.get(SecurityPolicy, security.scope_key({'kind': 'instance'})).overrides == {
            'tool_install_check': True, 'secrets': False}
        assert security.resolve(db, owner='casey')['switches']['tool_install_check'] is True
    stale = await api.put('/api/guardrails', headers=headers, json={'revision': 1, 'custom': []})
    assert stale.status_code == 409
    assert (await api.get('/api/guardrails', headers=headers)).json()['custom'][0]['pattern'] == 'blocked-command'
    updated = await api.put('/api/guardrails', headers=headers,
                            json={'revision': 2, 'rules': {'force_push': False}})
    assert updated.status_code == 200, updated.text
    assert updated.json()['custom'][0]['pattern'] == 'blocked-command'
    with api.app.state.db() as db:
        assert db.get(SecurityPolicy, security.scope_key({'kind': 'instance'})).overrides == {
            'tool_install_check': True, 'secrets': False, 'force_push': False}


async def test_fresh_default_account_inherits_and_can_save_without_project(api):
    from test_admin_features import owner_browser

    from acc.server import auth
    from acc.server.models import AuthSession
    headers = await owner_browser(api)
    with api.app.state.db() as db:
        db.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = auth.now()
        db.commit()
    scope = {'kind': 'account', 'machine': 'local', 'account': 'agent'}
    from test_admin_features import owner_browser
    headers = await owner_browser(api)
    response = await api.get('/api/security/effective', headers=headers, params={'machine': 'local', 'account': 'agent'})
    assert response.status_code == 200, response.text
    assert response.json()['mode'] == 'default'
    assert response.json()['sources']['secrets'] == {'kind': 'default'}
    preview = await api.post('/api/security/changes/preview', headers=headers,
                             json={'scope': scope, 'overrides': {'secrets': False}, 'revision': 0})
    assert preview.status_code == 200, preview.text
    change = preview.json()
    applied = await api.post(f"/api/security/changes/{change['id']}/apply", headers=headers,
                             json={'digest': change['digest']})
    assert applied.status_code == 200, applied.text
    assert (await api.get('/api/security/effective', headers=headers, params={'machine': 'local', 'account': 'unknown-agent'})).status_code == 422
