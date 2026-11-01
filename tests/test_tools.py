import asyncio

from helpers import write_db
from sqlalchemy import select

from acc.server.api.context import Principal
from acc.server.models import AuditLog, Session, User


async def seed(api, project):
    response = await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "Tools"})
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    with write_db(api.app.state.db) as db:
        db.add(User(username="alex", password_hash="unused", totp_secret="unused", role="member", account="alex-agent"))
        session = db.get(Session, sid)
        session.owner, session.run_as = "alex", "alex-agent"
        db.commit()
    return sid


def as_member(api, username="alex", account="alex-agent"):
    api.app.dependency_overrides[api.app.state.ctx.principal] = lambda: Principal(username, "member", account)


async def test_team_helper_tool_destination_and_approval_recheck(api, project, monkeypatch):
    sid = await seed(api, project)
    with write_db(api.app.state.db) as db:
        # Seed the helper’s frozen placement, the source of routing authority.
        db.get(Session, sid).placement.host = "build"
        db.commit()
    as_member(api)
    body = {"session_id": sid, "manager": "npm", "name": "prettier", "reason": "Format helper output"}
    response = await api.post("/api/tools/requests", json=body)
    assert response.status_code == 201, response.text
    rid = response.json()["id"]
    data = (await api.get("/api/tools")).json()
    assert data["requests"][0]["host"] == "build"
    service = api.app.state.supervisor.tools

    async def carry(request):
        return {"ok": True, "version": "3.6.2", "detail": "done"}

    monkeypatch.setattr(service, "_carry", carry)
    assert (await api.post(f"/api/tools/requests/{rid}/approve")).status_code == 200
    await asyncio.gather(*service.tasks)
    assert (await api.get("/api/tools")).json()["tools"][0]["host"] == "build"
    again = (await api.post("/api/tools/requests", json={**body, "name": "typescript"})).json()["id"]
    with write_db(api.app.state.db) as db:
        # Simulate a changed stored binding while approval is pending.
        db.get(Session, sid).placement.host = "other"
        db.commit()
    assert (await api.post(f"/api/tools/requests/{again}/approve")).status_code == 409


async def test_own_request_approve_result_remove_and_audit(api, project, monkeypatch):
    sid = await seed(api, project)
    as_member(api)
    r = await api.post("/api/tools/requests", json={"session_id": sid, "manager": "npm", "name": "prettier",
                                                  "reason": "Format output"})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    inbox = (await api.get("/api/attention")).json()
    item = next(i for i in inbox if i["kind"] == "tool_request")
    assert item["tool"]["name"] == "prettier" and item["tool"]["account"] == "alex-agent"
    assert (await api.get("/api/tools")).json()["tools"] == []
    commands = []

    async def carry(request):
        commands.append((request.manager, request.name, request.account, request.host, request.operation))
        return {"ok": True, "version": "3.6.2" if request.operation == "install" else None, "detail": "done"}

    service = api.app.state.supervisor.tools
    monkeypatch.setattr(service, "_carry", carry)
    assert (await api.post(f"/api/tools/requests/{rid}/approve")).status_code == 200
    await asyncio.gather(*service.tasks)
    data = (await api.get("/api/tools")).json()
    assert data["tools"][0]["version"] == "3.6.2"
    assert data["requests"][0]["status"] == "applied"
    assert (await api.post(f"/api/tools/requests/{rid}/approve")).status_code == 409
    removed = await api.post(f"/api/tools/{data['tools'][0]['id']}/remove")
    assert removed.status_code == 201
    assert (await api.get("/api/tools")).json()["tools"]
    assert (await api.post(f"/api/tools/requests/{removed.json()['id']}/approve")).status_code == 200
    await asyncio.gather(*service.tasks)
    assert (await api.get("/api/tools")).json()["tools"] == []
    assert commands == [("npm", "prettier", "alex-agent", "local", "install"),
                        ("npm", "prettier", "alex-agent", "local", "remove")]
    with api.app.state.db() as db:
        actions = list(db.scalars(select(AuditLog.action).where(AuditLog.action.like("tool.%"))))
        assert actions.count("tool.request") == 2 and actions.count("tool.approve") == 2 and actions.count("tool.result") == 2


