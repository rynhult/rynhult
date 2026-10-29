"""GitHub Copilot CLI support against the fake CLI, which follows Copilot's documented flags and hooks."""
from __future__ import annotations

import json

from helpers import control, reconcile_passes, wait_for
from test_supervisor import get, launch, state_is


async def test_copilot_is_listed_as_an_installed_preview(api):
    copilot = next(p for p in (await api.get("/api/providers/registry")).json()["providers"] if p["id"] == "copilot")
    assert copilot["installed"] is True and copilot["preview"] is True and copilot["login"] == "device"
    assert [o["key"] for o in copilot["options"]] == ["mode", "permissions"]


async def test_preview_shows_the_exact_command(api, project):
    preview = (await api.post("/api/sessions/preview", json={
        "project_id": project["id"], "provider": "copilot", "name": "c", "model": "auto", "effort": "high",
        "options": {"mode": "plan"}, "profile": "frontend"})).json()
    command = preview["command"]
    assert command[:4] == ["copilot", "--session-id", "<session-uuid>", "--no-auto-update"]
    assert ["--model", "auto"] == command[4:6] and "--reasoning-effort" in command and ["--mode", "plan"] == command[8:10]
    assert "instructions/AGENTS.md" in preview["files"]  # the profile's instructions, via COPILOT_CUSTOM_INSTRUCTIONS_DIRS
    assert any(name.startswith("copilot/.github/skills/") for name in preview["files"])
    assert "--add-dir" in command


async def test_copilot_session_runs_on_hooks_waits_and_resumes(api, project, env):
    session = await launch(api, project, provider="copilot", profile="frontend")
    assert session["native_id"]  # assigned up front, like Claude
    ready = await wait_for(state_is(api, session["id"], "idle"))
    assert ready["status_source"] == "copilot_hook" and ready["runs"][0]["verified_at"]

    control(env, session["id"], "permission")  # Copilot's camelCase `notification` event
    waiting = await wait_for(state_is(api, session["id"], "waiting_input"))
    assert waiting["status_source"] == "copilot_hook"

    control(env, session["id"], "crash")

    async def resumed():
        s = await get(api, session["id"])
        return s if len(s["runs"]) == 2 and s["runs"][1]["verified_at"] and s["observed_state"] == "idle" else None
    after = await wait_for(resumed)
    assert after["native_id"] == session["native_id"] and after["runs"][1]["reason"] == "resume"
    spool = (env["run_dir"] / session["id"] / "events.jsonl").read_text().splitlines()
    assert any(json.loads(line).get("event") == "notification" for line in spool)
    assert (env["run_dir"] / session["id"] / "instructions" / "AGENTS.md").exists()
    hooks = json.loads((env["home"] / ".copilot/hooks/acc.json").read_text())  # written by the broker at launch
    assert hooks["hooks"]["SessionStart"][0]["bash"].endswith("acc-hook copilot SessionStart")


async def test_trust_and_login_use_the_cli(api, project, env):
    from acc.providers.copilot import _read_jsonc
    config = _read_jsonc(env["home"] / ".copilot/config.json")
    assert project["workspace_path"] in config["trustedFolders"]  # trusting the project told Copilot too
    flow = (await api.post("/api/accounts/copilot/login")).json()
    assert flow["url"] == "https://github.com/login/device" and flow["user_code"] == "ABCD-1234"
    settings = json.loads((env["home"] / ".copilot/settings.json").read_text())
    assert settings["storeTokenPlaintext"] is True  # no keychain on a server: keep the token in a private file

    async def finished():
        result = (await api.get(f"/api/accounts/login/{flow['flow_id']}")).json()
        return result if result.get("done") else None
    assert (await wait_for(finished))["ok"]
    health = (await api.get("/api/accounts?refresh=1")).json()
    assert health["copilot"]["logged_in"] and health["copilot"]["login"] == "fake-agent"


async def test_allow_all_needs_confirming(api, project):
    body = {"project_id": project["id"], "provider": "copilot", "name": "x", "options": {"permissions": "all"}}
    assert (await api.post("/api/sessions", json=body)).status_code == 409
    bad = await api.post("/api/sessions/preview", json={**body, "options": {"permissions": "everything"}})
    assert bad.status_code == 422


