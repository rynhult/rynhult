"""Migrations 0067-0078 downgrade and upgrade again to exactly the schema a fresh upgrade makes, on SQLite and on the
test-created PostgreSQL (postgres_matrix; skipped without a container runtime)."""
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from acc.server.db import alembic_config, make_engine

BELOW = '0066'  # below mobile (0067), the public git backup (0068) and orchestrator v2 (0069-0078)


def _text(value):
    """SQL text of a clause or default (reflection returns TextClause objects, which only compare by identity)."""
    if value is None or isinstance(value, str):
        return value
    text = getattr(value, 'text', None)
    return text if isinstance(text, str) else str(value)


def _where(index) -> str | None:
    """A partial index's condition, whichever dialect reported it."""
    options = index.get('dialect_options') or {}
    where = options.get('sqlite_where')
    return _text(where if where is not None else options.get('postgresql_where'))


def schema(url) -> dict:
    """Every table's columns, primary key, indexes, unique, foreign-key and check constraints, order-free."""
    engine = make_engine(url)
    try:
        found = inspect(engine)
        out = {}
        for table in found.get_table_names():
            out[table] = {
                'columns': sorted((c['name'], str(c['type']), c['nullable'], _text(c.get('default')))
                                  for c in found.get_columns(table)),
                'primary_key': sorted(found.get_pk_constraint(table).get('constrained_columns') or []),
                'indexes': sorted((i['name'], tuple(i['column_names']), bool(i['unique']), _where(i))
                                  for i in found.get_indexes(table)),
                'unique': sorted(tuple(u['column_names']) for u in found.get_unique_constraints(table)),
                'foreign_keys': sorted((tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']),
                                        (f.get('options') or {}).get('ondelete')) for f in found.get_foreign_keys(table)),
                'checks': sorted(_text(c['sqltext']) for c in found.get_check_constraints(table)),
            }
        with engine.connect() as db:
            version = db.exec_driver_sql('SELECT version_num FROM alembic_version').scalar()
        return {'version': version, 'tables': out}
    finally:
        engine.dispose()


def test_downgrade_below_the_new_migrations_and_upgrade_again_matches_a_fresh_upgrade(backend_url):
    config = alembic_config(backend_url)
    command.upgrade(config, 'head')
    fresh = schema(backend_url)
    assert fresh['version'] == ScriptDirectory.from_config(config).get_current_head()
    command.downgrade(config, BELOW)
    below = schema(backend_url)
    assert below['version'] == BELOW
    assert {'work_item', 'selection', 'message_delivery', 'gate_job', 'orchestration_plan'} <= (
        set(fresh['tables']) - set(below['tables']))
    command.upgrade(config, 'head')
    again = schema(backend_url)
    assert again['version'] == fresh['version']
    assert sorted(again['tables']) == sorted(fresh['tables'])
    for table in fresh['tables']:
        assert again['tables'][table] == fresh['tables'][table], table
