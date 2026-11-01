"""The browser deadline fixture must not mutate another worker's team or wrap-up policy."""
from datetime import datetime

import pytest
from helpers import write_db
from test_team import grant, lead

from acc.server.models import Setting, Team
from acc.server.orchestrator.verification import WRAPUP_KEY


@pytest.mark.parametrize('minutes', [0, 5, 120])
async def test_owned_deadline_fixture_expires_without_global_wrapup_changes(api, project, env, minutes):
    from e2e_team_deadlines import install
    install(api.app)
    own = await lead(api, project, env, hours=1)
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
        'name': 'Other lead', 'worktree': True, 'orchestrator': grant(hours=2)})
    assert response.status_code == 201, response.text
    other = response.json()
    with write_db(api.app.state.db) as db:
        db.merge(Setting(key=WRAPUP_KEY, value=minutes))
        db.commit()
        other_deadline = db.get(Team, other['id']).expires_at
    url = f"/e2e-test/teams/{own['id']}/deadline"
    warning = await api.post(url, json={'phase': 'warning'})
    assert warning.status_code == 200, warning.text
    assert datetime.fromisoformat(warning.json()['expires_at']) > datetime.fromisoformat(warning.json()['now'])
    assert any(row.get('session_id') == own['id'] and row['kind'] == 'team_lifetime'
               for row in (await api.get('/api/attention')).json())
    expired = await api.post(url, json={'phase': 'expired'})
    assert expired.status_code == 200, expired.text
    with api.app.state.db() as db:
        assert db.get(Setting, WRAPUP_KEY).value == minutes
        assert db.get(Team, own['id']).stopped
        assert not db.get(Team, other['id']).stopped
        assert db.get(Team, other['id']).expires_at == other_deadline
    assert expired.json()['expires_at'] < expired.json()['now']


async def test_deadline_fixture_rejects_unknown_phase_and_team(api, project, env):
    from e2e_team_deadlines import install
    install(api.app)
    own = await lead(api, project, env, hours=1)
    with api.app.state.db() as db:
        before = db.get(Team, own['id']).expires_at
    assert (await api.post(f"/e2e-test/teams/{own['id']}/deadline", json={'phase': 'anything'})).status_code == 400
    assert (await api.post('/e2e-test/teams/missing/deadline', json={'phase': 'expired'})).status_code == 404
    with api.app.state.db() as db:
        assert db.get(Team, own['id']).expires_at == before


async def test_owned_settlement_consumes_real_hook_and_statusline_without_a_background_tick(api, project, env):
    import json
    import uuid

    from e2e_team_deadlines import install
    from helpers import pause_supervisor

    from acc.server.models import Observation, TeamRequest
    install(api.app)
    own = await lead(api, project, env, hours=1)
    await pause_supervisor(api)
    rid = uuid.uuid4().hex
    run = env['run_dir'] / own['id']
    with (run / 'events.jsonl').open('a') as file:
        file.write(json.dumps({'acc_session': own['id'], 'hook': {'hook_event_name': 'AccTeam',
            'request': {'id': rid, 'action': 'list'}}}) + '\n')
    (run / 'statusline.json').write_text(json.dumps({'cost': {'total_cost_usd': 0.12}}))
    response = await api.post(f"/e2e-test/sessions/{own['id']}/settle")
    assert response.status_code == 200, response.text
    reply = json.loads((run / 'team' / f'{rid}.json').read_text())
    assert reply['ok'], reply
    with api.app.state.db() as db:
        assert db.get(TeamRequest, (own['id'], rid)).delivered
        assert db.get(Observation, f"claude.statusline.{own['id']}").value['cost']['total_cost_usd'] == 0.12
    assert (await api.post('/e2e-test/sessions/missing/settle')).status_code == 404
