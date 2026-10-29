"""Catalog items per provider: what each CLI receives at launch and on resume (docs/reviews/skills-by-provider.md).

One small catalog with a skill, a subagent, an MCP server with a secret and a profile is compiled for every provider.
Each test asserts the mechanism the CLI really reads (checked against its --help on 2026-10-22; Cursor's and
Antigravity's with live one-shot prompts the same day), and that what a provider can't receive is filtered with a
reason instead of being dropped silently.
"""
from __future__ import annotations

import json
import shutil
import textwrap

import pytest

from acc import providers
from acc.server import compiler
from acc.server.catalog import load
from acc.server.compiler import (
    CompileError,
    LaunchInputs,
    build_args,
    compile_files,
    materialize,
    not_delivered,
    resume_skills_override,
)

HOOK, STATUS = "/srv/acc/bin/acc-hook", "/srv/acc/bin/acc-statusline"
SID = "11111111-2222-3333-4444-555555555555"
HOME = "/home/agent"
SECRET = "s3cret-value"
ALL = ["claude", "codex", "copilot", "antigravity", "cursor", "opencode"]


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    root = tmp_path_factory.mktemp("catalog")
    (root / "skills/probe").mkdir(parents=True)
    (root / "skills/probe/SKILL.md").write_text("---\nname: probe\ndescription: a probe skill\n---\nBody.\n")
    (root / "skills/probe/run.py").write_text("print('hi')\n")
    (root / "skills/other").mkdir()
    (root / "skills/other/SKILL.md").write_text("---\nname: other\ndescription: not chosen\n---\n")
    (root / "skills/cursor-only").mkdir()
    (root / "skills/cursor-only/SKILL.md").write_text("---\nname: cursor-only\ndescription: x\n---\n")
    (root / "agents").mkdir()
    (root / "agents/helper.md").write_text("---\nname: helper\ndescription: a probe subagent\ntools: Read, Grep\n"
                                          "model: opus\nhooks: {}\n---\nHelp. !`date`\n")
    (root / "agents/codex-listed.md").write_text("---\nname: codex-listed\ndescription: x\n---\n")
    (root / "profiles").mkdir()
    (root / "manifest.toml").write_text(textwrap.dedent(f"""
        [[skill]]
        id = "probe"
        [[skill]]
        id = "other"
        [[skill]]
        id = "cursor-only"
        providers = ["cursor"]
        [[agent]]
        id = "helper"
        [[agent]]
        id = "codex-listed"
        providers = ["claude", "codex", "copilot"]
        [[mcp]]
        id = "tool"
        command = "/opt/tool-mcp"
        args = ["--stdio"]
        env = {{ TOOL_TOKEN = "{SECRET}" }}
        [[mcp]]
        id = "remote"
        url = "https://mcp.example.com/mcp"
        providers = ["claude", "copilot", "cursor"]
        """))
    (root / "profiles/probe.toml").write_text(textwrap.dedent("""
        id = "probe"
        name = "Probe"
        skills = ["probe"]
        agents = ["helper"]
        mcps = ["tool"]
        instructions = "Probe instructions."
        """))
    return load(root)


def profile_text(value):
    from acc.server.agent_contract import CORE
    assert value.startswith(CORE + '\n\n')
    return value.removeprefix(CORE + '\n\n')


def launch(provider: str, catalog, **extra) -> LaunchInputs:
    """What the API makes of a profile launch (SessionIn.inputs): the profile's items this provider can use."""
    profile = catalog.profiles["probe"]
    picks = {kind: tuple(x for x in profile[kind] if provider in catalog.pool(kind[:-1] if kind != "mcps" else "mcp")[x].providers)
             for kind in ("skills", "agents", "mcps")}
    return LaunchInputs(provider=provider, profile="probe", **picks, **extra)


def compiled(provider: str, catalog, **extra):
    inputs = launch(provider, catalog, **extra)
    files = compile_files(inputs, HOOK, STATUS, catalog=catalog, agent_home=HOME).files
    return inputs, files


def argv(inputs, files, resume=False, extra=None):
    native = SID if (resume or providers.get(inputs.provider).session_id == "assigned") else None
    return build_args(inputs, run_dir="/r", tmux_name="t", native_id=native, resume=resume, files=files,
                      extra_overrides=extra)


# ---------------------------------------------------------------------------------------------- availability


