"""Person-launched independent runs, compared without granting a team role."""
from pathlib import Path

import pytest
from helpers import wait_for, write_db
from sqlalchemy import select
from test_budgets import seed
from test_changes import repo
from test_member_views import member as member  # noqa: F401
from test_supervisor import state_is
from test_users import csrf
from test_worktrees import branches

from acc.server.broker_client import BrokerError
from acc.server.db import WRITE_INTENT
from acc.server.models import AuditLog, MultiRun, Observation, Project, Session


def task(project, runs=None):
    return {"project_id": project["id"], "name": "Fix Login", "initial_prompt": "Repair the demo login.",
            "runs": runs or [{"provider": "claude"}, {"provider": "codex"}, {"provider": "copilot"}]}


async def start(api, project, env, runs=None):
    repo(env["workspaces"] / "demo")
    response = await api.post("/api/multiruns", json=task(project, runs))
    assert response.status_code == 201, response.text
    return response.json()


async def test_one_request_launches_independent_sessions_and_named_worktrees(api, project, env):
    group = await start(api, project, env, [{"provider": "claude", "model": "sonnet"},
                                          {"provider": "claude", "model": "opus"}, {"provider": "codex"}])
    assert len(group["runs"]) == 3 and group["winner_session_id"] is None
    sessions = [run["session"] for run in group["runs"]]
    assert len({x["id"] for x in sessions}) == len({x["worktree"] for x in sessions}) == 3
    for session in sessions:
        assert session["multirun_id"] == group["id"] and session["team_id"] is None
        assert session["worktree_branch"].startswith(f"acc/fix-login-{session['provider']}-")
        assert session["worktree_branch"] in branches(env["workspaces"] / "demo")
        detail = (await api.get(f"/api/sessions/{session['id']}")).json()
        assert detail["launch_spec"]["inputs"]["initial_prompt"] == "Repair the demo login."
        await wait_for(state_is(api, session["id"], "idle"))
    assert {r["id"] for r in (await api.get("/api/sessions")).json()} >= {s["id"] for s in sessions}
    assert [g["id"] for g in (await api.get("/api/multiruns")).json()] == [group["id"]]
    got = (await api.get(f"/api/multiruns/{group['id']}")).json()
    assert len(got["runs"]) == 3
    with api.app.state.db() as s:
        audit = s.scalar(select(AuditLog).where(AuditLog.action == "multirun.start"))
        assert audit.target == group["id"] and "Repair" not in str(audit.detail)


async def test_budget_refusal_does_not_cancel_other_launches(api, project, env):
    seed(api, used=90)
    await api.put("/api/budgets", json={"rules": {
        "claude.seven_day": {"mode": "cap", "limit": 80, "action": "pause"}}})
    group = await start(api, project, env)
    refused, codex, copilot = group["runs"]
    assert refused["session"] is None and "budget pauses" in refused["error"]
    assert codex["session"]["provider"] == "codex" and copilot["session"]["provider"] == "copilot"
    assert len((await api.get("/api/sessions")).json()) == 2
    assert len(branches(env["workspaces"] / "demo")) == 3  # main and the two accepted runs


@pytest.mark.parametrize("runs,status", [
    ([{"provider": "claude"}], 422),
    ([{"provider": "claude"}] * 6, 422),
    ([{"provider": "claude"}, {"provider": "unknown"}], 422),
    ([{"provider": "claude"}, {"provider": "codex", "budget_ok": True}], 422),
    ([{"provider": "claude"}, {"provider": "codex", "profile": "missing"}], 422),
    ([{"provider": "claude"}, {"provider": "claude", "options": {"permission_mode": "bypassPermissions"}}], 422),
])
async def test_invalid_or_unconfirmed_choices_start_nothing(api, project, env, runs, status):
    repo(env["workspaces"] / "demo")
    response = await api.post("/api/multiruns", json=task(project, runs))
    assert response.status_code == status, response.text
    assert (await api.get("/api/sessions")).json() == []
    assert branches(env["workspaces"] / "demo") == ["main"]


