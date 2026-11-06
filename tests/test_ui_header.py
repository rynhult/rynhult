"""Header presentation is bounded and belongs to the authenticated caller only."""
import pytest

DEFAULT = {'order': ['machine_load', 'plan_usage', 'go_to', 'conversation_search', 'file_search'], 'hidden': []}
CUSTOM = {'order': ['file_search', 'go_to', 'plan_usage', 'conversation_search', 'machine_load'], 'hidden': ['plan_usage', 'conversation_search']}


async def test_header_auth_csrf_persistence_and_person_isolation(api, env, member_accounts):
    from test_auth import cookie_client, do_setup
    from test_users import browser, csrf, join
    token_headers = {'Authorization': api.headers['Authorization']}
    async with browser(api) as anonymous:
        assert (await anonymous.get('/api/preferences/ui-header')).status_code == 401
        assert (await anonymous.put('/api/preferences/ui-header', json=CUSTOM)).status_code in (401, 403)
    await do_setup(api, env)
    owner = cookie_client(api)
    assert (await owner.get('/api/preferences/ui-header')).json() == DEFAULT
    assert (await owner.put('/api/preferences/ui-header', json=CUSTOM)).status_code == 403
    assert (await owner.put('/api/preferences/ui-header', headers=csrf(owner), json=CUSTOM)).json() == CUSTOM
    assert (await owner.get('/api/preferences/ui-header')).json() == CUSTOM
    invite = await owner.post('/api/users/invites', headers=csrf(owner), json={'account': 'dana-agent'})
    async with browser(api) as member:
        assert (await join(member, invite.json()['link'], 'alex')).status_code == 200
        assert (await member.get('/api/preferences/ui-header')).json() == DEFAULT
        # Spoofed selectors in either body or query cannot address the owner's setting.
        assert (await member.put('/api/preferences/ui-header', headers=csrf(member), json={**DEFAULT, 'username': 'casey'})).status_code == 422
        mine = {**DEFAULT, 'hidden': ['file_search']}
        assert (await member.put('/api/preferences/ui-header?username=casey', headers=csrf(member), json=mine)).json() == mine
        assert (await member.get('/api/preferences/ui-header?username=casey')).json() == mine
        assert (await member.get('/api/board')).status_code == 403
        assert (await member.get('/api/accounts')).json()['accounts'] == ['dana-agent']
        assert (await member.get('/api/accounts?account=alice-agent')).status_code == 404
    assert (await owner.get('/api/preferences/ui-header')).json() == CUSTOM
    # The installation token follows the existing preference-principal contract, in its own namespace.
    async with browser(api) as service:
        assert (await service.get('/api/preferences/ui-header', headers=token_headers)).json() == DEFAULT
        assert (await service.put('/api/preferences/ui-header', headers=token_headers, json={**DEFAULT, 'hidden': ['go_to']})).status_code == 200
    assert (await owner.get('/api/preferences/ui-header')).json() == CUSTOM
    assert (await owner.put('/api/preferences/ui-header', headers=csrf(owner), json=DEFAULT)).json() == DEFAULT


@pytest.mark.parametrize('invalid', [
    {}, {'order': [], 'hidden': []}, {**DEFAULT, 'order': DEFAULT['order'][:-1]},
    {**DEFAULT, 'order': DEFAULT['order'] + ['go_to']},
    {**DEFAULT, 'order': ['go_to'] * 5}, {**DEFAULT, 'hidden': ['go_to'] * 6},
    {**DEFAULT, 'hidden': ['go_to', 'go_to']}, {**DEFAULT, 'hidden': 'go_to'},
    {**DEFAULT, 'hidden': ['security']}, {**DEFAULT, 'hidden': ['identity']},
    {**DEFAULT, 'hidden': ['account']}, {**DEFAULT, 'hidden': ['live']},
    {**DEFAULT, 'hidden': ['inbox']}, {**DEFAULT, 'hidden': ['connection']},
    {**DEFAULT, 'hidden': ['updates']}, {**DEFAULT, 'hidden': ['new_session']},
    {**DEFAULT, 'hidden': ['x' * 5000]}, {**DEFAULT, 'order': [False] * 5},
    {**DEFAULT, 'host': 'other.example.lan'},
])
async def test_header_rejects_unknown_duplicate_missing_and_oversized_modules(api, invalid):
    assert (await api.put('/api/preferences/ui-header', json=CUSTOM)).status_code == 200
    assert (await api.put('/api/preferences/ui-header', json=invalid)).status_code == 422
    assert (await api.get('/api/preferences/ui-header')).json() == CUSTOM


async def test_corrupt_stored_header_falls_back_to_safe_defaults(api):
    from helpers import write_db

    from acc.server.models import Setting
    assert (await api.put('/api/preferences/ui-header', json=CUSTOM)).status_code == 200
    with write_db(api.app.state.db) as db:
        row = db.query(Setting).filter(Setting.key.like('ui_header.%')).one()
        row.value = {'order': ['security'], 'hidden': ['account']}
        db.commit()
    assert (await api.get('/api/preferences/ui-header')).json() == DEFAULT
