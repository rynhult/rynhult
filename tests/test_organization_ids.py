"""Dry-run uses a private snapshot and reports ambiguous ownership without rewriting it."""
import sqlite3
import subprocess
import sys


def test_dry_run_reports_mapping_orphans_and_never_changes_source(tmp_path):
    from acc.organization_ids import report_copy
    path = tmp_path / 'copy.db'
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE user(id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE retired_user(username TEXT PRIMARY KEY);
            CREATE TABLE project(id INTEGER PRIMARY KEY, slug TEXT, owner TEXT);
            CREATE TABLE session(id TEXT PRIMARY KEY, name TEXT, owner TEXT, project_id INTEGER);
            CREATE TABLE acc_group(id TEXT PRIMARY KEY, name TEXT, created_by TEXT);
            INSERT INTO user VALUES(1, 'alex');
            INSERT INTO retired_user VALUES('robin');
            INSERT INTO project VALUES(5, 'demo', 'alex');
            INSERT INTO project VALUES(6, 'old', 'robin');
            INSERT INTO session VALUES('opaque-session', 'Mutable title', 'unknown-person', 5);
            INSERT INTO acc_group VALUES('opaque-group', 'Mutable group', '@api');
        """)
    before = path.read_bytes()
    result = report_copy(path, namespace='e41c9c3b-4f72-4ba4-9834-e42af726a754')
    assert path.read_bytes() == before
    assert result['entities']['user']['rows'] == 1
    assert result['entities']['session']['preserve_existing_ids'] is True
    assert result['entities']['acc_group']['preserve_existing_ids'] is True
    assert result['references']['project.owner'] == {'resolved': 2, 'unresolved': 0, 'null': 0}
    assert result['references']['session.owner']['unresolved'] == 1
    assert result['references']['acc_group.created_by']['resolved'] == 1
    assert result['blocked'] is True
    assert result == report_copy(path, namespace='e41c9c3b-4f72-4ba4-9834-e42af726a754')


def test_cli_missing_file_does_not_create_database(tmp_path):
    path = tmp_path / 'absent.db'
    result = subprocess.run([sys.executable, '-m', 'acc.organization_ids', str(path),
                             '--namespace', 'e41c9c3b-4f72-4ba4-9834-e42af726a754'], capture_output=True)
    assert result.returncode != 0
    assert not path.exists()


def test_complete_schema_is_inventoried(tmp_path):
    from sqlalchemy import create_engine

    from acc.organization_ids import report_copy
    from acc.server.models import Base
    path = tmp_path / 'empty.db'
    Base.metadata.create_all(create_engine(f'sqlite:///{path}'))
    result = report_copy(path, namespace='e41c9c3b-4f72-4ba4-9834-e42af726a754')
    assert result['blocked'] is False
    assert 'placement.person' in result['references']
    assert 'infrastructure_credential.owner' in result['references']
    assert 'audit_log.actor' in result['references']


def test_foreign_keys_and_name_keyed_settings_are_reported(tmp_path):
    from sqlalchemy import create_engine

    from acc.organization_ids import report_copy
    from acc.server.models import Base
    path = tmp_path / 'copy.db'
    Base.metadata.create_all(create_engine(f'sqlite:///{path}'))
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO setting(key,value,updated_at) VALUES('budget.alex','{}','2026-10-24')")
    result = report_copy(path, namespace='e41c9c3b-4f72-4ba4-9834-e42af726a754')
    assert result['foreign_keys']['session.project_id']['target'] == 'project.id'
    assert result['name_keyed_settings'] == {'budget.': 1, 'tools.person.': 0, 'risk_labels.': 0, 'phone_banner.': 0}


def test_reserved_identity_collision_blocks_mapping(tmp_path):
    import pytest

    from acc.organization_ids import report_copy
    path = tmp_path / 'copy.db'
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE user(id INTEGER PRIMARY KEY, username TEXT); INSERT INTO user VALUES(1,'@api');")
    with pytest.raises(ValueError, match='collision'):
        report_copy(path, namespace='e41c9c3b-4f72-4ba4-9834-e42af726a754')
