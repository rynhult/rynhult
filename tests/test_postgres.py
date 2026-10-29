"""Backend parity against databases owned exclusively by this test run."""
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from acc.server.config import Settings
from acc.server.db import make_engine, migrate


@pytest.mark.parametrize('entrypoint', [make_engine, migrate])
@pytest.mark.parametrize('query, expected', [('', 'verify-full'), ('?sslmode=disable', 'disable'),
                                            ('?sslmode=verify-ca', 'verify-ca')])
def test_postgres_tls_is_verified_unless_explicitly_overridden(monkeypatch, entrypoint, query, expected):
    from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg

    from acc.server import db

    options = {}

    class EngineBoundary(Exception):
        pass

    def capture(url, **kwargs):
        _, arguments = PGDialect_psycopg().create_connect_args(make_url(url))
        options.update(arguments | kwargs.get('connect_args', {}))
        raise EngineBoundary

    monkeypatch.setattr(db, 'create_engine', capture)
    with pytest.raises(EngineBoundary):
        entrypoint('postgresql+psycopg://db.example.lan/acc' + query)
    assert options.get('sslmode') == expected


def test_database_setting(monkeypatch, tmp_path):
    monkeypatch.setenv('ACC_DATABASE_URL', 'postgresql+psycopg://acc:example@localhost/acc')
    assert Settings.from_env().db_url == 'postgresql+psycopg://acc:example@localhost/acc'
    assert Settings(data_dir=tmp_path).db_url == f'sqlite:///{tmp_path}/acc.db'


def test_migrations_and_transactions(backend_url):
    migrate(backend_url)
    migrate(backend_url)
    engine = make_engine(backend_url)
    try:
        with engine.begin() as db:
            from alembic.script import ScriptDirectory

            from acc.server.db import alembic_config
            assert db.execute(text('SELECT version_num FROM alembic_version')).scalar() == ScriptDirectory.from_config(alembic_config(backend_url)).get_current_head()
            db.execute(text("INSERT INTO setting (key, value, updated_at) VALUES ('parity', '{}', CURRENT_TIMESTAMP)"))
        with engine.connect() as db:
            assert db.execute(text("SELECT value FROM setting WHERE key='parity'")).scalar() in ('{}', {})
            db.execute(text("DELETE FROM setting WHERE key='parity'"))
            db.commit()
    finally:
        engine.dispose()


def test_search_parity(backend_url):
    from sqlalchemy.orm import Session as DbSession

    from acc.server import history
    from acc.server.models import LaunchSpec, Project, Session
    migrate(backend_url)
    engine = make_engine(backend_url)
    try:
        with DbSession(engine) as db:
            project = Project(slug='search', name='Search', account='alex-agent', workspace_path='/demo')
            spec = LaunchSpec(provider='claude', inputs={}, files={}, digest='example')
            for name in ('mine', 'hidden'):
                db.add(Session(id=name, project=project, launch_spec=spec, name=name,
                               provider='claude', tmux_name=name, run_as='alex-agent'))
            db.flush()
            history.replace(db, 'mine', [{'text': 'deploying billing webhooks', 'role': 'user', 'ts': 't'}] * 5)
            history.replace(db, 'hidden', [{'text': 'billing billing billing webhooks', 'role': 'assistant'}] * 20)
            db.commit()
            hits = history.search(db, 'billing webho', {'mine'}, limit=1)
            assert [r['session']['id'] for r in hits] == ['mine']
            assert hits[0]['matches'] == 5
            assert len(hits[0]['hits']) == 3
            assert '\x02billing\x03' in hits[0]['hits'][0]['snippet']
            assert history.search(db, 'billing', {'nobody'}) == []
            history.replace(db, 'mine', [{'text': 'replacement', 'role': 'assistant'}])
            assert history.search(db, 'billing', {'mine'}) == []
            db.rollback()
    finally:
        engine.dispose()


