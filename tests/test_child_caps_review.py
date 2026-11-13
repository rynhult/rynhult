"""Security review regressions at admission, reconciliation and human approval."""
import json
from types import SimpleNamespace

import pytest
from helpers import write_db
from sqlalchemy import select
from test_session_capabilities import family

from acc.server import child_caps, session_caps
from acc.server.compiler import LaunchInputs, launch_digest
from acc.server.models import AuditLog, Session, ToolRequest, User


@pytest.mark.parametrize('stop_before', [True, False])
async def test_explanations_survive_stopped_sources(api, project, env, monkeypatch, stop_before):
    from test_explanations import source
    original = api.app.state.supervisor.broker.call

    async def call(operation, **kwargs):
        if operation == 'explanation_result':
            return {'status': 'running'}
        return await original(operation, **kwargs)

    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', call)
    src = await source(api, project, env)
    if stop_before:
        await api.app.state.supervisor.stop(src['id'], '@api')
    response = await api.post(f"/api/sessions/{src['id']}/explanation", json={'provider': 'codex', 'model': 'test'})
    assert response.status_code == 200, response.text
    sid = response.json()['helper_id']
    if not stop_before:
        await api.app.state.supervisor.stop(src['id'], '@api')
    with write_db(api.app.state.db) as db:
        worker = db.get(Session, sid)
        assert worker.launch_spec.inputs['sandbox'] == 'read-only'
        assert worker.launch_spec.inputs['approval'] == 'never'
        assert json.loads(worker.launch_spec.files['guardrails.json'])['explanation_only']
        session_caps.check_execution(db, worker)
        await api.app.state.supervisor._reconcile_one(db, worker, {'dead': False})
        assert worker.desired_state == 'running', worker.last_error


async def test_security_edit_elsewhere_keeps_child_proof_and_execution(api, project):
    from acc.server import security_settings as security
    from acc.server.models import Project
    root, child = family(api, project, {})
    with write_db(api.app.state.db) as db:
        other = Project(name='Other', slug='other', account='other-agent', owner='other', workspace_path='/tmp/other')
        db.add(other)
        db.flush()
        parent, worker = db.get(Session, root), db.get(Session, child)
        before = child_caps.grant_digest(db, parent)
        security.save(db, {'kind': 'project', 'project_id': other.id}, {'force_push': False},
                      actor='@api', expected_revision=security.revision(db))
        assert child_caps.grant_digest(db, parent) == before
        session_caps.check_execution(db, worker)
        await api.app.state.supervisor._reconcile_one(db, worker, None)
        assert worker.desired_state == 'running'
        # Scope-relevant changes still invalidate the saved parent proof.
        security.save(db, {'kind': 'project', 'project_id': project['id']}, {'force_push': False},
                      actor='@api', expected_revision=security.revision(db))
        assert child_caps.grant_digest(db, parent) != before
        with pytest.raises(child_caps.Refused, match='parent grant changed'):
            child_caps.check(db, worker)


@pytest.mark.parametrize('provider,key,parent_value,child_value', [
    ('claude', 'permission_mode', 'default', 'acceptEdits'),
    ('claude', 'permission_mode', 'dontAsk', 'default'),
    ('claude', 'permission_mode', 'acceptEdits', 'auto'),
    ('codex', 'approval', 'untrusted', 'on-request'),
    ('codex', 'sandbox', 'workspace-write', 'danger-full-access'),
    ('antigravity', 'mode', 'accept-edits', 'auto'),
    ('copilot', 'mode', 'plan', 'autopilot'),
    ('opencode', 'permissions', 'all', 'commands'),
    ('cursor', 'mode', 'ask', 'plan'),
])
def test_provider_modes_cannot_widen(monkeypatch, provider, key, parent_value, child_value):
    def inputs(value):
        return LaunchInputs(provider=provider, **({key: value} if key in ('permission_mode', 'approval', 'sandbox')
                                                  else {'options': {key: value}}))
    parent = SimpleNamespace(id='parent', launch_spec=SimpleNamespace(inputs=inputs(parent_value).as_dict()))
    monkeypatch.setattr(child_caps, 'check', lambda *_: None)
    with pytest.raises(child_caps.Refused, match='limited by parent'):
        child_caps.constrain(None, parent, inputs(child_value), {})


def policy_fixture(monkeypatch, parent_inputs):
    from acc.server import security_settings
    parent = SimpleNamespace(id='parent', project_id=1, owner='alex', host='local', run_as='agent',
                             launch_spec=SimpleNamespace(inputs=parent_inputs.as_dict()))
    monkeypatch.setattr(child_caps, 'check', lambda *_: None)
    monkeypatch.setattr(security_settings, 'frozen_protections', lambda *_: ({}, {'provider_prompts': True}))
    monkeypatch.setattr(security_settings, 'resolve', lambda *a, **kw: {'switches': {'provider_prompts': True}})
    return parent, {'security': {'switches': {'provider_prompts': True}}, 'rules': {},
                    'tool_approval': True, 'local_only': False, 'custom': []}


