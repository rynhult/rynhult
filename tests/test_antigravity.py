"""Google Antigravity CLI (`agy`) against the fake CLI, which follows its observed behaviour (1.4.4)."""
from __future__ import annotations

import json

from helpers import control, wait_for
from test_supervisor import get, launch, state_is


async def test_listed_as_a_standard_preview_without_needs_you(api):
    agy = next(p for p in (await api.get("/api/providers/registry")).json()["providers"] if p["id"] == "antigravity")
    assert agy["installed"] and agy["preview"] and agy["tier"] == "standard" and agy["login"] == "paste"
    assert "needs_you" not in agy["supports"] and [o["key"] for o in agy["options"]] == ["mode", "skip_permissions"]


async def test_preview_and_dangerous_flag(api, project):
    base = {"project_id": project["id"], "provider": "antigravity", "name": "a"}
    preview = (await api.post("/api/sessions/preview", json={**base, "model": "gemini-3-pro", "effort": "high",
                                                                 "options": {"mode": "plan"}})).json()
    assert preview["command"][:-2] == ["agy", "--mode", "plan", "--model", "gemini-3-pro", "--effort", "high"]
    assert preview["command"][-2] == "--add-dir" and preview["command"][-1].endswith("/antigravity")
    assert any("acc-request tool --manager" in text for name, text in preview["files"].items()
               if name.endswith("/rules/AGENTS.md"))
    skip = {**base, "options": {"skip_permissions": True}}
    assert (await api.post("/api/sessions", json=skip)).status_code == 409  # needs confirming
    assert "--dangerously-skip-permissions" in (await api.post("/api/sessions/preview", json=skip)).json()["command"]


async def test_session_learns_its_id_tracks_turns_and_resumes(api, project, env):
    session = await launch(api, project, provider="antigravity")
    started = await wait_for(lambda: _with_id(api, session["id"]))
    native = started["native_id"]
    hooks = json.loads((env["home"] / ".gemini/config/hooks.json").read_text())  # written by the broker at launch
    assert hooks["acc"]["Stop"][0]["command"].endswith("acc-hook antigravity Stop")

    control(env, session["id"], "turn")  # PreInvocation, a mid-turn Stop (fullyIdle false), then the final Stop
    await wait_for(state_is(api, session["id"], "active"))
    done = await wait_for(state_is(api, session["id"], "idle"))
    assert done["status_source"] == "antigravity_hook"

    control(env, session["id"], "crash")

    async def resumed():
        s = await get(api, session["id"])
        return s if len(s["runs"]) == 2 and s["runs"][1]["verified_at"] and s["observed_state"] == "idle" else None
    after = await wait_for(resumed)
    assert after["native_id"] == native  # resumed with --conversation <id>


async def _with_id(api, acc_id):
    s = await get(api, acc_id)
    return s if s["native_id"] and s["observed_state"] == "idle" else None


async def test_sign_in_pastes_the_code_back(api, env):
    flow = (await api.post("/api/accounts/antigravity/login")).json()
    assert flow["url"].startswith("https://accounts.google.com/") and not flow.get("user_code")
    done = (await api.post(f"/api/accounts/login/{flow['flow_id']}/code", json={"code": "good-code"})).json()
    assert done["done"] and done["ok"]
    health = (await api.get("/api/accounts?refresh=1")).json()
    assert health["antigravity"]["logged_in"] is True
    models = (await api.get("/api/models?refresh=1")).json()
    assert [m["id"] for m in models["antigravity"]] == ["gemini-3-pro", "gemini-3-flash"]


def test_bootstrap_keeps_other_hook_groups(tmp_path):
    from acc.providers import ANTIGRAVITY
    path = tmp_path / ".gemini/config/hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"my-linter": {"PostToolUse": []}}))
    ANTIGRAVITY.bootstrap(tmp_path, "/srv/acc/bin/acc-hook")
    hooks = json.loads(path.read_text())
    assert set(hooks) == {"my-linter", "acc"} and set(hooks["acc"]) == {"SessionStart", "PreInvocation", "PreToolUse", "PostToolUse", "Stop"}
    assert hooks["acc"]["Stop"][0]["command"].endswith("acc-hook antigravity Stop")  # flat: agy skips a grouped Stop
    assert hooks["acc"]["PostToolUse"][0]["matcher"] == ""


def test_trust_records_the_folder(tmp_path):
    from acc.providers import ANTIGRAVITY
    path = tmp_path / ".gemini/antigravity-cli/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"theme": "dark", "trustedWorkspaces": ["/a"]}))
    ANTIGRAVITY.trust(tmp_path, "/srv/ws", "agy")
    ANTIGRAVITY.trust(tmp_path, "/srv/ws", "agy")
    assert json.loads(path.read_text()) == {"theme": "dark", "trustedWorkspaces": ["/a", "/srv/ws"]}
