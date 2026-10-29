"""Migration upgrades preserve routing and roll back DDL as well as data."""
import sqlite3
from contextlib import closing

import pytest
from alembic import command
from sqlalchemy import create_engine, event, inspect, text

from acc.server.db import alembic_config, migrate


def test_failed_group_migration_rolls_back(backend_url):
    url = backend_url
    command.upgrade(alembic_config(url), '0037')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO project (id, slug, name, workspace_path, account, owner, host, trusted, archived, import_mode, created_at) VALUES (1, 'demo', 'Demo', '/private/demo', 'alex-agent', 'alex', 'local', false, false, 'managed', CURRENT_TIMESTAMP)"))
    # Inject failure after most DDL, at the first backfill statement.
    from sqlalchemy.engine import Engine
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO placement'):
            raise RuntimeError('injected backfill failure')
    event.listen(Engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='injected'):
            migrate(url)
    finally:
        event.remove(Engine, 'before_cursor_execute', fail)
    assert 'acc_group' not in inspect(engine).get_table_names()
    assert 'group_id' not in {c['name'] for c in inspect(engine).get_columns('project')}
    with engine.connect() as c:
        assert c.scalar(text('SELECT version_num FROM alembic_version')) == '0037'
        assert c.scalar(text('SELECT workspace_path FROM project')) == '/private/demo'
    migrate(url)
    with engine.connect() as c:
        assert c.execute(text('SELECT person, host, account, path FROM placement')).one() == ('alex', 'local', 'alex-agent', '/private/demo')
        assert c.scalar(text('SELECT count(*) FROM acc_group')) == 0
    engine.dispose()


def test_populated_migration_freezes_worktree_and_helper_routing(backend_url):
    """Pre-group sessions retain their real routes, specs, backup identity and personal authority."""
    from sqlalchemy.orm import Session as DbSession

    from acc.server.models import LaunchSpec, Project, Session
    url = backend_url
    engine = create_engine(url)
    migrate(url)
    with DbSession(engine) as s:
        p = Project(slug='demo', name='Demo', owner='alex', account='alex-agent', workspace_path='/private/source',
                    host='local', backup_identity='unchanged-backup')
        spec = LaunchSpec(provider='claude', inputs={'private': 'prompt-canary'}, files={'instructions.md': 'unchanged'}, digest='original')
        session = Session(id='legacy-session', project=p, launch_spec=spec, name='Private', provider='claude',
                          tmux_name='legacy-pane', owner='alex', run_as='alex-agent', worktree='/private/worktree',
                          team_host='helper-machine')
        s.add(session)
        s.commit()
    command.downgrade(alembic_config(url), '0037')
    migrate(url)
    with DbSession(engine) as s:
        session = s.get(Session, 'legacy-session')
        assert session.placement_id is not None
        assert (session.placement.person, session.placement.host, session.placement.account, session.placement.path) == (
            'alex', 'helper-machine', 'alex-agent', '/private/worktree')
        assert session.project.owner == 'alex' and session.project.group_id is None
        assert session.project.backup_identity == 'unchanged-backup'
        assert session.launch_spec.inputs == {'private': 'prompt-canary'}
        assert session.launch_spec.files == {'instructions.md': 'unchanged'}
        assert session.launch_spec.digest == 'original'
        session.project.workspace_path = '/moved'
        session.project.host = 'another-host'
        s.commit()
        assert session.cwd == '/private/worktree' and session.host == 'helper-machine'
    engine.dispose()


