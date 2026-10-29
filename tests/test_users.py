"""Users and roles (multi-user step 8): invites, members, resets, sign-ins and passwords."""
from __future__ import annotations

from datetime import timedelta

import httpx
from helpers import TOTP_AT, verified_post, write_db
from test_auth import ORIGIN, PASSWORD, cookie_client, do_setup

from acc.server import auth
from acc.server.models import Invite


def browser(api) -> httpx.AsyncClient:
    """Another browser on the same app: cookies only."""
    return httpx.AsyncClient(transport=api._transport, base_url="http://acc")


def csrf(client) -> dict:
    return {**ORIGIN, "X-ACC-CSRF": client.cookies.get("acc_csrf") or ""}


async def join(client, link: str, username: str | None, password: str = "member password 123") -> httpx.Response:
    token = link.rsplit("/", 1)[1]
    begin = await client.post("/api/auth/invite/begin", json={"token": token}, headers=ORIGIN)
    assert begin.status_code == 200, begin.text
    code = auth.totp(begin.json()["totp_secret"], TOTP_AT)
    return await client.post("/api/auth/invite/complete", headers=ORIGIN,
                             json={"token": token, "username": username, "password": password, "code": code})


async def test_owner_invites_a_member_who_joins_with_their_own_login(api, env, member_accounts):
    await do_setup(api, env)
    owner = cookie_client(api)
    assert (await owner.get("/api/auth/state")).json()["role"] == "owner"
    invite = await owner.post("/api/users/invites", json={"role": "member", "account": "dana-agent"}, headers=csrf(owner))
    assert invite.status_code == 201, invite.text
    link = invite.json()["link"]
    assert link.startswith("/invite/")
    async with browser(api) as alex:
        joined = await join(alex, link, "alex")
        assert joined.status_code == 200, joined.text
        state = (await alex.get("/api/auth/state")).json()
        assert state["username"] == "alex" and state["role"] == "member" and state["account"] == "dana-agent"
        assert (await alex.get("/api/users")).status_code == 403  # people are owner-only
        again = await alex.post("/api/auth/invite/begin", json={"token": link.rsplit("/", 1)[1]}, headers=ORIGIN)
        assert again.status_code == 410  # one-time
    listing = (await owner.get("/api/users")).json()
    assert [(u["username"], u["role"]) for u in listing["users"]] == [("casey", "owner"), ("alex", "member")]
    assert listing["invites"] == []
    bad = await owner.post("/api/users/invites", json={"role": "member", "account": "nobody"}, headers=csrf(owner))
    assert bad.status_code == 422


async def test_expired_links_and_a_reset_ends_old_sign_ins(api, env, member_accounts):
    await do_setup(api, env)
    owner = cookie_client(api)
    link = (await owner.post("/api/users/invites", json={"account": "dana-agent"}, headers=csrf(owner))).json()["link"]
    with write_db(api.app.state.db) as s:
        s.query(Invite).update({Invite.expires_at: auth.now() - timedelta(minutes=1)})
        s.commit()
    async with browser(api) as late:
        assert (await late.post("/api/auth/invite/begin", json={"token": link.rsplit("/", 1)[1]}, headers=ORIGIN)).status_code == 410
    link = (await owner.post("/api/users/invites", json={"account": "dana-agent"}, headers=csrf(owner))).json()["link"]
    async with browser(api) as first, browser(api) as second:
        assert (await join(first, link, "alex")).status_code == 200
        users = (await owner.get("/api/users")).json()["users"]
        alex = next(u for u in users if u["username"] == "alex")
        reset = (await verified_post(owner, f"/api/users/{alex['id']}/reset")).json()["link"]
        assert (await join(second, reset, None, password="a brand new password")).status_code == 200
        assert (await first.get("/api/sessions")).status_code == 401  # the old sign-in ended
        assert (await second.get("/api/auth/state")).json()["username"] == "alex"


async def test_sign_ins_can_be_listed_and_revoked_and_password_changed(api, env):
    await do_setup(api, env)
    owner = cookie_client(api)
    sessions = (await owner.get("/api/auth/sessions")).json()
    assert len(sessions) == 1 and sessions[0]["current"]
    assert (await owner.post(f"/api/auth/sessions/{sessions[0]['id']}/revoke", headers=csrf(owner))).status_code == 409
    wrong = await owner.post("/api/auth/password", json={"current_password": "nope", "new_password": "x" * 12}, headers=csrf(owner))
    assert wrong.status_code == 403
    changed = await owner.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "another long password"},
                               headers=csrf(owner))
    assert changed.status_code == 200
    assert (await owner.get("/api/sessions")).status_code == 200  # this sign-in stays


async def test_at_least_one_owner_and_no_self_removal(api, env, member_accounts):
    await do_setup(api, env)
    owner = cookie_client(api)
    me = (await owner.get("/api/users")).json()["users"][0]
    assert (await owner.post(f"/api/users/{me['id']}/role", json={"role": "member"}, headers=csrf(owner))).status_code == 409
    assert (await owner.post(f"/api/users/{me['id']}/remove", json={"resources": "archive"}, headers=csrf(owner))).status_code == 409
    link = (await owner.post("/api/users/invites", json={"account": "dana-agent"}, headers=csrf(owner))).json()["link"]
    async with browser(api) as alex:
        await join(alex, link, "alex")
        pid = next(u["id"] for u in (await owner.get("/api/users")).json()["users"] if u["username"] == "alex")
        assert (await owner.post(f"/api/users/{pid}/remove", json={"resources": "archive"}, headers=csrf(owner))).status_code == 200
        assert (await alex.get("/api/sessions")).status_code == 401