async def test_dangerous_choice_is_explicit_and_audited(api, project, env):
    from acc.server import security_settings
    with api.app.state.db() as db:
        security_settings.save(db, {'kind': 'project', 'project_id': project['id']},
                               {'provider_prompts': False}, actor='casey', expected_revision=0)
        db.commit()
    # Supported scoped bypass mappings, each explicitly confirmed by the person.
    group = await start(api, project, env, [{'provider': 'claude', 'options': {'permission_mode': 'bypassPermissions'}, 'confirm_dangerous': True},
                                          {'provider': 'codex', 'options': {'sandbox': 'danger-full-access', 'approval': 'never'},
                                           'confirm_dangerous': True}])
    assert all(x["session"] for x in group["runs"])
    with api.app.state.db() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.action == "session.dangerous_confirmed"))


async def test_compare_uses_each_runs_baseline_and_refuses_unrelated_ids(api, project, env):
    group = await start(api, project, env)
    left, right, _ = [r["session"] for r in group["runs"]]
    for session in (left, right):
        await wait_for(state_is(api, session["id"], "idle"))
    (Path(left["worktree"]) / "app.py").write_text("one\nleft solution\n")
    (Path(right["worktree"]) / "other.txt").write_text("right solution\n")
    url = f"/api/multiruns/{group['id']}/compare"
    response = await api.get(url, params={"left": left["id"], "right": right["id"]})
    assert response.status_code == 200, response.text
    data = response.json()
    assert "+left solution" in data["left"]["diff"]
    assert data["right"]["list"][0]["path"] == "other.txt"
    assert data["left"]["revision"] and data["right"]["revision"]
    assert data["left"] == (await api.get(f"/api/sessions/{left['id']}/changes")).json()
    assert (await api.get(url, params={"left": left["id"], "right": left["id"]})).status_code == 422
    assert (await api.get(url, params={"left": left["id"], "right": "not-a-member"})).status_code == 404


async def test_keep_requires_confirmation_and_archive_keeps_branches_and_comparison(api, project, env):
    group = await start(api, project, env)
    winner, *others = [r["session"] for r in group["runs"]]
    for session in (winner, *others):
        await wait_for(state_is(api, session["id"], "idle"))
    url = f"/api/multiruns/{group['id']}/keep"
    body = {"session_id": winner["id"], "archive_others": True}
    assert (await api.post(url, json=body)).status_code == 409
    assert (await api.post(url, json={"session_id": "outsider"})).status_code == 404
    marked = await api.post(url, json={"session_id": winner["id"]})
    assert marked.json()["winner_session_id"] == winner["id"]
    for session in others:
        assert (await api.get(f"/api/sessions/{session['id']}")).json()["desired_state"] == "running"
    result = await api.post(url, json={**body, "confirm": True})
    assert result.status_code == 200, result.text
    assert result.json()["archive_errors"] == []
    for session in others:
        detail = (await api.get(f"/api/sessions/{session['id']}")).json()
        assert detail["desired_state"] == "archived"
        assert Path(detail["worktree"]).exists()
        assert detail["worktree_branch"] in branches(env["workspaces"] / "demo")
    assert (await api.get(f"/api/sessions/{winner['id']}")).json()["desired_state"] == "running"
    assert (await api.get(f"/api/multiruns/{group['id']}/compare",
                          params={"left": winner["id"], "right": others[0]["id"]})).status_code == 200
    with api.app.state.db() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.action == "multirun.keep"))


async def test_group_unknown_usage_is_not_zero_and_known_usage_has_provenance(api, project, env):
    group = await start(api, project, env)
    first = group["runs"][0]["session"]["id"]
    with api.app.state.db() as s:
        s.merge(Observation(key=f"claude.statusline.{first}", source="statusline", observed_at=1234,
                            value={"cost": {"total_cost_usd": 1.25}, "context_window": {"used_percentage": 20}}))
        s.commit()
    data = (await api.get(f"/api/multiruns/{group['id']}")).json()
    assert data["runs"][0]["usage"]["cost"]["total_cost_usd"] == 1.25
    assert data["runs"][0]["usage"]["source"] == "statusline"
    assert data["runs"][0]["usage"]["observed_at"] == 1234
    assert data["runs"][1]["usage"] is None


async def test_members_cannot_read_compare_or_keep_others_groups(api, project, env, member):  # noqa: F811
    group = await start(api, project, env)
    ids = [r["session"]["id"] for r in group["runs"]]
    assert (await member.get("/api/multiruns")).json() == []
    base = f"/api/multiruns/{group['id']}"
    assert (await member.get(base)).status_code == 404
    assert (await member.get(base + "/compare", params={"left": ids[0], "right": ids[1]})).status_code == 404
    assert (await member.post(base + "/keep", headers=csrf(member),
                              json={"session_id": ids[0]})).status_code == 404
    assert (await member.post("/api/multiruns", headers=csrf(member),
                              json=task(project))).status_code == 404