def test_every_migration_roundtrip(backend_url):
    from alembic import command
    from alembic.script import ScriptDirectory

    from acc.server.db import alembic_config
    config = alembic_config(backend_url)
    revisions = list(reversed(list(ScriptDirectory.from_config(config).walk_revisions())))
    engine = make_engine(backend_url)
    try:
        for revision in revisions:
            command.upgrade(config, revision.revision)
            with engine.connect() as db:
                assert db.scalar(text('SELECT version_num FROM alembic_version')) == revision.revision
        command.downgrade(config, 'base')
        migrate(backend_url)
    finally:
        engine.dispose()


def test_postgres_import(postgres_url, tmp_path):
    from sqlalchemy.orm import Session as DbSession

    from acc.db_transfer import copy_database, open_source, verify_database
    from acc.server.models import Project, Setting
    # The destination belongs to the test-created container, never to an environment URL.
    target = make_engine(postgres_url)
    with target.begin() as db:
        db.exec_driver_sql('DROP SCHEMA public CASCADE')
        db.exec_driver_sql('CREATE SCHEMA public')
    migrate(postgres_url)
    source = tmp_path / 'import.db'
    url = f'sqlite:///{source}'
    migrate(url)
    engine = make_engine(url)
    with DbSession(engine) as db:
        db.add(Project(id=42, slug='imported', name='Imported', account='alex-agent', workspace_path='/demo'))
        db.add(Setting(key='nested', value={'example': [True, None, 'café']}))
        db.commit()
    engine.dispose()
    try:
        with open_source(source) as src, target.begin() as dst:
            report = copy_database(src, dst)
            assert report['project']['rows'] == 1
            assert report == verify_database(src, dst)
        with DbSession(target) as db:
            project = Project(slug='new', name='New', account='alex-agent', workspace_path='/new')
            db.add(project)
            db.commit()
            assert project.id > 42
            assert db.get(Setting, 'nested').value == {'example': [True, None, 'café']}
    finally:
        target.dispose()


def test_postgres_mutations_serialize_and_background_locks_fail_fast(postgres_url):
    import threading

    from sqlalchemy.exc import OperationalError

    from acc.server.db import WRITE_INTENT
    engine = make_engine(postgres_url)
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE IF NOT EXISTS lock_probe (id INTEGER PRIMARY KEY, value INTEGER)')
        db.exec_driver_sql('DELETE FROM lock_probe')
        db.exec_driver_sql('INSERT INTO lock_probe VALUES (1,0)')
    token = WRITE_INTENT.set(True)
    errors = []
    started = threading.Event()
    def second_writer():
        inner_token = WRITE_INTENT.set(True)
        try:
            started.set()
            with engine.begin() as db:
                value = db.scalar(text('SELECT value FROM lock_probe WHERE id=1'))
                db.execute(text('UPDATE lock_probe SET value=:value WHERE id=1'), {'value': value + 1})
        except Exception as error:
            errors.append(error)
        finally:
            WRITE_INTENT.reset(inner_token)
    try:
        with engine.begin() as first:
            value = first.scalar(text('SELECT value FROM lock_probe WHERE id=1'))
            thread = threading.Thread(target=second_writer)
            thread.start()
            assert started.wait(timeout=2)
            first.execute(text('UPDATE lock_probe SET value=:value WHERE id=1'), {'value': value + 1})
            background = WRITE_INTENT.set(False)
            try:
                with pytest.raises(OperationalError) as failure, engine.begin() as other:
                    other.exec_driver_sql('UPDATE lock_probe SET value=99 WHERE id=1')
                assert failure.value.orig.sqlstate == '55P03'
                from acc.server.api.context import refresh_read
                with refresh_read(), engine.begin() as optional:
                    optional.exec_driver_sql('UPDATE lock_probe SET value=99 WHERE id=1')
            finally:
                WRITE_INTENT.reset(background)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert errors == []
        with engine.connect() as db:
            assert db.scalar(text('SELECT value FROM lock_probe WHERE id=1')) == 2
    finally:
        WRITE_INTENT.reset(token)
        engine.dispose()


