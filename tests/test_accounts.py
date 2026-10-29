"""Per-account brokers: each agent account's sessions run through its own broker, in its own run folder."""
from __future__ import annotations

import asyncio
import dataclasses
import os
import subprocess
import uuid

import httpx
import pytest
from helpers import pause_supervisor, reconcile_passes, wait_for, write_db

from acc.broker.server import Broker, serve
from acc.server import broker_client
from acc.server.app import create_app
from acc.server.broker_client import BrokerClient, BrokerUnavailable
from acc.server.models import Project


@pytest.fixture
async def world(env, tmp_path, monkeypatch, supervisor_threads):
    # Both fictional accounts use brokers owned by the test process.
    real_uid = broker_client.uid_of
    monkeypatch.setattr(broker_client, "uid_of",
                        lambda account: os.getuid() if account in ("alpha", "beta") else real_uid(account))
    socket_dir = tmp_path / "brokers"
    socket_dir.mkdir()
    brokers, tasks = {}, {}
    for account in ("alpha", "beta"):
        (env["run_dir"] / account).mkdir()
        brokers[account] = dataclasses.replace(env["broker"], socket_path=socket_dir / f"{account}.sock",
                                               run_dir=env["run_dir"] / account,
                                               tmux_socket=f"acc-test-{account}-{uuid.uuid4().hex[:6]}")
        tasks[account] = asyncio.create_task(serve(brokers[account]))
    await wait_for(lambda: asyncio.sleep(0, result=all(b.socket_path.exists() for b in brokers.values())))
    for sub in ("requests", "results"):
        (tmp_path / "accounts" / sub).mkdir(parents=True)
    settings = dataclasses.replace(env["settings"], broker_socket_dir=socket_dir, agent_home=tmp_path / "home" / "alpha",
                                   accounts_dir=tmp_path / "accounts", accounts_timeout=10.0)
    app = create_app(settings)
    loop = asyncio.create_task(app.state.supervisor.run())
    token = settings.api_token()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://acc",
                                 headers={"Authorization": f"Bearer {token}"}) as api:
        api.app = app
        api.supervisor_task = loop
        api.supervisor_threads = supervisor_threads
        yield {"api": api, "brokers": brokers, "tasks": tasks, "app": app, "env": env}
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)
    await app.state.supervisor.broker.close()
    for task in tasks.values():
        task.cancel()
    await asyncio.gather(*tasks.values(), return_exceptions=True)
    # owned_tmux collects every broker socket after fixture shutdown.


async def make_project(world, slug: str, account: str) -> dict:
    api, env = world["api"], world["env"]
    (env["workspaces"] / slug).mkdir(exist_ok=True)
    project = (await api.post("/api/projects", json={"slug": slug, "name": slug, "workspace_path": str(env["workspaces"] / slug)})).json()
    with write_db(world["app"].state.db) as s:  # assigning accounts comes with multi-user; set it directly here
        s.get(Project, project["id"]).account = account
        s.commit()
    trusted = await world["api"].post(f"/api/projects/{project['id']}/trust")
    assert trusted.status_code == 200, trusted.text
    return project


async def state(api, acc_id):
    return (await api.get(f"/api/sessions/{acc_id}")).json()


async def test_sessions_run_through_their_accounts_broker(world):
    api = world["api"]
    a = (await api.post("/api/sessions", json={"project_id": (await make_project(world, "pa", "alpha"))["id"],
                                                "provider": "claude", "name": "a"})).json()
    b = (await api.post("/api/sessions", json={"project_id": (await make_project(world, "pb", "beta"))["id"],
                                                "provider": "claude", "name": "b"})).json()
    for session, account in ((a, "alpha"), (b, "beta")):
        assert session["run_as"] == account

    async def both_idle():
        return [(await state(api, s["id"]))["observed_state"] for s in (a, b)] == ["idle", "idle"]
    assert await wait_for(both_idle)
    run = world["env"]["run_dir"]
    assert (run / "alpha" / a["id"] / "events.jsonl").exists() and (run / "beta" / b["id"] / "events.jsonl").exists()
    for session, config in ((a, world["brokers"]["alpha"]), (b, world["brokers"]["beta"])):
        listed = subprocess.run(["tmux", "-L", config.tmux_socket, "ls", "-F", "#{session_name}"], capture_output=True, text=True).stdout
        assert session["tmux_name"] in listed  # each in its own account's tmux server

    # beta's broker goes away: its session is left alone (not resumed, not called dead), alpha's carries on
    await pause_supervisor(api)
    world["tasks"]["beta"].cancel()
    await api.app.state.supervisor.broker.get("beta").close()
    await asyncio.gather(world["tasks"]["beta"], return_exceptions=True)
    world["brokers"]["beta"].socket_path.unlink()
    await reconcile_passes(api)
    assert (await state(api, b["id"]))["observed_state"] == "idle"
    assert (await state(api, a["id"]))["observed_state"] == "idle"
    stopped = await api.post(f"/api/sessions/{a['id']}/stop")
    assert stopped.status_code == 200 and stopped.json()["observed_state"] == "stopped"


async def test_accounts_api_lists_accounts_and_scopes_logins(world):
    api = world["api"]
    listing = (await api.get("/api/accounts")).json()
    assert listing["account"] == "alpha" and listing["accounts"] == ["alpha", "beta"]
    assert (await api.get("/api/accounts", params={"account": "beta"})).json()["account"] == "beta"
    assert (await api.get("/api/accounts", params={"account": "gamma"})).status_code == 404
    assert (await api.get("/api/accounts/login/gamma.x")).status_code == 404


async def test_services_broker_only_runs_compose(env):
    broker = Broker(dataclasses.replace(env["broker"], role="services"))
    assert set(broker.methods) == {"ping", "compose_up", "compose_ps", "compose_digest"}
    refused = await broker.dispatch(b'{"id": 1, "method": "launch", "params": {}}')
    assert not refused["ok"] and "unknown method" in refused["error"]


async def test_a_socket_held_by_another_user_is_refused(broker, env):
    client = BrokerClient(env["broker"].socket_path, trusted_uids=frozenset({12345}))
    with pytest.raises(BrokerUnavailable, match="refusing"):
        await client.call("ping")


async def test_owner_invites_someone_new_and_acc_creates_their_agent_account(world, tmp_path):
    import json as _json
    accounts = tmp_path / "accounts"
    socket_dir = world["brokers"]["alpha"].socket_path.parent

    async def fake_root_helper():  # stands in for acc-accounts.path + acc-accounts
        while True:
            for path in (accounts / "requests").glob("*.json"):
                request = _json.loads(path.read_text())
                (socket_dir / f"{request['account']}.sock").touch()  # its broker socket appears
                (accounts / "results" / f"{request['id']}.json").write_text(_json.dumps({"ok": True}))
                path.unlink()
            await asyncio.sleep(0.05)
    task = asyncio.create_task(fake_root_helper())
    try:
        response = await world["api"].post("/api/users/invites", json={"role": "member", "account": "carol-agent",
                                                                       "create_account": True})
        assert response.status_code == 201, response.text
        assert "carol-agent" in (await world["api"].get("/api/users")).json()["accounts"]
        bad = await world["api"].post("/api/users/invites", json={"role": "member", "account": "carol", "create_account": True})
        assert bad.status_code == 422
    finally:
        task.cancel()
