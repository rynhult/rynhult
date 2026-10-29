"""Attention inbox rules and provider login health / re-login (M4)."""
from __future__ import annotations

import time

import pytest
from helpers import control, wait_for, write_db
from test_supervisor import get, launch, state_is

from acc.server import attention
from acc.server.models import AttentionItem


@pytest.mark.parametrize("key,kind", [
    ("machine_offline:demo-cloud", "machine_offline"),
    ("backup:failed:demo", "backup"),
    ("backup:stale:demo", "backup"),
])
async def test_operational_alert_ack_does_not_hide_snooze_expires_and_resolution_clears(api, key, kind):
    from datetime import timedelta

    from helpers import pause_supervisor

    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        item = attention.raise_item(db, key, kind=kind, title="Operational alert", source="test")
        db.commit()
        item_id = item.id
    assert (await api.post(f"/api/attention/{item_id}/ack")).status_code == 200
    item = next((i for i in await open_items(api) if i["id"] == item_id), None)
    assert item is not None, "Acknowledgement must not conceal an unresolved operational alert"
    assert item["persistent"] and item["acked_at"]
    response = await api.post(f"/api/attention/{item_id}/snooze", json={"minutes": 60})
    assert response.status_code == 200, response.text
    assert not any(i["id"] == item_id for i in await open_items(api))
    with write_db(api.app.state.db) as db:
        db.get(AttentionItem, item_id).snoozed_until = attention.now() - timedelta(seconds=1)
        db.commit()
    assert any(i["id"] == item_id for i in await open_items(api))
    with write_db(api.app.state.db) as db:
        attention.resolve(db, key)
        db.commit()
    assert not any(i["id"] == item_id for i in await open_items(api))


@pytest.mark.parametrize("key", ["test:telemetry", "machineXoffline:demo"])
async def test_ordinary_notice_dismissal_and_snooze_validation(api, key):
    from helpers import pause_supervisor

    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        item = attention.raise_item(db, key, kind="telemetry_consent",
                                    title="Optional usage statistics and bug reports are available", source="test",
                                    severity="info")
        db.commit()
        item_id = item.id
    assert (await api.post(f"/api/attention/{item_id}/snooze", json={"minutes": 60})).status_code == 400
    assert (await api.post(f"/api/attention/{item_id}/ack")).status_code == 200
    assert not any(i["id"] == item_id for i in await open_items(api))
    assert (await api.post("/api/attention/999999/snooze", json={"minutes": 60})).status_code == 404
    assert (await api.post(f"/api/attention/{item_id}/snooze", json={"minutes": -1})).status_code == 422


async def test_persistent_alert_is_not_displaced_by_newer_notices(api):
    from datetime import timedelta

    from helpers import pause_supervisor

    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        item = attention.raise_item(db, "backup:failed:demo", kind="backup", title="Old backup failure", source="test")
        item.created_at = attention.now() - timedelta(days=1)
        for i in range(3):
            attention.raise_item(db, f"test:note:{i}", kind="host", title="New notice", source="test")
        db.commit()
        item_id = item.id
    assert [item["id"] for item in (await api.get("/api/attention?limit=1")).json()] == [item_id]


async def test_machine_alert_snooze_survives_presence_evaluation_and_can_end_early(api):
    from datetime import timedelta

    from helpers import pause_supervisor

    from acc.server.machine_presence import evaluate
    from acc.server.models import Node

    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        node = Node(id="test-cloud", name="demo-cloud", status="active", role="always_on",
                    offline_since=attention.now() - timedelta(hours=1))
        db.add(node)
        db.flush()
        evaluate(db, node)
        db.commit()
    item_id = next(item["id"] for item in await open_items(api) if item["kind"] == "machine_offline")
    assert (await api.post(f"/api/attention/{item_id}/snooze", json={"minutes": 60})).status_code == 200
    with write_db(api.app.state.db) as db:
        node = db.get(Node, "test-cloud")
        evaluate(db, node)
        assert node.snoozed_until == db.get(AttentionItem, item_id).snoozed_until
        db.commit()
    assert not any(item["id"] == item_id for item in await open_items(api))
    assert (await api.post(f"/api/attention/{item_id}/snooze", json={"minutes": 0})).status_code == 200
    assert any(item["id"] == item_id for item in await open_items(api))
    with api.app.state.db() as db:
        assert db.get(Node, "test-cloud").snoozed_until is None


