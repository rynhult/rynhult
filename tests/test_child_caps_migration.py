"""0092 protects retained lineage even from SQL writers bypassing the ORM."""
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_team_lifetime_migration import seed

from acc.server.db import alembic_config, make_engine


@pytest.mark.parametrize('legacy_owner', ['alex', None])
def test_0092_backfills_and_database_refuses_lineage_updates(tmp_path, legacy_owner):
    url = f'sqlite:///{tmp_path / "lineage.db"}'
    config = alembic_config(url)
    command.upgrade(config, '0091')
    engine = make_engine(url)
    seed(engine, 'launch_spec', {'id': 1, 'provider': 'claude', 'inputs': {}, 'files': {}, 'digest': 'test'})
    for sid, parent in [('root', None), ('child', 'root'), ('grandchild', 'child')]:
        seed(engine, 'session', {'id': sid, 'launch_spec_id': 1, 'provider': 'claude', 'name': sid,
             'tmux_name': sid, 'run_as': 'agent', 'owner': legacy_owner, 'parent_session_id': parent})
    with engine.connect() as db:
        original_rows = list(db.execute(text('SELECT id, rowid FROM session ORDER BY id')))
        original_rootpage = db.scalar(text("SELECT rootpage FROM sqlite_master WHERE type='table' AND name='session'"))
    engine.dispose()
    command.upgrade(config, '0092')
    engine = make_engine(url)
    with engine.connect() as db:
        import json
        assert list(db.execute(text('SELECT id, rowid FROM session ORDER BY id'))) == original_rows
        assert db.scalar(text("SELECT rootpage FROM sqlite_master WHERE type='table' AND name='session'")) == original_rootpage
        assert json.loads(db.scalar(text("SELECT parent_chain FROM session WHERE id='grandchild'"))) == ['root', 'child']
        assert db.scalar(text("SELECT root_owner FROM session WHERE id='grandchild'")) == (legacy_owner or '@api')
        assert set(db.scalars(text("SELECT owner FROM session"))) == {legacy_owner or '@api'}
    from sqlalchemy.orm import Session as DBSession

    from acc.server.child_caps import chain
    from acc.server.models import Session
    with DBSession(engine) as db:
        assert [row.id for row in chain(db, db.get(Session, 'grandchild'))] == ['child', 'root']
    for assignment in ("parent_session_id=NULL", "parent_session_id='grandchild'", "parent_chain='[]'", "root_owner='other'", "parent_session_id=parent_session_id", "root_owner=root_owner", "owner='other', root_owner='wrong'", "owner='other', root_owner='other', parent_session_id=NULL", "owner=NULL, root_owner=NULL", "owner=NULL", "root_owner=NULL"):
        with engine.begin() as db, pytest.raises(IntegrityError, match='immutable'):
            db.execute(text(f"UPDATE session SET {assignment} WHERE id='child'"))
    with engine.begin() as db:
        db.execute(text("UPDATE session SET owner='other', root_owner='other' WHERE id='child'"))
        assert db.scalar(text("SELECT root_owner FROM session WHERE id='child'")) == 'other'
    # Unrelated updates remain possible.
    with engine.begin() as db:
        db.execute(text("UPDATE session SET name='Renamed' WHERE id='child'"))
    engine.dispose()
    command.downgrade(config, '0091')


def test_lineage_triggers_survive_all_migrations(tmp_path):
    config = alembic_config(f'sqlite:///{tmp_path / "head.db"}')
    command.upgrade(config, 'head')
    engine = make_engine(config.get_main_option('sqlalchemy.url'))
    with engine.connect() as db:
        names = set(db.scalars(text("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='session'")))
        assert {'session_parent_immutable', 'session_root_owner_transfer', 'session_owner_nonnull'} <= names
    engine.dispose()
