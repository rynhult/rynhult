"""0075 keeps legacy reports readable through upgrade/downgrade; no live DB."""
from datetime import timedelta

from alembic import command
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, MetaData, Table, select
from sqlalchemy.orm import Session as DBSession

from acc.server.db import alembic_config, make_engine
from acc.server.models import Team, utcnow
from acc.server.orchestrator.delivery import TaskReport


def test_legacy_report_survives_assignment_migration_round_trip(backend_url):
    config = alembic_config(backend_url)
    command.upgrade(config, '0073')
    engine = make_engine(backend_url)
    # Reflect the historical schema, rather than inserting with future ORM fields
    # added by parallel lanes. Only migration/data preservation is under test here.
    def seed(name, values):
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
    seed('project', {'id':1, 'slug':'legacy', 'name':'Legacy', 'workspace_path':'/tmp/legacy'})
    seed('launch_spec', {'id':1, 'provider':'claude', 'inputs':{}, 'files':{}, 'digest':'fixture'})
    seed('session', {'id':'a'*32, 'project_id':1, 'launch_spec_id':1, 'provider':'claude', 'name':'Legacy lead',
                     'tmux_name':'legacy', 'run_as':'agent', 'team_id':'a'*32})
    seed('team', {'id':'a'*32, 'limits':{}, 'expires_at':utcnow()+timedelta(hours=1)})
    old = Table('task_report', MetaData(), autoload_with=engine)
    with engine.begin() as db:
        db.execute(old.insert().values(session_id='a' * 32, team_id='a' * 32, task_id='legacy-task', status='done',
                   summary='Keep this evidence', changed=[], tests=[], needs=[], needs_ingested=False, received_at=utcnow()))
    engine.dispose()
    command.upgrade(config, 'head')
    engine = make_engine(backend_url)
    with DBSession(engine) as db:
        row = db.get(TaskReport, ('a' * 32, 'legacy-task'))
        assert row.summary == 'Keep this evidence' and row.version == 1 and row.attempt_id is None
        assert db.get(Team, 'a' * 32).protocol_version == 1
    engine.dispose()
    command.downgrade(config, '0073')
    engine = make_engine(backend_url)
    old = Table('task_report', MetaData(), autoload_with=engine)
    with engine.connect() as db:
        assert db.scalar(select(old.c.summary)) == 'Keep this evidence'
    issued = Table('launch_spec', MetaData(), autoload_with=engine)
    with engine.begin() as db:
        db.execute(issued.update().values(files={'mcp.json':'{"mcpServers":{"acc-team":{"args":["--orchestrator"]}}}'}))
    engine.dispose()
    command.upgrade(config, 'head')
    engine = make_engine(backend_url)
    with DBSession(engine) as db:
        assert db.get(Team, 'a' * 32).protocol_version == 2
        assert db.get(TaskReport, ('a' * 32, 'legacy-task')).summary == 'Keep this evidence'
    engine.dispose()
