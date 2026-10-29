"""Break-it 12: synthetic 0.14 data and controlled owner interleavings only."""
import upgrade_rehearsal as rehearsal
from sqlalchemy import MetaData

from acc.server.db import make_engine, migrate


def seed_014(url, data):
    """Project the existing rehearsal fixture onto the real 0.14 (0042) schema.

    Newer tables/columns are intentionally absent rather than pretending a 0066
    database is 0.14. Preserve every supported old column's synthetic values.
    """
    data.mkdir(parents=True, exist_ok=True)
    rows = rehearsal.rows(data)
    rehearsal.backup_targets(data)
    engine = make_engine(url)
    counts = {}
    try:
        meta = MetaData()
        meta.reflect(engine)
        with engine.begin() as db:
            for name, records in rows.items():
                if name not in meta.tables:
                    continue
                table = meta.tables[name]
                for record in records:
                    db.execute(table.insert(), {k: v for k, v in record.items() if k in table.c and v is not None})
                counts[name] = len(records)
    finally:
        engine.dispose()
    return counts


def test_014_rehearsal_fixture_upgrades_without_losing_original_rows(tmp_path):
    url = f'sqlite:///{tmp_path}/acc.db'
    rehearsal.upgrade(url, '0042')
    counts = seed_014(url, tmp_path / 'data')
    assert counts['event'] == 120 and counts['session'] > 0
    baseline = rehearsal.schema(url)
    before = rehearsal.snapshot(url, baseline)
    migrate(url)
    assert rehearsal.version(url) == rehearsal.head(url)
    assert rehearsal.integrity(url) == []
    after = rehearsal.snapshot(url, baseline)
    assert {table for table in before if before[table] != after[table]} == set()


async def test_teams_off_during_layout_wait_refuses_new_lead_without_recording_a_session(api, project, env, monkeypatch):
    from sqlalchemy import select
    from test_admin_features import change
    from test_changes import repo
    from test_team import grant

    from acc.server.models import Session, Team

    repo(env['workspaces'] / 'demo')
    supervisor = api.app.state.supervisor
    original = supervisor.layout

    async def layout(*args):
        result = await original(*args)
        response = await change(api, 'teams', False)
        assert response.status_code == 200
        return result

    monkeypatch.setattr(supervisor, 'layout', layout)
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
        'name': 'lead interrupted by Off', 'worktree': True, 'orchestrator': grant()})
    assert response.status_code == 409, response.text
    assert 'Admin features' in response.text
    with api.app.state.db() as db:
        assert list(db.scalars(select(Session))) == []
        assert list(db.scalars(select(Team))) == []


async def test_telemetry_off_keeps_canonical_deletion_and_rejects_unsupported_aliases(api):
    from sqlalchemy import select
    from test_admin_features import change

    from acc.server.models import AuditLog

    # The app's /api fallback deliberately does not redirect unsupported routes.
    assert (await api.post('/api/telemetry/delete/', follow_redirects=True)).status_code == 404
    assert (await change(api, 'telemetry', False)).status_code == 200
    assert (await api.post('/api/telemetry/delete/', follow_redirects=True)).status_code == 404
    response = await api.post('/api/telemetry/delete')
    assert response.status_code == 200, response.text
    with api.app.state.db() as db:
        assert len(list(db.scalars(select(AuditLog).where(AuditLog.action == 'telemetry.delete')))) == 1
    blocked = await api.get('/api/telemetry/', follow_redirects=True)
    assert blocked.status_code == 404 and 'Admin features' in blocked.text


async def test_schedules_off_during_launch_wait_keeps_the_saved_schedule_but_refuses_dispatch(api, project, monkeypatch):
    from helpers import pause_supervisor, write_db
    from sqlalchemy import select
    from test_admin_features import change, owner_browser
    from test_schedules import payload, utc

    from acc.server.models import Schedule, ScheduleRun, Session
    from acc.server.schedules import Scheduler, enqueue

    await pause_supervisor(api)
    headers = await owner_browser(api)
    made = await api.post('/api/schedules', json=payload(project), headers=headers)
    assert made.status_code == 201, made.text
    schedule_id = made.json()['id']
    with write_db(api.app.state.db) as db:
        run = enqueue(db, db.get(Schedule, schedule_id), utc('2026-10-27T00:00'))
        run_id = run.id
        db.commit()
    supervisor = api.app.state.supervisor
    original = supervisor.layout

    async def layout(*args):
        result = await original(*args)
        response = await change(api, 'schedules', False, headers=headers)
        assert response.status_code == 200
        return result

    monkeypatch.setattr(supervisor, 'layout', layout)
    await Scheduler(api.app.state.ctx).dispatch(run_id)
    with api.app.state.db() as db:
        assert db.get(Schedule, schedule_id).enabled
        assert db.get(ScheduleRun, run_id).status == 'failed'
        assert 'Admin features' in db.get(ScheduleRun, run_id).reason
        assert list(db.scalars(select(Session))) == []
    assert (await change(api, 'schedules', True, headers=headers)).status_code == 200
    assert (await api.get('/api/schedules', headers=headers)).json()[0]['id'] == schedule_id


async def test_turning_teams_off_stops_helpers_without_stopping_an_ordinary_session(api, project, env):
    from test_admin_features import change
    from test_team import lead, request

    from acc.server.models import Session, Team

    ordinary = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                   'name': 'ordinary session stays available'})
    assert ordinary.status_code == 201, ordinary.text
    captain = await lead(api, project, env)
    started = await request(api, env, captain, 'start', name='helper', agent='claude', profile=None, task='synthetic work')
    assert started['ok'], started
    helper = started['result']
    assert (await change(api, 'teams', False)).status_code == 200
    with api.app.state.db() as db:
        assert db.get(Team, captain['id']).stopped
        assert db.get(Session, helper['id']).desired_state == 'stopped'
        assert db.get(Session, ordinary.json()['id']).desired_state == 'running'
    assert (await api.post(f"/api/sessions/{helper['id']}/resume")).status_code == 409
    assert (await api.post(f"/api/sessions/{ordinary.json()['id']}/stop")).status_code == 200
