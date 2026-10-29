"""App login, CSRF/Origin protections and the websocket terminal (M3)."""
from __future__ import annotations

import asyncio
import json
import socket

import pytest
import uvicorn
import websockets
from helpers import TOTP_AT, verified_post, wait_for, write_db

from acc.server import auth

ORIGIN = {"Origin": "http://acc"}
PASSWORD = "correct horse battery staple"


async def do_setup(api, env):
    setup_token = env["settings"].setup_token_path.read_text().strip()
    anon = {"Authorization": ""}
    begin = await api.post("/api/auth/setup/begin", json={"setup_token": setup_token}, headers={**ORIGIN, **anon})
    assert begin.status_code == 200, begin.text
    secret = begin.json()["totp_secret"]
    assert "<svg" in begin.json()["qr_svg"]
    done = await api.post("/api/auth/setup/complete", headers={**ORIGIN, **anon}, json={
        "setup_token": setup_token, "username": "casey", "password": PASSWORD, "code": auth.totp(secret, TOTP_AT)})
    assert done.status_code == 200, done.text
    return secret


def cookie_client(api):
    """Same app, no bearer token: browser-style cookie auth only."""
    api.headers.pop("Authorization", None)
    return api


async def test_totp_matches_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32("12345678901234567890"), RFC 6238 test key  gitleaks:allow
    assert auth.totp(secret, 59, digits=8) == "94287082"
    assert auth.totp(secret, 1111111109, digits=8) == "07081804"


@pytest.mark.parametrize("elapsed_steps", [0, 1], ids=["same-step", "rollover"])
async def test_setup_then_login_flow(api, env, monkeypatch, elapsed_steps):
    state = (await api.get("/api/auth/state")).json()
    assert state["setup_required"] is True
    secret = await do_setup(api, env)
    monkeypatch.setattr(auth.time, "time", lambda: TOTP_AT + elapsed_steps * 30)
    assert not env["settings"].setup_token_path.exists()  # one-time token consumed
    client = cookie_client(api)
    assert (await client.get("/api/auth/state")).json()["authenticated"] is True
    assert (await client.get("/api/sessions")).status_code == 200

    # login from scratch; the setup code's time step cannot be replayed
    client.cookies.clear()
    assert (await client.get("/api/sessions")).status_code == 401
    replay = await client.post("/api/auth/login", headers=ORIGIN, json={
        "username": "casey", "password": PASSWORD, "code": auth.totp(secret, TOTP_AT)})
    assert replay.status_code == 401
    fresh = await client.post("/api/auth/login", headers=ORIGIN, json={
        "username": "casey", "password": PASSWORD, "code": auth.totp(secret, TOTP_AT + 30)})
    assert fresh.status_code == 200, fresh.text
    assert (await client.get("/api/sessions")).status_code == 200


async def test_setup_rejects_wrong_token_and_runs_once(api, env):
    bad = await api.post("/api/auth/setup/begin", json={"setup_token": "nope"}, headers=ORIGIN)
    assert bad.status_code == 403
    await do_setup(api, env)
    again = await api.post("/api/auth/setup/begin", json={"setup_token": "anything"}, headers=ORIGIN)
    assert again.status_code == 409


async def test_cookie_mutations_need_csrf_and_origin(api, env, project):
    await do_setup(api, env)
    client = cookie_client(api)
    csrf = client.cookies.get("acc_csrf")
    body = {"slug": "x", "name": "X", "workspace_path": str(env["workspaces"] / "x")}
    assert (await client.post("/api/projects", json=body, headers=ORIGIN)).status_code == 403  # no csrf
    assert (await client.post("/api/projects", json=body,
                              headers={"Origin": "https://evil.example", "X-ACC-CSRF": csrf})).status_code == 403
    ok = await client.post("/api/projects", json=body, headers={**ORIGIN, "X-ACC-CSRF": csrf})
    assert ok.status_code == 201, ok.text


async def test_login_is_throttled(api, env):
    await do_setup(api, env)
    client = cookie_client(api)
    client.cookies.clear()
    for _ in range(5):
        r = await client.post("/api/auth/login", headers=ORIGIN, json={"username": "casey", "password": "x", "code": "000000"})
        assert r.status_code == 401
    r = await client.post("/api/auth/login", headers=ORIGIN, json={"username": "casey", "password": PASSWORD, "code": "000000"})
    assert r.status_code == 429


