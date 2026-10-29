"""A large synthetic installation for performance checks (scripts/perf_check.py, tests/test_performance.py).

Invented data only: 5 people, 40 projects, 2,000 sessions with long histories, 20,000 audit events, 200 Inbox items
and 10 machines by default. Written with SQLAlchemy Core in large batches at the current schema.
"""
from __future__ import annotations

import base64
import datetime as dt
import random
from pathlib import Path

from sqlalchemy import insert

from acc.server import auth
from acc.server.db import make_engine, migrate
from acc.server.models import AttentionItem, AuditLog, Event, LaunchSpec, Node, Project, Session, SessionRun, User

OWNER, OWNER_PASSWORD, OWNER_TOTP = 'alex', 'perf-password-alex', 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'
NOW = dt.datetime(2026, 10, 27, 12, 0, 0)
SCALE = dict(people=5, projects=40, sessions=2000, events=300_000, long_sessions=50, long_events=2000,
             audit=20_000, inbox=200, machines=10, running=20)


def batched(rows, size=5000):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def seed(url: str, data_dir: Path, scale: dict | None = None, seed_value: int = 7) -> dict[str, int]:
    """Creates the schema and writes the data; returns the row count per table."""
    scale = {**SCALE, **(scale or {})}
    rnd = random.Random(seed_value)
    data_dir.mkdir(parents=True, exist_ok=True)
    migrate(url)
    names = ['alex', 'sam', 'robin', 'kim', 'jo', 'lee', 'ari', 'noa'][:scale['people']]
    users = [dict(id=i, username=name, password_hash=auth.hash_password(f'perf-password-{name}'),
                  totp_secret=OWNER_TOTP if name == OWNER else base64.b32encode(rnd.randbytes(20)).decode(),
                  created_at=NOW - dt.timedelta(days=90), role='owner' if name == OWNER else 'member',
                  account=f'{name}-agent') for i, name in enumerate(names, start=1)]
    # Most projects live on the main ('local'); every fourth on one of the other machines.
    projects = [dict(id=i, slug=f'project-{i:02d}', name=f'Project {i}',
                     host=f'machine-{(i // 4) % (scale["machines"] - 1) + 1:02d}' if i % 4 == 0 else 'local',
                     workspace_path=f'/srv/acc/workspaces/project-{i:02d}', repo=f'https://github.com/example-org/p{i}',
                     import_mode='clone', trusted=True, archived=i % 10 == 0, created_at=NOW - dt.timedelta(days=80 - i),
                     account=users[i % len(users)]['account'], owner=users[i % len(users)]['username'])
                for i in range(1, scale['projects'] + 1)]
    specs = [dict(id=i, provider=p, inputs={'model': 'default', 'prompt': 'Fix the failing tests'}, files={},
                  digest=f'{i:064x}', created_at=NOW - dt.timedelta(days=60)) for i, p in
             enumerate(('claude', 'codex', 'copilot', 'cursor'), start=1)]
    nodes = [dict(id=f'{i:016x}', name=f'machine-{i:02d}', status='active' if i < scale['machines'] else 'revoked',
                  info={'os': 'linux', 'arch': 'x86_64', 'hostname': f'machine-{i:02d}'},
                  last_seen=NOW - dt.timedelta(minutes=i), created_by=OWNER, created_at=NOW - dt.timedelta(days=30),
                  role='always_on' if i % 2 else 'workstation') for i in range(1, scale['machines'] + 1)]
    sessions, runs = [], []
    for i in range(scale['sessions']):
        project = projects[i % len(projects)]
        running = i < scale['running']
        state = 'running' if running else ('stopped' if i % 3 else 'archived')
        created = NOW - dt.timedelta(minutes=30 * (scale['sessions'] - i))
        sessions.append(dict(
            id=f's{i:031d}', project_id=project['id'], provider=specs[i % 4]['provider'], name=f'task-{i}',
            tmux_name=f'acc-perf-{i}', native_id=f'native-{i}', launch_spec_id=specs[i % 4]['id'],
            desired_state=state, observed_state=state, recovery_attempts=0, spool_offset=0, created_at=created,
            updated_at=created + dt.timedelta(hours=1), run_as=project['account'], owner=project['owner'],
            workdir=project['workspace_path'], activity='Running npm test' if running else None,
            changes={'files': i % 17, 'added': i % 400, 'removed': i % 90}))
        for n in range(3):
            runs.append(dict(session_id=f's{i:031d}', reason=('launch', 'resume', 'resume')[n],
                             started_at=created + dt.timedelta(minutes=10 * n),
                             ended_at=None if running and n == 2 else created + dt.timedelta(minutes=10 * n + 9),
                             exit_status=None if running and n == 2 else 0))
    kinds = ('user_prompt', 'tool_use', 'tool_result', 'notification', 'stop')
    long_ids = set(range(scale['sessions'] - scale['long_sessions'], scale['sessions']))
    per_session = max(1, (scale['events'] - scale['long_sessions'] * scale['long_events'])
                      // max(1, scale['sessions'] - scale['long_sessions']))
    events = []
    for i in range(scale['sessions']):
        count = scale['long_events'] if i in long_ids else per_session
        start = sessions[i]['created_at']
        for n in range(count):
            events.append(dict(session_id=f's{i:031d}', ts=start + dt.timedelta(seconds=7 * n), source='hook',
                               kind=kinds[n % 5], verified=True,
                               payload={'tool': 'Bash', 'text': f'step {n}: ' + 'x' * (n % 120), 'n': n}))
    audit = [dict(ts=NOW - dt.timedelta(minutes=n), actor=names[n % len(names)],
                  action=('auth.login', 'session.launch', 'project.create', 'backup.capture', 'vault.give')[n % 5],
                  target=f'target-{n}', detail={'n': n}) for n in range(scale['audit'])]
    inbox = [dict(dedupe_key=f'question:{n}', session_id=f's{n % scale["sessions"]:031d}', kind='question',
                  severity=('warn', 'info', 'error')[n % 3], source='hook', verified=True,
                  title=f'task-{n} asks a question', created_at=NOW - dt.timedelta(minutes=n),
                  updated_at=NOW - dt.timedelta(minutes=n), owner=names[n % len(names)],
                  resolved_at=None if n % 4 else NOW - dt.timedelta(minutes=n // 2)) for n in range(scale['inbox'])]
    tables = [(User, users), (Project, projects), (LaunchSpec, specs), (Node, nodes), (Session, sessions),
              (SessionRun, runs), (Event, events), (AuditLog, audit), (AttentionItem, inbox)]
    engine = make_engine(url)
    try:
        with engine.begin() as db:
            for model, values in tables:
                for chunk in batched(values):
                    db.execute(insert(model.__table__), chunk)
    finally:
        engine.dispose()
    return {model.__tablename__: len(values) for model, values in tables}


if __name__ == '__main__':
    import json
    import sys
    target = Path(sys.argv[1])
    print(json.dumps(seed(f'sqlite:///{target / "acc.db"}', target)))
