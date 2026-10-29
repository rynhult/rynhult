"""Pairing must upgrade the shipped main schema without losing existing records."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from acc.server.db import alembic_config, make_engine, migrate


def assert_pairing_upgrade(path):
    url = f'sqlite:///{path}'
    migrate(url)
    engine = make_engine(url)
    try:
        schema = inspect(engine)
        assert {'device', 'device_pairing', 'group_work', 'group_launch'} <= set(schema.get_table_names())
        assert {'person', 'fingerprint', 'serial', 'expires_at', 'node_id', 'retire_after'} <= {
            column['name'] for column in schema.get_columns('device')}
        assert {'token_hash', 'receipt_hash', 'renew_id', 'status'} <= {
            column['name'] for column in schema.get_columns('device_pairing')}
        with engine.connect() as connection:
            assert connection.exec_driver_sql('SELECT version_num FROM alembic_version').all() == [
                (ScriptDirectory.from_config(alembic_config(url)).get_current_head(),)]
            assert connection.exec_driver_sql('PRAGMA integrity_check').scalar() == 'ok'
            assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
    finally:
        engine.dispose()


def test_pairing_upgrades_fresh_main_database(tmp_path):
    path = tmp_path / 'fresh.db'
    command.upgrade(alembic_config(f'sqlite:///{path}'), '0049')
    assert_pairing_upgrade(path)


async def test_pairing_upgrades_copy_of_populated_main_fixtures(api, project, env, tmp_path):
    source = env['settings'].data_dir / 'acc.db'
    copy = tmp_path / 'fixture-copy.db'
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(copy)) as destination:
        original.backup(destination)
    # Run current fixtures on the current schema; only the isolated copy becomes legacy.
    # This keeps the supervisor from reading new model columns against an old live DB.
    command.downgrade(alembic_config(f'sqlite:///{copy}'), '0049')
    with closing(sqlite3.connect(copy)) as legacy:
        assert legacy.execute('SELECT version_num FROM alembic_version').fetchall() == [('0049',)]
        projects = legacy.execute('SELECT * FROM project ORDER BY id')
        original_columns = [column[0] for column in projects.description]
        before = projects.fetchall()
        assert before
    assert_pairing_upgrade(copy)
    with closing(sqlite3.connect(copy)) as upgraded:
        upgraded_columns = [column[1] for column in upgraded.execute('PRAGMA table_info(project)')]
        assert upgraded_columns[:len(original_columns)] == original_columns
        # Head may append fields: compare the original values by name, including
        # IDs, rather than comparing tuples with different numbers of columns.
        projection = ', '.join(f'"{name}"' for name in original_columns)
        assert upgraded.execute(f'SELECT {projection} FROM project ORDER BY id').fetchall() == before
        assert upgraded.execute('SELECT managed_checkout, deleting FROM project ORDER BY id').fetchall() == [
            (0, 0) for _ in before]
        assert upgraded.execute('SELECT name FROM project WHERE id = ?', (project['id'],)).fetchone() == ('Demo',)