@pytest.mark.parametrize("copy_existing", [False, True], ids=["fresh", "copy-at-0054"])
def test_main_oidc_head_upgrades_to_group_machine_accounts(tmp_path, copy_existing):
    """An installation at main's OIDC head keeps its identities and private routing."""
    from alembic.script import ScriptDirectory

    url = f'sqlite:///{tmp_path / "main-upgrade.db"}'
    config = alembic_config(url)
    command.upgrade(config, '0054')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO project (id, slug, name, workspace_path, account, owner, host, trusted, archived, import_mode, created_at) VALUES (1, 'demo', 'Demo', '/private/demo', 'alex-agent', 'alex', 'local', false, false, 'managed', CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO oidc_provider (id, issuer, client_id, mfa_acr, passkey_enrollment, updated_at) VALUES (1, 'https://identity.example.com', 'demo', :acr, false, CURRENT_TIMESTAMP)"), {'acr': '["urn:example:mfa"]'})
        c.execute(text("INSERT INTO placement (id,project_id,person,host,account,path,trust,sync_status) VALUES (1,1,'alex','local','alex-agent','/private/demo','approved','ready')"))
        c.execute(text("INSERT INTO user (id,username,password_hash,totp_secret,person_id,company_only,created_at) VALUES (1,'alex','test-hash','test-totp','11111111-1111-4111-8111-111111111111',true,CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO external_identity (id,organization,issuer,subject,person_id,created_by,created_at) VALUES (1,'installation','https://identity.example.com','alex-subject','11111111-1111-4111-8111-111111111111','casey',CURRENT_TIMESTAMP)"))
    if copy_existing:
        engine.dispose()
        source = tmp_path / "main-upgrade.db"
        destination = tmp_path / "restored-copy.db"
        with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(destination)) as restored:
            original.backup(restored)
        url = f'sqlite:///{destination}'
        config = alembic_config(url)
        engine = create_engine(url)
    assert 'group_machine_account' not in inspect(engine).get_table_names()
    migrate(url)
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1  # one head; phase 4 (0063, 0064) is in its chain, later migrations may follow
    assert 'group_machine_account' in inspect(engine).get_table_names()
    assert 'machine_account_id' in {column['name'] for column in inspect(engine).get_columns('placement')}
    assert any(fk['referred_table'] == 'group_machine_account' for fk in inspect(engine).get_foreign_keys('placement'))
    with engine.connect() as c:
        assert c.scalar(text('SELECT version_num FROM alembic_version')) == heads[0]
        assert c.scalar(text('SELECT issuer FROM oidc_provider')) == 'https://identity.example.com'
        assert c.scalar(text('SELECT workspace_path FROM project')) == '/private/demo'
        assert c.scalar(text('SELECT count(*) FROM group_machine_account')) == 0
        assert c.execute(text('SELECT person_id, company_only FROM user')).one() == (
            '11111111-1111-4111-8111-111111111111', 1)
        assert c.execute(text('SELECT issuer, subject, person_id FROM external_identity')).one() == (
            'https://identity.example.com', 'alex-subject', '11111111-1111-4111-8111-111111111111')
        assert c.execute(text('SELECT mfa_acr, passkey_enrollment FROM oidc_provider')).one() == ('["urn:example:mfa"]', 0)
        assert c.execute(text('SELECT person, host, account, path, machine_account_id FROM placement')).one() == (
            'alex', 'local', 'alex-agent', '/private/demo', None)
    command.downgrade(config, '0054')
    assert 'group_machine_account' not in inspect(engine).get_table_names()
    assert 'oidc_provider' in inspect(engine).get_table_names()
    assert 'mfa_acr' in {column['name'] for column in inspect(engine).get_columns('oidc_provider')}
    with engine.connect() as c:
        assert c.scalar(text('SELECT version_num FROM alembic_version')) == '0054'
        assert c.execute(text('SELECT person, host, account, path FROM placement')).one() == (
            'alex', 'local', 'alex-agent', '/private/demo')
        assert c.scalar(text('SELECT company_only FROM user')) == 1
        assert c.scalar(text('SELECT subject FROM external_identity')) == 'alex-subject'
        assert c.execute(text('PRAGMA foreign_key_check')).all() == []
    if copy_existing:
        with closing(sqlite3.connect(tmp_path / 'main-upgrade.db')) as original:
            assert original.execute('SELECT version_num FROM alembic_version').fetchone() == ('0054',)
            assert original.execute("SELECT count(*) FROM sqlite_master WHERE name='group_machine_account'").fetchone() == (0,)
    migrate(url)
    engine.dispose()


def test_proof_upgrade_does_not_invent_evidence_for_existing_approvals(tmp_path):
    url = f'sqlite:///{tmp_path / "proof-upgrade.db"}'
    config = alembic_config(url)
    command.upgrade(config, '0063')
    engine = create_engine(url)
    with engine.begin() as c:
        # Seed revision 0063's schema, not today's ORM with later node columns.
        c.execute(text("INSERT INTO node (id,name,status,info,created_by,created_at) "
                       "VALUES (:node,'alex-box','pending','{}','casey',CURRENT_TIMESTAMP)"), {'node': 'a' * 32})
        c.execute(text("INSERT INTO group_machine_account (id,node_id,person,account,status,approved_by) VALUES (:binding,:node,'alex','agent','approved','casey')"),
                  {'binding': 'b' * 32, 'node': 'a' * 32})
    migrate(url)
    with engine.connect() as c:
        assert c.execute(text('SELECT status, approved_by, proof_verified_at FROM group_machine_account')).one() == ('approved', 'casey', None)
    command.downgrade(config, '0063')
    with engine.connect() as c:
        assert c.scalar(text('SELECT person FROM group_machine_account')) == 'alex'
    engine.dispose()


def test_reserved_phase4_chain_fresh_upgrade_and_both_downgrades(tmp_path):
    from alembic.script import ScriptDirectory

    url = f'sqlite:///{tmp_path / "fresh-phase4.db"}'
    config = alembic_config(url)
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision('0064').down_revision == '0063'
    assert scripts.get_revision('0063').down_revision == '0054'
    command.upgrade(config, 'head')
    engine = create_engine(url)
    assert 'proof_verified_at' in {column['name'] for column in inspect(engine).get_columns('group_machine_account')}
    command.downgrade(config, '0063')
    assert 'proof_verified_at' not in {column['name'] for column in inspect(engine).get_columns('group_machine_account')}
    assert any(index['name'] == 'uq_group_machine_account_reserved' and index['unique']
               for index in inspect(engine).get_indexes('group_machine_account'))
    command.downgrade(config, '0054')
    assert 'group_machine_account' not in inspect(engine).get_table_names()
    assert 'machine_account_id' not in {column['name'] for column in inspect(engine).get_columns('placement')}
    assert 'company_only' in {column['name'] for column in inspect(engine).get_columns('user')}
    command.upgrade(config, 'head')
    with engine.connect() as c:
        assert c.scalar(text('SELECT version_num FROM alembic_version')) == scripts.get_current_head()
        assert c.execute(text('PRAGMA foreign_key_check')).all() == []
    engine.dispose()
