"""Google Antigravity CLI (`agy`), Google's successor to Gemini CLI for subscription users.

Checked against the CLI itself (1.4.4, 2026-10-21) and Google's hooks reference:
- options: --mode accept-edits|auto|plan, --dangerously-skip-permissions, --model, --effort; resume with
  `--conversation <id>` (ids can't be chosen up front: learned from the SessionStart hook's conversationId);
- hooks: ~/.gemini/config/hooks.json, named groups of SessionStart / PreToolUse / PostToolUse / PreInvocation /
  PostInvocation / Stop with camelCase payloads (conversationId, transcriptPath, fullyIdle). Per agy's built-in hooks
  reference, the tool events take `matcher` groups and the others a flat list of handlers; a handler must print a JSON
  object (acc-hook prints `{}` for antigravity). A group in the wrong shape loads but never runs (verified 1.4.4); There is no permission,
  notification or session-end event: "needs you" comes from its approval prompt on screen ("Requesting permission
  for:"; tier "standard");
- trust: the folder prompt's answer goes to `trustedWorkspaces` in ~/.gemini/antigravity-cli/settings.json;
- conversations live in ~/.gemini/antigravity-cli/{brain/<id>, conversations/};
- sign-in: a prompt (`agy -p …`) prints a Google URL and waits up to 60 s for the authorization code to be pasted
  back, but only when stdin is a terminal (a pipe gets "Run 'agy' to log in"); `agy models` only says "Please sign
  in" (verified 2026-10-21). Without a desktop Secret Service, tokens go to ~/.config/antigravity/plugin-secrets.json;
- catalog items (verified live 2026-10-22 with one-shot prompts, at launch and with --conversation): `--add-dir
  <run>/antigravity` makes that folder a customization root, so its plugin .agents/plugins/acc/ (plugin.json) is
  loaded: rules/AGENTS.md (the profile's instructions, capped by agy at 24 KB), skills/<id>/, agents/<id>.md and
  mcp_config.json (servers named acc_<id>). A subagent whose frontmatter carries Claude's `tools`/`model` is listed
  but refused ("not found or not allowed to be invoked"), so agents get only a name and a description. The account's
  ~/.gemini/config (MCP servers, rules, skills) still applies: there is no per-session switch.
Labelled preview until the docs/PROVIDERS.md checklist passes.
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import yaml

from . import transcripts
from .base import (
    CompileContext,
    CompileError,
    Option,
    Provider,
    fill_placeholders,
    guard_path,
    joined,
    mcp_servers_json,
    run,
)

HOOK_EVENTS = ("SessionStart", "PreInvocation", "PreToolUse", "PostToolUse", "Stop")
EVENT_NAMES = {"PreInvocation": "UserPromptSubmit", "PostInvocation": "PostInvocation"}
HOOKS_KEY = "acc"  # ACC's named group in hooks.json (others' groups are left alone)
TOOL_EVENTS = ("PreToolUse", "PostToolUse")  # matched by tool name, so wrapped in {"matcher", "hooks"} groups
ROOT = "antigravity"  # <run dir>/antigravity: the customization root passed with --add-dir
PLUGIN = f"{ROOT}/.agents/plugins/acc"
SECRET_PLACEHOLDER = "acc-secret:{mcp}/{key}"


def _agent(content: str, agent_id: str) -> str:
    """A catalog subagent as agy runs it: name and description only (agy refuses one with Claude's tools/model)."""
    from ..server.catalog_import import parse_frontmatter
    from ..server.compiler import safe_markdown
    parsed = parse_frontmatter(safe_markdown(content))
    meta = {"name": agent_id, "description": str(parsed.meta.get("description") or agent_id)}
    return f"---\n{yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1 << 30)}---\n{parsed.body}"


class Antigravity(Provider):
    def compile(self, inputs, ctx: CompileContext, instructions: str) -> dict[str, str]:
        from ..server.compiler import safe_markdown
        catalog = ctx.catalog
        files: dict[str, str] = {}
        if instructions:
            files[f"{PLUGIN}/rules/AGENTS.md"] = instructions + "\n"
        for skill_id in inputs.skills:
            for name, content in catalog.skills[skill_id].files.items():
                files[f"{PLUGIN}/skills/{skill_id}/{name}"] = safe_markdown(content) if name.endswith(".md") else content
        for agent_id in inputs.agents:
            files[f"{PLUGIN}/agents/{agent_id}.md"] = _agent(catalog.agents[agent_id].files[f"{agent_id}.md"], agent_id)
        if catalog is not None and inputs.mcps:
            config = mcp_servers_json(inputs, catalog, SECRET_PLACEHOLDER)
            for server in config["mcpServers"].values():
                server.pop("type", None)  # agy tells a remote server by its url
            files[f"{PLUGIN}/mcp_config.json"] = json.dumps(config, indent=2) + "\n"
        if files:
            files[f"{PLUGIN}/plugin.json"] = json.dumps({"name": "acc"}, indent=2) + "\n"
        return files

    def add_context(self, files, text):
        if not text:
            return files
        rules = f"{PLUGIN}/rules/AGENTS.md"
        return {**files, rules: joined(files.get(rules), text),
                f"{PLUGIN}/plugin.json": files.get(f"{PLUGIN}/plugin.json") or json.dumps({"name": "acc"}, indent=2) + "\n"}

    def secret_files(self, files):
        return [name for name in (f"{PLUGIN}/mcp_config.json",) if name in files]

    def materialize(self, files, catalog):
        name = f"{PLUGIN}/mcp_config.json"
        if name not in files:
            return files
        config = fill_placeholders(json.loads(files[name]), catalog, SECRET_PLACEHOLDER)
        return {**files, name: json.dumps(config, indent=2) + "\n"}

    def args(self, inputs, *, run_dir, tmux_name, native_id, resume, files, extra):
        args = []
        if resume:
            if not native_id:
                raise CompileError("cannot resume an Antigravity conversation whose id was never reported")
            args += ["--conversation", native_id]
        mode = self.option(inputs, "mode")
        if mode:
            args += ["--mode", mode]
        if self.option(inputs, "skip_permissions") is True:
            args += ["--dangerously-skip-permissions"]
        if inputs.model:
            args += ["--model", inputs.model]
        if inputs.effort:
            args += ["--effort", inputs.effort]
        if any(name.startswith(f"{ROOT}/") for name in files):
            args += ["--add-dir", f"{run_dir}/{ROOT}"]
        return args + list(extra)

    def normalize_hook(self, hook: dict) -> dict:
        out = dict(hook)
        if isinstance(hook.get("conversationId"), str):
            out["session_id"] = hook["conversationId"]
        if isinstance(hook.get("transcriptPath"), str):
            out["transcript_path"] = hook["transcriptPath"]
        name = out.get("hook_event_name")
        if name == "Stop":
            if hook.get("error"):
                out["hook_event_name"] = "StopFailure"
            elif hook.get("fullyIdle") is False:  # one execution ended, the turn goes on
                out["hook_event_name"] = "PostInvocation"
        elif isinstance(name, str):
            out["hook_event_name"] = EVENT_NAMES.get(name, name)
        call = hook.get("toolCall")
        if isinstance(call, dict):
            out["tool_name"] = call.get("name")
            out["tool_input"] = call.get("args") if isinstance(call.get("args"), dict) else {}
        return out

    # ------------------------------------------------------------------ broker side

    def bootstrap(self, home: Path, hook_path: str, exe: str | None = None) -> None:
        path = home / ".gemini" / "config" / "hooks.json"
        try:
            hooks = json.loads(path.read_text())
        except (OSError, ValueError):
            hooks = {}
        if not isinstance(hooks, dict):
            hooks = {}
        ours = {}
        for event in HOOK_EVENTS:
            script = guard_path(hook_path) if event == "PreToolUse" else hook_path
            handler = {"type": "command", "command": f"{shlex.quote(script)} antigravity {event}", "timeout": 10}
            ours[event] = [{"matcher": "", "hooks": [handler]}] if event in TOOL_EVENTS else [handler]
        if hooks.get(HOOKS_KEY) == ours:
            return
        hooks[HOOKS_KEY] = ours
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.acc-tmp")
        tmp.write_text(json.dumps(hooks, indent=2) + "\n")
        tmp.replace(path)

    def trust(self, home: Path, workspace: str, exe: str | None = None) -> None:
        if not exe:
            return
        path = home / ".gemini" / "antigravity-cli" / "settings.json"
        try:
            settings = json.loads(path.read_text())
        except (OSError, ValueError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        folders = settings.get("trustedWorkspaces") if isinstance(settings.get("trustedWorkspaces"), list) else []
        if workspace in folders:
            return
        settings["trustedWorkspaces"] = [*folders, workspace]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.acc-tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n")
        tmp.chmod(0o600)
        tmp.replace(path)

    def transcript(self, home: Path, native_id: str) -> Path | None:
        root = home / ".gemini" / "antigravity-cli"
        brain = root / "brain" / native_id
        if brain.exists():
            return brain
        return next((p for p in (root / "conversations").glob(f"*{native_id}*")), None) \
            if (root / "conversations").is_dir() else None

    def messages_source(self, home: Path, native_id: str) -> Path | None:
        path = home / ".gemini" / "antigravity-cli" / "brain" / native_id / ".system_generated" / "logs" / "transcript.jsonl"
        return path if path.is_file() else None

    def messages(self, path: Path) -> list[dict]:
        return transcripts.antigravity(path)

    def auth_health(self, exe: str, home: Path) -> dict:
        """`agy models` lists models when signed in; otherwise it prints a sign-in URL (stdin closed: it gives up)."""
        health: dict = {"logged_in": False}
        try:
            result = run([exe, "models"], timeout=8)
        except Exception:  # subprocess.TimeoutExpired: it is waiting for a sign-in
            return {**health, "status": "sign-in needed"}
        text = result.stdout + result.stderr
        health["logged_in"] = result.returncode == 0 and "Authentication required" not in text \
            and "sign in" not in text.lower()
        version = run([exe, "--version"], timeout=20).stdout.strip().splitlines()
        if version:
            health["version"] = version[0][:40]
        return health

    def login_argv(self, exe: str) -> list[str]:
        """A one-line prompt: it needs an account, so agy prints the sign-in URL and reads the pasted code, then
        answers (`agy models` only says to sign in interactively)."""
        return [exe, "-p", "Reply with OK.", "--print-timeout", "120s"]

    def models(self, exe: str, home: Path) -> list[dict]:
        result = run([exe, "models"], timeout=30)
        if result.returncode or "Authentication required" in result.stdout or "sign in" in result.stdout.lower():
            return []
        rows = []
        for line in result.stdout.splitlines():
            if not line.strip() or line.rstrip().endswith("...") or line.strip().lower().startswith(("error", "fetching")):
                continue
            token = line.strip().lstrip("-*• ").split()[0]
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{1,79}", token) and re.search(r"[\d-]", token):
                rows.append({"id": token, "label": line.strip()[:80], "description": "", "source": "agy models"})
        return rows[:100]


PROVIDER = Antigravity(
    id="antigravity", label="Google Antigravity CLI", short="Antigravity", executable="agy", color="#4285f4",
    tier="standard", preview=True, install="Google's installer (Providers → Agent software)",
    homepage="https://antigravity.google/docs/getting-started?tab=cli", usage_url="https://one.google.com/",
    login="paste", login_tty=True, prompt_flag="-i", session_id="learned", start_verify="settle", quiet_idle_seconds=90,
    prompt_patterns=(r"Requesting permission for:\s*\n\s*(?P<what>.+)", r"Run this command\?"),
    supports=frozenset({"resume", "effort", "models", "instructions", "skills", "agents", "mcp"}),
    options=(
        Option("mode", "Mode", choices=(("accept-edits", "Accept edits"), ("auto", "Auto"), ("plan", "Plan only")),
               default="accept-edits"),
        Option("skip_permissions", "Skip all permission prompts ⚠", kind="bool", default=False,
               help="--dangerously-skip-permissions", dangerous=(True,)),
    ),
    hook_events=HOOK_EVENTS,
)
