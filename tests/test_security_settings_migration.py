"""Historical SQLite upgrade and isolated backup recovery preserve explicit choices."""
import sqlite3

import pytest
from alembic import command
from sqlalchemy.orm import Session

from acc.server.db import alembic_config, make_engine
from acc.server.models import Setting


@pytest.mark.parametrize('baseline', ['0079', '0080'])
def test_upgrade_and_backup_restore_keep_false_and_person_tool_exception(tmp_path, baseline):
    from acc.server import security_settings as security
    path = tmp_path / 'legacy.db'
    url = 'sqlite:///' + str(path)
    config = alembic_config(url)
    command.upgrade(config, baseline)
    engine = make_engine(url)
    with Session(engine) as db:
        db.add_all([Setting(key='guardrails', value={'rules': {'secrets': False, 'force_push': False},
            'custom': [{'pattern': 'terraform destroy', 'reason': 'Keep'}]}),
            Setting(key='tools.person.alex', value={'required': False})])
        db.commit()
    engine.dispose()
    backup = tmp_path / 'before.db'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as dest:
        source.backup(dest)
    command.upgrade(config, 'head')
    engine = make_engine(url)
    with Session(engine) as db:
        effective = security.resolve(db, owner='alex', machine='local', account='alex-agent')
        assert effective['switches']['secrets'] is False
        assert effective['switches']['force_push'] is False
        assert effective['switches']['tool_install_check'] is False
        assert db.get(Setting, 'guardrails').value['custom'][0]['reason'] == 'Keep'
        security.save(db, {'kind': 'instance'}, {'secrets': True}, actor='alex', expected_revision=0)
        db.commit()
    engine.dispose()
    with pytest.raises(RuntimeError, match='restore'):
        command.downgrade(config, '0079')
    # Restore backup into a different owned path, upgrade again and compare behavior.
    restored = tmp_path / 'restored.db'
    with sqlite3.connect(backup) as source, sqlite3.connect(restored) as dest:
        source.backup(dest)
    restored_url = 'sqlite:///' + str(restored)
    command.upgrade(alembic_config(restored_url), 'head')
    engine = make_engine(restored_url)
    with Session(engine) as db:
        assert security.resolve(db, owner='alex')['switches']['secrets'] is False
        assert security.resolve(db, owner='alex')['switches']['tool_install_check'] is False
    engine.dispose()


def test_folder_policy_storage_retains_applied_vs_desired_revision(api):
    from acc.server.models import SecurityFolderPolicy
    with api.app.state.db() as db:
        db.add(SecurityFolderPolicy(key='local:alex-agent', machine_id='local', account='alex-agent',
            roots=['/home/alex/src/demo-api'], revision=2, applied_revision=1, state='pending',
            detail={}, updated_by='alex'))
        db.commit()
    with api.app.state.db() as db:
        row = db.get(SecurityFolderPolicy, 'local:alex-agent')
        assert row.revision == 2 and row.applied_revision == 1 and row.state == 'pending'
        assert row.roots == ['/home/alex/src/demo-api']


def test_security_follows_main_team_migration_with_one_head(tmp_path):
    from alembic.script import ScriptDirectory
    scripts = ScriptDirectory.from_config(alembic_config('sqlite:///' + str(tmp_path / 'chain.db')))
    assert scripts.get_heads() == ['0081']
    assert scripts.get_revision('0081').down_revision == '0080'
