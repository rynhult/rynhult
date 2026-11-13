"""Retired layout state is removed; stable presentation identity is authenticated."""
from acc.server.admin_features import FEATURES, Features
from acc.server.config import Settings


def test_layout_feature_is_absent():
    assert 'new_shell' not in FEATURES
    assert all(row['id'] != 'new_shell' for row in Features(Settings()).rows())

async def test_presentation_identity_is_authenticated_and_caller_scoped(api, env, member_accounts):
    from test_auth import cookie_client, do_setup
    from test_users import browser, csrf, join
    async with browser(api) as anonymous:
        state = (await anonymous.get('/api/auth/state')).json()
        assert not state['authenticated'] and 'person_id' not in state
    await do_setup(api, env)
    owner = cookie_client(api)
    owner_id = (await owner.get('/api/auth/state')).json()['person_id']
    assert isinstance(owner_id, str) and len(owner_id) == 36
    invite = await owner.post('/api/users/invites', headers=csrf(owner), json={'role': 'member', 'account': 'dana-agent'})
    async with browser(api) as member:
        assert (await join(member, invite.json()['link'], 'alex')).status_code == 200
        assert (await member.get('/api/auth/state')).json()['person_id'] != owner_id
    assert (await owner.get('/api/auth/state')).json()['person_id'] == owner_id
    assert (await owner.get('/api/preferences/ui-shell')).status_code == 404
    assert (await owner.put('/api/preferences/ui-shell', headers=csrf(owner), json={'enabled': False})).status_code == 404
    assert (await owner.put('/api/admin-features/new_shell', headers=csrf(owner), json={'enabled': True, 'revision': 0})).status_code == 404

async def test_shell_identity_is_not_reassigned_when_sqlite_reuses_user_id(api, env, member_accounts):
    from test_auth import cookie_client, do_setup
    from test_users import browser, csrf, join
    await do_setup(api, env)
    owner = cookie_client(api)
    identities = []
    ids = []
    for name in ('alex', 'blair'):
        invite = await owner.post('/api/users/invites', headers=csrf(owner), json={'account': 'dana-agent'})
        async with browser(api) as member:
            assert (await join(member, invite.json()['link'], name)).status_code == 200
            identities.append((await member.get('/api/auth/state')).json()['person_id'])
            users = (await owner.get('/api/users')).json()['users']
            ids.append(next(u['id'] for u in users if u['username'] == name))
            if name == 'alex':
                removed = await owner.post(f'/api/users/{ids[-1]}/remove', headers=csrf(owner), json={'resources': 'archive'})
                assert removed.status_code == 200
    assert ids[0] == ids[1]  # SQLite really did recycle the numeric primary key.
    assert identities[0] != identities[1]

def test_upgrade_removes_only_retired_choices(tmp_path):
    from alembic import command
    from sqlalchemy.orm import Session

    from acc.server.db import alembic_config, make_engine
    from acc.server.models import Setting

    url = f'sqlite:///{tmp_path}/upgrade.db'
    config = alembic_config(url)
    command.upgrade(config, '0087')
    engine = make_engine(url)
    with Session(engine) as db:
        db.add_all([
            Setting(key='ui_shell.alex', value={'enabled': False}),
            Setting(key='ui_shell.blair', value={'enabled': True}),
            Setting(key='uiXshell.alex', value={'unrelated': True}),
            Setting(key='ui_header.alex', value={'order': ['preserved']}),
            Setting(key='setup_level.alex', value={'level': 3}),
            Setting(key='admin_features', value={'choices': {'new_shell': False, 'publishing': False}, 'revision': 7}),
        ])
        db.commit()
    engine.dispose()
    command.upgrade(config, 'head')
    engine = make_engine(url)
    with Session(engine) as db:
        assert db.get(Setting, 'ui_shell.alex') is None
        assert db.get(Setting, 'ui_shell.blair') is None
        assert db.get(Setting, 'uiXshell.alex').value == {'unrelated': True}
        assert db.get(Setting, 'ui_header.alex').value == {'order': ['preserved']}
        assert db.get(Setting, 'setup_level.alex').value == {'level': 3}
        assert db.get(Setting, 'admin_features').value == {'choices': {'publishing': False}, 'revision': 7}
    engine.dispose()
