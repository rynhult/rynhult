"""A disposable 0079 database, consistent backup/restore and fail-closed downgrade."""
import sqlite3
from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, MetaData, Table, inspect, text

from acc.server.db import alembic_config, make_engine
from acc.server.models import utcnow


def seed(engine, name, values):
    table = Table(name, MetaData(), autoload_with=engine)
    for column in table.columns:
        if column.name in values or column.nullable or column.server_default is not None:
            continue
        if isinstance(column.type, JSON):
            values[column.name] = {}
        elif isinstance(column.type, DateTime):
            values[column.name] = utcnow()
        elif isinstance(column.type, Boolean):
            values[column.name] = False
        elif isinstance(column.type, (Integer, Float)):
            values[column.name] = 0
        else:
            values[column.name] = ''
    with engine.begin() as db:
        db.execute(table.insert().values(**values))


def test_team_upgrade_and_backup_restore(tmp_path):
    path = tmp_path / 'before.db'
    url = f'sqlite:///{path}'
    config = alembic_config(url)
    command.upgrade(config, '0079')
    engine = make_engine(url)
    seed(engine, 'project', {'id': 1, 'slug': 'lifetime', 'name': 'Lifetime', 'workspace_path': '/tmp/example'})
    seed(engine, 'launch_spec', {'id': 1, 'provider': 'claude', 'inputs': {}, 'files': {}, 'digest': 'fixture'})
    seed(engine, 'session', {'id': 'lead', 'project_id': 1, 'launch_spec_id': 1, 'provider': 'claude',
                           'name': 'Legacy lead', 'tmux_name': 'legacy', 'run_as': 'agent', 'team_id': 'lead'})
    seed(engine, 'team', {'id': 'lead', 'limits': {'hours': 8}, 'expires_at': utcnow() + timedelta(hours=8)})
    seed(engine, 'task_report', {'session_id': 'lead', 'team_id': 'lead', 'task_id': 'legacy-task',
        'status': 'done', 'summary': 'Preserve existing evidence', 'changed': [], 'tests': [], 'needs': []})
    with engine.connect() as db:
        original = db.execute(text('SELECT expires_at, limits FROM team')).one()
    engine.dispose()
    backup = tmp_path / 'backup.db'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    command.upgrade(config, 'head')
    engine = make_engine(url)
    with engine.connect() as db:
        assert db.execute(text('SELECT expires_at, limits FROM team')).one() == original
        assert db.scalar(text('SELECT count(*) FROM session')) == 1
        assert db.scalar(text('SELECT summary FROM task_report')) == 'Preserve existing evidence'
        assert not db.execute(text('PRAGMA foreign_key_check')).all()
        assert next(c for c in inspect(db).get_columns('team') if c['name'] == 'expires_at')['nullable']
    with engine.begin() as db:
        db.execute(text('UPDATE team SET expires_at=NULL'))
    engine.dispose()
    with pytest.raises(RuntimeError, match='Unlimited teams'):
        command.downgrade(config, '0079')
    # Restore the consistent pre-upgrade backup into another isolated database.
    restored = tmp_path / 'restored.db'
    with sqlite3.connect(backup) as source, sqlite3.connect(restored) as destination:
        source.backup(destination)
    recovered = alembic_config(f'sqlite:///{restored}')
    command.upgrade(recovered, 'head')
    command.downgrade(recovered, '0079')
    recovered_engine = make_engine(f'sqlite:///{restored}')
    with recovered_engine.connect() as db:
        assert db.execute(text('SELECT expires_at, limits FROM team')).one() == original
        assert not db.execute(text('PRAGMA foreign_key_check')).all()
    recovered_engine.dispose()
