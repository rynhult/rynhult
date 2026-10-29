"""The provider registry: what the API serves, how launch options are checked, and that Claude and Codex launches
compile exactly as before the registry existed (stored launch specs resume unchanged)."""
from __future__ import annotations

import hashlib
import json

import pytest

from acc import providers
from acc.server.compiler import CompileError, LaunchInputs, build_args, compile_files


async def test_registry_lists_providers_with_options_and_install_state(api):
    listing = (await api.get("/api/providers/registry")).json()["providers"]
    by_id = {p["id"]: p for p in listing}
    assert list(by_id)[:2] == ["claude", "codex"]
    claude, codex = by_id["claude"], by_id["codex"]
    assert claude["installed"] is True and codex["installed"] is True  # the fake CLIs
    assert [o["key"] for o in claude["options"]] == ["permission_mode", "remote_control"]
    assert "bypassPermissions" in claude["options"][0]["dangerous"]
    assert {o["key"] for o in codex["options"]} == {"sandbox", "approval"}
    assert claude["login"] == "paste" and codex["login"] == "device"
    assert "agents" in claude["supports"] and "agents" not in codex["supports"]


async def test_unknown_provider_and_unknown_options_are_refused(api, project):
    base = {"project_id": project["id"], "name": "x"}
    assert (await api.post("/api/sessions/preview", json={**base, "provider": "bash"})).status_code == 422
    bad = await api.post("/api/sessions/preview", json={**base, "provider": "claude", "options": {"yolo": "1"}})
    assert bad.status_code == 422 and "no option" in bad.text
    shape = await api.post("/api/sessions/preview", json={**base, "provider": "claude", "options": {"x": "a b"}})
    assert shape.status_code == 422  # option values are bounded before any provider sees them


def test_dangerous_rule_comes_from_the_option_schema():
    claude, codex = providers.get("claude"), providers.get("codex")
    assert claude.dangerous(LaunchInputs(provider="claude", permission_mode="bypassPermissions"))
    assert not claude.dangerous(LaunchInputs(provider="claude"))
    assert codex.dangerous(LaunchInputs(provider="codex", approval="never"))
    assert codex.dangerous(LaunchInputs(provider="codex", sandbox="danger-full-access"))
    with pytest.raises(CompileError, match="unknown permissions"):
        compile_files(LaunchInputs(provider="claude", permission_mode="yolo"), "/h", "/s")
    with pytest.raises(CompileError, match="Remote Control is not supported for Codex"):
        compile_files(LaunchInputs(provider="codex", remote_control=True), "/h", "/s")


def test_launch_specs_from_before_the_registry_are_unchanged():
    """The digest covers inputs and files: an empty `options` is left out, so older specs and their digests match."""
    inputs = LaunchInputs(provider="claude", model="opus", append_prompt="Be brief.")
    compiled = compile_files(inputs, "/srv/acc/bin/acc-hook", "/srv/acc/bin/acc-statusline")
    assert "options" not in inputs.as_dict()
    legacy = {"inputs": {**inputs.as_dict()}, "files": compiled.files}
    assert compiled.digest == hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    assert LaunchInputs.from_dict(inputs.as_dict()) == inputs
    args = build_args(inputs, run_dir="/r", tmux_name="t", native_id="11111111-1111-1111-1111-111111111111", resume=True,
                      files=compiled.files)
    assert args[:4] == ["--settings", "/r/settings.json", "--resume", "11111111-1111-1111-1111-111111111111"]


def test_hooks_pass_through_in_acc_vocabulary_for_claude_and_codex():
    hook = {"hook_event_name": "Notification", "notification_type": "permission_prompt", "session_id": "abc"}
    for name in ("claude", "codex"):
        assert providers.get(name).normalize_hook(dict(hook)) == hook


def test_codex_bootstrap_needs_acc_own_hook_group(tmp_path, env):
    """D16: ACC's command inside a group with a matcher that never matches used to count as installed."""
    codex = providers.get("codex")
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    guard = "/srv/acc/bin/acc-guard codex PreToolUse"
    neutered = {"matcher": "^never$", "hooks": [{"type": "command", "command": guard}]}
    other = {"hooks": [{"type": "command", "command": "/usr/local/bin/someone-elses"}]}
    path.write_text(json.dumps({"hooks": {"PreToolUse": [neutered, other]}}))
    codex.bootstrap(tmp_path, "/srv/acc/bin/acc-hook", exe=env["broker"].providers["codex"])
    groups = json.loads(path.read_text())["hooks"]["PreToolUse"]
    assert groups[:2] == [neutered, other]  # others' entries are kept
    assert {"hooks": [{"type": "command", "command": guard}]} in groups
    before = path.read_text()
    codex.bootstrap(tmp_path, "/srv/acc/bin/acc-hook", exe=env["broker"].providers["codex"])
    assert path.read_text() == before  # idempotent


def test_claude_session_settings_keep_hooks_on():
    """D16: command-line settings outrank the project's and the user's, so disableAllHooks there can't silence ACC."""
    files = compile_files(LaunchInputs(provider="claude"), "/srv/acc/bin/acc-hook", "/srv/acc/bin/acc-statusline").files
    assert json.loads(files["settings.json"])["disableAllHooks"] is False
