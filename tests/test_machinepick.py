"""A session's selected machine keeps project/account authority and uses the existing broker checkout path."""
import asyncio
import base64
from pathlib import Path

import pytest
from helpers import pause_supervisor, wait_for, write_db
from sqlalchemy import select
from test_auth import live  # noqa: F401
from test_changes import repo
from test_multihost import StandInNode, _connected, enroll
from test_ownership import two  # noqa: F401
from test_schedules import person  # noqa: F401
from test_supervisor import state_is
from test_users import csrf

from acc.server.models import AuditLog, GroupMachineAccount, Node, Project, ScheduleRun, Session
from acc.server.orchestrator import project_briefs
from acc.server.schedules import Scheduler


@pytest.fixture
def planning(monkeypatch):
    monkeypatch.setenv("ACC_ORCHESTRATOR", "1")
    monkeypatch.setenv("ACC_ORCHESTRATOR_V2", "1")


@pytest.fixture
async def machine(api, env, live):  # noqa: F811
    key, node_id = await enroll(api)
    node = StandInNode(env["settings"].broker_socket, key, node_id)
    task = asyncio.create_task(node.run(live))
    await wait_for(lambda: _connected(api))
    try:
        yield node
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("with_helpers", [False, True])
async def test_machine_choice_requires_clone_consent_then_reuses_its_repository(api, project, env, machine, with_helpers):
    repo(env["workspaces"] / "demo")
    body = {"project_id": project["id"], "provider": "claude", "name": "Elsewhere", "machine": "pc"}
    if with_helpers:
        body.update(worktree=True, orchestrator={"agents": [{"agent": "claude", "profile": None}], "machines": ["pc"], "brief_version": 0})
    preview = await api.post("/api/sessions/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["machine"] == "pc"
    assert preview.json()["checkout"]["requires_clone"] is True
    assert not list(env["workspaces"].glob("team-repo-*"))
    refused = await api.post("/api/sessions", json=body)
    assert refused.status_code == 409 and "Clone on pc" in refused.json()["detail"]
    first = await api.post("/api/sessions", json={**body, "clone_on_machine": True})
    assert first.status_code == 201, first.text
    first = first.json()
    await wait_for(state_is(api, first["id"], "idle"))
    assert first["host"] == "pc" and first["worktree"]
    with api.app.state.db() as db:
        session = db.get(Session, first["id"])
        target_repo = session.team_repo
        assert target_repo and session.cwd != project["workspace_path"]
        event = db.scalar(select(AuditLog).where(AuditLog.action == "session.create", AuditLog.target == first["id"]))
        assert event.detail["machine"] == "pc" and event.detail["account"] == "agent"
    preview = (await api.post("/api/sessions/preview", json=body)).json()
    assert preview["checkout"]["requires_clone"] is False
    second = await api.post("/api/sessions", json={**body, "name": "Reuse"})
    assert second.status_code == 201, second.text
    with api.app.state.db() as db:
        assert db.get(Session, second.json()["id"]).team_repo == target_repo
        assert db.get(Project, project["id"]).host == "local"
    assert len(list(env["workspaces"].glob("team-repo-*"))) == 1


async def test_machine_choice_uses_an_existing_registered_checkout(api, project, env, machine):
    repo(env["workspaces"] / "demo")
    remote_path = env["workspaces"] / "existing"
    repo(remote_path)
    with write_db(api.app.state.db) as db:
        original = db.get(Project, project["id"])
        original.remote_url = "https://example.com/demo/app.git"
        db.add(Project(slug="existing", name="Existing", workspace_path=str(remote_path), host="pc", account="agent",
                       owner=original.owner, trusted=True, import_mode="managed", remote_url=original.remote_url))
        db.commit()
    body = {"project_id": project["id"], "provider": "claude", "name": "Reuse checkout", "machine": "pc"}
    preview = (await api.post("/api/sessions/preview", json=body)).json()
    assert preview["checkout"]["requires_clone"] is False
    launched = await api.post("/api/sessions", json=body)
    assert launched.status_code == 201, launched.text
    assert launched.json()["host"] == "pc"
    assert not list(env["workspaces"].glob("team-repo-*"))


async def test_machine_inventory_and_provider_queries_use_the_projects_account(api, project, machine, monkeypatch):
    broker = api.app.state.ctx.broker
    call = broker.call

    async def scoped(method, *, account=None, host=None, **params):
        if method == "installed_providers" and host == "pc":
            assert account == "agent"
            return {"claude": False, "codex": True}
        return await call(method, account=account, host=host, **params)

    monkeypatch.setattr(broker, "call", scoped)
    inventory = await api.get("/api/nodes", params={"launch": 1, "project_id": project["id"]})
    assert inventory.status_code == 200
    rows = {row["name"]: row for row in inventory.json()}
    assert rows["local"]["label"] == "This Rynhult (main)"
    assert rows["pc"]["connected"] and rows["pc"]["system"]
    assert {p["id"]: p["installed"] for p in rows["pc"]["providers"]}["claude"] is False
    providers = (await api.get("/api/providers/registry", params={"project_id": project["id"], "machine": "pc"})).json()
    assert {p["id"]: p["installed"] for p in providers["providers"]}["claude"] is False
    refused = await api.post("/api/sessions", json={
        "project_id": project["id"], "provider": "claude", "name": "Unavailable", "machine": "pc", "clone_on_machine": True})
    assert refused.status_code == 422 and "not installed" in refused.json()["detail"]


@pytest.mark.parametrize("route", ["/api/nodes?launch=1", "/api/providers/registry", "/api/models"])
async def test_machine_queries_cannot_probe_another_persons_project(two, route):  # noqa: F811
    response = await two["member"].get(route, params={"project_id": two["project"]["id"], "machine": "pc",
                                                     **({"launch": 1} if route.startswith("/api/nodes") else {})})
    assert response.status_code == 404


async def test_machine_choice_refuses_unreported_accounts_before_creating_a_checkout(api, project, env, machine):
    repo(env["workspaces"] / "demo")
    api.app.state.ctx.broker.hub.links["pc"].accounts = ["someone-else"]
    response = await api.post("/api/sessions", json={
        "project_id": project["id"], "provider": "claude", "name": "Wrong account", "machine": "pc", "clone_on_machine": True})
    assert response.status_code in (404, 422)
    assert not list(env["workspaces"].glob("team-repo-*"))


async def test_members_cannot_select_or_discover_another_persons_reserved_machine_account(two, env, machine):  # noqa: F811
    member = two["member"]
    username = (await member.get("/api/auth/state")).json()["username"]
    with write_db(two["owner"].app.state.db) as db:
        project = Project(slug="member", name="Member", workspace_path=str(env["workspaces"] / "demo"),
                          host="local", account="dana-agent", owner=username, trusted=True, import_mode="managed")
        db.add(project)
        db.commit()
        project_id = project.id
    link = two["owner"].app.state.ctx.broker.hub.links["pc"]
    link.accounts = ["agent", "dana-agent"]
    allowed = await member.get("/api/providers/registry", params={"project_id": project_id, "machine": "pc"})
    assert allowed.status_code == 200
    with write_db(two["owner"].app.state.db) as db:
        node = db.scalar(select(Node).where(Node.name == "pc"))
        db.add(GroupMachineAccount(id="1" * 32, node_id=node.id, account="dana-agent", person="another-person",
                                   approved_by="casey", status="approved"))
        db.commit()
    inventory = await member.get("/api/nodes", params={"launch": 1, "project_id": project_id})
    assert inventory.status_code == 200 and "pc" not in [item["name"] for item in inventory.json()]
    for route in ["/api/providers/registry", "/api/models"]:
        assert (await member.get(route, params={"project_id": project_id, "machine": "pc"})).status_code == 404
    denied = await member.post("/api/sessions", headers=csrf(member), json={
        "project_id": project_id, "provider": "claude", "name": "Denied", "machine": "pc", "clone_on_machine": True})
    assert denied.status_code == 404
    assert not list(env["workspaces"].glob("team-repo-*"))


async def test_several_agents_use_the_selected_machine_and_share_only_the_backing_repository(api, project, env, machine):
    repo(env["workspaces"] / "demo")
    response = await api.post("/api/multiruns", json={
        "project_id": project["id"], "name": "Compare", "initial_prompt": "Review the demo",
        "machine": "pc", "clone_on_machine": True, "runs": [{"provider": "claude"}, {"provider": "codex"}]})
    assert response.status_code == 201, response.text
    runs = response.json()["runs"]
    assert all(run["session"] and run["session"]["host"] == "pc" for run in runs), runs
    assert len({run["session"]["worktree"] for run in runs}) == 2
    with api.app.state.db() as db:
        assert len({db.get(Session, run["session"]["id"]).team_repo for run in runs}) == 1
    assert len(list(env["workspaces"].glob("team-repo-*"))) == 1


@pytest.mark.parametrize("outage", ["none", "selected", "source"])
async def test_schedules_launch_and_check_availability_on_the_selected_machine(api, project, env, machine, person, outage):  # noqa: F811
    repo(env["workspaces"] / "demo")
    await pause_supervisor(api)
    if outage == "source":
        local = env["workspaces"] / "available"
        repo(local)
        with write_db(api.app.state.db) as db:
            original = db.get(Project, project["id"])
            original.host, original.remote_url = "pc", "https://example.com/demo/app.git"
            db.add(Project(slug="available", name="Available", workspace_path=str(local), host="local",
                           account=person.account, owner=person.username, trusted=True, import_mode="managed",
                           remote_url=original.remote_url))
            db.commit()
    selected = "local" if outage == "source" else "pc"
    made = await api.post("/api/schedules", json={
        "launch": {"project_id": project["id"], "provider": "claude", "name": "Scheduled elsewhere",
                   "machine": selected, "clone_on_machine": True},
        "timing": {"kind": "daily", "time": "09:00"}, "timezone": "UTC", "enabled": False})
    assert made.status_code == 201, made.text
    schedule = made.json()
    assert schedule["launch"]["machine"] == selected
    if outage != "none":
        await machine.ws.close()
        links = api.app.state.ctx.broker.hub.links
        await wait_for(lambda: "pc" not in links or links["pc"].closed)
    run = (await api.post(f"/api/schedules/{schedule['id']}/run")).json()
    await Scheduler(api.app.state.ctx).dispatch(run["id"])
    with api.app.state.db() as db:
        result = db.get(ScheduleRun, run["id"])
        if outage == "selected":
            assert result.status == "skipped" and result.session_id is None
            assert "offline" in result.reason.lower()
        else:
            assert result.session_id, (result.status, result.reason)
            session = db.get(Session, result.session_id)
            assert session.host == selected and session.run_as == person.account


async def test_old_machine_preview_requests_an_update_without_creating_a_checkout(api, project, env, machine, monkeypatch):
    call = api.app.state.ctx.broker.call

    async def old_machine(method, *, account=None, host=None, **params):
        if method == "ping" and host == "pc":
            return {"roots": [str(env["workspaces"])]}
        return await call(method, account=account, host=host, **params)

    monkeypatch.setattr(api.app.state.ctx.broker, "call", old_machine)
    response = await api.post("/api/sessions/preview", json={
        "project_id": project["id"], "provider": "claude", "name": "Outdated", "machine": "pc"})
    assert response.status_code == 409 and "Update Rynhult" in response.json()["detail"]
    assert not list(env["workspaces"].glob("team-repo-*"))


@pytest.mark.usefixtures("planning")
async def test_remote_lead_materializes_brief_documents_on_its_selected_machine(api, project, env, machine, monkeypatch):
    repo(env["workspaces"] / "demo")
    documents = await api.post(f"/api/projects/{project['id']}/brief/documents", json={"files": [
        {"name": "scope.txt", "data": base64.b64encode(b"Room booking board").decode()}]})
    assert documents.status_code == 200, documents.text
    reference = documents.json()["documents"][0]["reference_id"]
    lead = await api.post("/api/sessions", json={
        "project_id": project["id"], "name": "Remote lead", "provider": "claude",
        "machine": "pc", "clone_on_machine": True, "worktree": True,
        "orchestrator": {"agents": [{"agent": "claude", "profile": None}], "machines": ["pc"], "brief_version": 0}})
    assert lead.status_code == 201, lead.text
    lead_id = lead.json()["id"]
    await wait_for(state_is(api, lead_id, "idle"))
    broker = api.app.state.ctx.broker
    call, locations = broker.call, []

    async def record_reference(method, *, account=None, host=None, **params):
        if method == "brief_reference":
            locations.append(host)
        return await call(method, account=account, host=host, **params)

    monkeypatch.setattr(broker, "call", record_reference)
    result = await project_briefs.TEAM_ACTIONS["brief_reference"](
        api.app.state.supervisor, lead_id, {"action": "brief_reference", "reference": reference})
    assert (Path(lead.json()["worktree"]) / result["path"]).read_bytes() == b"Room booking board"
    assert locations == ["pc"]