async def test_members_can_launch_and_keep_their_own_runs_but_not_transferred_sessions(api, project, env, member):  # noqa: F811
    repo(env["workspaces"] / "demo")
    with api.app.state.db() as s:
        s.get(Project, project["id"]).owner = "dana"
        s.commit()
    response = await member.post("/api/multiruns", headers=csrf(member), json=task(project))
    assert response.status_code == 201, response.text
    group = response.json()
    ids = [r["session"]["id"] for r in group["runs"]]
    assert (await api.get(f"/api/multiruns/{group['id']}")).status_code == 200  # owners: any
    assert (await member.get("/api/multiruns")).json()[0]["id"] == group["id"]
    assert (await member.post(f"/api/multiruns/{group['id']}/keep", headers=csrf(member),
                              json={"session_id": ids[0]})).status_code == 200
    with api.app.state.db() as s:
        s.get(Session, ids[1]).owner = "someone-else"
        s.commit()
    assert (await member.get("/api/multiruns")).json() == []
    assert (await member.get(f"/api/multiruns/{group['id']}")).status_code == 404
    assert (await member.get(f"/api/multiruns/{group['id']}/compare",
                             params={"left": ids[0], "right": ids[1]})).status_code == 404
    assert (await member.post(f"/api/multiruns/{group['id']}/keep", headers=csrf(member),
                              json={"session_id": ids[0], "archive_others": True, "confirm": True})).status_code == 404
    assert (await api.get(f"/api/sessions/{ids[1]}")).json()["desired_state"] == "running"


async def test_all_refused_attempts_remain_visible_without_creating_sessions(api, project, env):
    seed(api, used=90)
    await api.put("/api/budgets", json={"rules": {
        "claude.seven_day": {"mode": "cap", "limit": 80, "action": "pause"}}})
    group = await start(api, project, env, [{"provider": "claude"}, {"provider": "claude", "model": "opus"}])
    assert all(r["session"] is None and "budget pauses" in r["error"] for r in group["runs"])
    assert (await api.get("/api/multiruns")).json()[0]["id"] == group["id"]
    assert branches(env["workspaces"] / "demo") == ["main"]


async def test_compare_redacts_each_sessions_keys(api, project, env):
    canary = "multirun-artificial-secret-0123456789"
    key = (await api.post("/api/vault/keys", json={"name": "Demo", "variable": "DEMO_TOKEN", "value": canary})).json()
    assert "id" in key, key
    grant = await api.post(f"/api/vault/keys/{key['id']}/give", json={"project_id": project["id"]})
    assert grant.status_code in (200, 201), grant.text
    group = await start(api, project, env)
    left, right = [r["session"] for r in group["runs"][:2]]
    for session in (left, right):
        await wait_for(state_is(api, session["id"], "idle"))
        (Path(session["worktree"]) / "app.py").write_text(canary + "\n")
    response = await api.get(f"/api/multiruns/{group['id']}/compare",
                             params={"left": left["id"], "right": right["id"]})
    assert response.status_code == 200 and canary not in response.text
    assert "key: Demo" in response.json()["left"]["diff"] and "key: Demo" in response.json()["right"]["diff"]


async def test_ci_matches_each_branch_and_reports_snapshot_errors(api, project, env):
    group = await start(api, project, env)
    branch = group["runs"][0]["session"]["worktree_branch"]
    with write_db(api.app.state.db) as s:
        s.merge(Observation(key=f"github.{project['id']}", source="github", observed_at=1234, value={
            "runs": [{"branch": "main", "status": "completed", "conclusion": "failure"},
                     {"branch": branch, "status": "completed", "conclusion": "success"}]}))
        s.commit()
    data = (await api.get(f"/api/multiruns/{group['id']}")).json()
    assert data["runs"][0]["ci"]["run"]["conclusion"] == "success"
    assert data["runs"][1]["ci"]["run"] is None
    with write_db(api.app.state.db) as s:
        s.get(Observation, f"github.{project['id']}").value = {"error": "GitHub is unavailable"}
        s.commit()
    data = (await api.get(f"/api/multiruns/{group['id']}")).json()
    assert data["runs"][0]["ci"]["error"] == "GitHub is unavailable"


