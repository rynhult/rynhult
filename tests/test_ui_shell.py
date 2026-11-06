"""The new shell is always on; legacy storage remains caller-scoped."""
from acc.server.admin_features import FEATURES, Features
from acc.server.config import Settings


def test_new_shell_is_preview_and_default_on():
    assert 'new_shell' in FEATURES
    assert FEATURES['new_shell'].preview
    assert Features(Settings(), {}).enabled('new_shell')


async def test_ui_shell_auth_csrf_and_person_isolation(api, env, member_accounts):
    from test_auth import cookie_client, do_setup
    from test_users import browser, csrf, join
    async with browser(api) as anonymous:
        assert (await anonymous.get('/api/preferences/ui-shell')).status_code == 401
        assert (await anonymous.put('/api/preferences/ui-shell', json={'enabled': True})).status_code in (401, 403)
    await do_setup(api, env)
    owner = cookie_client(api)
    initial = await owner.get('/api/preferences/ui-shell')
    assert initial.status_code == 200
    assert initial.json()['enabled'] is True
    owner_id = initial.json()['person_id']
    assert isinstance(owner_id, str) and len(owner_id) == 36
    assert (await owner.put('/api/preferences/ui-shell', json={'enabled': True})).status_code == 403
    saved = await owner.put('/api/preferences/ui-shell', headers=csrf(owner), json={'enabled': True})
    assert saved.status_code == 200 and saved.json()['enabled'] is True
    invite = await owner.post('/api/users/invites', headers=csrf(owner), json={'role': 'member', 'account': 'dana-agent'})
    async with browser(api) as member:
        assert (await join(member, invite.json()['link'], 'alex')).status_code == 200
        own = (await member.get('/api/preferences/ui-shell')).json()
        assert own['enabled'] is True and own['person_id'] != owner_id
        assert (await member.put('/api/preferences/ui-shell', headers=csrf(member), json={'enabled': False, 'username': 'casey'})).status_code == 200
        assert (await member.put('/api/preferences/ui-shell', headers=csrf(member), json={'enabled': 'yes'})).status_code == 422
    assert (await owner.get('/api/preferences/ui-shell')).json() == {'enabled': True, 'person_id': owner_id}
    assert (await owner.put('/api/preferences/ui-shell', headers=csrf(owner), json={'enabled': False})).json()['enabled'] is True


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
            identities.append((await member.get('/api/preferences/ui-shell')).json()['person_id'])
            users = (await owner.get('/api/users')).json()['users']
            ids.append(next(u['id'] for u in users if u['username'] == name))
            if name == 'alex':
                removed = await owner.post(f'/api/users/{ids[-1]}/remove', headers=csrf(owner), json={'resources': 'archive'})
                assert removed.status_code == 200
    assert ids[0] == ids[1]  # SQLite really did recycle the numeric primary key.
    assert identities[0] != identities[1]


async def test_upgrade_preserves_explicit_layout_choices(api, env):
    from test_auth import cookie_client, do_setup

    from acc.server.models import Setting
    await do_setup(api, env)
    owner = cookie_client(api)
    username = (await owner.get('/api/auth/state')).json()['username']
    for choice in (False, True):
        with api.app.state.ctx.db() as db:
            db.merge(Setting(key=f'ui_shell.{username}', value={'enabled': choice}))
            db.commit()
        assert (await owner.get('/api/preferences/ui-shell')).json()['enabled'] is True
        with api.app.state.ctx.db() as db:
            assert db.get(Setting, f'ui_shell.{username}').value['enabled'] is choice
    # An existing person who never chose adopts the new default without a backfill.
    with api.app.state.ctx.db() as db:
        db.delete(db.get(Setting, f'ui_shell.{username}'))
        db.commit()
    assert (await owner.get('/api/preferences/ui-shell')).json()['enabled'] is True


def test_explicit_admin_rollout_choice_cannot_disable_shell():
    for choice in (False, True):
        features = Features(Settings(), {'new_shell': choice})
        assert features.enabled('new_shell') is True
        assert all(row['id'] != 'new_shell' for row in features.rows())


def test_server_override_cannot_disable_shell():
    assert Features(Settings(feature_overrides={'new_shell': False})).enabled('new_shell')
