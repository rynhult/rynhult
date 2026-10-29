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