async def test_unconfirmed_stop_is_reported_without_preventing_other_archives(api, project, env, monkeypatch):
    group = await start(api, project, env)
    winner, offline, other = [r["session"] for r in group["runs"]]
    for session in (winner, offline, other):
        await wait_for(state_is(api, session["id"], "idle"))
    original = api.app.state.supervisor.broker.call

    async def call(method, **kwargs):
        if method == "kill" and kwargs["tmux_name"] == offline["tmux_name"]:
            raise BrokerError("machine disconnected")
        return await original(method, **kwargs)

    monkeypatch.setattr(api.app.state.supervisor.broker, "call", call)
    response = await api.post(f"/api/multiruns/{group['id']}/keep", json={
        "session_id": winner["id"], "archive_others": True, "confirm": True})
    assert response.status_code == 200, response.text
    assert response.json()["archive_errors"][0]["session_id"] == offline["id"]
    assert (await api.get(f"/api/sessions/{other['id']}")).json()["desired_state"] == "archived"
    with api.app.state.db() as s:
        assert s.get(Session, offline["id"]).multirun_stop_pending
    supervisor = api.app.state.supervisor
    monkeypatch.setattr(supervisor.broker, "call", original)
    token = WRITE_INTENT.set(False)
    try:
        await supervisor.reconcile()
    finally:
        WRITE_INTENT.reset(token)
    panes = await original("list")
    assert offline["tmux_name"] not in {p["tmux_name"] for p in panes}
    stopped = (await api.get(f"/api/sessions/{offline['id']}")).json()
    assert stopped["desired_state"] == "archived" and stopped["last_error"] is None
    with api.app.state.db() as s:
        assert not s.get(Session, offline["id"]).multirun_stop_pending


async def test_user_removal_transfers_multirun_ownership(api, project, env, member):  # noqa: F811
    group = await start(api, project, env)
    with api.app.state.db() as s:
        s.get(MultiRun, group["id"]).owner = "dana"
        for run in group["runs"]:
            s.get(Session, run["session"]["id"]).owner = "dana"
        s.commit()
    users = (await api.get("/api/users")).json()["users"]
    dana = next(u for u in users if u["username"] == "dana")
    removed = await api.post(f"/api/users/{dana['id']}/remove", json={"resources": "transfer"})
    assert removed.status_code == 200, removed.text
    assert (await api.get(f"/api/multiruns/{group['id']}")).json()["owner"] == "@api"


async def test_reconcile_does_not_recover_a_run_waiting_for_its_initial_launch(api, project, env, monkeypatch):
    supervisor = api.app.state.supervisor
    original = supervisor.broker.call

    async def call(method, **kwargs):
        if method == "trust_workspace":
            token = WRITE_INTENT.set(False)
            try:
                await supervisor.reconcile()
            finally:
                WRITE_INTENT.reset(token)
        return await original(method, **kwargs)

    monkeypatch.setattr(supervisor.broker, "call", call)
    group = await start(api, project, env)
    for run in group["runs"]:
        await wait_for(state_is(api, run["session"]["id"], "idle"))
    with api.app.state.db() as s:
        assert not list(s.scalars(select(AuditLog).where(AuditLog.action == "session.recovery_needed")))


async def test_archive_while_initial_launch_is_pending_never_starts_the_agent(api, project, env, monkeypatch):
    supervisor = api.app.state.supervisor
    original = supervisor.broker.call
    launches = []

    async def call(method, **kwargs):
        if method == "trust_workspace":
            with api.app.state.db() as s:
                acc_id = s.scalar(select(Session.id).where(Session.worktree == kwargs["path"]))
            await supervisor.stop(acc_id, "@api", archive=True)
        if method == "launch":
            launches.append(kwargs["acc_id"])
        return await original(method, **kwargs)

    monkeypatch.setattr(supervisor.broker, "call", call)
    group = await start(api, project, env)
    assert launches == []
    assert all(r["session"]["desired_state"] == "archived" for r in group["runs"])


async def test_multirun_confirmation_cannot_override_prompts_on(api, project, env):
    repo(env['workspaces'] / 'demo')
    response = await api.post('/api/multiruns', json=task(project, [
        {'provider': 'claude'}, {'provider': 'copilot', 'options': {'permissions': 'all'}, 'confirm_dangerous': True}]))
    assert response.status_code == 422, response.text
    assert 'Security requires provider prompts' in response.json()['detail']
    assert (await api.get('/api/sessions')).json() == []
    assert branches(env['workspaces'] / 'demo') == ['main']