async def test_postgres_app_initializes_and_serves_database_reads(postgres_url, env):
    from dataclasses import replace

    import httpx

    from acc.server.app import create_app

    engine = make_engine(postgres_url)
    with engine.begin() as db:
        db.exec_driver_sql('DROP SCHEMA public CASCADE')
        db.exec_driver_sql('CREATE SCHEMA public')
    engine.dispose()
    settings = replace(env['settings'], database_url=postgres_url)
    app = create_app(settings)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acc',
                                     headers={'Authorization': f'Bearer {settings.api_token()}'}) as client:
            response = await client.get('/api/auth/state')
            assert response.status_code == 200
            assert response.json()['setup_required'] is True
            response = await client.get('/api/projects')
            assert response.status_code == 200
            assert response.json() == []
            response = await client.post('/api/projects', json={
                'slug': 'example', 'name': 'Example', 'workspace_path': str(env['workspaces'] / 'demo')})
            assert response.status_code == 201
            response = await client.get('/api/projects')
            assert [project['slug'] for project in response.json()] == ['example']
    finally:
        await app.state.supervisor.broker.close()
        app.state.db.kw['bind'].dispose()


def test_postgres_deferred_stale_read_cannot_overwrite_api_commit(postgres_url):
    from sqlalchemy.exc import OperationalError

    from acc.server.db import WRITE_INTENT

    engine = make_engine(postgres_url)
    try:
        with engine.begin() as db:
            db.exec_driver_sql('CREATE TABLE IF NOT EXISTS stale_probe (value INTEGER)')
            db.exec_driver_sql('DELETE FROM stale_probe')
            db.exec_driver_sql('INSERT INTO stale_probe VALUES (0)')
        with engine.connect() as reader:
            transaction = reader.begin()
            assert reader.scalar(text('SELECT value FROM stale_probe')) == 0
            token = WRITE_INTENT.set(True)
            try:
                with engine.begin() as writer:
                    writer.exec_driver_sql('UPDATE stale_probe SET value=1')
            finally:
                WRITE_INTENT.reset(token)
            with pytest.raises(OperationalError) as failure:
                reader.exec_driver_sql('UPDATE stale_probe SET value=2')
            assert failure.value.orig.sqlstate == '40001'
            transaction.rollback()
        with engine.connect() as db:
            assert db.scalar(text('SELECT value FROM stale_probe')) == 1
    finally:
        engine.dispose()


def test_roles_upgrade_preserves_new_feature_data(backend_url):
    """Exercise the post-fork revisions with stored JSON and real dialect defaults."""
    from datetime import datetime, timezone

    import sqlalchemy as sa
    from alembic import command

    from acc.server.db import alembic_config

    config = alembic_config(backend_url)
    command.upgrade(config, '0041')
    engine = make_engine(backend_url)
    nodes = sa.table('node', sa.column('id', sa.String), sa.column('name', sa.String),
                     sa.column('status', sa.String), sa.column('info', sa.JSON), sa.column('created_at', sa.DateTime))
    try:
        with engine.begin() as db:
            db.execute(nodes.insert().values(id='wsl2', name='wsl2', status='active', info={'os': 'linux', 'wsl': '2'},
                                            created_at=datetime.now(timezone.utc)))
        command.upgrade(config, 'head')
        with engine.connect() as db:
            assert db.execute(text("SELECT role, offline_threshold_seconds FROM node WHERE id='wsl2'")).one() == (
                'workstation', 600)
        command.downgrade(config, '0041')
        command.upgrade(config, 'head')
        with engine.connect() as db:
            assert db.scalar(text("SELECT role FROM node WHERE id='wsl2'")) == 'workstation'
    finally:
        engine.dispose()
