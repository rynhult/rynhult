"""Import refuses existing data and verifies every table without changing SQLite."""
import hashlib
import json

import pytest
from sqlalchemy import text

from acc.server.db import make_engine, migrate


def test_readonly_copy_and_verification(tmp_path):
    from acc.db_transfer import copy_database, open_source, verify_database
    source = tmp_path / 'source.db'
    url = f'sqlite:///{source}'
    migrate(url)
    engine = make_engine(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO setting (key,value,updated_at) VALUES ('demo', :json, CURRENT_TIMESTAMP)"),
                   {'json': '{"unicode":"café","nested":[true,1,null]}'})
        db.execute(text("INSERT INTO transcript_fts (session_id,text,role,ts,seq) VALUES ('example','billing','user','t',0)"))
    engine.dispose()
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    target = make_engine(f'sqlite:///{tmp_path}/target.db')
    migrate(str(target.url))
    with open_source(source) as src, target.begin() as dst:
        with pytest.raises(Exception, match='readonly'):
            src.exec_driver_sql("DELETE FROM setting")
        report = copy_database(src, dst)
        assert report['setting']['rows'] == 1
        assert report['transcript_fts']['rows'] == 1
        assert len(report['setting']['sha256']) == 64
        assert verify_database(src, dst) == report
        dst.execute(text("UPDATE setting SET value='{}' WHERE key='demo'"))
        with pytest.raises(ValueError, match='setting'):
            verify_database(src, dst)
    with open_source(source) as src, target.begin() as dst:
        with pytest.raises(ValueError, match='empty'):
            copy_database(src, dst)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    target.dispose()