@pytest.fixture
async def live(api):
    """Serve the same app on a real port for websocket tests."""
    sock = socket.socket()  # a free port: other copies of the suite (worktrees) may be running at the same time
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await wait_for(lambda: asyncio.sleep(0, result=server.started))
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        sock.close()


async def test_terminal_websocket(api, env, project, live):
    await do_setup(api, env)
    session = (await api.post("/api/sessions", headers={"X-ACC-CSRF": api.cookies.get("acc_csrf"), **ORIGIN},
                              json={"project_id": project["id"], "provider": "claude", "name": "tty"})).json()

    async def idle():
        return (await api.get(f"/api/sessions/{session['id']}")).json()["observed_state"] == "idle"
    await wait_for(idle)
    url = f"{live}/api/sessions/{session['id']}/tty"
    cookie = {"Cookie": f"acc_session={api.cookies.get('acc_session')}"}
    good_origin = {"Origin": "http://127.0.0.1:18740"}

    for headers in ({**good_origin}, {**cookie, "Origin": "https://evil.example"}, {**cookie}):
        # closing before accept() rejects the handshake itself: HTTP 403, no socket ever opens
        with pytest.raises(websockets.InvalidStatus) as rejected:
            async with websockets.connect(url, additional_headers=headers):
                pass
        assert rejected.value.response.status_code == 403

    async with websockets.connect(url, additional_headers={**cookie, **good_origin}) as writer_ws:
        mode = json.loads(await asyncio.wait_for(writer_ws.recv(), 5))
        assert mode == {"t": "mode", "write": True}
        output = await asyncio.wait_for(writer_ws.recv(), 5)
        assert isinstance(output, bytes) and output

        async with websockets.connect(url, additional_headers={**cookie, **good_origin}) as viewer:
            assert json.loads(await asyncio.wait_for(viewer.recv(), 5)) == {"t": "mode", "write": False}

        async with websockets.connect(url + "?take=1", additional_headers={**cookie, **good_origin}) as taker:
            assert json.loads(await asyncio.wait_for(taker.recv(), 5)) == {"t": "mode", "write": True}
            with pytest.raises(websockets.ConnectionClosed) as closed:
                while True:
                    await asyncio.wait_for(writer_ws.recv(), 5)
            assert closed.value.rcvd.code == 4001

    # the session survives every browser coming and going
    assert (await api.get(f"/api/sessions/{session['id']}")).json()["observed_state"] == "idle"


async def test_forwarded_header_does_not_dodge_the_throttle_and_parallel_guesses_count(api, env):
    await do_setup(api, env)
    client = cookie_client(api)
    client.cookies.clear()
    statuses = []
    for n in range(7):  # a local process claiming a new address each time (over TCP the header is ignored)
        r = await client.post("/api/auth/login", headers={**ORIGIN, "X-Forwarded-For": f"10.9.9.{n}"},
                              json={"username": "casey", "password": "x", "code": "000000"})
        statuses.append(r.status_code)
    assert statuses[:5] == [401] * 5 and statuses[5:] == [429, 429]


    burst = await asyncio.gather(*[client.post("/api/auth/login", headers=ORIGIN, json={"username": f"nobody{n % 2}", "password": "x", "code": "1"})
                                   for n in range(20)])
    assert all(r.status_code == 429 for r in burst)  # this address is already over its limit


async def test_account_limit_holds_across_addresses(api, env):
    from acc.server.auth import LoginThrottle
    throttle = LoginThrottle(limit=5, account_limit=10)
    for n in range(10):
        assert not throttle.blocked("user:casey")
        throttle.fail("user:casey")
        throttle.fail(f"ip:10.0.0.{n}")
    assert throttle.blocked("user:casey") and not throttle.blocked("ip:10.0.0.11")


async def test_non_ascii_credentials_are_refused_not_crashing(api, env, project):
    await do_setup(api, env)
    client = cookie_client(api)
    bad_csrf = await client.post("/api/projects", json={"slug": "y", "name": "Y", "workspace_path": "/x"},
                                 headers={**ORIGIN, "X-ACC-CSRF": "é".encode()})
    assert bad_csrf.status_code == 403
    bad_bearer = await client.get("/api/sessions", headers={"Authorization": "Bearer é".encode()})
    assert bad_bearer.status_code == 401