async def test_a_quiet_screen_ends_a_turn_copilot_never_closed(api, project, env):
    object.__setattr__(api.app.state.supervisor.settings, "quiet_idle_override", 1.5)
    session = await launch(api, project, provider="copilot")
    await wait_for(state_is(api, session["id"], "idle"))
    control(env, session["id"], "busy")
    await wait_for(state_is(api, session["id"], "active"))
    quiet = await wait_for(state_is(api, session["id"], "idle"), timeout=20)
    assert quiet["status_source"] == "tmux"


def test_plan_and_ai_credits_from_the_copilot_account(tmp_path, monkeypatch):
    """The shape GitHub returned for an enterprise seat (2026-10-21), trimmed."""
    import io
    import urllib.request

    from acc.providers.copilot import PROVIDER
    (tmp_path / ".copilot").mkdir()
    (tmp_path / ".copilot/config.json").write_text('// managed\n' + json.dumps({
        "lastLoggedInUser": {"host": "https://github.com", "login": "octo"},
        "authTokens": {"https://github.com:octo": {"token": "gho_test"}}}))
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"], seen["auth"] = request.full_url, request.headers.get("Authorization")
        return io.BytesIO(json.dumps({
            "copilot_plan": "enterprise", "access_type_sku": "copilot_enterprise_seat_quota",
            "organization_login_list": ["Example-Org"], "quota_reset_date_utc": "2026-11-01T00:00:00.000Z",
            "quota_snapshots": {"chat": {"unlimited": True}, "completions": {"unlimited": True},
                                "premium_interactions": {"credits_used": 1200, "entitlement": 1000000, "remaining": 998800,
                                                         "percent_remaining": 99.88, "unlimited": False,
                                                         "overage_permitted": True, "overage_count": 0}}}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    usage = PROVIDER.usage("copilot", tmp_path)
    assert seen == {"url": "https://api.github.com/copilot_internal/user", "auth": "token gho_test"}
    assert usage["plan"] == "enterprise" and usage["organizations"] == ["Example-Org"]
    assert usage["credits"]["entitlement"] == 1000000 and usage["credits"]["used"] == 1200
    assert usage["chat"] == {"unlimited": True} and usage["resets_at"].startswith("2026-11-01")
    assert "gho_test" not in json.dumps(usage)


async def test_models_come_from_the_cli_and_the_probe_leaves_nothing(api, env):
    models = (await api.get("/api/models?refresh=1")).json()["copilot"]
    assert [m["id"] for m in models] == ["auto", "gpt-test-sol"] and models[1].get("default") is True
    assert not list((env["home"] / ".copilot/session-state").glob("*"))  # the probe session was removed


async def test_copilot_permission_requests_alone_are_not_needs_you(api, project, env):
    """Copilot sends PermissionRequest for every tool, even ones it approves itself, and nothing more until a long tool
    finishes: the hook alone never means "needs you" (no flicker, no Inbox item, no push). Its prompt on screen does
    (tests/test_screen_prompt.py)."""
    from acc.server.models import AttentionItem
    session = await launch(api, project, provider="copilot", profile="frontend")
    await wait_for(state_is(api, session["id"], "idle"))
    control(env, session["id"], "permission-request")
    async def request_ingested():
        events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
        return any(event["kind"] == "PermissionRequest" for event in events)
    await wait_for(request_ingested)
    seen = []
    for _ in range(15):
        await api.app.state.supervisor.ingest_pass()
        await api.app.state.supervisor.reconcile()
        seen.append((await get(api, session["id"]))["observed_state"])
    with api.app.state.db() as s:
        items = s.query(AttentionItem).filter(AttentionItem.kind == "needs_input", AttentionItem.session_id == session["id"]).count()
    assert "waiting_input" not in seen and items == 0


async def test_copilot_working_screen_overrides_quiet_idle(api, project, env):
    """Owner-reported status-line capture, 2026-10-23; no heartbeat during a long shell tool."""
    import asyncio
    from pathlib import Path

    from acc.providers import get as provider
    screen = Path(__file__).with_name('fixtures').joinpath('copilot-working-screen.txt').read_text()
    object.__setattr__(api.app.state.supervisor.settings, 'quiet_idle_override', .5)
    session = await launch(api, project, provider='copilot')
    await wait_for(state_is(api, session['id'], 'idle'))
    run = env['run_dir'] / session['id']
    for indicator in [screen, '○ Run the test suite … esc esc interrupt\n']:
        assert provider('copilot').screen_working(indicator)
        (run / 'fake.screen').write_text('\x1b[2J\x1b[H' + indicator)
        control(env, session['id'], 'screen')
        await wait_for(state_is(api, session['id'], 'active'))
        await asyncio.sleep(1.2)  # longer than the quiet timeout, with a static captured screen
        assert (await get(api, session['id']))['observed_state'] == 'active'
    assert not provider('copilot').screen_working('❯ \n? help\n')
    assert not provider('claude').screen_working(screen)
    (run / 'fake.screen').write_text('\x1b[2J\x1b[H❯ \n? help\n')
    control(env, session['id'], 'screen')
    await wait_for(state_is(api, session['id'], 'idle'))


def hostile_settings(env, name=".github/copilot/settings.json"):
    target = env["workspaces"] / "demo" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"disableAllHooks": true}')
    return target


def refusals(api, acc_id):
    from sqlalchemy import select

    from acc.server.models import AuditLog
    with api.app.state.db() as db:
        return [row.detail for row in db.scalars(select(AuditLog).where(
            AuditLog.target == acc_id, AuditLog.action == "session.copilot_hooks_refused").order_by(AuditLog.id))]


async def test_copilot_never_starts_where_the_repository_switches_its_hooks_off(api, project, env):
    """Owner decision (option a): a trusted folder's .github/copilot settings with disableAllHooks would switch
    Rynhult's guard off for Copilot. The broker refuses; the session waits for its person with the reason, an Inbox
    item and an audit entry, and isn't retried."""
    from acc.providers import copilot_settings

    target = hostile_settings(env, ".github/copilot/settings.local.json")
    session = await launch(api, project, provider="copilot")
    stuck = await wait_for(state_is(api, session["id"], "recovery_needed"))
    assert stuck["last_error"].startswith(copilot_settings.REFUSAL) and str(target) in stuck["last_error"]
    assert stuck["last_error"].endswith("then start a new session.")  # never started: nothing to resume
    assert not stuck["runs"]
    [audit] = refusals(api, session["id"])
    assert audit["reason"] == "launch" and str(target) in audit["problem"] and audit["cwd"] == project["workspace_path"]
    items = (await api.get("/api/attention")).json()
    assert any(i["kind"] == "recovery_needed" and i["session_id"] == session["id"] and str(target) in i["detail"]
               for i in items)
    await reconcile_passes(api)
    assert len(refusals(api, session["id"])) == 1 and not (await get(api, session["id"]))["runs"]  # no retry loop


async def test_copilot_resume_and_relaunch_are_refused_too(api, project, env):
    session = await launch(api, project, provider="copilot")
    await wait_for(state_is(api, session["id"], "idle"))
    target = hostile_settings(env)  # e.g. a pull brought it in while the session ran
    control(env, session["id"], "crash")  # the automatic resume
    stuck = await wait_for(state_is(api, session["id"], "recovery_needed"))
    assert str(target) in stuck["last_error"] and stuck["last_error"].endswith("then resume the session.")
    assert len(stuck["runs"]) == 1
    assert [a["reason"] for a in refusals(api, session["id"])] == ["resume"]

    assert (await api.post(f"/api/sessions/{session['id']}/resume")).status_code == 200  # the person's relaunch
    await wait_for(lambda: _refused_times(api, session["id"], 2))
    assert len((await get(api, session["id"]))["runs"]) == 1

    target.write_text('{"disableAllHooks": false}')
    assert (await api.post(f"/api/sessions/{session['id']}/resume")).status_code == 200

    async def resumed():
        current = await get(api, session["id"])
        return current if len(current["runs"]) == 2 and current["observed_state"] == "idle" else None
    after = await wait_for(resumed)
    assert after["runs"][1]["reason"] == "resume" and not after["last_error"]


async def _refused_times(api, acc_id, count):
    current = await get(api, acc_id)
    return current if len(refusals(api, acc_id)) == count and current["observed_state"] == "recovery_needed" else None


async def test_other_providers_are_not_refused_by_copilot_settings(api, project, env):
    hostile_settings(env)
    session = await launch(api, project)
    await wait_for(state_is(api, session["id"], "idle"))
    assert not refusals(api, session["id"])
