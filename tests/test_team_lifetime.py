"""Optional deadlines, durable warnings and explicit team resume consent."""
from datetime import timedelta

import pytest
from helpers import write_db
from test_team import lead

from acc.server.models import Team, utcnow


async def test_default_grant_has_no_deadline_and_hours_can_be_removed(api, project, env):
    session = await lead(api, project, env, hours=None)
    url = f"/api/sessions/{session['id']}/team"
    assert (await api.get(url)).json()['expires_at'] is None
    changed = await api.post(url + '/lifetime', json={'hours': 4})
    assert changed.status_code == 200, changed.text
    assert (await api.get(url)).json()['expires_at']
    assert (await api.post(url + '/lifetime', json={'hours': None})).status_code == 200
    assert (await api.get(url)).json()['expires_at'] is None


async def test_expiry_warning_and_resume_preview(api, project, env):
    session = await lead(api, project, env, hours=1)
    sid = session['id']
    with write_db(api.app.state.db) as db:
        db.get(Team, sid).expires_at = utcnow() + timedelta(minutes=20)
        db.commit()
    await api.app.state.supervisor.expire_teams()
    alerts = (await api.get('/api/attention')).json()
    assert any('time limit' in row['title'].lower() for row in alerts)
    await api.post(f'/api/sessions/{sid}/team/stop')
    preview = await api.get(f'/api/sessions/{sid}/team/resume')
    assert preview.status_code == 200
    assert sid in [r['id'] for r in preview.json()['sessions']]
    response = await api.post(f'/api/sessions/{sid}/team/resume', json={'revision': preview.json()['revision'], 'hours': None})
    assert response.status_code == 200, response.text
    assert (await api.get(f'/api/sessions/{sid}')).json()['desired_state'] == 'running'


async def test_expired_team_reactivates_same_grant_and_resumes_only_eligible_helpers(api, project, env):
    from helpers import pause_supervisor, wait_for
    from test_orchestrator_selection_api import assigned_start
    from test_supervisor import state_is

    from acc.server.models import Session, Setting
    from acc.server.orchestrator.verification import WRAPUP_KEY
    owner = await lead(api, project, env, hours=1)
    helper = (await assigned_start(api, env, owner, name='Unfinished', task='Work'))['result']
    finished = (await assigned_start(api, env, owner, name='Finished', task='Done'))['result']
    await wait_for(state_is(api, helper['id'], 'idle'))
    await wait_for(state_is(api, finished['id'], 'idle'))
    await api.post(f"/api/sessions/{finished['id']}/archive")
    failed = (await assigned_start(api, env, owner, name='Failed', task='Failed task'))['result']
    await wait_for(state_is(api, failed['id'], 'idle'))
    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        saved = dict(db.get(Session, helper['id']).launch_spec.inputs)
        db.get(Session, failed['id']).desired_state = 'stopped'
        db.get(Session, failed['id']).observed_state = 'recovery_needed'
        db.merge(Setting(key=WRAPUP_KEY, value=0))
        db.get(Team, owner['id']).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    sup = api.app.state.supervisor
    await sup.expire_teams()
    # Exact reported regression: a lead alone resumes but helper is blocked by the old grant.
    assert (await api.post(f"/api/sessions/{owner['id']}/resume")).status_code == 200
    blocked = await api.post(f"/api/sessions/{helper['id']}/resume")
    assert blocked.status_code == 409 and 'Resume team' in blocked.text
    url = f"/api/sessions/{owner['id']}/team/resume"
    preview = (await api.get(url)).json()
    assert {r['id'] for r in preview['sessions']} == {owner['id'], helper['id']}
    resumed = await api.post(url, json={'revision': preview['revision'], 'hours': None})
    assert resumed.status_code == 200 and not resumed.json()['errors'], resumed.text
    with sup.db() as db:
        assert not db.get(Team, owner['id']).stopped
        assert db.get(Team, owner['id']).expires_at is None
        assert db.get(Session, helper['id']).desired_state == 'running'
        assert db.get(Session, helper['id']).launch_spec.inputs == saved
        assert db.get(Session, finished['id']).desired_state == 'archived'
        assert db.get(Session, failed['id']).desired_state == 'stopped'
        assert db.get(Team, owner['id']).started == 3
    assert (await api.post(f"/api/sessions/{helper['id']}/resume")).status_code == 200


async def test_pre_upgrade_expired_team_recovered_from_proven_state_events(api, project, env):
    from helpers import pause_supervisor, wait_for
    from test_orchestrator_selection_api import assigned_start
    from test_supervisor import state_is
    owner = await lead(api, project, env)
    helper = (await assigned_start(api, env, owner, name='Old helper', task='Work'))['result']
    await wait_for(state_is(api, helper['id'], 'idle'))
    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        db.get(Team, owner['id']).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    await api.app.state.supervisor.stop_team(owner['id'], 'supervisor', expired=True)
    await api.app.state.supervisor.stop_team(owner['id'], 'supervisor')
    with write_db(api.app.state.db) as db:
        row = db.get(Team, owner['id'])
        row.resume_members, row.lifetime_revision, row.stop_reason = [], 0, None
        db.commit()
    preview = (await api.get(f"/api/sessions/{owner['id']}/team/resume")).json()
    assert helper['id'] in [r['id'] for r in preview['sessions']]


