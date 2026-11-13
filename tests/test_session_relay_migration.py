import pytest
from alembic import command
from relay_helpers import setup
from sqlalchemy import inspect, text

from acc.server.db import alembic_config, make_engine

TABLES = {'relay_session', 'relay_run', 'relay_operation', 'relay_event', 'relay_decision', 'relay_preference'}


def test_forward_upgrade_no_backfill_and_empty_round_trip(tmp_path):
    url = f'sqlite:///{tmp_path}/migration.db'
    config = alembic_config(url)
    command.upgrade(config, '0089')
    engine = make_engine(url)
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, '0090')
    assert set(inspect(engine).get_table_names()) - before == TABLES
    with engine.connect() as db:
        assert all(db.scalar(text('SELECT count(*) FROM ' + table)) == 0 for table in TABLES)
    command.downgrade(config, '0089')
    assert set(inspect(engine).get_table_names()) == before
    command.upgrade(config, 'head')
    assert TABLES <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_retained_outcomes_refuse_destructive_downgrade(tmp_path):
    case = setup(tmp_path)
    with pytest.raises(RuntimeError, match='Relay records exist'):
        command.downgrade(alembic_config(case.url), '0089')
    assert TABLES <= set(inspect(case.engine).get_table_names())
    case.engine.dispose()


def test_ownership_migration_is_forward_only_and_does_not_enroll_sessions(tmp_path):
    url = f'sqlite:///{tmp_path}/ownership-migration.db'
    config = alembic_config(url)
    command.upgrade(config, '0091')
    engine = make_engine(url)
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, '0093')
    assert set(inspect(engine).get_table_names()) - before == {'relay_ownership'}
    with engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM relay_ownership')) == 0
        assert db.scalar(text('SELECT count(*) FROM relay_session')) == 0
    command.downgrade(config, '0091')
    assert set(inspect(engine).get_table_names()) == before
    engine.dispose()