async def test_permissions_reject_and_current_account_rechecked(api, project):
    sid = await seed(api, project)
    as_member(api)
    body = {"session_id": sid, "manager": "apt", "name": "ripgrep", "reason": "Search"}
    assert (await api.post("/api/tools/requests", json=body)).status_code == 403
    body["manager"] = "cargo"
    r = await api.post("/api/tools/requests", json=body)
    assert r.status_code == 201
    rid = r.json()["id"]
    as_member(api, "someone-else", "other-agent")
    assert (await api.post(f"/api/tools/requests/{rid}/approve")).status_code == 404
    assert (await api.post(f"/api/tools/requests/{rid}/reject")).status_code == 404
    assert (await api.get("/api/tools")).json()["requests"] == []
    as_member(api)
    assert (await api.post(f"/api/tools/requests/{rid}/reject")).status_code == 200
    assert (await api.get("/api/tools")).json()["requests"][0]["status"] == "rejected"
    again = (await api.post("/api/tools/requests", json=body)).json()["id"]
    with write_db(api.app.state.db) as db:
        db.scalar(select(User).where(User.username == "alex")).account = "different-agent"
        db.commit()
    assert (await api.post(f"/api/tools/requests/{again}/approve")).status_code == 404
    api.app.dependency_overrides.clear()
    assert (await api.post(f"/api/tools/requests/{again}/approve")).status_code == 409


async def test_owner_system_flow_and_failure_is_visible(api, project, monkeypatch):
    sid = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "System"})).json()["id"]
    service = getattr(api.app.state.supervisor, "tools", None)
    assert service is not None, "tools approval service is not wired"
    r = await api.post("/api/tools/requests", json={"session_id": sid, "manager": "apt", "name": "ripgrep",
                                                  "reason": "Search source"})
    assert r.status_code == 201
    rid = r.json()["id"]
    as_member(api)
    assert (await api.post(f"/api/tools/requests/{rid}/approve")).status_code == 404
    api.app.dependency_overrides.clear()

    async def fail(request):
        return {"ok": False, "detail": "registry unavailable"}
    monkeypatch.setattr(service, "_carry", fail)
    assert (await api.post(f"/api/tools/requests/{rid}/approve")).status_code == 200
    await asyncio.gather(*service.tasks)
    data = (await api.get("/api/tools")).json()
    assert data["requests"][0]["status"] == "failed" and data["tools"] == []
    assert any("registry unavailable" in (i["detail"] or "") for i in (await api.get("/api/attention")).json())


async def test_owner_scoped_exemption_policy_and_preview(api, project):
    # Historical explicit exemptions remain effective after retiring the writer.
    from acc.server.models import Setting
    with api.app.state.db() as db:
        db.merge(Setting(key=f"tools.project.{project['id']}", value={'required': False}))
        db.commit()
    value = await api.put("/api/tools/policy", json={"scope": "project", "target": str(project["id"]), "required": False})
    assert value.status_code == 403, value.text
    response = await api.post("/api/sessions/preview", json={"project_id": project["id"], "provider": "claude", "name": "Preview"})
    assert response.status_code == 200, response.text
    preview = response.json()
    import json
    assert json.loads(preview["files"]["guardrails.json"])["tool_approval"] is False
    assert json.loads(preview["files"]["guardrails.json"])["rules"]["secrets"] is True
    assert any("acc-request tool --manager" in text for text in preview["files"].values())


async def test_changed_account_between_decision_and_execution_fails_visibly(api, project):
    from acc.server.db import WRITE_INTENT
    sid = await seed(api, project)
    service = api.app.state.supervisor.tools
    rid = service.request_session(sid, manager="npm", name="prettier", reason="Format")
    token = WRITE_INTENT.set(True)
    try:
        service.decide(rid, "@api", True)
    finally:
        WRITE_INTENT.reset(token)
    with write_db(api.app.state.db) as db:
        db.scalar(select(User).where(User.username == "alex")).account = "different-agent"
        db.commit()
    await asyncio.gather(*service.tasks)
    row = next(r for r in (await api.get("/api/tools")).json()["requests"] if r["id"] == rid)
    assert row["status"] == "failed" and "account changed" in row["detail"]


