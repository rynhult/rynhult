"""Unshipped install migration follows relay without losing retained evidence."""
import pytest
import upgrade_rehearsal as rehearsal
from alembic import command
from relay_helpers import setup
from sqlalchemy import inspect, text
from test_migration_round_trip import schema
from test_session_relay_migration import TABLES as RELAY_TABLES

from acc.server.db import alembic_config
from acc.server.models import InstallRecipe

INSTALL_TABLES = {'install_recipe', 'account_install_journal'}


def relay_snapshot(url):
    columns = rehearsal.schema(url)
    return rehearsal.snapshot(url, {name: columns[name] for name in RELAY_TABLES})


def test_install_0091_after_populated_relay_0090_and_empty_round_trip(tmp_path):
    case = setup(tmp_path)
    config = alembic_config(case.url)
    try:
        command.downgrade(config, '0090')
        before_schema = schema(case.url)
        before = relay_snapshot(case.url)
        assert before_schema['version'] == '0090' and before['relay_session'] and before['relay_run']
        assert INSTALL_TABLES.isdisjoint(before_schema['tables'])
        command.upgrade(config, '0091')
        assert set(inspect(case.engine).get_table_names()) - set(before_schema['tables']) == INSTALL_TABLES
        assert relay_snapshot(case.url) == before
        with case.engine.connect() as db:
            assert db.scalar(text('SELECT version_num FROM alembic_version')) == '0091'
            assert all(db.scalar(text('SELECT count(*) FROM ' + table)) == 0 for table in INSTALL_TABLES)
        command.downgrade(config, '0090')
        assert schema(case.url) == before_schema
        assert relay_snapshot(case.url) == before
        command.upgrade(config, 'head')
        assert relay_snapshot(case.url) == before
    finally:
        case.engine.dispose()


def test_install_evidence_fences_0091_downgrade_without_discarding_relay(tmp_path):
    case = setup(tmp_path)
    try:
        before = relay_snapshot(case.url)
        with case.db() as db:
            db.add(InstallRecipe(digest='a' * 64, recipe_id='fixture', recipe={}, metadata_entry={},
                artifact=b'retained fixture', packument_digest='b' * 64, reviewer='reviewer', approver='alex'))
            db.commit()
        with pytest.raises(RuntimeError, match='Install evidence exists'):
            command.downgrade(alembic_config(case.url), '0090')
        assert relay_snapshot(case.url) == before
        with case.engine.connect() as db:
            from alembic.script import ScriptDirectory
            assert db.scalar(text('SELECT version_num FROM alembic_version')) == ScriptDirectory.from_config(alembic_config(case.url)).get_current_head()
            assert db.scalar(text('SELECT artifact FROM install_recipe')) == b'retained fixture'
    finally:
        case.engine.dispose()