async def test_logout_closes_open_terminals(api, env, project, live):
    await do_setup(api, env)
    session = (await api.post("/api/sessions", headers={"X-ACC-CSRF": api.cookies.get("acc_csrf"), **ORIGIN},
                              json={"project_id": project["id"], "provider": "claude", "name": "bye"})).json()

    async def idle():
        return (await api.get(f"/api/sessions/{session['id']}")).json()["observed_state"] == "idle"
    await wait_for(idle)
    url = f"{live}/api/sessions/{session['id']}/tty"
    headers = {"Cookie": f"acc_session={api.cookies.get('acc_session')}", "Origin": "http://127.0.0.1:18740"}
    async with websockets.connect(url, additional_headers=headers) as ws:
        assert json.loads(await asyncio.wait_for(ws.recv(), 5))["t"] == "mode"
        out = await api.post("/api/auth/logout", headers={"X-ACC-CSRF": api.cookies.get("acc_csrf"), **ORIGIN})
        assert out.status_code == 200
        with pytest.raises(websockets.ConnectionClosed) as closed:
            while True:
                await asyncio.wait_for(ws.recv(), 5)
        assert closed.value.rcvd.code == 4401


@pytest.mark.parametrize("change", ["demotion", "role_recheck", "ownership_recheck"])
@pytest.mark.parametrize("mode", ["read", "write"])
async def test_terminal_reauthorizes_role_and_current_ownership(
        api, env, project, live, change, mode, member_accounts, monkeypatch):
    from sqlalchemy import select
    from test_users import browser, csrf, join

    from acc.server.models import Session, User

    owned = False
    checked_owned = asyncio.Event()
    if change == "ownership_recheck":
        from acc.server.api import terminal
        real_bridge = terminal.bridge

        async def observe_checks(*args, signed_in, **kwargs):
            async def checked():
                allowed = await signed_in()
                if owned and allowed:
                    checked_owned.set()
                return allowed
            return await real_bridge(*args, signed_in=checked, **kwargs)
        monkeypatch.setattr(terminal, "bridge", observe_checks)

    await do_setup(api, env)
    owner = cookie_client(api)
    # their own agent account, so a demotion to member is allowed (members never run as the owners' account)
    link = (await verified_post(owner, "/api/users/invites", {"role": "owner", "account": "dana-agent"})).json()["link"]
    async with browser(api) as other:
        await join(other, link, "alex")
        session = (await owner.post("/api/sessions", headers=csrf(owner),
                                   json={"project_id": project["id"], "provider": "claude", "name": "reauth"})).json()
        await wait_for(lambda: _idle(owner, session["id"]))
        ctx = api.app.state.ctx
        ctx.terminal_recheck = 0.05 if change != "demotion" else 3600
        headers = {"Cookie": f"acc_session={other.cookies.get('acc_session')}", **ORIGIN}
        async with websockets.connect(f"{live}/api/sessions/{session['id']}/tty?mode={mode}", additional_headers=headers) as ws:
            assert json.loads(await asyncio.wait_for(ws.recv(), 5))["write"] is (mode == "write")
            with write_db(api.app.state.db) as s:
                user = s.scalar(select(User).where(User.username == "alex"))
                uid = user.id
                if change == "ownership_recheck":
                    s.get(Session, session["id"]).owner = "alex"
                if change != "demotion":
                    user.role = "member"
                s.commit()
            if change == "demotion":
                response = await owner.post(f"/api/users/{uid}/role", json={"role": "member"}, headers=csrf(owner))
                assert response.status_code == 200
            elif change == "ownership_recheck":
                # The member initially owns it; transferring it must revoke the existing connection.
                owned = True
                await asyncio.wait_for(checked_owned.wait(), 5)
                with write_db(api.app.state.db) as s:
                    s.get(Session, session["id"]).owner = "another-user"
                    s.commit()
            with pytest.raises(websockets.ConnectionClosed) as closed:
                while True:
                    await asyncio.wait_for(ws.recv(), 2)
            assert closed.value.rcvd.code in (4401, 4403)
            assert (await other.get("/api/auth/state")).json()["authenticated"]


async def _idle(client, session_id):
    return (await client.get(f"/api/sessions/{session_id}")).json()["observed_state"] == "idle"


