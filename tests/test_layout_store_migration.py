"""Upgrade, downgrade refusal, disposable SQLite backup restore and transfer evidence."""
import sqlite3

import pytest
from alembic import command
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from acc.server.db import alembic_config, make_engine, migrate
from acc.server.models import LayoutFileRef, PersonalLayout, Project, User


def test_upgrade_guard_restore_transfer_and_explicit_empty_downgrade(tmp_path):
    from acc.db_transfer import copy_database, open_source, verify_database
    path = tmp_path / 'upgrade.db'
    url = f'sqlite:///{path}'
    config = alembic_config(url)
    command.upgrade(config, '0081')
    command.upgrade(config, '0082')
    layout_engine = make_engine(url)
    try:
        assert {'personal_layout', 'layout_file_ref'} <= set(inspect(layout_engine).get_table_names())
    finally:
        layout_engine.dispose()
    command.upgrade(config, 'head')
    engine = make_engine(url)
    with Session(engine) as db:
        db.add(User(username='alex', password_hash='unused', totp_secret='unused'))
        project = Project(slug='example', name='Example', workspace_path='/example', account='alex-agent', owner='alex')
        db.add(project)
        db.flush()
        db.add(PersonalLayout(owner='alex', project_id=project.id, device='desktop', viewport='wide', revision=3,
                              layout={'schema_version': 1, 'views': []}))
        db.add(LayoutFileRef(id='reference', owner='alex', project_id=project.id, location='project', target=str(project.id),
                             path='docs/notes.md', path_hash='h' * 64))
        db.commit()
    engine.dispose()
    backup = tmp_path / 'backup.db'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    with pytest.raises(RuntimeError, match='Layout file references exist'):
        command.downgrade(config, '0081')
    restored = tmp_path / 'restored.db'
    with sqlite3.connect(backup) as source, sqlite3.connect(restored) as target:
        source.backup(target)
    migrate(f'sqlite:///{restored}')
    target_url = f'sqlite:///{tmp_path}/transfer.db'
    migrate(target_url)
    target = make_engine(target_url)
    try:
        with open_source(restored) as source, target.begin() as dest:
            report = copy_database(source, dest)
            assert report['personal_layout']['rows'] == 1
            assert report['layout_file_ref']['rows'] == 1
            assert verify_database(source, dest) == report
        with Session(target) as db:
            row = db.scalar(select(PersonalLayout))
            assert row.revision == 3 and row.owner == 'alex'
            assert row.layout == {'schema_version': 1, 'views': []}
    finally:
        target.dispose()
    engine = make_engine(url)
    with Session(engine) as db:
        row = db.scalar(select(PersonalLayout))
        assert row.revision == 3  # failed downgrade did not drop data
        db.delete(row)
        db.delete(db.scalar(select(LayoutFileRef)))
        db.commit()
    engine.dispose()
    command.downgrade(config, '0081')
    engine = make_engine(url)
    with engine.connect() as db:
        assert 'personal_layout' not in inspect(db).get_table_names()
    engine.dispose()
    command.upgrade(config, 'head')
