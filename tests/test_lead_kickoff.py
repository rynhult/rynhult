"""A v2 lead gets one actionable first message, never a copy of private brief text."""
import pytest
from test_changes import repo
from test_team import grant

from acc.server.compiler import LaunchInputs, build_args
from acc.server.orchestrator import project_briefs as brief

pytest_plugins = ['project_brief_migrations']


@pytest.fixture(autouse=True)
def v2(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')


@pytest.mark.parametrize('mode', ['prepare', 'build'])
@pytest.mark.parametrize('with_brief', [False, True])
async def test_lead_launch_and_preview_share_kickoff(api, project, env, mode, with_brief):
    repo(env['workspaces'] / 'demo')
    if with_brief:
        saved = await api.post(f'/api/projects/{project["id"]}/brief', json={
            'version': 0, 'fields': {'product': 'PRIVATE BODY sentinel', 'run_mode': mode}})
        assert saved.status_code == 200, saved.text
    body = {'project_id': project['id'], 'provider': 'claude', 'name': 'Kickoff',
            'worktree': True, 'orchestrator': grant(run_mode=mode)}
    preview = await api.post('/api/sessions/preview', json=body)
    assert preview.status_code == 200, preview.text
    prompt = preview.json()['inputs']['initial_prompt']
    assert prompt and project['name'] in prompt
    assert 'working list' in prompt and 'decision cards' in prompt
    assert 'PRIVATE BODY sentinel' not in prompt
    if with_brief:
        assert 'brief tool' in prompt and 'brief_interpret' in prompt and 'brief_draft' in prompt
        assert 'v1' in prompt and 'PROJECT-BRIEF.md' in prompt
    else:
        assert 'no accepted brief' in prompt and 'idea' in prompt and 'success' in prompt
        assert 'adding a project brief' in prompt
    assert ('no helpers' if mode == 'prepare' else 'plan_ready') in prompt
    launched = await api.post('/api/sessions', json=body)
    assert launched.status_code == 201, launched.text
    session = launched.json()
    assert session['team_depth'] == 0
    assert session['launch_spec']['inputs']['initial_prompt'] == prompt
    assert prompt in preview.json()['command']
    inputs = LaunchInputs(**session['launch_spec']['inputs'])
    # Resume uses the same persisted launch inputs but never sends the kickoff again.
    assert prompt not in build_args(inputs, run_dir='/r', tmux_name='test', native_id='a' * 32, resume=True)


async def test_person_prompt_wins_and_non_team_launch_is_unchanged(api, project, env):
    repo(env['workspaces'] / 'demo')
    body = {'project_id': project['id'], 'provider': 'claude', 'name': 'Personal task', 'worktree': True}
    for extra, expected in [({}, None), ({'orchestrator': grant(), 'initial_prompt': 'My own plan.'}, 'My own plan.')]:
        response = await api.post('/api/sessions', json={**body, **extra})
        assert response.status_code == 201, response.text
        assert response.json()['launch_spec']['inputs']['initial_prompt'] == expected


async def test_kickoff_uses_selected_saved_version_and_never_proposed_body(api, project):
    from acc.server.models import Project
    from acc.server.orchestrator.kickoff import initial_inputs
    with api.app.state.db() as db:
        brief.save(db, project['id'], '@api', {'product': 'Private first', 'run_mode': 'build'}, 0)
        brief.save(db, project['id'], '@api', {'product': 'Private latest', 'run_mode': 'prepare'}, 1)
        brief.save(db, project['id'], '@api', {'product': 'Private proposal'}, 2, proposed=True)
        p = db.get(Project, project['id'])
        inputs = initial_inputs(db, p.id, LaunchInputs(provider='claude'), {'brief_version': 1, 'run_mode': 'build'})
        assert 'v1' in inputs.initial_prompt and 'plan_ready' in inputs.initial_prompt
        assert 'Private' not in inputs.initial_prompt
        latest = initial_inputs(db, p.id, LaunchInputs(provider='claude'), brief.launch_grant(db, p.id, {}))
        assert 'v2' in latest.initial_prompt and 'no helpers' in latest.initial_prompt


@pytest.mark.parametrize('flag', ['ACC_ORCHESTRATOR', 'ACC_ORCHESTRATOR_V2'])
async def test_disabled_v2_is_not_given_kickoff(api, project, monkeypatch, flag):
    from acc.server.orchestrator.kickoff import initial_inputs
    monkeypatch.setenv(flag, '0')
    inputs = LaunchInputs(provider='claude')
    with api.app.state.db() as db:
        assert initial_inputs(db, project['id'], inputs, {}) == inputs


async def test_helper_keeps_its_assignment_prompt(api, project, env):
    from test_orchestrator_selection_api import assigned_start
    from test_team import lead, request
    x = await lead(api, project, env)
    planned = await request(api, env, x, 'plan_update', reason='Bounded scope', idea='A demo feature',
                            scope={'in': ['A demo'], 'out': [], 'parked': []},
                            phases=[{'key': 'demo', 'title': 'Demo'}])
    assert planned['ok'], planned
    assert (await request(api, env, x, 'plan_ready', reason='Confirmed scope'))['ok']
    reply = await assigned_start(api, env, x, name='Helper', task='Implement the bounded demo.')
    assert reply['ok'], reply
    response = await api.get(f'/api/sessions/{reply["result"]["id"]}')
    assert response.status_code == 200, response.text
    child = response.json()
    assert child['team_depth'] == 1
    prompt = child['launch_spec']['inputs']['initial_prompt']
    assert 'Implement the bounded demo.' in prompt
    assert 'Start the planning session for project' not in prompt