async def test_browser_leaving_during_broker_connect_releases_control_without_server_error(
        api, env, project, live, monkeypatch, caplog):
    await do_setup(api, env)
    session = (await api.post("/api/sessions", headers={"X-ACC-CSRF": api.cookies.get("acc_csrf"), **ORIGIN},
                              json={"project_id": project["id"], "provider": "claude", "name": "early-close"})).json()
    await wait_for(lambda: _idle(api, session["id"]))
    ctx = api.app.state.ctx
    gate = asyncio.Event()
    original = ctx.broker.open_stream

    async def delayed(account, host):
        await gate.wait()
        return await original(account, host)

    monkeypatch.setattr(ctx.broker, "open_stream", delayed)
    headers = {"Cookie": f"acc_session={api.cookies.get('acc_session')}", **ORIGIN}
    async with websockets.connect(f"{live}/api/sessions/{session['id']}/tty", additional_headers=headers):
        pass  # close after accept, before attach has finished
    gate.set()

    async def released():
        return not ctx.terminals and session["id"] not in ctx.writers.by_session

    await wait_for(released)
    assert not [r for r in caplog.records if "Exception in ASGI application" in r.message]


async def test_cookie_gets_survive_last_seen_autoflush_under_writer(api, env):
    from datetime import timedelta

    from sqlalchemy import text

    from acc.server.models import AuthSession

    await do_setup(api, env)
    client = cookie_client(api)
    api.supervisor_task.cancel()
    await asyncio.gather(api.supervisor_task, return_exceptions=True)
    await asyncio.gather(*api.supervisor_threads, return_exceptions=True)
    with write_db(api.app.state.db) as s:
        record = s.query(AuthSession).one()
        record.last_seen_at = auth.now() - timedelta(minutes=10)
        s.commit()
    with api.app.state.db() as writer:
        writer.execute(text("UPDATE attention_item SET title = title"))
        state = await client.get("/api/auth/state")
        assert state.status_code == 200
        assert state.json()["authenticated"] is True
        assert state.json()["role"] == "owner"
        assert (await client.get("/api/host")).status_code == 200
        writer.rollback()


@pytest.mark.parametrize('header', [None, 'stale'])
@pytest.mark.parametrize('origin', ['http://acc', None, 'https://evil.example', 'null'])
async def test_logout_requires_same_origin_but_not_a_csrf_header(api, env, header, origin):
    await do_setup(api, env)
    client = cookie_client(api)
    headers = {}
    if origin is not None:
        headers['Origin'] = origin
    if header is not None:
        headers['X-ACC-CSRF'] = header
    response = await client.post('/api/auth/logout', headers=headers)
    assert response.status_code == (200 if origin == 'http://acc' else 403), response.text
    state = (await client.get('/api/auth/state')).json()
    assert state['authenticated'] is (origin != 'http://acc')


async def test_csrf_token_endpoint_returns_current_session_without_caching(api, env):
    assert (await api.get('/api/auth/csrf', headers={'Authorization': ''})).status_code == 401
    assert (await api.get('/api/auth/csrf')).status_code == 401  # bearer authority is not a browser session
    await do_setup(api, env)
    client = cookie_client(api)
    expected = client.cookies.get('acc_csrf')
    client.cookies.set('acc_csrf', 'stale', domain='acc.local', path='/')
    response = await client.get('/api/auth/csrf')
    assert response.status_code == 200, response.text
    assert response.json() == {'csrf': expected}
    assert response.headers['cache-control'] == 'no-store'
    assert 'acc_csrf=' + expected in response.headers['set-cookie']


def test_https_login_cookies_expire_the_legacy_plain_csrf(env):
    from dataclasses import replace

    from fastapi import Response

    from acc.server.api.context import Context
    ctx = object.__new__(Context)
    ctx.settings = replace(env['settings'], cookie_secure=True)
    response = Response()
    ctx.set_login_cookies(response, 'session', 'fresh')
    cookies = response.headers.getlist('set-cookie')
    assert any(c.startswith('__Host-acc_csrf=fresh;') for c in cookies)
    for name in ('acc_session', 'acc_csrf'):
        assert any(c.startswith(name+'="";') and 'Max-Age=0' in c and 'Secure' in c for c in cookies)
    assert all(c.startswith('__Host-') or 'Max-Age=0' in c for c in cookies)
