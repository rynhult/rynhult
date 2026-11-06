"""Extend only this harness's private e2e database; never seed a supplied database URL."""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
from sqlalchemy import func, insert, select
from acc.server.db import make_engine, write_intent
from acc.server.models import Project, Session


@write_intent
def main():
    base = Path(sys.argv[1]).resolve()
    if not base.is_relative_to(Path('/tmp')) or not (base / 'perf-owned.json').is_file():
        raise ValueError('Seed only a /tmp fixture marked as owned by this performance harness.')
    info = json.loads((base / 'info.json').read_text())
    state = Path(info['state']).resolve()
    if not state.is_relative_to(base):
        raise ValueError('Fixture state escaped the owned base.')
    path = state / 'data/acc.db'
    if not path.is_file() or path.stat().st_uid != os.getuid():
        raise ValueError('Expected an existing private SQLite fixture owned by this account.')
    projects, sessions, live_id = int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    engine = make_engine(f'sqlite:///{path}')
    now = dt.datetime.now(dt.timezone.utc)
    with engine.begin() as db:
        live = db.execute(select(Session.__table__).where(Session.id == live_id)).mappings().one()
        first = db.execute(select(Project.__table__).where(Project.id == live['project_id'])).mappings().one()
        assert db.scalar(select(func.count()).select_from(Project)) == 1
        assert db.scalar(select(func.count()).select_from(Session)) == 1
        rows = []
        for i in range(2, projects + 1):
            folder = state / 'workspaces' / f'cedar-{i:04d}'
            folder.mkdir()
            rows.append(dict(id=i, slug=f'cedar-{i:04d}', name=f'Cedar {i:04d}', host='local',
                             workspace_path=str(folder), account=first['account'], owner=first['owner'],
                             trusted=True, git_backup=False, created_at=now - dt.timedelta(days=i)))
        if rows:
            db.execute(insert(Project), rows)
        stopped = []
        for i in range(2, sessions + 1):
            project_id = (i - 1) % projects + 1
            folder = first['workspace_path'] if project_id == 1 else str(state / 'workspaces' / f'cedar-{project_id:04d}')
            stopped.append(dict(id=f'f{i:031x}', project_id=project_id, provider='claude',
                                name=f'Cedar stopped {i:04d}', tmux_name=f'perf-stopped-{i:04d}',
                                launch_spec_id=live['launch_spec_id'], desired_state='stopped', observed_state='stopped',
                                owner=first['owner'], run_as=live['run_as'], workdir=folder,
                                created_at=now - dt.timedelta(hours=i), updated_at=now - dt.timedelta(hours=i)))
        if stopped:
            db.execute(insert(Session), stopped)
        counts = dict(projects=db.scalar(select(func.count()).select_from(Project)),
                      sessions=db.scalar(select(func.count()).select_from(Session)), live_sessions=1)
        assert counts['projects'] == projects and counts['sessions'] == sessions
    engine.dispose()
    print(json.dumps(counts))


if __name__ == '__main__':
    main()