@pytest.mark.parametrize('parent_inputs,child_inputs,allowed', [
    (LaunchInputs(provider='claude', permission_mode='default'), LaunchInputs(provider='codex'), False),
    (LaunchInputs(provider='claude', permission_mode='acceptEdits'), LaunchInputs(provider='codex'), True),
    (LaunchInputs(provider='claude', permission_mode='default'), LaunchInputs(provider='codex', approval='untrusted'), True),
    (LaunchInputs(provider='claude', permission_mode='default'), LaunchInputs(provider='codex', sandbox='read-only'), True),
    (LaunchInputs(provider='claude', permission_mode='acceptEdits'), LaunchInputs(provider='codex', approval='never'), False),
    (LaunchInputs(provider='claude', permission_mode='acceptEdits'), LaunchInputs(provider='codex', sandbox='danger-full-access'), False),
    (LaunchInputs(provider='claude', permission_mode='bypassPermissions'), LaunchInputs(provider='codex'), True),
    (LaunchInputs(provider='claude', permission_mode='bypassPermissions'), LaunchInputs(provider='codex', approval='never'), False),
    (LaunchInputs(provider='codex'), LaunchInputs(provider='claude', permission_mode='default'), True),
    (LaunchInputs(provider='claude', permission_mode='default'), LaunchInputs(provider='antigravity'), False),
    (LaunchInputs(provider='claude', permission_mode='acceptEdits'), LaunchInputs(provider='antigravity'), True),
    (LaunchInputs(provider='claude', permission_mode='auto'), LaunchInputs(provider='copilot', options={'mode': 'autopilot'}), True),
    (LaunchInputs(provider='claude', permission_mode='default'), LaunchInputs(provider='cursor'), True),
    (LaunchInputs(provider='claude', permission_mode='default'), LaunchInputs(provider='opencode', options={'permissions': 'all'}), True),
    (LaunchInputs(provider='claude', permission_mode='default'), LaunchInputs(provider='opencode'), False),
])
def test_cross_provider_permission_levels(monkeypatch, parent_inputs, child_inputs, allowed):
    parent, policy = policy_fixture(monkeypatch, parent_inputs)
    if allowed:
        assert child_caps.constrain(None, parent, child_inputs, policy)[0] == child_inputs
    else:
        with pytest.raises(child_caps.Refused):
            child_caps.constrain(None, parent, child_inputs, policy)


@pytest.mark.parametrize('endpoint,allowed', [(None, True), ('parent-endpoint', True), ('other-endpoint', False)])
def test_endpoint_intersection(monkeypatch, endpoint, allowed):
    parent, policy = policy_fixture(monkeypatch, LaunchInputs(provider='claude', endpoint='parent-endpoint'))
    inputs = LaunchInputs(provider='claude', endpoint=endpoint)
    if allowed:
        assert child_caps.constrain(None, parent, inputs, policy)[0].endpoint == endpoint
    else:
        with pytest.raises(child_caps.Refused, match='endpoint'):
            child_caps.constrain(None, parent, inputs, policy)


async def test_owners_only_child_alert_stays_owners_only(api, project):
    from acc.server import attention
    root, child = family(api, project, {})
    with write_db(api.app.state.db) as db:
        db.add(User(username='alex', role='member', account='agent', password_hash='unused', totp_secret='unused'))
        child_caps.transfer_owner(db, [db.get(Session, root), db.get(Session, child)], 'alex', '@api')
        item = attention.raise_item(db, 'owner-alert', kind='replacement_held', source='supervisor',
                                    title='Held', session_id=child, owner='')
        assert item.owner is None
        assert root in item.detail and child in item.detail
        member = attention.raise_item(db, 'root-alert', kind='needs_input', source='hook', title='Input', session_id=child)
        assert member.owner == 'alex'


async def test_unexpected_cap_error_stops_execution_and_logs(api, project, monkeypatch, caplog):
    root, child = family(api, project, {})
    def broken(*args):
        raise RuntimeError('cap resolver broke')
    monkeypatch.setattr(session_caps, 'check_execution', broken)
    with write_db(api.app.state.db) as db:
        worker = db.get(Session, child)
        await api.app.state.supervisor._reconcile_one(db, worker, None)
        assert worker.desired_state == 'stopped'
        assert 'execution refused' in worker.last_error
    assert 'cap resolver broke' in caplog.text and child in caplog.text


