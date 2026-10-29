"""What members can learn about the host and other people's things (prerelease review D11, D12 and the low
"Accounts and members" items)."""
from __future__ import annotations

import pytest
from helpers import TOTP_AT, write_db
from test_auth import do_setup
from test_users import browser, csrf, join

from acc.server.broker_client import BrokerError
from acc.server.models import Project


@pytest.fixture
async def member(api, env, member_accounts):
    await do_setup(api, env)
    link = (await api.post("/api/users/invites", json={"role": "member", "account": "dana-agent"})).json()["link"]
    client = browser(api)
    assert (await join(client, link, "dana")).status_code == 200
    yield client
    await client.aclose()


@pytest.mark.parametrize("background_poll", [False, True])
async def test_runtime_isnt_a_folder_probe_for_members(api, env, member, monkeypatch, background_poll):
    """GET runtime asks the services account, which can look anywhere: a member's project may name any folder."""
    # The fixture broker shares pytest's process, unlike the per-account service.
    # Simulate its identity response for this deliberately inaccessible host path;
    # never inspect /home from the test runner or weaken the host-access guard.
    broker = api.app.state.supervisor.broker
    original_identity_call = broker.call

    async def identity_call(method, account=None, **params):
        if method == "workspace_identity" and params.get("path") == "/home/someone-else/secret-stack":
            assert account == "dana-agent"
            return {"path": params["path"]}
        return await original_identity_call(method, account=account, **params)

    monkeypatch.setattr(broker, "call", identity_call)
    made = await member.post("/api/projects", headers=csrf(member), json={
        "slug": "probe", "name": "Probe", "workspace_path": "/home/someone-else/secret-stack"})
    assert made.status_code == 201, made.text
    pid = made.json()["id"]
    asked = []
    broker = api.app.state.supervisor.broker
    original_call = broker.call

    async def call(method, account=None, host=None, **params):
        # Observe folder probes, not unrelated supervisor polling on the shared client.
        if method.startswith("compose_") or params.get("path") == "/home/someone-else/secret-stack":
            asked.append((method, account, params.get("path")))
            raise BrokerError("compose ps failed: no such directory /home/someone-else/secret-stack")
        return await original_call(method, account=account, host=host, **params)
    monkeypatch.setattr(broker, "call", call)
    if background_poll:
        # Provider polling shares this broker with the request handler.
        await broker.call("installed_providers")
    got = (await member.get(f"/api/projects/{pid}/runtime")).json()
    assert got["containers"] == [] and got["not_started"] and asked == []
    with write_db(api.app.state.db) as s:
        s.get(Project, pid).compose_autostart = True  # an owner made the stack ACC's
        s.commit()
    got = (await member.get(f"/api/projects/{pid}/runtime")).json()
    assert asked and "secret-stack" not in got["note"] and "no such" not in got["note"]
    assert "no such directory" in (await api.get(f"/api/projects/{pid}/runtime")).json()["note"]  # owners: the detail


async def test_members_get_the_machines_health_not_host_wide_readings(api, env, member):
    import json
    import time
    env["settings"].hoststate_path.write_text(json.dumps({
        "collected_at": time.time(), "hostname": "box", "kernel": "6.8", "uptime_seconds": 60, "reboot_required": False,
        "disks": [{"mount": "/", "device": "sda", "total": 10, "used": 5, "free": 5, "percent": 50.0}], "smart": [],
        "gpu": [], "tailscale": {"state": "Running", "ips": [], "online": True},
        "services": {"units": {"accd": "active"}, "failed": []},
        "containers": [{"name": "owners-db-1", "image": "x", "state": "running", "status": "Up", "project": "owners"},
                       {"name": "mine-web-1", "image": "x", "state": "running", "status": "Up", "project": "mine"}],
        "accounts": [{"account": "agent", "role": "agents"}, {"account": "dana-agent", "role": "agents"},
                     {"account": "bob-agent", "role": "agents"}]}))
    made = await member.post("/api/projects", headers=csrf(member), json={
        "slug": "mine", "name": "Mine", "workspace_path": str(env["workspaces"] / "demo")})
    with write_db(api.app.state.db) as s:
        s.get(Project, made.json()["id"]).compose_autostart = True
        s.commit()
    seen = (await member.get("/api/host")).json()
    assert "services" not in seen["snapshot"] and "git_backup" not in seen
    assert [a["account"] for a in seen["snapshot"]["accounts"]] == ["dana-agent"]
    assert [c["name"] for c in seen["snapshot"]["containers"]] == ["mine-web-1"]
    assert seen["snapshot"]["disks"] and seen["live"]["cpu_count"]
    full = (await api.get("/api/host")).json()
    assert len(full["snapshot"]["accounts"]) == 3 and len(full["snapshot"]["containers"]) == 2 and "services" in full["snapshot"]