def test_items_are_offered_only_to_providers_that_receive_them(catalog):
    assert catalog.skills["probe"].providers == ALL
    assert catalog.agents["helper"].providers == ["claude", "antigravity", "cursor", "opencode"]
    assert catalog.mcps["tool"].providers == ALL
    # A literal list naming a provider that can't receive this kind of item: that provider is left out, so the
    # New session dialog (which filters by `providers`) never offers something the launch would drop.
    assert catalog.agents["codex-listed"].providers == ["claude"]
    assert catalog.mcps["remote"].providers == ["claude", "copilot", "cursor"]
    public = {i["id"]: i["providers"] for i in catalog.public()["skills"]}
    assert public["cursor-only"] == ["cursor"]


@pytest.mark.parametrize("provider, kind, item, reason", [
    ("antigravity", "skills", "cursor-only", "limited to cursor"),
    ("antigravity", "mcps", "remote", "limited to claude, copilot, cursor"),
    ("codex", "agents", "helper", "Codex can't receive subagents from Rynhult"),
    ("copilot", "agents", "helper", "GitHub Copilot CLI can't receive subagents from Rynhult"),
    ("codex", "mcps", "remote", "limited to claude, copilot, cursor"),
])
def test_explicit_picks_a_provider_cannot_receive_are_refused_with_a_reason(catalog, provider, kind, item, reason):
    with pytest.raises(CompileError, match=f"not available for {provider}: {reason}"):
        compile_files(LaunchInputs(provider=provider, **{kind: (item,)}), HOOK, STATUS, catalog=catalog)


# ---------------------------------------------------------------------------------------------- per provider


def test_claude_gets_skills_subagents_mcp_and_instructions_at_launch_and_resume(catalog):
    inputs, files = compiled("claude", catalog)
    assert {"plugin/skills/probe/SKILL.md", "plugin/skills/probe/run.py", "plugin/agents/helper.md"} <= set(files)
    assert not any("/other/" in name for name in files)  # only what was chosen
    assert profile_text(files["append-prompt.md"]).startswith("# Rynhult profile: Probe") and "Probe instructions." in files["append-prompt.md"]
    servers = json.loads(files["mcp.json"])["mcpServers"]
    assert servers == {"tool": {"command": "/opt/tool-mcp", "args": ["--stdio"], "env": {"TOOL_TOKEN": "acc-secret:tool/TOOL_TOKEN"}}}
    assert json.loads(materialize(files, catalog, "claude")["mcp.json"])["mcpServers"]["tool"]["env"] == {"TOOL_TOKEN": SECRET}
    for resume in (False, True):  # the stored files are reused verbatim: a resume passes the same flags
        args = argv(inputs, files, resume=resume)
        assert args[args.index("--plugin-dir") + 1] == "/r/plugin"
        assert args[args.index("--mcp-config") + 1] == "/r/mcp.json" and "--strict-mcp-config" in args
        assert args[args.index("--append-system-prompt-file") + 1] == "/r/append-prompt.md"
        assert ("--resume" if resume else "--session-id") in args
    assert compiler.session_environment(inputs, catalog, "/r", files) == {}  # the secret is in mcp.json (0600), not the environment


def test_codex_gets_skills_mcp_and_instructions_through_overrides(catalog):
    inputs, files = compiled("codex", catalog)
    assert inputs.agents == ()  # Codex has no subagents Rynhult can pass
    overrides = json.loads(files["codex-overrides.json"])
    instructions = json.loads(overrides[0].removeprefix("developer_instructions="))
    assert profile_text(instructions).startswith("# Rynhult profile: Probe") and "Probe instructions." in instructions
    assert 'mcp_servers.tool.command="/opt/tool-mcp"' in overrides
    assert 'mcp_servers.tool.args=["--stdio"]' in overrides
    assert 'mcp_servers.tool.env_vars=["TOOL_TOKEN"]' in overrides  # by name: the value comes from the environment
    hide = next(o for o in overrides if o.startswith("skills.config="))
    assert f"{HOME}/.codex/skills/acc-other/SKILL.md" in hide  # linked for every session, hidden unless chosen
    assert "acc-probe/" not in hide
    for resume in (False, True):
        args = argv(inputs, files, resume=resume)
        assert [args[i + 1] for i, a in enumerate(args) if a == "-c"] == ["check_for_update_on_startup=false", *overrides,
                                                                       "features.hooks=true"]
        assert args[:2] == (["resume", SID] if resume else ["--sandbox", "workspace-write"])
        assert compiler.session_environment(inputs, catalog, "/r", files) == {"TOOL_TOKEN": SECRET}  # recomputed for every start


