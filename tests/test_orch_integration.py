"""Cross-lane contracts on the rolling integration branch, with disposable fixtures only."""
import base64
from datetime import timedelta

import pytest


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')


def test_private_reference_delivery_accepts_real_session_ids(tmp_path):
    from isolated_settings import isolated_broker
    from test_changes import repo

    from acc.broker.brief_references import receive, seal
    from acc.broker.ops import exclude_artifacts
    config = isolated_broker(tmp_path)
    session = 'abcdef123456'  # Supervisor's real IDs have twelve characters.
    run = config.run_dir / session
    run.mkdir(parents=True)
    root = tmp_path / 'workspace'
    repo(root)
    exclude_artifacts(root)
    key = b'test-private-workspace-key'
    (run / 'private-workspace.json').write_text(seal(session, str(root), key))
    out = receive(config, session, 'REF-1', 'png', 0, base64.b64encode(b'example').decode(), binding_key=key)
    assert out['received'] == 7


async def test_brief_initializes_the_real_planning_adapter(api, project, env):
    from test_project_briefs import person
    from test_team import grant, lead

    from acc.server.models import Team
    from acc.server.orchestrator.planning import OrchestrationPlan
    api = await person(api, env)
    saved = await api.post(f'/api/projects/{project["id"]}/brief', json={'version': 0,
        'fields': {'product': 'Example feature', 'run_mode': 'prepare'}})
    assert saved.status_code == 200, saved.text
    created = await lead(api, project, env)
    with api.app.state.db() as db:
        assert db.get(Team, created['id']).limits['run_mode'] == 'prepare'
        assert db.get(OrchestrationPlan, created['id']).run_mode == 'prepare'
    assert created['launch_spec']['inputs']['permission_mode'] == 'plan'
    preview = await api.post('/api/sessions/preview', json={'project_id': project['id'], 'provider': 'claude',
        'name': 'Preview', 'worktree': True, 'orchestrator': grant(brief_version=1, run_mode='build')})
    assert preview.status_code == 200, preview.text
    assert preview.json()['inputs']['permission_mode'] == 'plan'
    assert 'run mode: prepare' in str(preview.json()['files'])



async def test_runtime_contract_advertises_only_bounded_wrapup_tools(api, project, env):
    from helpers import pause_supervisor
    from test_team import lead

    from acc.server import agent_contract
    from acc.server.compiler import LaunchInputs
    from acc.server.models import Session, Team, utcnow
    created = await lead(api, project, env)
    await pause_supervisor(api)
    with api.app.state.db() as db:
        team = db.get(Team, created['id'])
        team.stopped = True
        team.expires_at = utcnow() - timedelta(minutes=1)
        team.wrapup_until = utcnow() + timedelta(minutes=10)
        caller = db.get(Session, created['id'])
        context = agent_contract.for_session(db, caller, LaunchInputs.from_dict(caller.launch_spec.inputs))
        assert 'report/plan/brief/worklist tools: unavailable' not in context
        assert 'bounded wrap-up' in context.lower()
        assert 'Open one consolidated PR' not in context
        assert 'gates: off' in context


async def test_assignment_keeps_the_pinned_project_brief_version(api, project, env):
    from test_project_briefs import person
    from test_team import lead, request
    api = await person(api, env)
    saved = await api.post(f'/api/projects/{project["id"]}/brief', json={'version': 0,
        'fields': {'product': 'Example feature', 'run_mode': 'build'}})
    assert saved.status_code == 200, saved.text
    created = await lead(api, project, env)
    reply = await request(api, env, created, 'assign', goal='Implement the brief', task_type='implementation',
        acceptance=['Meets the brief'], requirement_ids=[],
        need={'role': 'general', 'classification_reason': 'Small implementation with the granted general role'})
    assert reply['ok'], reply
    assert reply['result']['brief_version'] == 1


async def test_legacy_team_is_still_plan_gated_when_v2_is_on(api, project, env):
    from helpers import write_db
    from test_team import lead, request

    from acc.server.models import Team
    created = await lead(api, project, env)
    with write_db(api.app.state.db) as db:
        db.get(Team, created['id']).protocol_version = 1  # a previously issued legacy grant
        db.commit()
    reply = await request(api, env, created, 'start', name='Legacy worker', agent='claude', task='Work')
    assert not reply['ok'] and 'finish the planning session' in reply['error'], reply