async def test_a_locked_account_still_lets_in_a_fully_correct_sign_in(api, env):
    """Anyone can fail ten times for a name (from two addresses, or an agent through Caddy): that mustn't lock its
    owner out. A wrong attempt while locked is still refused as throttled."""
    from test_auth import ORIGIN, PASSWORD, cookie_client

    from acc.server import auth
    secret = await do_setup(api, env)
    throttle = api.app.state.ctx.throttle
    for _ in range(10):
        throttle.fail("user:casey")  # from elsewhere
    client = cookie_client(api)
    client.cookies.clear()
    wrong = await client.post("/api/auth/login", headers=ORIGIN, json={"username": "casey", "password": "x", "code": "1"})
    assert wrong.status_code == 429
    right = await client.post("/api/auth/login", headers=ORIGIN, json={
        "username": "casey", "password": PASSWORD, "code": auth.totp(secret, TOTP_AT + 30)})
    assert right.status_code == 200, right.text


def test_ipv6_clients_are_counted_per_64():
    from acc.server.api.context import network_key
    assert network_key("2001:db8:1:2:aaaa::1") == network_key("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert network_key("2001:db8:1:3::1") != network_key("2001:db8:1:2::1")
    assert network_key("192.0.2.7") == "192.0.2.7" and network_key("::ffff:192.0.2.7") == "192.0.2.7"
    assert network_key("?") == "?"


async def test_members_dont_learn_which_machines_or_accounts_exist(api, env, member):
    from acc.server.models import Node
    with write_db(api.app.state.db) as s:
        s.add(Node(id="n1", name="lab-box", status="active", info={"accounts": ["agent", "bob-agent"]}))
        s.commit()
    offline = await member.get("/api/accounts", params={"machine": "lab-box"})
    unknown = await member.get("/api/accounts", params={"machine": "no-such-box"})
    assert offline.status_code == unknown.status_code == 404
    assert (await api.get("/api/accounts", params={"machine": "lab-box"})).status_code == 409  # owners: it's offline
    made = await member.post("/api/projects", headers=csrf(member), json={
        "slug": "far", "name": "Far", "host": "lab-box", "workspace_path": "/srv/x"})
    assert made.status_code == 422 and "bob-agent" not in made.text


def test_another_persons_pending_publish_request_isnt_named(api):
    from acc.server.publish import PublishRefused
    publisher = api.app.state.publisher
    first = publisher.request(action="publish", host="demo.example.com", port=8100, access="internal",
                              requested_by="alice", owner="alice")
    with pytest.raises(PublishRefused) as theirs:
        publisher.request(action="publish", host="demo.example.com", port=8101, access="internal",
                          requested_by="bob", owner="bob")
    assert first not in str(theirs.value) and "in progress" in str(theirs.value)
    with pytest.raises(PublishRefused) as own:
        publisher.request(action="publish", host="demo.example.com", port=8101, access="internal",
                          requested_by="alice", owner="alice")
    assert first in str(own.value)


async def test_publishing_addresses_are_owner_only(api, member):
    for method, path, body in [
        ('get', '/api/publish/addresses', None),
        ('post', '/api/publish/addresses/detect', {}),
        ('post', '/api/publish/addresses', {'address': '8.8.8.8', 'label': 'Edge', 'access': 'public'}),
        ('post', '/api/publish/addresses/missing/remove', {}),
        ('post', '/api/publish/addresses/missing/default', {}),
    ]:
        response = await member.request(method, path, headers=csrf(member), **({'json': body} if body is not None else {}))
        assert response.status_code == 403
    zones = (await member.get('/api/publish/zones')).json()
    assert 'public_ip' not in zones and 'internal_ip' not in zones