async def test_stale_expiry_and_wrapup_jobs_do_not_stop_an_extended_or_resumed_team(api, project, env):
    owner = await lead(api, project, env, hours=None)
    sup = api.app.state.supervisor
    result = await sup.stop_team(owner['id'], 'supervisor', expired=True)
    assert result['stopped'] == []
    result = await sup.stop_team(owner['id'], 'supervisor', wrapup_ended=True)
    assert result['stopped'] == []
    assert (await api.get(f"/api/sessions/{owner['id']}")).json()['desired_state'] == 'running'


async def test_resume_is_fenced_and_budget_refusal_does_not_reactivate_team(api, project, env, monkeypatch):
    from acc.server import budgets
    owner = await lead(api, project, env)
    url = f"/api/sessions/{owner['id']}/team"
    await api.post(url + '/stop')
    before = (await api.get(url + '/resume')).json()
    assert (await api.post(url + '/resume', json={'revision': 'stale', 'hours': None})).status_code == 409
    monkeypatch.setattr(budgets, 'paused', lambda *args: ['paused'])
    blocked = await api.post(url + '/resume', json={'revision': before['revision'], 'hours': None})
    assert blocked.status_code == 409 and 'budget' in blocked.text
    assert (await api.get(url)).json()['stopped'] is True


async def test_missing_hours_defaults_to_unlimited_and_lifetime_changes_are_audited(api, project, env):
    from test_changes import repo
    from test_team import grant
    repo(env['workspaces'] / 'demo')
    limits = grant()
    del limits['hours']
    made = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Unlimited', 'worktree': True, 'orchestrator': limits})
    assert made.status_code == 201
    sid = made.json()['id']
    url = f'/api/sessions/{sid}/team'
    assert (await api.get(url)).json()['limits']['hours'] is None
    assert (await api.post(url + '/lifetime', json={'hours': 4})).status_code == 200
    assert (await api.post(url + '/lifetime', json={'hours': None})).status_code == 200
    entries = (await api.get('/api/audit')).json()
    assert {'team.extend', 'team.remove_limit'} <= {r['action'] for r in entries if r['target'] == sid}


async def test_team_resume_waits_for_the_existing_workspace_operation_lease(api, project, env):
    import asyncio

    from helpers import wait_for
    owner = await lead(api, project, env)
    sup = api.app.state.supervisor
    url = f"/api/sessions/{owner['id']}/team"
    await api.post(url + '/stop')
    plan = (await api.get(url + '/resume')).json()
    path = project['workspace_path']
    key = sup.workspace_operations.key(path)
    async with sup.workspace_operations.hold(path, kind='delete'):
        task = asyncio.create_task(api.post(url + '/resume', json={'revision': plan['revision'], 'hours': None}))
        async def waiting():
            return sup.workspace_operations._entries[key].users > 1
        await wait_for(waiting)
        assert not task.done()
        assert (await api.get(url)).json()['stopped']
    assert (await task).status_code == 200


@pytest.mark.parametrize(('provider', 'options', 'expected'), [
    ('copilot', {'permissions': 'all'}, 'Allow everything ⚠'),
    ('cursor', {'force': True}, 'On'),
    ('antigravity', {'skip_permissions': True}, 'On'),
    ('opencode', {'permissions': 'auto'}, 'Approve everything not denied ⚠'),
])
async def test_resume_preview_includes_provider_permissions_without_arbitrary_options(api, project, env, provider, options, expected):
    from helpers import pause_supervisor

    from acc.server.models import Session

    await pause_supervisor(api)
    owner = await lead(api, project, env)
    sup = api.app.state.supervisor
    await sup.stop_team(owner['id'], '@api')
    with sup.db() as db:
        session = db.get(Session, owner['id'])
        session.provider = provider
        session.launch_spec.inputs = {**session.launch_spec.inputs, 'provider': provider,
            'options': {**options, 'unregistered_private_option': 'DO-NOT-EXPOSE-fixture'}}
        db.commit()
    response = await api.get(f"/api/sessions/{owner['id']}/team/resume")
    assert response.status_code == 200, response.text
    saved = response.json()['sessions'][0]
    assert expected in saved['permissions'].values()
    assert saved['dangerous_permissions'] is True
    assert 'acceptEdits' not in saved['permissions'].values()
    assert 'workspace-write' not in saved['permissions'].values()
    assert 'DO-NOT-EXPOSE-fixture' not in response.text