def test_codex_resume_hides_skills_added_since_launch(catalog, tmp_path):
    inputs, files = compiled("codex", catalog)
    assert resume_skills_override(inputs, files, catalog, HOME, None) == []
    root = tmp_path / "grown"
    shutil.copytree(catalog.root, root)
    (root / "skills/newer").mkdir()
    (root / "skills/newer/SKILL.md").write_text("---\nname: newer\ndescription: added later\n---\n")
    (root / "manifest.toml").write_text((root / "manifest.toml").read_text() + '\n[[skill]]\nid = "newer"\n')
    extra = resume_skills_override(inputs, files, load(root), HOME, None)
    assert len(extra) == 1 and f"{HOME}/.codex/skills/acc-newer/SKILL.md" in extra[0]
    assert f"{HOME}/.codex/skills/acc-other/SKILL.md" in extra[0]  # later -c replaces the list: keep the old ones


def test_copilot_gets_skills_mcp_and_instructions_through_its_run_folder(catalog):
    inputs, files = compiled("copilot", catalog)
    assert inputs.agents == ()
    assert {"copilot/.github/skills/probe/SKILL.md", "copilot/.github/skills/probe/run.py"} <= set(files)
    assert not any("/other/" in name for name in files)
    assert not any(name.startswith("copilot/.github/agents") for name in files)
    assert profile_text(files["instructions/AGENTS.md"]).startswith("# Rynhult profile: Probe")
    server = json.loads(files["mcp.json"])["mcpServers"]["tool"]
    assert server["type"] == "local" and server["tools"] == ["*"] and server["env"] == {"TOOL_TOKEN": "acc-secret:tool/TOOL_TOKEN"}
    assert json.loads(materialize(files, catalog, "copilot")["mcp.json"])["mcpServers"]["tool"]["env"] == {"TOOL_TOKEN": SECRET}
    for resume in (False, True):  # --session-id starts or resumes; the flags and env come from the stored files
        args = argv(inputs, files, resume=resume)
        assert args[args.index("--add-dir") + 1] == "/r/copilot"
        assert args[args.index("--additional-mcp-config") + 1] == "@/r/mcp.json"
        assert args[args.index("--session-id") + 1] == SID
        assert compiler.session_environment(inputs, catalog, "/r", files) == {"COPILOT_CUSTOM_INSTRUCTIONS_DIRS": "/r/instructions"}


def test_copilot_remote_mcp_server(catalog):
    inputs = LaunchInputs(provider="copilot", mcps=("remote",))
    files = compile_files(inputs, HOOK, STATUS, catalog=catalog).files
    assert json.loads(files["mcp.json"])["mcpServers"]["remote"] == {"type": "http", "url": "https://mcp.example.com/mcp",
                                                                      "tools": ["*"]}


def test_cursor_gets_skills_subagents_mcp_and_instructions_through_a_session_plugin(catalog):
    inputs, files = compiled("cursor", catalog)
    plugin = "cursor/acc"
    assert json.loads(files[f"{plugin}/.cursor-plugin/plugin.json"])["name"] == "acc"
    assert {f"{plugin}/skills/probe/SKILL.md", f"{plugin}/skills/probe/run.py", f"{plugin}/agents/helper.md"} <= set(files)
    assert not any("/other/" in name for name in files)
    agent = files[f"{plugin}/agents/helper.md"]
    assert "hooks" not in agent and "! `date`" in agent  # same safe_markdown as Claude's plugin
    # Plugin rules aren't applied by cursor-agent: the instructions come from the plugin's sessionStart hook
    hook = json.loads(files[f"{plugin}/hooks/hooks.json"])["hooks"]["sessionStart"]
    assert hook == [{"command": 'sed "s|@PLUGIN_ROOT@|$CURSOR_PLUGIN_ROOT|g" "$CURSOR_PLUGIN_ROOT/session-start.json"',
                     "timeout": 10}]
    context = json.loads(files[f"{plugin}/session-start.json"])["additional_context"]
    assert profile_text(context).startswith("# Rynhult profile: Probe") and "Probe instructions." in context
    assert profile_text(files[f"{plugin}/instructions.md"]).startswith("# Rynhult profile: Probe")
    servers = json.loads(files[f"{plugin}/.mcp.json"])["mcpServers"]
    assert servers == {"tool": {"command": "/opt/tool-mcp", "args": ["--stdio"], "env": {"TOOL_TOKEN": "acc-secret:tool/TOOL_TOKEN"}}}
    written = materialize(files, catalog, "cursor")
    assert json.loads(written[f"{plugin}/.mcp.json"])["mcpServers"]["tool"]["env"] == {"TOOL_TOKEN": SECRET}
    for resume in (False, True):  # the stored plugin is passed again on every resume
        args = argv(inputs, files, resume=resume)
        assert args[args.index("--plugin-dir") + 1] == "/r/cursor/acc"
        assert ("--resume" in args) == resume
        assert "--approve-mcps" not in args  # each MCP tool call is approved on screen ("Run this MCP tool?")
    assert compiler.session_environment(inputs, catalog, "/r", files) == {}


