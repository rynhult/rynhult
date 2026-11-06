"""0085 creates layout_file_ref on databases that ran 0082 before the table was added, and is a no-op otherwise."""
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from acc.server.db import alembic_config


def _upgrade(url, rev):
    command.upgrade(alembic_config(url), rev)


def test_repair_creates_missing_table(tmp_path):
    url = f"sqlite:///{tmp_path / 'acc.db'}"
    _upgrade(url, '0084')
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        Operations(MigrationContext.configure(conn)).drop_table('layout_file_ref')  # the upgraded-install state
    assert 'layout_file_ref' not in sa.inspect(engine).get_table_names()
    _upgrade(url, '0085')
    columns = {c['name'] for c in sa.inspect(engine).get_columns('layout_file_ref')}
    assert {'id', 'owner', 'project_id', 'location', 'target', 'folder', 'path', 'path_hash', 'created_at'} <= columns


def test_repair_is_a_noop_on_fresh_installs(tmp_path):
    url = f"sqlite:///{tmp_path / 'acc.db'}"
    _upgrade(url, '0085')
    assert 'layout_file_ref' in sa.inspect(sa.create_engine(url)).get_table_names()