@pytest.mark.parametrize('root_person', ['@api', 'casey'])
async def test_child_tools_only_root_human_browser(api, project, env, monkeypatch, root_person):
    from test_auth import cookie_client, do_setup
    from test_users import csrf

    from acc.server.tools import ToolRefused
    await do_setup(api, env)
    root, child = family(api, project, {})
    service = api.app.state.supervisor.tools
    monkeypatch.setattr(service, 'schedule', lambda *args: None)
    with write_db(api.app.state.db) as db:
        db.add(User(username='other-owner', role='owner', account='other-agent', password_hash='unused', totp_secret='unused'))
        if root_person != '@api':
            child_caps.transfer_owner(db, [db.get(Session, root), db.get(Session, child)], root_person, '@api')
        row = db.get(Session, child)
        db.add(ToolRequest(id='child-tool', session_id=child, project_id=project['id'], owner=root_person,
                           account=row.run_as, host='local', manager='npm', name='prettier', reason='format', operation='install'))
        db.commit()
    path = '/api/tools/requests/child-tool/approve'
    assert (await api.post(path)).status_code == 403  # installation token
    if root_person != '@api':
        with pytest.raises(ToolRefused, match='root person'):
            service.decide('child-tool', 'other-owner', True, browser_session=True)
    with pytest.raises(ToolRefused):
        service.decide('child-tool', 'session:' + root, True, browser_session=True)
    with pytest.raises(ToolRefused, match='root person'):
        service.decide('child-tool', 'casey', True)  # no browser session
    browser = cookie_client(api)
    result = await browser.post(path, headers=csrf(browser))
    assert result.status_code == 200, result.text
    with api.app.state.db() as db:
        assert db.get(ToolRequest, 'child-tool').decided_by == 'casey'


async def test_removal_transfers_whole_chain_and_refreshes_proofs(api, project, env, member_accounts):
    from test_auth import do_setup
    from test_identity import member_with_things

    from acc.server.models import Project
    await do_setup(api, env)
    user_id, _ = await member_with_things(api, env)
    root, child = family(api, project, {})
    with write_db(api.app.state.db) as db:
        child_caps.transfer_owner(db, [db.get(Session, root), db.get(Session, child)], 'dana', '@api',
                                  projects=[db.get(Project, project['id'])])
        db.commit()
    result = await api.post(f'/api/users/{user_id}/remove', json={'resources': 'transfer'})
    assert result.status_code == 200, result.text
    with api.app.state.db() as db:
        parent, worker = db.get(Session, root), db.get(Session, child)
        assert parent.owner == parent.root_owner == worker.owner == worker.root_owner == '@api'
        assert worker.parent_session_id == root and worker.parent_chain == [root]
        session_caps.check_execution(db, worker)
        audit = db.scalars(select(AuditLog).where(AuditLog.action == 'session.owner.transfer')).all()
        assert any(set(row.detail['sessions']) == {root, child} and row.actor == '@api' for row in audit)


async def test_non_owner_cannot_transfer_chain(api, project):
    root, child = family(api, project, {})
    with write_db(api.app.state.db) as db:
        db.add(User(username='member', role='member', account='agent', password_hash='unused', totp_secret='unused'))
        db.flush()
        with pytest.raises(child_caps.Refused, match='Only an owner'):
            child_caps.transfer_owner(db, [db.get(Session, root), db.get(Session, child)], 'member', 'member')
        assert db.get(Session, child).root_owner == '@api'


async def test_person_files_ignore_stopped_parent_and_agent_folder_cap(api, project, env):
    from test_team import lead, request
    root = await lead(api, project, env)
    child = await request(api, env, root, 'start', name='Reader', agent='claude', profile=None, task='work')
    assert child['ok'], child
    sid = child['result']['id']
    with write_db(api.app.state.db) as db:
        session_caps.narrow(db, db.get(Session, root['id']), {'folders': []}, '@api')
        db.get(Session, root['id']).desired_state = 'stopped'
        db.commit()
    response = await api.get('/api/files/session/' + sid)
    assert response.status_code == 200, response.text