def test_cursor_long_instructions_are_pointed_to(catalog):
    inputs = LaunchInputs(provider="cursor", append_prompt="x" * 10_001)
    files = compile_files(inputs, HOOK, STATUS, catalog=catalog).files
    context = json.loads(files["cursor/acc/session-start.json"])["additional_context"]
    assert "Read @PLUGIN_ROOT@/instructions.md" in context  # the hook puts the plugin's folder in
    assert profile_text(files["cursor/acc/instructions.md"]) == "x" * 10_001 + "\n"


def test_cursor_remote_mcp_server_and_approval_prompt(catalog):
    files = compile_files(LaunchInputs(provider="cursor", mcps=("remote",)), HOOK, STATUS, catalog=catalog).files
    assert json.loads(files["cursor/acc/.mcp.json"])["mcpServers"]["remote"] == {"url": "https://mcp.example.com/mcp"}
    assert "cursor/acc/hooks/hooks.json" in files  # Mandatory core needs its actual instruction transport.
    assert "Rynhult mandatory core" in files["cursor/acc/session-start.json"]
    screen = "  plugin-acc-tool: lookup\n Run this MCP tool?\n  → Run (once) (y)\n"
    assert providers.CURSOR.screen_prompt(screen) == "plugin-acc-tool: lookup"


def test_antigravity_gets_skills_subagents_mcp_and_instructions_through_a_session_plugin(catalog):
    inputs, files = compiled("antigravity", catalog)
    plugin = "antigravity/.agents/plugins/acc"
    assert json.loads(files[f"{plugin}/plugin.json"]) == {"name": "acc"}
    assert profile_text(files[f"{plugin}/rules/AGENTS.md"]).startswith("# Rynhult profile: Probe")
    assert {f"{plugin}/skills/probe/SKILL.md", f"{plugin}/skills/probe/run.py"} <= set(files)
    assert not any("/other/" in name for name in files)
    # agy refuses to run a subagent with Claude's tools/model keys: name and description only
    assert files[f"{plugin}/agents/helper.md"] == "---\nname: helper\ndescription: a probe subagent\n---\nHelp. ! `date`\n"
    servers = json.loads(files[f"{plugin}/mcp_config.json"])["mcpServers"]
    assert servers == {"tool": {"command": "/opt/tool-mcp", "args": ["--stdio"], "env": {"TOOL_TOKEN": "acc-secret:tool/TOOL_TOKEN"}}}
    written = materialize(files, catalog, "antigravity")
    assert json.loads(written[f"{plugin}/mcp_config.json"])["mcpServers"]["tool"]["env"] == {"TOOL_TOKEN": SECRET}
    for resume in (False, True):  # --add-dir makes the folder a customization root, at launch and with --conversation
        args = argv(inputs, files, resume=resume)
        assert args[args.index("--add-dir") + 1] == "/r/antigravity"
        assert ("--conversation" in args) == resume
    assert compiler.session_environment(inputs, catalog, "/r", files) == {}


def test_opencode_gets_skills_subagents_mcp_and_instructions_in_its_config_folder(catalog):
    inputs, files = compiled("opencode", catalog)
    assert profile_text(files["opencode/AGENTS.md"]).startswith("# Rynhult profile: Probe") and "Probe instructions." in files["opencode/AGENTS.md"]
    assert {"opencode/skills/probe/SKILL.md", "opencode/skills/probe/run.py"} <= set(files)
    assert not any("/other/" in name for name in files)
    # OpenCode's agent fields: Claude's tools/model (and the hooks the catalog never passes) are left out
    assert files["opencode/agents/helper.md"] == "---\ndescription: a probe subagent\nmode: subagent\n---\nHelp. ! `date`\n"
    servers = json.loads(files["opencode-mcp.json"])["mcp"]["servers"]
    assert servers == {"tool": {"type": "local", "command": ["/opt/tool-mcp", "--stdio"],
                                "environment": {"TOOL_TOKEN": "acc-secret:tool/TOOL_TOKEN"}}}
    written = materialize(files, catalog, "opencode")
    assert json.loads(written["opencode-mcp.json"])["mcp"]["servers"]["tool"]["environment"] == {"TOOL_TOKEN": SECRET}
    assert providers.OPENCODE.secret_files(files) == ["opencode-mcp.json"]  # removed when the session stops
    for resume in (False, True):  # the same files and flags at launch and resume: the folder carries everything
        assert argv(inputs, files, resume=resume) == ["--standalone", "--session", SID]
    env = compiler.session_environment(inputs, catalog, "/r", files, native_id=SID)
    assert env["OPENCODE_CONFIG_DIR"] == "/r/opencode" and env["OPENCODE_CONFIG"] == "/r/opencode-mcp.json"
    assert SECRET not in json.dumps(env)  # the secret is in the 0600 file, not the environment


