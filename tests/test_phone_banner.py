"""The Overview's "Connect your phone" banner: shown until the person dismisses it, remembered per person (owner
answer, 2026-10-23), not per browser."""
from __future__ import annotations


async def test_phone_banner_dismissal_is_personal(api, env, member_accounts):
    from test_auth import cookie_client, do_setup
    from test_users import browser, csrf, join
    await do_setup(api, env)
    owner = cookie_client(api)
    assert (await owner.get("/api/preferences/phone-banner")).json() == {"dismissed": False}
    dismissed = await owner.put("/api/preferences/phone-banner", headers=csrf(owner), json={"dismissed": True})
    assert dismissed.status_code == 200 and dismissed.json() == {"dismissed": True}
    assert (await owner.get("/api/preferences/phone-banner")).json() == {"dismissed": True}
    invite = await owner.post("/api/users/invites", headers=csrf(owner), json={"role": "member", "account": "dana-agent"})
    async with browser(api) as member:
        assert (await join(member, invite.json()["link"], "alex")).status_code == 200
        assert (await member.get("/api/preferences/phone-banner")).json() == {"dismissed": False}
        assert (await member.put("/api/preferences/phone-banner", json={"dismissed": True})).status_code == 403  # CSRF
    assert (await owner.get("/api/preferences/phone-banner")).json() == {"dismissed": True}


async def test_phone_banner_needs_a_boolean(api):
    assert (await api.put("/api/preferences/phone-banner", json={"dismissed": "yes"})).status_code == 422
    assert (await api.put("/api/preferences/phone-banner", json={"dismissed": False})).json() == {"dismissed": False}


async def test_inbox_phone_prompt_is_personal_and_stays_dismissed(api, env, member_accounts):
    from test_auth import cookie_client, do_setup
    from test_users import browser, csrf, join
    await do_setup(api, env)
    owner = cookie_client(api)
    enable_pairing(api)
    async def prompt(client):
        return [i for i in (await client.get('/api/attention')).json() if i['kind'] == 'phone_setup']
    items = await prompt(owner)
    assert len(items) == 1 and items[0]['severity'] == 'info'
    assert items[0]['bulk_dismissible']
    assert (await prompt(owner))[0]['id'] == items[0]['id']
    response = await owner.post(f"/api/attention/{items[0]['id']}/ack", headers=csrf(owner))
    assert response.status_code == 200
    assert await prompt(owner) == []
    invite = await owner.post('/api/users/invites', headers=csrf(owner), json={'role': 'member', 'account': 'dana-agent'})
    async with browser(api) as member:
        assert (await join(member, invite.json()['link'], 'alex')).status_code == 200
        personal = await prompt(member)
        assert len(personal) == 1 and personal[0]['id'] != items[0]['id']
        assert (await member.post(f"/api/attention/{items[0]['id']}/ack", headers=csrf(member))).status_code == 404
    assert await prompt(cookie_client(api)) == []


async def test_inbox_phone_prompt_honors_old_dismissal_and_connection(api, env):
    from helpers import write_db
    from test_auth import cookie_client, do_setup
    from test_users import csrf

    from acc.server.models import PushSubscription
    await do_setup(api, env)
    owner = cookie_client(api)
    enable_pairing(api)
    await owner.put('/api/preferences/phone-banner', headers=csrf(owner), json={'dismissed': True})
    assert not any(i['kind'] == 'phone_setup' for i in (await owner.get('/api/attention')).json())
    await owner.put('/api/preferences/phone-banner', headers=csrf(owner), json={'dismissed': False})
    items = (await owner.get('/api/attention')).json()
    phone = next(i for i in items if i['kind'] == 'phone_setup')
    with write_db(api.app.state.db) as db:
        from acc.server.models import AttentionItem
        username = db.get(AttentionItem, phone['id']).owner
        db.add(PushSubscription(username=username, endpoint='https://example.com/push', p256dh='test', auth='test', label='phone'))
        db.commit()
    assert not any(i['kind'] == 'phone_setup' for i in (await owner.get('/api/attention')).json())
    history = (await owner.get('/api/attention?scope=all')).json()
    assert next(i for i in history if i['id'] == phone['id'])['resolved_at']


async def test_inbox_phone_prompt_resolves_for_active_paired_device(api, env):
    from datetime import timedelta

    from helpers import write_db
    from test_auth import cookie_client, do_setup

    from acc.server import auth
    from acc.server.models import AttentionItem, Device
    await do_setup(api, env)
    owner = cookie_client(api)
    enable_pairing(api)
    phone = next(i for i in (await owner.get('/api/attention')).json() if i['kind'] == 'phone_setup')
    with write_db(api.app.state.db) as db:
        username = db.get(AttentionItem, phone['id']).owner
        db.add(Device(id='phone-test', person=username, label='Phone', kind='browser', status='active',
                      certificate_pem='test', fingerprint='phone-test', serial='phone-test',
                      expires_at=auth.now() + timedelta(days=1)))
        db.commit()
    assert not any(i['kind'] == 'phone_setup' for i in (await owner.get('/api/attention')).json())


def enable_pairing(api, enabled=True):
    from helpers import write_db

    from acc.server.models import Setting
    with write_db(api.app.state.db) as db:
        db.merge(Setting(key="remote_access", value={"pairing_enabled": enabled}))
        db.commit()


async def test_inbox_phone_prompt_requires_pairing_and_a_real_person(api, env):
    from test_auth import cookie_client, do_setup

    await do_setup(api, env)
    bearer = api.headers["Authorization"]
    owner = cookie_client(api)
    assert not any(i['kind'] == 'phone_setup' for i in (await owner.get('/api/attention')).json())
    enable_pairing(api)
    # The bearer token cannot manage devices, even after a person registers.
    assert not any(i['kind'] == 'phone_setup' for i in (await api.get('/api/attention', headers={'Authorization': bearer})).json())
    from helpers import write_db

    from acc.server.api.context import Principal
    from acc.server.models import User
    with write_db(api.app.state.db) as db:
        person = db.query(User).one()
        username, account = person.username, person.account
    # Matching a real username still cannot turn a synthetic principal into a person.
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: Principal(username, 'owner', account)
    try:
        assert not any(i['kind'] == 'phone_setup' for i in (await api.get('/api/attention')).json())
    finally:
        api.app.dependency_overrides.pop(api.app.state.ctx.principal)
    phone = next(i for i in (await owner.get('/api/attention')).json() if i['kind'] == 'phone_setup')
    assert phone['tool'] is None and phone['publish'] is None
    enable_pairing(api, False)
    assert not any(i['kind'] == 'phone_setup' for i in (await owner.get('/api/attention')).json())
    history = (await owner.get('/api/attention?scope=all')).json()
    assert next(i for i in history if i['id'] == phone['id'])['resolved_at']
