"""The native metadata migration preserves existing browser enrollment and sign-ins."""
import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from acc.server.db import alembic_config, make_engine, migrate


@pytest.mark.parametrize('predecessor', ['0053', '0066'])
def test_mobile_upgrades_populated_previous_schema_and_downgrades(tmp_path, predecessor):
    path = tmp_path / 'legacy.db'
    url = 'sqlite:///' + str(path)
    config = alembic_config(url)
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    assert '0067' in {revision.revision for revision in scripts.walk_revisions()}
    assert scripts.get_revision('0067').down_revision == '0066'
    command.upgrade(config, predecessor)
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO device_pairing (id,person,label,token_hash,status,expires_at,created_at) VALUES "
                   "('pair','alex','Browser','hash','pending','2030-01-01','2026-01-01')")
        db.commit()
    migrate(url)
    engine = make_engine(url)
    try:
        schema = inspect(engine)
        assert {'device_fingerprint', 'device_bound_at'} <= {c['name'] for c in schema.get_columns('auth_session')}
        assert {'installation_id', 'platform', 'key_hash'} <= {c['name'] for c in schema.get_columns('device')}
        with engine.connect() as db:
            assert db.exec_driver_sql('SELECT person,label,kind,csr_pem FROM device_pairing').all() == [('alex','Browser','browser',None)]
            assert db.exec_driver_sql('PRAGMA foreign_key_check').all() == []
    finally:
        engine.dispose()
    command.downgrade(config, predecessor)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT person,label,status FROM device_pairing').fetchall() == [('alex','Browser','pending')]
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
