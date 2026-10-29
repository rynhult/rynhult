"""Catalog, profile compilation and the launch wizard (M7)."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from helpers import wait_for

from acc.broker import ops
from acc.server.catalog import load
from acc.server.compiler import CompileError, LaunchInputs, build_args, compile_files

CATALOG = load(Path(__file__).resolve().parents[1] / "catalog")
HOOK, STATUS = "/srv/acc/bin/acc-hook", "/srv/acc/bin/acc-statusline"


def test_catalog_is_consistent():
    assert {"acc-workflow", "frontend-design", "acc-council", "evidence-research"} <= set(CATALOG.skills)
    assert {"coordinator", "code-reviewer"} <= set(CATALOG.profiles)
    for profile in CATALOG.profiles.values():
        assert "acc-workflow" in profile["skills"]
    public = CATALOG.public()
    assert all("env" not in m for m in public["mcps"])
    assert all(m.get("license") for m in public["skills"] + public["agents"] + public["mcps"])


def claude(profile, **extra):
    p = CATALOG.profiles[profile]
    return LaunchInputs(provider="claude", profile=profile, skills=tuple(p["skills"]), agents=tuple(p["agents"]),
                        mcps=tuple(p["mcps"]), **extra)


def test_claude_profile_compiles_to_a_session_plugin_and_strict_mcp():
    compiled = compile_files(claude("frontend"), HOOK, STATUS, catalog=CATALOG)
    files = compiled.files
    skills = {name.split("/")[2] for name in files if name.startswith("plugin/skills/")}
    assert skills == set(CATALOG.profiles["frontend"]["skills"])
    assert json.loads(files["mcp.json"])["mcpServers"].keys() == {"playwright"}
    assert files["append-prompt.md"].startswith("Rynhult mandatory core")
    assert "# Rynhult profile: Frontend" in files["append-prompt.md"]
    args = build_args(claude("frontend"), run_dir="/r", tmux_name="t", native_id="u", resume=False, files=files)
    assert args[args.index("--plugin-dir") + 1] == "/r/plugin"
    assert args[args.index("--mcp-config") + 1] == "/r/mcp.json" and "--strict-mcp-config" in args
    assert args[args.index("--append-system-prompt-file") + 1] == "/r/append-prompt.md"
    resume = build_args(claude("frontend"), run_dir="/r", tmux_name="t", native_id="u", resume=True, files=files)
    assert [a for a in resume if a != "--resume"] != args and "--plugin-dir" in resume  # resume re-passes everything


def test_agents_land_in_the_plugin():
    files = compile_files(claude("qa"), HOOK, STATUS, catalog=CATALOG).files
    assert {"plugin/agents/e2e-runner.md", "plugin/agents/code-reviewer.md"} <= set(files)


def test_no_mcp_selected_still_isolates_the_session():
    files = compile_files(LaunchInputs(provider="claude"), HOOK, STATUS, catalog=CATALOG).files
    assert json.loads(files["mcp.json"]) == {"mcpServers": {}}


def test_codex_profile_uses_overrides_and_hides_unselected_skills():
    inputs = LaunchInputs(provider="codex", profile="qa", skills=("acc-workflow", "webapp-testing"), mcps=("playwright",))
    files = compile_files(inputs, HOOK, STATUS, catalog=CATALOG, agent_home="/home/agent",
                          codex_plugins={"plugins": ["sites@openai-curated-remote", "pages@openai-curated-remote"],
                                         "skill_paths": ["/home/agent/.codex/plugins/cache/m/sites/1.0/skills/sites/SKILL.md"]}).files
    overrides = json.loads(files["codex-overrides.json"])
    instructions = json.loads(overrides[0].removeprefix('developer_instructions='))
    assert instructions.startswith('Rynhult mandatory core')
    assert '# Rynhult profile: QA' in instructions
    assert 'mcp_servers.playwright.command="/srv/acc/tools/mcp/node_modules/.bin/playwright-mcp"' in overrides  # installed, never npx
    assert 'mcp_servers.playwright.env_vars=["PLAYWRIGHT_BROWSERS_PATH"]' in overrides  # values come from the session environment
    hide = next(o for o in overrides if o.startswith("skills.config="))
    assert "/home/agent/.codex/skills/acc-frontend-design/SKILL.md" in hide
    assert "acc-webapp-testing" not in hide and "acc-acc-workflow" not in hide
    assert "/skills/acc-council/SKILL.md" not in hide  # retired ECC council is no longer linked
    assert "/skills/acc-acc-council/SKILL.md" in hide  # the Rynhult council is, and is hidden unless chosen
    assert "/home/agent/.codex/skills/.system/skill-installer/SKILL.md" in hide
    assert 'plugins."pages@openai-curated-remote".enabled=false' in overrides
    assert 'plugins."sites@openai-curated-remote".enabled=false' in overrides
    assert "/home/agent/.codex/plugins/cache/m/sites/1.0/skills/sites/SKILL.md" in hide
    args = build_args(inputs, run_dir="/r", tmux_name="t", native_id=None, resume=False, files=files)
    assert args.count("-c") == len(overrides) + 2  # plus Rynhult's fixed update and hooks settings


@pytest.mark.parametrize("inputs, message", [
    (LaunchInputs(provider="codex", agents=("architect",)), "not available for codex"),
    (LaunchInputs(provider="codex", mcps=("mslearn",)), "not available for codex"),
    (LaunchInputs(provider="codex", skills=("council",)), "unknown skill"),
    (LaunchInputs(provider="claude", skills=("nope",)), "unknown skill"),
])
def test_incompatible_selections_are_refused(inputs, message):
    with pytest.raises(CompileError, match=message):
        compile_files(inputs, HOOK, STATUS, catalog=CATALOG)


def test_sync_codex_skills(env):
    home = Path(env["home"])
    (home / ".codex/skills/.system").mkdir(parents=True)  # Codex's own: must survive
    (home / ".codex/skills/personal").mkdir()  # not ours: must survive
    (home / ".codex/skills/acc-stale").symlink_to("/nonexistent")
    (home / ".agents/skills/personal").mkdir(parents=True)
    (home / ".agents/skills/acc-frontend-design").symlink_to("/nonexistent")  # where older versions linked them
    import dataclasses
    config = dataclasses.replace(env["broker"], catalog_dir=CATALOG.root)
    result = ops.sync_codex_skills(config, ["acc-workflow", "frontend-design"])
    assert sorted(result["linked"]) == ["acc-acc-workflow", "acc-frontend-design"]
    assert result["removed"] == ["acc-stale", f"{home}/.agents/skills/acc-frontend-design"]
    assert (home / ".codex/skills/acc-frontend-design/SKILL.md").is_file()
    assert (home / ".codex/skills/personal").is_dir() and (home / ".codex/skills/.system").is_dir()
    # Copilot CLI reads ~/.agents/skills as "Personal skills" and can't hide one per session: nothing of ours there
    assert [p.name for p in (home / ".agents/skills").iterdir()] == ["personal"]
    with pytest.raises(ops.OpError):
        ops.sync_codex_skills(config, ["../etc"])


async def test_profile_defaults_and_explicit_choices(api, project):
    preview = (await api.post("/api/sessions/preview", json={"project_id": project["id"], "provider": "claude",
                                                             "name": "r", "profile": "researcher"})).json()
    assert preview["inputs"]["permission_mode"] == "plan" and preview["inputs"]["mcps"] == ["mslearn"]
    explicit = (await api.post("/api/sessions/preview", json={
        "project_id": project["id"], "provider": "claude", "name": "r", "profile": "researcher",
        "permission_mode": "acceptEdits", "mcps": []})).json()
    assert explicit["inputs"]["permission_mode"] == "acceptEdits" and explicit["inputs"]["mcps"] == []
    codex = (await api.post("/api/sessions/preview", json={"project_id": project["id"], "provider": "codex",
                                                           "name": "q", "profile": "qa"})).json()
    assert codex["inputs"]["agents"] == [] and "playwright" in codex["inputs"]["mcps"]  # Claude-only agents dropped
    catalog = (await api.get("/api/catalog")).json()
    assert {p["id"] for p in catalog["profiles"]} >= {"frontend", "qa", "security"}


async def test_preview_matches_the_launched_command_byte_for_byte(api, project, env):
    body = {"project_id": project["id"], "provider": "claude", "name": "exact", "profile": "frontend", "model": "opus"}
    preview = (await api.post("/api/sessions/preview", json=body)).json()
    session = (await api.post("/api/sessions", json=body)).json()

    async def launched():
        events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
        return next((e for e in events if e["kind"] == "run_launch"), None)
    event = await wait_for(launched)
    actual = shlex.split(event["payload"]["command"])[1:]
    expected = [a.replace("<run-dir>", str(env["run_dir"] / session["id"])).replace("<tmux-name>", session["tmux_name"])
                 .replace("<session-uuid>", session["native_id"]) for a in preview["command"][1:]]
    assert actual == expected
    # A renders role/capability context at each start; the saved spec deliberately
    # excludes it. Compare the provider-loaded runtime instructions instead.
    for name, value in preview["files"].items():
        if name == 'append-prompt.md':
            assert (Path(env['run_dir']) / session['id'] / name).read_text() == value
        else:
            assert session['launch_spec']['files'][name] == value


def test_codex_plugins_listing(env):
    cache = Path(env["home"]) / ".codex/plugins/cache/openai-curated-remote"
    for name in ("pages", "sites"):
        (cache / name / "1.0" / "skills" / name).mkdir(parents=True)
        (cache / name / "1.0" / "skills" / name / "SKILL.md").write_text("x")
    (cache.parent / ".staging").mkdir()
    found = ops.codex_plugins()
    assert found["plugins"] == ["pages@openai-curated-remote", "sites@openai-curated-remote"]
    assert [Path(p).parts[-2] for p in found["skill_paths"]] == ["pages", "sites"]