async def test_system_execution_uses_helper_request_file_and_resolved_result(api, project):
    import json

    from acc.server.models import ToolRequest
    sid = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "System"})).json()["id"]
    service = api.app.state.supervisor.tools
    rid = service.request_session(sid, manager="apt", name="ripgrep", version="14.1.0-1", reason="Search")
    root = service.settings.packages_dir
    (root / "requests").mkdir(parents=True)
    (root / "results").mkdir()
    with api.app.state.db() as db:
        r = db.get(ToolRequest, rid)
        db.expunge(r)
    task = asyncio.create_task(service._carry(r))
    await asyncio.sleep(0)
    assert json.loads((root / "requests" / f"{rid}.json").read_text()) == {
        "id": rid, "action": "tool", "manager": "apt", "name": "ripgrep", "version": "14.1.0-1", "operation": "install"}
    (root / "results" / f"{rid}.json").write_text(json.dumps({"ok": True, "version": "14.1.0-1", "detail": "installed"}))
    assert (await task)["version"] == "14.1.0-1"


async def test_restart_records_interrupted_change_without_replaying(api, project):
    from acc.server.models import ToolRequest
    from acc.server.tools import Tools
    sid = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "Interrupted"})).json()["id"]
    service = api.app.state.supervisor.tools
    rid = service.request_session(sid, manager="npm", name="prettier", reason="Format")
    with write_db(api.app.state.db) as db:
        db.get(ToolRequest, rid).status = "applying"
        db.commit()
    restarted = Tools(service.settings, service.db, service.brokers)
    assert not restarted.tasks
    row = next(r for r in (await api.get("/api/tools")).json()["requests"] if r["id"] == rid)
    assert row["status"] == "failed" and "restarted" in row["detail"]


async def test_system_change_is_deduplicated_across_accounts(api, project):
    import pytest

    from acc.server.tools import ToolRefused

    sid = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "System"})).json()["id"]
    service = api.app.state.supervisor.tools
    service.request_session(sid, manager="apt", name="ripgrep", reason="Search")
    with write_db(api.app.state.db) as db:
        db.add(User(username="blair", password_hash="unused", totp_secret="unused", role="owner", account="blair-agent"))
        session = db.get(Session, sid)
        session.owner, session.run_as = "blair", "blair-agent"
        db.commit()
    with pytest.raises(ToolRefused, match="already"):
        service.request_session(sid, manager="apt", name="ripgrep", reason="Search from another account")


async def test_owner_can_remove_system_tool_after_original_requester_is_demoted(api, project, monkeypatch):
    sid = await seed(api, project)
    with write_db(api.app.state.db) as db:
        db.scalar(select(User).where(User.username == "alex")).role = "owner"
        db.commit()
    service = api.app.state.supervisor.tools

    async def carry(request):
        return {"ok": True, "version": "14.1.0-1", "detail": "done"}

    monkeypatch.setattr(service, "_carry", carry)
    rid = service.request_session(sid, manager="apt", name="ripgrep", reason="Search")
    service.decide(rid, "@api", True)
    await asyncio.gather(*service.tasks)
    with write_db(api.app.state.db) as db:
        db.scalar(select(User).where(User.username == "alex")).role = "member"
        db.commit()
    tool = (await api.get("/api/tools")).json()["tools"][0]
    response = await api.post(f"/api/tools/{tool['id']}/remove")
    assert response.status_code == 201, response.text
    with api.app.state.db() as db:
        request = db.scalar(select(AuditLog).where(AuditLog.action == "tool.request",
                                                  AuditLog.target == response.json()["id"]))
        assert request.actor == "@api"
