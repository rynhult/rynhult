"""Regression checks for revisions introduced after the PostgreSQL branch forked."""
import importlib.util
from io import StringIO
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from acc.server.db import MIGRATIONS


def revision(number):
    path = next((MIGRATIONS / 'versions').glob(f'{number}_*.py'))
    spec = importlib.util.spec_from_file_location(f'migration_{number}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('number', ['0040', '0041', '0068'])
def test_new_boolean_defaults_compile_for_postgresql(number):
    output = StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    with Operations.context(context):
        revision(number).upgrade()
    assert 'BOOLEAN DEFAULT false NOT NULL' in output.getvalue()


@pytest.mark.parametrize('info', [{'os': 'linux', 'wsl': '2'}, '{"os": "linux", "wsl": "2"}'])
def test_roles_backfill_accepts_driver_decoded_and_text_json(info, monkeypatch):
    migration = revision('0042')
    writes = []

    def execute(statement, params=None):
        if params is not None:
            writes.append(params)
        return SimpleNamespace(mappings=lambda: [{'id': 'workstation', 'status': 'active', 'info': info}])

    monkeypatch.setattr(migration, 'op', SimpleNamespace(
        add_column=lambda *args: None, get_bind=lambda: SimpleNamespace(execute=execute)))
    migration.upgrade()
    assert writes == [{'id': 'workstation', 'role': 'workstation'}]
