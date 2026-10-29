"""Cursor CLI (`cursor-agent`, installed as `agent` too).

Checked against the CLI itself (2026.10.19, `--help`, `status`, `login`) and Cursor's hooks reference:
- options: --mode plan|ask (default: agent), --force/--yolo, --model; `--trust` skips the workspace trust prompt;
  resume with `--resume <chatId>` (ids learned from the hooks' conversation_id);
- hooks: ~/.cursor/hooks.json ({"version": 1, "hooks": {event: [{"command": ...}]}}), camelCase events with
  snake_case fields (conversation_id, hook_event_name); sessionStart, beforeSubmitPrompt, postToolUse, stop
  (status completed|aborted|error), sessionEnd. No permission or notification event: "needs you" comes from its
  approval prompt on screen ("Run this command?"; tier "standard"); forum reports say some hooks don't fire in the CLI on Linux, so a quiet screen also ends a turn;
- sign-in: `NO_OPEN_BROWSER=1 cursor-agent login` prints a cursor.com link and completes once approved in a browser
  (no code); `status` says "Not logged in" otherwise;
- catalog items (verified live 2026-10-22 with one-shot prompts): a session plugin, `--plugin-dir <run>/cursor/acc`
  (.cursor-plugin/plugin.json), carries skills/<id>/, agents/<id>.md (runs through its Task tool) and .mcp.json (MCP
  tools need approval on screen: "Run this MCP tool?"). Plugin rules/ and an AGENTS.md in an --add-dir are not
  applied by the CLI, so the profile's instructions come from the plugin's sessionStart hook as `additional_context`
  (at most 10,000 characters; longer instructions are pointed to instead). The account's ~/.cursor/mcp.json and
  third-party skill folders (~/.claude/skills, ~/.codex/skills) still apply: there is no per-session switch.
Labelled preview until the docs/PROVIDERS.md checklist passes.
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

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

HOOK_EVENTS = ("sessionStart", "beforeSubmitPrompt", "preToolUse", "postToolUse", "stop", "sessionEnd")
EVENT_NAMES = {"sessionStart": "SessionStart", "sessionEnd": "SessionEnd", "beforeSubmitPrompt": "UserPromptSubmit",
               "preToolUse": "PreToolUse", "postToolUse": "PostToolUse", "stop": "Stop"}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PLUGIN = "cursor/acc"  # <run dir>/cursor/acc: the session's plugin (its folder name prefixes MCP servers: plugin-acc-<id>)
SECRET_PLACEHOLDER = "acc-secret:{mcp}/{key}"
CONTEXT_LIMIT = 10_000  # sessionStart additional_context, in UTF-16 units (cursor-agent drops a longer one)
PLUGIN_ROOT = "@PLUGIN_ROOT@"  # replaced by the hook with the plugin's folder (unknown when compiling)
SESSION_START = f'sed "s|{PLUGIN_ROOT}|$CURSOR_PLUGIN_ROOT|g" "$CURSOR_PLUGIN_ROOT/session-start.json"'


def _session_start(instructions: str) -> str:
    """The sessionStart hook's output: the instructions themselves, or where to read them when they are too long."""
    context = instructions
    if len(instructions.encode("utf-16-le")) // 2 > CONTEXT_LIMIT:
        context = (f"Rynhult gave this session profile instructions that are too long to include here. Read "
                   f"{PLUGIN_ROOT}/instructions.md before you start, and follow them.")
    return json.dumps({"additional_context": context}) + "\n"


def _instruction_files(instructions: str) -> dict[str, str]:
    return {f"{PLUGIN}/instructions.md": instructions + "\n", f"{PLUGIN}/session-start.json": _session_start(instructions),
            f"{PLUGIN}/hooks/hooks.json": json.dumps({"version": 1, "hooks": {"sessionStart": [
                {"command": SESSION_START, "timeout": 10}]}}, indent=2) + "\n"}


def _manifest() -> str:
    return json.dumps({
        "name": "acc", "description": "What Rynhult's catalog gives this session (profile, skills, subagents, MCP)",
    }, indent=2) + "\n"