async def test_recurrent_backup_failure_does_not_inherit_a_previous_outage_snooze(api, env):
    from helpers import pause_supervisor

    from acc.backup_server import canonical, private_write
    from acc.server.backup_jobs import BackupJobs

    await pause_supervisor(api)
    jobs = BackupJobs(env["settings"], api.app.state.db)
    jobs.service.initialize()
    target = "a" * 32
    private_write(jobs.service.root / "targets.json", canonical([
        {"id": target, "kind": "local", "repository": "/mounted/example", "enabled": True, "created_at": time.time()}]))
    jobs.remind()
    item_id = next(item["id"] for item in await open_items(api) if item["persistent"] == "backup_stale")
    assert (await api.post(f"/api/attention/{item_id}/snooze", json={"minutes": 60})).status_code == 200
    result = jobs.service.root / f"result-{target}.json"
    private_write(result, canonical({"captured_at": time.time()}))
    jobs.remind()
    result.unlink()
    jobs.remind()
    assert any(item["id"] == item_id for item in await open_items(api))


async def open_items(api, kind=None):
    items = (await api.get("/api/attention")).json()
    return [i for i in items if kind is None or i["kind"] == kind]


async def test_permission_prompt_raises_and_resolves(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    control(env, session["id"], "permission")
    items = await wait_for(lambda: open_items(api, "needs_input"))
    item = items[0]
    assert item["session_id"] == session["id"] and item["verified"] and item["source"] == "claude_hook"
    assert "permission" in item["detail"].lower()
    interrupted = await api.post(f"/api/sessions/{session['id']}/interrupt")  # Esc cancels the prompt
    assert interrupted.status_code == 200, interrupted.text
    assert interrupted.json()["observed_state"] == "idle"
    await wait_for(lambda: _none(open_items(api, "needs_input")))
    everything = (await api.get("/api/attention?scope=all")).json()
    assert any(i["kind"] == "needs_input" and i["resolved_at"] for i in everything)


async def _none(coro):
    return not await coro


async def test_crash_recovery_is_noted_and_recovery_needed_is_critical(api, project, env):
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    control(env, session["id"], "crash")
    noted = await wait_for(lambda: open_items(api, "crash_recovered"))
    assert noted[0]["severity"] == "info" and "status 3" in noted[0]["detail"]

    async def resumed():
        current = await get(api, session["id"])
        return (len(current["runs"]) == 2 and current["runs"][-1]["verified_at"]
                and current["observed_state"] == "idle")
    await wait_for(resumed)
    for path in (env["home"] / ".claude/projects").rglob(f"{session['native_id']}.jsonl"):
        path.unlink()
    control(env, session["id"], "crash")
    critical = await wait_for(lambda: open_items(api, "recovery_needed"))
    assert critical[0]["severity"] == "critical" and "wasn't found on the machine" in critical[0]["detail"]
    ack = await api.post(f"/api/attention/{critical[0]['id']}/ack")
    assert ack.status_code == 200
    assert not await open_items(api, "recovery_needed")


def test_login_expiry_rules(api):
    db = api.app.state.db
    day = 86400
    cases = [
        ({"claude": {"logged_in": True, "login_expires_at": time.time() + 30 * day}}, None),
        ({"claude": {"logged_in": True, "login_expires_at": time.time() + 5 * day}}, "warn"),
        ({"claude": {"logged_in": True, "login_expires_at": time.time() + 1 * day}}, "critical"),
        ({"claude": {"logged_in": False}}, "critical"),
        ({"claude": {"logged_in": True, "login_expires_at": time.time() + 30 * day}}, None),  # re-login resolves
    ]
    for health, expected in cases:
        with write_db(db) as s:
            attention.evaluate_auth(s, {**health, "codex": {"logged_in": True}})
            s.commit()
            item = s.query(AttentionItem).filter_by(dedupe_key="auth:claude", resolved_at=None).one_or_none()
            assert (item.severity if item else None) == expected, health


async def test_accounts_health_and_relogin_flow(api):
    health = (await api.get("/api/accounts?refresh=1")).json()
    assert health["claude"]["logged_in"] and health["claude"]["subscription"] == "max"
    assert health["codex"]["logged_in"]

    flow = (await api.post("/api/accounts/claude/login")).json()
    assert flow["url"].startswith("https://claude.com/") and not flow["done"]
    bad = (await api.post(f"/api/accounts/login/{flow['flow_id']}/code", json={"code": "nope"})).json()
    assert bad["done"] and not bad["ok"]

    flow = (await api.post("/api/accounts/claude/login")).json()
    good = (await api.post(f"/api/accounts/login/{flow['flow_id']}/code", json={"code": "good-code"})).json()
    assert good["done"] and good["ok"] and "http" not in good["message"]


async def test_codex_device_login_parses_coloured_output(api):
    flow = (await api.post("/api/accounts/codex/login")).json()
    assert flow["url"] == "https://auth.openai.com/codex/device"
    assert flow["user_code"] == "37HC-WVMZN"
    done = await wait_for(lambda: _done(api, flow["flow_id"]))
    assert done["ok"]


async def _done(api, flow_id):
    result = (await api.get(f"/api/accounts/login/{flow_id}")).json()
    return result if result.get("done") else None


async def test_tool_progress_clears_needs_input(api, project, env):
    import json as _json
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    control(env, session["id"], "permission")
    await wait_for(lambda: open_items(api, "needs_input"))
    record = {"ts": "x", "provider": "claude", "acc_session": session["id"],
              "hook": {"hook_event_name": "PermissionDenied", "session_id": session["native_id"]}}
    with (env["run_dir"] / session["id"] / "events.jsonl").open("a") as handle:
        handle.write(_json.dumps(record) + "\n")
    await wait_for(state_is(api, session["id"], "active"))
    await wait_for(lambda: _none(open_items(api, "needs_input")))


async def test_model_catalog(api, env):
    (env["home"] / ".claude.json").write_text('{"additionalModelOptionsCache": [{"value": "claude-fable-5-1[1m]", "label": "Fable 1M"}]}')
    catalog = (await api.get("/api/models?refresh=1")).json()
    assert [m["id"] for m in catalog["codex"]] == ["gpt-test"] and catalog["codex"][0]["efforts"] == ["low", "high"]
    ids = [m["id"] for m in catalog["claude"]]
    assert "opus" in ids and "claude-fable-5-1[1m]" in ids
    assert (await api.get("/api/models")).json()["fetched_at"] == catalog["fetched_at"]


async def test_github_device_login_without_a_browser(api):
    health = (await api.get("/api/accounts?refresh=1")).json()
    assert health["github"] == {"logged_in": True, "login": "fake-agent", "scopes": "gist, repo, workflow"}
    flow = (await api.post("/api/accounts/github/login")).json()
    assert flow["url"] == "https://github.com/login/device" and flow["user_code"] == "ABCD-1234"
    done = await wait_for(lambda: _done(api, flow["flow_id"]))  # gh's "Press Enter" was answered for it
    assert done["ok"]
    assert (await api.post("/api/accounts/gitlab/login")).status_code == 404


def test_only_providers_an_account_has_used_can_be_logged_out(api):
    from acc.server import attention
    from acc.server.models import AttentionItem
    out = {"claude": {"logged_in": False}, "codex": {"logged_in": False}, "github": {"logged_in": False}}
    with write_db(api.app.state.db) as s:
        attention.evaluate_auth(s, out, "fresh-agent")  # never set up: nothing to say
        s.commit()
        assert not s.query(AttentionItem).filter(AttentionItem.kind == "auth").count()
        attention.evaluate_auth(s, {**out, "claude": {"logged_in": True}}, "fresh-agent")  # Claude set up, Codex never
        s.commit()
        attention.evaluate_auth(s, out, "fresh-agent")  # Claude drops out later
        s.commit()
        titles = [i.title for i in s.query(AttentionItem).filter(AttentionItem.kind == "auth", AttentionItem.resolved_at.is_(None))]
    assert titles == ["Claude Code is logged out for fresh-agent"]  # not Codex or GitHub, which it never used


async def test_items_of_sessions_nobody_signs_in_as_go_to_owners(api, project, env):
    """A session started with the API token belongs to "@api"; its items must reach owners, not a pseudo-user."""
    from test_supervisor import launch

    from acc.server import attention
    from acc.server.db import WRITE_INTENT
    from acc.server.models import Session, User
    session = await launch(api, project)
    intent = WRITE_INTENT.set(True)
    with api.app.state.db() as s:
        assert s.get(Session, session["id"]).owner == "@api"
        item = attention.raise_item(s, "test:api-owned", kind="needs_input", title="t", source="test", session_id=session["id"])
        assert item.owner is None  # owners see it
        s.add(User(username="real", role="member", password_hash="x", totp_secret="x"))
        s.flush()
        mine = attention.raise_item(s, "test:mine", kind="needs_input", title="t", source="test", owner="real")
        assert mine.owner == "real"
        s.rollback()
    WRITE_INTENT.reset(intent)
