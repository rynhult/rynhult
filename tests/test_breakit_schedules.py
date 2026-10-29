"""Calendar and archive boundaries exercised during release break-it review."""
from datetime import timedelta

from helpers import pause_supervisor, write_db
from sqlalchemy import select
from test_schedules import payload, person, utc  # noqa: F401

from acc.server.models import Project, Schedule, ScheduleRun, Session
from acc.server.schedules import Scheduler, next_time


def test_leap_day_and_fall_back_only_first_fold():
    assert next_time({'kind': 'cron', 'expression': '0 9 29 2 *'}, 'UTC', utc('2026-10-23T00:00')) == utc('2028-02-29T09:00')
    assert next_time({'kind': 'daily', 'time': '01:30'}, 'America/Chicago', utc('2026-11-01T06:29')) == utc('2026-11-01T06:30')
    assert next_time({'kind': 'daily', 'time': '01:30'}, 'America/Chicago', utc('2026-11-01T06:31')) == utc('2026-11-02T07:30')


async def test_timer_firing_after_project_archive_creates_no_session(api, project, person):  # noqa: F811
    await pause_supervisor(api)
    made = (await api.post('/api/schedules', json=payload(project))).json()
    now = utc('2026-10-23T16:00')
    with write_db(api.app.state.db) as db:
        db.get(Project, project['id']).archived = True
        db.get(Schedule, made['id']).next_run_at = now - timedelta(seconds=1)
        db.commit()
    scheduler = Scheduler(api.app.state.ctx)
    scheduler.materialize(now)
    with api.app.state.db() as db:
        run_id = db.scalar(select(ScheduleRun.id))
    await scheduler.dispatch(run_id)
    with api.app.state.db() as db:
        run = db.get(ScheduleRun, run_id)
        assert run.status == 'skipped'
        assert not run.session_id
        assert not list(db.scalars(select(Session)))