async def test_team_explanation_can_admit_stopped_helper_source(api, project, env):
    from test_team import lead, request

    from acc.server.models import LaunchSpec, Team
    root = await lead(api, project, env)
    result = await request(api, env, root, 'start', name='Worker', agent='claude', profile=None, task='work')
    assert result['ok'], result
    sid = result['result']['id']
    with write_db(api.app.state.db) as db:
        # A completed helper still has its parent's frozen grant, despite liveness changes.
        db.get(Session, root['id']).desired_state = 'stopped'
        db.get(Session, sid).desired_state = 'stopped'
        team = db.get(Team, root['id'])
        team.stopped = True
        team.lifetime_revision += 1
        source = db.get(Session, sid)
        # Use the actual source grant with the explanation admission override.
        from acc.server import guardrails
        policy = guardrails.policy(db, '/tmp/explanation/workspace', source.owner, source.project_id,
                                   machine=source.host, account=source.run_as)
        inputs = LaunchInputs(provider='codex', sandbox='read-only', approval='never')
        child_caps.constrain(db, source, inputs, policy, explanation=True)
        policy['explanation_only'] = True
        explanation = Session(id='team-explanation', provider='codex', owner=source.owner,
                              parent_session_id=source.id, run_as=source.run_as, project_id=source.project_id,
                              launch_spec=LaunchSpec(provider='codex', inputs=inputs.as_dict(), files={
                                  'explanation.json': '{}', 'guardrails.json': json.dumps(policy),
                                  child_caps.FILE: json.dumps(child_caps.proof(db, source))}))
        session_caps.attach(db, explanation)
        session_caps.check_execution(db, explanation)
        with pytest.raises(child_caps.Refused, match='parent team is stopped'):
            child_caps.check(db, source)


async def test_member_root_approves_own_child_in_browser(api, project, env, member_accounts, monkeypatch):
    from test_auth import do_setup
    from test_users import browser, csrf, join
    await do_setup(api, env)
    async with browser(api) as member:
        link = (await api.post('/api/users/invites', json={'role': 'member', 'account': 'dana-agent'})).json()['link']
        assert (await join(member, link, 'alex')).status_code == 200
        root, child = family(api, project, {})
        with write_db(api.app.state.db) as db:
            rows = [db.get(Session, root), db.get(Session, child)]
            for row in rows:
                row.run_as = 'dana-agent'
            # Fixture account changes precede proof binding, just as a launch placement does.
            rows[1].launch_spec.files = {child_caps.FILE: json.dumps(child_caps.proof(db, rows[0]))}
            rows[1].launch_spec.digest = launch_digest(rows[1].launch_spec.inputs, rows[1].launch_spec.files)
            child_caps.transfer_owner(db, rows, 'alex', '@api')
            db.add(ToolRequest(id='member-child-tool', session_id=child, project_id=project['id'], owner='alex',
                               account='dana-agent', host='local', manager='npm', name='prettier',
                               reason='format', operation='install'))
            db.commit()
        service = api.app.state.supervisor.tools
        monkeypatch.setattr(service, 'schedule', lambda *a: None)
        from acc.server.tools import ToolRefused
        with pytest.raises(ToolRefused, match='root person'):
            service.decide('member-child-tool', 'casey', True, browser_session=True)
        response = await member.post('/api/tools/requests/member-child-tool/approve', headers=csrf(member))
        assert response.status_code == 200, response.text


def test_all_provider_permission_orders_cover_their_options():
    from acc import providers
    for provider in providers.PROVIDERS.values():
        for option in provider.options:
            if option.permission_order:
                values = {False, True} if option.kind == 'bool' else {value for value, _ in option.choices}
                assert set(option.permission_order) == values
                assert len(option.permission_levels) == len(option.permission_order)
                assert all(0 <= level <= 4 for level in option.permission_levels)


def test_never_is_not_a_normal_child_or_security_exception(monkeypatch):
    from acc.server import security_settings
    inputs = LaunchInputs(provider='codex', sandbox='read-only', approval='never')
    with pytest.raises(ValueError, match='provider prompts'):
        security_settings.validate_launch_inputs(inputs, {'switches': {'provider_prompts': True}})
    parent, policy = policy_fixture(monkeypatch, LaunchInputs(provider='codex', approval='untrusted'))
    with pytest.raises(child_caps.Refused):
        child_caps.constrain(None, parent, inputs, policy)


@pytest.mark.parametrize('sandbox,approval,level', [
    ('read-only', 'untrusted', 0), ('read-only', 'on-request', 0), ('read-only', 'never', 0),
    ('workspace-write', 'untrusted', 1), ('workspace-write', 'on-request', 2), ('workspace-write', 'never', 2),
    ('danger-full-access', 'untrusted', 4), ('danger-full-access', 'on-request', 4), ('danger-full-access', 'never', 4),
])
def test_codex_action_levels_keep_read_only_untrusted_lower(sandbox, approval, level):
    from acc import providers
    inputs = LaunchInputs(provider='codex', sandbox=sandbox, approval=approval)
    assert providers.get('codex').permission_level(inputs) == level


def test_codex_never_still_needs_its_own_bypass_authorization(monkeypatch):
    parent, policy = policy_fixture(monkeypatch, LaunchInputs(provider='codex', approval='on-request'))
    with pytest.raises(child_caps.Refused, match='parent does not permit provider bypass'):
        child_caps.constrain(None, parent, LaunchInputs(provider='codex', approval='never'), policy)