def test_opencode_remote_mcp_server_and_no_items(catalog):
    from acc.providers.opencode import _mcp
    servers = _mcp(LaunchInputs(provider="opencode", mcps=("remote",)), catalog)["mcp"]["servers"]
    assert servers == {"remote": {"type": "remote", "url": "https://mcp.example.com/mcp"}}
    files = compile_files(LaunchInputs(provider="opencode"), HOOK, STATUS, catalog=catalog).files
    assert set(files) == {"opencode/opencode.json", "opencode/plugins/acc/index.js", "opencode/plugins/acc/package.json",
                          "opencode/bin/bash", "opencode/AGENTS.md"}  # Guard and mandatory core come with every session.
    assert "Rynhult mandatory core" in files["opencode/AGENTS.md"]


@pytest.mark.parametrize("provider", ["antigravity", "cursor"])
def test_antigravity_and_cursor_without_catalog_items_get_the_mandatory_core_plugin(catalog, provider):
    files = compile_files(LaunchInputs(provider=provider), HOOK, STATUS, catalog=catalog).files
    assert "Rynhult mandatory core" in "\n".join(files.values())
    assert not any("/skills/" in name or "/agents/" in name for name in files)
    assert {"--add-dir", "--plugin-dir"} & set(argv(LaunchInputs(provider=provider), files))


@pytest.mark.parametrize("provider", ALL)
def test_secrets_never_stored_in_the_launch_spec(catalog, provider):
    _, files = compiled(provider, catalog)
    assert SECRET not in json.dumps(files)


# ---------------------------------------------------------------------------------------------- what is left out


@pytest.mark.parametrize("provider, expected", [
    ("claude", []),
    ("codex", [("agent", "helper", "Codex can't receive subagents from Rynhult")]),
    ("copilot", [("agent", "helper", "GitHub Copilot CLI can't receive subagents from Rynhult")]),
    ("antigravity", []),
    ("cursor", []),
    ("opencode", []),
])
def test_what_a_provider_wont_receive_is_listed_with_a_reason(catalog, provider, expected):
    left = not_delivered(launch(provider, catalog), catalog)
    assert [(x["kind"], x["id"], x["reason"]) for x in left] == expected


def test_capabilities_match_what_compile_writes():
    """A provider claims a capability only if its compile step delivers it (and the dialog only offers those)."""
    for provider in providers.PROVIDERS.values():
        writes = type(provider).compile is not providers.Provider.compile
        assert writes == bool({"skills", "mcp", "instructions"} & provider.supports), provider.id
    assert {p.id for p in providers.PROVIDERS.values() if "agents" in p.supports} == {"claude", "antigravity", "cursor",
                                                                                      "opencode"}


async def test_preview_lists_what_the_provider_wont_receive(api, project):
    body = {"project_id": project["id"], "name": "c", "profile": "qa"}
    codex = (await api.post("/api/sessions/preview", json={**body, "provider": "codex"})).json()
    assert {x["kind"] for x in codex["not_delivered"]} == {"agent"}
    assert codex["inputs"]["agents"] == []
    for provider, root in (("cursor", "cursor/acc/"), ("antigravity", "antigravity/.agents/plugins/acc/")):
        preview = (await api.post("/api/sessions/preview", json={**body, "provider": provider})).json()
        assert preview["not_delivered"] == [], provider
        assert preview["inputs"]["skills"] and preview["inputs"]["agents"] and preview["inputs"]["mcps"]
        assert any(name.startswith(root) for name in preview["files"])
    claude = (await api.post("/api/sessions/preview", json={**body, "provider": "claude"})).json()
    assert claude["not_delivered"] == []
