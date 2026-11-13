"""Experience choices are personal presentation, never Security or launch authority."""
import pytest
from test_auth import cookie_client, do_setup
from test_users import browser, csrf, join

from acc.server.setup_wizard import initial


async def test_setup_level_requires_a_person_and_csrf(api, env):
    # The installation token has no personal experience to configure.
    assert (await api.get('/api/preferences/setup-level')).status_code == 403
    async with browser(api) as anonymous:
        assert (await anonymous.get('/api/preferences/setup-level')).status_code == 401
    await do_setup(api, env)
    owner = cookie_client(api)
    assert (await owner.get('/api/preferences/setup-level')).json() == {
        'level': None, 'dismissed': False, 'version': 1, 'wizard': initial(fresh=True)}
    assert (await owner.put('/api/preferences/setup-level', json={'level': 0, 'dismissed': True})).status_code == 403
    assert (await owner.put('/api/preferences/setup-level', headers=csrf(owner),
                            json={'level': None, 'dismissed': True})).json()['level'] is None
    assert (await cookie_client(api).get('/api/preferences/setup-level')).json()['dismissed'] is True


async def test_each_level_preserves_security_and_other_people(api, env, member_accounts):
    await do_setup(api, env)
    owner = cookie_client(api)
    from acc.server import security_settings
    with api.app.state.db() as db:
        security_settings.save(db, {'kind': 'instance'}, {'secrets': False}, actor='casey', expected_revision=0)
        db.commit()
    before = (await owner.get('/api/security/registry')).json()
    features_before = (await owner.get('/api/admin-features')).json()
    invite = await owner.post('/api/users/invites', headers=csrf(owner), json={'account': 'dana-agent'})
    async with browser(api) as member:
        assert (await join(member, invite.json()['link'], 'alex')).status_code == 200
        for level in range(7):
            response = await owner.put('/api/preferences/setup-level', headers=csrf(owner),
                                       json={'level': level, 'dismissed': True})
            assert response.status_code == 200
            assert response.json() == {'level': level, 'dismissed': True, 'version': 1, 'wizard': initial(fresh=True)}
            assert (await member.get('/api/preferences/setup-level')).json()['level'] is None
        assert (await member.put('/api/preferences/setup-level', headers=csrf(member),
                                 json={'level': 1, 'dismissed': True})).status_code == 200
        assert (await owner.get('/api/preferences/setup-level')).json()['level'] == 6
    assert (await owner.get('/api/security/registry')).json() == before
    assert (await owner.get('/api/admin-features')).json() == features_before


@pytest.mark.parametrize('body', [
    {'level': -1}, {'level': 7}, {'level': True}, {'level': '0'}, {'level': 1.5},
    {'level': 0, 'username': 'alex'}, {'level': 0, 'dismissed': 'yes'},
    {'level': 0, 'security': {'provider_prompts': False}},
])
async def test_invalid_or_authority_fields_are_refused(api, env, body):
    await do_setup(api, env)
    owner = cookie_client(api)
    assert (await owner.put('/api/preferences/setup-level', headers=csrf(owner), json=body)).status_code == 422
    assert (await owner.get('/api/preferences/setup-level')).json()['level'] is None


@pytest.mark.parametrize('value', [
    {'version': 2, 'level': 1}, {'version': 1, 'level': 99},
    {'version': 1, 'level': True}, {},
])
async def test_unknown_saved_preferences_preserve_current_layout(api, env, value):
    from acc.server.models import Setting
    await do_setup(api, env)
    with api.app.state.db() as db:
        db.merge(Setting(key='setup_level.casey', value=value))
        db.commit()
    assert (await cookie_client(api).get('/api/preferences/setup-level')).json()['level'] is None
