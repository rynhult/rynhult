"""Cursor CLI (`cursor-agent`) against the fake CLI, which follows what the real one prints (2026.10.19)."""
from __future__ import annotations

import json

from helpers import control, wait_for
from test_supervisor import get, launch, state_is


async def test_listed_as_a_standard_preview_with_link_login(api):
    cursor = next(p for p in (await api.get("/api/providers/registry")).json()["providers"] if p["id"] == "cursor")
    assert cursor["installed"] and cursor["preview"] and cursor["tier"] == "standard" and cursor["login"] == "link"


async def test_preview_trusts_the_folder_and_force_needs_confirming(api, project):
    base = {"project_id": project["id"], "provider": "cursor", "name": "c"}
    preview = (await api.post("/api/sessions/preview", json={**base, "model": "gpt-test", "options": {"mode": "plan"}})).json()
    assert preview["command"][:-2] == ["cursor-agent", "--trust", "--mode", "plan", "--model", "gpt-test"]
    assert preview["command"][-2] == "--plugin-dir" and preview["command"][-1].endswith("/cursor/acc")
    context = json.loads(preview["files"]["cursor/acc/session-start.json"])["additional_context"]
    assert "acc-request tool --manager" in context
    force = {**base, "options": {"force": True}}
    assert (await api.post("/api/sessions", json=force)).status_code == 409


async def test_session_learns_its_chat_id_tracks_a_turn_and_resumes(api, project, env):
    session = await launch(api, project, provider="cursor")

    async def with_id():
        s = await get(api, session["id"])
        return s if s["native_id"] and s["observed_state"] == "idle" else None
    started = await wait_for(with_id)
    hooks = json.loads((env["home"] / ".cursor/hooks.json").read_text())
    assert hooks["version"] == 1 and hooks["hooks"]["stop"][0]["command"].endswith("acc-hook cursor stop")

    control(env, session["id"], "turn")
    await wait_for(state_is(api, session["id"], "active"))
    done = await wait_for(state_is(api, session["id"], "idle"))
    assert done["status_source"] == "cursor_hook"

    control(env, session["id"], "crash")

    async def resumed():
        s = await get(api, session["id"])
        return s if len(s["runs"]) == 2 and s["runs"][1]["verified_at"] and s["observed_state"] == "idle" else None
    assert (await wait_for(resumed))["native_id"] == started["native_id"]


async def test_link_login_completes_on_its_own(api):
    flow = (await api.post("/api/accounts/cursor/login")).json()
    assert flow["url"].startswith("https://cursor.com/loginDeepControl") and not flow.get("user_code")

    async def finished():
        result = (await api.get(f"/api/accounts/login/{flow['flow_id']}")).json()
        return result if result.get("done") else None
    assert (await wait_for(finished))["ok"]
    health = (await api.get("/api/accounts?refresh=1")).json()["cursor"]
    assert health["logged_in"] and health["email"] == "fake@example.com"
    models = (await api.get("/api/models?refresh=1")).json()["cursor"]
    assert [m["id"] for m in models] == ["gpt-test", "sonnet-test"]


def test_bootstrap_keeps_other_hooks(tmp_path):
    from acc.providers import CURSOR
    path = tmp_path / ".cursor/hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}))
    CURSOR.bootstrap(tmp_path, "/srv/acc/bin/acc-hook")
    CURSOR.bootstrap(tmp_path, "/srv/acc/bin/acc-hook")  # idempotent: no duplicates
    stops = json.loads(path.read_text())["hooks"]["stop"]
    assert [h["command"] for h in stops] == ["./mine.sh", "/srv/acc/bin/acc-hook cursor stop"]
