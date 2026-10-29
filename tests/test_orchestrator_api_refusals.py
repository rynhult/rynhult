"""Previously uncovered person-API refusals; no test invokes an agent entrypoint."""
import pytest
from helpers import pause_supervisor, write_db
from sqlalchemy import select
from test_orchestrator_planning import flag  # noqa: F401
from test_orchestrator_selection_api import human, proposal, selection_runtime  # noqa: F401
from test_team import grant

from acc.server.models import AuditLog, Team
from acc.server.orchestrator import gates, selection


@pytest.mark.parametrize('case,status', [
    ('missing', 404), ('other_team', 404), ('consumed', 409), ('out_of_range', 409),
    ('filtered', 409), ('not_a_team', 404), ('blank_reason', 422), ('consumed_during_probe', 409),
])
async def test_selection_override_refusals_do_not_write_person_authority(api, env, project, monkeypatch, case, status):
    headers = await human(api, env)
    caller, row = await proposal(api, env, project)
    target, identifier, choice, reason = caller['id'], row['id'], 1, 'Owner choice'
    if case == 'missing':
        identifier = 'f' * 32
    elif case == 'other_team':
        other = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                     'name': 'Other lead', 'worktree': True, 'orchestrator': grant()})
        assert other.status_code == 201, other.text
        target = other.json()['id']
    elif case == 'not_a_team':
        solo = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Solo'})
        assert solo.status_code == 201, solo.text
        target = solo.json()['id']
    elif case == 'out_of_range':
        choice = 11
    elif case == 'blank_reason':
        reason = '   '
    elif case in ('consumed', 'filtered'):
        with write_db(api.app.state.db) as db:
            ledger = db.get(selection.Selection, row['id'])
            if case == 'consumed':
                ledger.state = 'started'
            else:
                # A ledger can contain filtered choices alongside its eligible winner.
                ledger.candidates = [{**candidate, 'eligible': False} if i == choice else candidate
                                     for i, candidate in enumerate(ledger.candidates)]
            db.commit()
    elif case == 'consumed_during_probe':
        readiness = selection.readiness

        async def consumed(*args):
            ready = await readiness(*args)
            with write_db(api.app.state.db) as db:
                db.get(selection.Selection, row['id']).state = 'started'
                db.commit()
            return ready

        monkeypatch.setattr(selection, 'readiness', consumed)
    response = await api.post(f'/api/teams/{target}/selections/{identifier}/override', headers=headers,
                              json={'choice': choice, 'reason': reason})
    assert response.status_code == status, response.text
    with api.app.state.db() as db:
        ledger = db.get(selection.Selection, row['id'])
        assert ledger.override_by is None and ledger.override_reason is None and ledger.chosen == row['chosen']
        assert db.get(Team, caller['id']).started == 0
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'selection.overridden')) is None


async def test_invalid_owner_allowlists_and_commands_preserve_policy_and_finished_jobs(api, env, project):
    headers = await human(api, env)
    await pause_supervisor(api)
    preferences = (await api.get('/api/settings/orchestrator/selection', headers=headers)).json()
    bad = {'version': 1, 'rules': [{'profile': '*', 'task_type': 'implementation',
                                  'choices': [{'agent': 'claude', 'models': ['--not-a-model']}]}]}
    response = await api.put('/api/settings/orchestrator/selection', headers=headers, json=bad)
    assert response.status_code == 422, response.text
    assert (await api.get('/api/settings/orchestrator/selection', headers=headers)).json() == preferences
    path = f"/api/gates/projects/{project['id']}/commands"
    assert (await api.get(path, headers=headers)).json() == {'commands': None}
    response = await api.put(path, headers=headers, json={'related': ['not-an-approved-command']})
    assert response.status_code == 422, response.text
    assert (await api.get(path, headers=headers)).json() == {'commands': None}
    assert (await api.post('/api/gates/' + 'f' * 32 + '/cancel', headers=headers)).status_code == 404
    with write_db(api.app.state.db) as db:
        db.add(gates.GateJob(id='e' * 32, project_id=project['id'], branch='acc/team-finished', suite='related',
                            state='passed', priority='check', account=env['settings'].default_account,
                            requested_by='casey', results={}))
        db.commit()
    response = await api.post('/api/gates/' + 'e' * 32 + '/cancel', headers=headers)
    assert response.status_code == 409, response.text
    with api.app.state.db() as db:
        assert db.get(gates.GateJob, 'e' * 32).state == 'passed'
        assert db.scalar(select(AuditLog).where(AuditLog.action.in_(
            ['selection.preferences_changed', 'gate.commands', 'gate.cancel']))) is None


async def test_person_plan_routes_refuse_a_session_without_a_team(flag, api, env, project):  # noqa: F811
    headers = await human(api, env)
    solo = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Solo'})
    assert solo.status_code == 201, solo.text
    assert (await api.get(f"/api/teams/{solo.json()['id']}/plan", headers=headers)).status_code == 404