class Cursor(Provider):
    def compile(self, inputs, ctx: CompileContext, instructions: str) -> dict[str, str]:
        from ..server.compiler import safe_markdown
        catalog = ctx.catalog
        files: dict[str, str] = {}
        if instructions:
            files.update(_instruction_files(instructions))
        for skill_id in inputs.skills:
            for name, content in catalog.skills[skill_id].files.items():
                files[f"{PLUGIN}/skills/{skill_id}/{name}"] = safe_markdown(content) if name.endswith(".md") else content
        for agent_id in inputs.agents:
            files[f"{PLUGIN}/agents/{agent_id}.md"] = safe_markdown(catalog.agents[agent_id].files[f"{agent_id}.md"])
        if catalog is not None and inputs.mcps:
            config = mcp_servers_json(inputs, catalog, SECRET_PLACEHOLDER)
            for server in config["mcpServers"].values():
                server.pop("type", None)  # Cursor tells a remote server by its url
            files[f"{PLUGIN}/.mcp.json"] = json.dumps(config, indent=2) + "\n"
        if files:
            files[f"{PLUGIN}/.cursor-plugin/plugin.json"] = _manifest()
        return files

    def add_context(self, files, text):
        if not text:
            return files
        instructions = joined(files.get(f"{PLUGIN}/instructions.md"), text).rstrip("\n")
        return {**files, **_instruction_files(instructions),
                f"{PLUGIN}/.cursor-plugin/plugin.json": files.get(f"{PLUGIN}/.cursor-plugin/plugin.json") or _manifest()}

    def secret_files(self, files):
        return [name for name in (f"{PLUGIN}/.mcp.json",) if name in files]

    def materialize(self, files, catalog):
        name = f"{PLUGIN}/.mcp.json"
        if name not in files:
            return files
        config = fill_placeholders(json.loads(files[name]), catalog, SECRET_PLACEHOLDER)
        return {**files, name: json.dumps(config, indent=2) + "\n"}

    def args(self, inputs, *, run_dir, tmux_name, native_id, resume, files, extra):
        args = ["--trust"]  # Rynhult has already recorded that this folder is trusted
        if resume:
            if not native_id:
                raise CompileError("cannot resume a Cursor chat whose id was never reported")
            args += ["--resume", native_id]
        mode = self.option(inputs, "mode")
        if mode and mode != "agent":
            args += ["--mode", mode]
        if self.option(inputs, "force") is True:
            args += ["--force"]
        if inputs.model:
            args += ["--model", inputs.model]
        if any(name.startswith(f"{PLUGIN}/") for name in files):
            args += ["--plugin-dir", f"{run_dir}/{PLUGIN}"]
        return args + list(extra)

    def normalize_hook(self, hook: dict) -> dict:
        out = dict(hook)
        if isinstance(hook.get("conversation_id"), str):
            out["session_id"] = hook["conversation_id"]  # the chat id `--resume` takes
        name = out.get("hook_event_name")
        if name == "stop" and hook.get("status") == "error":
            out["hook_event_name"] = "StopFailure"
        elif isinstance(name, str):
            out["hook_event_name"] = EVENT_NAMES.get(name, name)
        if isinstance(out.get("tool_input"), str):
            try:
                out["tool_input"] = json.loads(out["tool_input"])
            except ValueError:
                out["tool_input"] = {"description": out["tool_input"][:200]}
        return out

    # ------------------------------------------------------------------ broker side

    def bootstrap(self, home: Path, hook_path: str, exe: str | None = None) -> None:
        """Rynhult's entries in ~/.cursor/hooks.json (recognised by their command); everyone else's are kept."""
        path = home / ".cursor" / "hooks.json"
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        hooks = config.get("hooks") if isinstance(config.get("hooks"), dict) else {}
        changed = config.get("version") != 1
        for event in HOOK_EVENTS:
            command = f"{shlex.quote(guard_path(hook_path) if event == 'preToolUse' else hook_path)} cursor {event}"
            entries = [e for e in hooks.get(event, []) if isinstance(e, dict)]
            kept = [e for e in entries if not re.search(r"acc-(hook|guard)['\"]? cursor", str(e.get("command", "")))]
            wanted = [*kept, {"command": command, "timeout": 10}]
            if entries != wanted:
                hooks[event], changed = wanted, True
        if not changed:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.acc-tmp")
        tmp.write_text(json.dumps({**config, "version": 1, "hooks": hooks}, indent=2) + "\n")
        tmp.replace(path)

    def transcript(self, home: Path, native_id: str) -> Path | None:
        """Cursor keeps chats under ~/.cursor (layout undocumented): anything named after the chat id counts."""
        root = home / ".cursor"
        if not root.is_dir():
            return None
        for depth in ("*", "*/*", "*/*/*", "*/*/*/*"):
            found = next((p for p in root.glob(depth) if native_id in p.name), None)
            if found:
                return found
        return None

    def messages_source(self, home: Path, native_id: str) -> Path | None:
        """~/.cursor/chats/<workspace hash>/<chat id>/store.db"""
        return next((home / ".cursor" / "chats").glob(f"*/{native_id}/store.db"), None)

    def messages(self, path: Path) -> list[dict]:
        return transcripts.cursor(path)

    def auth_health(self, exe: str, home: Path) -> dict:
        health: dict = {"logged_in": False}
        try:
            result = run([exe, "status"], timeout=20)
        except Exception as error:  # subprocess.TimeoutExpired
            return {**health, "error": type(error).__name__}
        text = ANSI.sub("", result.stdout + result.stderr)
        health["logged_in"] = result.returncode == 0 and "not logged in" not in text.lower()
        email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
        if health["logged_in"] and email:
            health["email"] = email.group(0)
        plan = re.search(r"(?im)^\s*(?:plan|membership|subscription)\s*[:\-]\s*(.+)$", text)
        if plan:
            health["plan"] = plan.group(1).strip()[:40]
        return health

    def login_argv(self, exe: str) -> list[str]:
        return [exe, "login"]  # with NO_OPEN_BROWSER (set by the broker's login flow env): prints a link

    def models(self, exe: str, home: Path) -> list[dict]:
        result = run([exe, "models"], timeout=30)
        text = ANSI.sub("", result.stdout)
        if result.returncode or "not logged in" in text.lower():
            return []
        rows = []
        for line in text.splitlines():
            match = re.match(r"^\s*[-*•]?\s*([A-Za-z0-9][A-Za-z0-9._:/\[\]=,-]{1,79})(?:\s+[-–(]\s*(.*?))?\)?\s*$", line)
            if match and match.group(1).lower() not in ("available", "models", "model"):
                rows.append({"id": match.group(1), "label": (match.group(2) or match.group(1))[:60], "description": "",
                             "source": "cursor-agent models"})
        return rows[:100]


PROVIDER = Cursor(
    id="cursor", label="Cursor CLI", short="Cursor", executable="cursor-agent", color="#a0a0a8", tier="standard",
    preview=True, install="Cursor's release packages (Providers → Agent software)",
    homepage="https://cursor.com/docs/cli/overview", usage_url="https://cursor.com/dashboard?tab=usage",
    login="link", session_id="learned", start_verify="settle", quiet_idle_seconds=90,
    prompt_patterns=(r"^\s*\$\s+(?P<what>.+?)(?:\s+in \S+)?\s*\n\s*Run this command\?",
                     r"^\s*(?P<what>\S+: \S+)\s*\n\s*Run this MCP tool\?",
                     r"Run this (?:MCP tool|command)\?|Waiting for approval"),
    supports=frozenset({"resume", "models", "instructions", "skills", "agents", "mcp"}),
    options=(
        Option("mode", "Mode", choices=(("agent", "Agent"), ("plan", "Plan (read-only)"), ("ask", "Ask (read-only Q&A)")),
               default="agent"),
        Option("force", "Run everything without asking ⚠", kind="bool", default=False, help="--force",
               dangerous=(True,)),
    ),
    hook_events=HOOK_EVENTS,
)
