"""Missing account inputs must never silently target a particular local user."""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from acc.server.models import Base, Project


def test_project_requires_explicit_agent_account():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with DBSession(engine) as db:
        db.add(Project(slug='demo', name='Demo', workspace_path='/tmp/demo'))
        with pytest.raises(IntegrityError):
            db.commit()


@pytest.mark.parametrize('table,column,values', [
    ('project', 'account', "slug, name, workspace_path, host, import_mode, trusted, archived, created_at"),
    ('session', 'run_as', "id, project_id, provider, name, tmux_name, launch_spec_id, desired_state, observed_state, recovery_attempts, spool_offset, updated_at, created_at"),
])
def test_database_requires_explicit_agent_account(table, column, values):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    literals = "'demo', 'Demo', '/tmp/demo', 'local', 'managed', 0, 0, CURRENT_TIMESTAMP" if table == 'project' else (
        "'demo', 1, 'claude', 'Demo', 'demo', 1, 'stopped', 'stopped', 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP")
    with engine.begin() as connection, pytest.raises(IntegrityError, match=column):
        connection.execute(text(f'INSERT INTO {table} ({values}) VALUES ({literals})'))


async def test_documentation_zone_can_be_requested(api):
    response = await api.post('/api/publish', json={
        'host': 'demo.example.com', 'port': 8100, 'access': 'internal',
    })
    assert response.status_code == 201, response.text


def test_upgrade_preserves_explicit_assignments_and_removes_implicit_accounts(tmp_path):
    from alembic import command
    from sqlalchemy import inspect

    from acc.server.db import alembic_config

    url = f'sqlite:///{tmp_path / "accounts.db"}'
    config = alembic_config(url)
    command.upgrade(config, '0022')
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO project (slug, name, workspace_path, host, import_mode, trusted, archived, account, created_at) "
                                "VALUES ('demo', 'Demo', '/tmp/demo', 'local', 'managed', 0, 0, 'alex-agent', CURRENT_TIMESTAMP)"))
    command.upgrade(config, 'head')
    for table, column in [('project', 'account'), ('session', 'run_as')]:
        definition = next(c for c in inspect(engine).get_columns(table) if c['name'] == column)
        assert definition['default'] is None
    with engine.connect() as connection:
        assert connection.execute(text("SELECT account FROM project WHERE slug='demo'")).scalar_one() == 'alex-agent'