def test_copy_rolls_back_on_verification_failure(tmp_path):
    from sqlalchemy import event

    from acc.db_transfer import copy_database, open_source
    source = tmp_path / 'source.db'
    target_url = f'sqlite:///{tmp_path}/target.db'
    migrate(f'sqlite:///{source}')
    migrate(target_url)
    source_engine = make_engine(f'sqlite:///{source}')
    with source_engine.begin() as db:
        db.exec_driver_sql("INSERT INTO setting(key,value,updated_at) VALUES('demo','{}',CURRENT_TIMESTAMP)")
    source_engine.dispose()
    target = make_engine(target_url)
    def corrupt(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO setting'):
            connection.exec_driver_sql("UPDATE setting SET value='true'")
    event.listen(target, 'after_cursor_execute', corrupt)
    try:
        with pytest.raises(ValueError, match='setting'), open_source(source) as src, target.begin() as dst:
            copy_database(src, dst)
        with target.connect() as db:
            assert db.scalar(text('SELECT count(*) FROM setting')) == 0
            assert db.scalar(text('SELECT revision FROM security_revision WHERE id=1')) == 0
    finally:
        event.remove(target, 'after_cursor_execute', corrupt)
        target.dispose()


def test_copy_preserves_sql_null_and_json_null(tmp_path):
    from sqlalchemy import JSON, null
    from sqlalchemy.orm import Session as DbSession

    from acc.db_transfer import copy_database, open_source
    from acc.server.models import LaunchSpec, Project, Session
    source = tmp_path / 'source.db'
    migrate(f'sqlite:///{source}')
    engine = make_engine(f'sqlite:///{source}')
    with DbSession(engine) as db:
        project = Project(slug='demo', name='Demo', account='alex-agent', workspace_path='/demo')
        spec = LaunchSpec(provider='claude', inputs={}, files={}, digest='demo')
        db.add(Session(id='example', name='Example', project=project, launch_spec=spec,
                       tmux_name='example', provider='claude', run_as='alex-agent',
                       changes=null(), automation=JSON.NULL))
        db.commit()
    engine.dispose()
    target_url = f'sqlite:///{tmp_path}/target.db'
    migrate(target_url)
    target = make_engine(target_url)
    try:
        with open_source(source) as src, target.begin() as dst:
            copy_database(src, dst)
            assert dst.execute(text('SELECT changes IS NULL, automation FROM session')).one() == (1, 'null')
    finally:
        target.dispose()


def test_transfer_cli_refuses_invalid_target_without_traceback(tmp_path, monkeypatch):
    import subprocess
    import sys

    monkeypatch.setenv('ACC_DATABASE_URL', 'invalid-url')
    result = subprocess.run([sys.executable, '-m', 'acc.db_transfer', '--sqlite', str(tmp_path / 'missing.db')],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert 'PostgreSQL URL' in result.stderr
    assert 'Traceback' not in result.stderr
    assert not (tmp_path / 'missing.db').exists()


@pytest.mark.parametrize('uid,euid', [(1000, 1000), (1000, 0), (0, 1000)])
def test_transfer_cli_refuses_non_root_before_opening_databases(monkeypatch, capsys, tmp_path, uid, euid):
    import sys

    from acc import db_transfer

    monkeypatch.setattr(db_transfer.os, 'getuid', lambda: uid)
    monkeypatch.setattr(db_transfer.os, 'geteuid', lambda: euid)
    monkeypatch.setenv('ACC_DATABASE_URL', 'postgresql+psycopg://db.example.lan/acc')
    monkeypatch.setattr(sys, 'argv', ['acc-db-transfer', '--sqlite', str(tmp_path / 'missing.db')])
    with pytest.raises(SystemExit) as failure:
        db_transfer.main()
    assert failure.value.code == 2
    assert 'root' in capsys.readouterr().err
    assert not (tmp_path / 'missing.db').exists()


@pytest.mark.parametrize('target_state', ['fresh', 'changed_revision', 'policy', 'extra_revision', 'other_data'])
def test_transfer_preserves_security_and_only_replaces_pristine_seed(tmp_path, target_state):
    from acc.db_transfer import copy_database, open_source
    source = tmp_path / 'security-source.db'
    target = tmp_path / 'security-target.db'
    migrate(f'sqlite:///{source}')
    migrate(f'sqlite:///{target}')
    src_engine = make_engine(f'sqlite:///{source}')
    with src_engine.begin() as db:
        db.exec_driver_sql('UPDATE security_revision SET revision=7')
        db.exec_driver_sql("INSERT INTO security_policy (key,scope,overrides,registry_version) VALUES ('instance', '{}', '{\"secrets\":false}', 1)")
    src_engine.dispose()
    engine = make_engine(f'sqlite:///{target}')
    try:
        with engine.begin() as db:
            if target_state == 'changed_revision':
                db.exec_driver_sql('UPDATE security_revision SET revision=1')
            elif target_state == 'extra_revision':
                db.exec_driver_sql('INSERT INTO security_revision VALUES (2,0)')
            elif target_state == 'other_data':
                db.exec_driver_sql("INSERT INTO setting(key,value,updated_at) VALUES ('existing','{}',CURRENT_TIMESTAMP)")
            elif target_state == 'policy':
                db.exec_driver_sql("INSERT INTO security_policy (key,scope,overrides,registry_version) VALUES ('instance', '{}', '{}', 1)")
        with open_source(source) as src, engine.begin() as dst:
            if target_state == 'fresh':
                report = copy_database(src, dst)
                assert report['security_policy']['rows'] == report['security_revision']['rows'] == 1
                assert dst.scalar(text('SELECT revision FROM security_revision WHERE id=1')) == 7
                assert json.loads(dst.scalar(text('SELECT overrides FROM security_policy'))) == {'secrets': False}
            else:
                with pytest.raises(ValueError, match='empty'):
                    copy_database(src, dst)
                # Refusal never clears the seed or existing target choices.
                assert dst.scalar(text('SELECT revision FROM security_revision WHERE id=1')) == (1 if target_state == 'changed_revision' else 0)
                assert dst.scalar(text('SELECT count(*) FROM security_policy')) == (1 if target_state == 'policy' else 0)
                assert dst.scalar(text('SELECT count(*) FROM setting')) == (1 if target_state == 'other_data' else 0)
    finally:
        engine.dispose()
