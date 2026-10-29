"""OpenCode (`opencode`, npm @opencode/cli 2.x): design in docs/providers/OPENCODE.md.

Checked against OpenCode 2.1.3 (its help, docs and source at v2.1.3, and live runs against a scripted fake model):
- `--standalone` runs a private server for this TUI only (`opencode serve --stdio --port 0`, ended with the TUI), never
  the account's shared background service; `--session <id>` resumes that session or creates it with that id on the
  first prompt (ids start with `ses`), so Rynhult assigns ids; `--prompt` is a first prompt; `--auto` approves whatever
  isn't denied; Escape twice interrupts a turn;
- the session's own config folder (`OPENCODE_CONFIG_DIR=<run>/opencode`: opencode.json, AGENTS.md, skills/, agents/,
  plugins/) replaces ~/.config/opencode; `OPENCODE_CONFIG_CONTENT` outranks the project's files; `OPENCODE_CONFIG`
  adds one more file (the MCP servers, with secrets);
- Rynhult's plugin (opencode_plugin.js, as plugins/acc/index.js) runs acc-guard before every tool call and reports the session's
  events. A repository can switch a plugin off (`"plugins": ["-acc"]` or an `integration.use` deny, both seen live):
  the inline config lists it again by its path, and the session's global config allows it (global policies outrank
  project ones). Its shell (bin/bash) refuses commands the plugin didn't mark, so a plugin that fails to load stops
  shell commands instead of letting them through;
- sessions and saved provider accounts live in one SQLite database, ~/.local/share/opencode/opencode.db (WAL), which
  every session's server shares; providers' API keys can also come from the environment (the vault).
"""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

from acc.env import environment

from . import transcripts
from .base import CompileContext, CompileError, Option, Provider, endpoint_of, guard_path, joined, run

CONFIG_DIR = "opencode"  # <run dir>/opencode: OPENCODE_CONFIG_DIR
CONFIG = f"{CONFIG_DIR}/opencode.json"
INSTRUCTIONS = f"{CONFIG_DIR}/AGENTS.md"
# Rynhult's plugin, a folder: the inline config names it by its absolute path (a bare name such as "acc" that matches no
# loaded plugin is an npm package to OpenCode, which it would download and run)
PLUGIN_DIR = f"{CONFIG_DIR}/plugins/acc"
PLUGIN = f"{PLUGIN_DIR}/index.js"
PLUGIN_PACKAGE = f"{PLUGIN_DIR}/package.json"
SHELL = f"{CONFIG_DIR}/bin/bash"  # the session's shell: runs a command only when Rynhult's plugin marked it (guarded)
MCP = "opencode-mcp.json"  # OPENCODE_CONFIG: holds MCP secrets, so it lives outside the config folder and is removed at stop
SECRET_PLACEHOLDER = "acc-secret:{mcp}/{key}"
ENDPOINT_PROVIDER = "acc"  # a Rynhult model endpoint as an OpenCode provider: models are acc/<model>
ENDPOINT_PACKAGES = {"openai": "@opencode/ai/providers/openai-compatible",
                     "anthropic": "@opencode/ai/providers/anthropic-compatible"}
DATABASE = Path(".local/share/opencode/opencode.db")  # under the agent account's home (release channel "latest")
HOOK_EVENTS = ("acc.ready", "session.execution.started", "session.execution.succeeded", "session.execution.failed",
               "session.execution.interrupted", "permission.asked", "permission.replied", "form.created",
               "form.replied", "form.cancelled", "PreToolUse", "tool.after", "acc.unload")
EVENT_NAMES = {"acc.ready": "SessionStart", "acc.unload": "SessionEnd", "session.execution.started": "UserPromptSubmit",
               "session.execution.succeeded": "Stop", "session.execution.failed": "StopFailure",
               "session.execution.interrupted": "Interrupt", "permission.asked": "PermissionRequest",
               "permission.replied": "PermissionReplied", "form.replied": "PermissionReplied",
               "form.cancelled": "PermissionReplied", "tool.after": "PostToolUse"}
PERMISSIONS = {  # the Permissions option -> rules in the inline config (outranks the project's own)
    "commands": [{"action": "shell", "resource": "*", "effect": "ask"},
                 {"action": "external_directory", "resource": "*", "effect": "ask"}],
    "all": [{"action": "shell", "resource": "*", "effect": "ask"},
            {"action": "edit", "resource": "*", "effect": "ask"},
            {"action": "external_directory", "resource": "*", "effect": "ask"}],
    "auto": [],  # --auto: anything not denied
}
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@#\[\]+-]*/[A-Za-z0-9._:@#/\[\]+-]{1,120}")


SHELL_SCRIPT = """#!/bin/sh
# Rynhult: the shell OpenCode runs this session's commands with (its "shell" setting). A command (-c) runs only when Rynhult's
# plugin marked it (ACC_OPENCODE_GUARDED, set as OpenCode creates the shell, after acc-guard checked the tool call), so
# if the plugin didn't load nothing runs unchecked. Interactive shells (the person's own terminal) start as usual.
if [ "$1" = "-c" ] && [ "${RYNHULT_OPENCODE_GUARDED-${ACC_OPENCODE_GUARDED:-}}" != 1 ]; then
  echo "Rynhult's guardrails aren't running in this OpenCode session (its plugin didn't load), so shell commands are" \\
    "refused. Restart the session from Rynhult; if it happens again, this OpenCode version may be newer than Rynhult supports." >&2
  exit 126
fi
exec /bin/bash "$@"
"""


def plugin_source(hook_path: str) -> str:
    """Rynhult's plugin with this machine's acc-guard filled in."""
    template = (Path(__file__).with_name("opencode_plugin.js")).read_text()
    template = template.replace("@ACC_ENV_HELPER@", Path(__file__).parents[1].joinpath("env.mjs").read_text().replace("export function", "function"))
    return template.replace("@ACC_GUARD@", guard_path(hook_path).replace("\\", "\\\\").replace('"', '\\"'))


def _agent(content: str, agent_id: str) -> str:
    """A catalog subagent as OpenCode reads it: its description, `mode: subagent` and the prompt. Claude's `tools` and
    `model` aren't OpenCode agent fields (a model there must be OpenCode's provider/model), so they're left out."""
    import yaml

    from ..server.catalog_import import parse_frontmatter
    from ..server.compiler import safe_markdown
    parsed = parse_frontmatter(safe_markdown(content))
    meta = {"description": str(parsed.meta.get("description") or agent_id), "mode": "subagent"}
    return f"---\n{yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1 << 30)}---\n{parsed.body}"


def _mcp(inputs, catalog) -> dict:
    servers = {}
    for mcp_id in inputs.mcps if catalog else ():
        meta = catalog.mcps[mcp_id].meta
        if meta.get("url"):
            servers[mcp_id] = {"type": "remote", "url": meta["url"]}
        else:
            servers[mcp_id] = {"type": "local", "command": [meta["command"], *meta.get("args", [])],
                               "environment": {key: SECRET_PLACEHOLDER.format(mcp=mcp_id, key=key)
                                               for key in meta.get("env", {})}}
    return {"mcp": {"servers": servers}}


def _config(inputs) -> dict:
    """The session's global config (opencode.json). The plugin's own path is only known once the session has a run
    folder, so the policy that keeps it allowed names it through $ACC_RUN_DIR (OpenCode's {env:…} substitution)."""
    config: dict = {
        "$schema": "https://opencode.ai/config.json",
        "update": "disable",  # acc-packages updates OpenCode (pinned), never OpenCode itself
        "policies": [{"action": "integration.use", "resource": "plugin:{env:ACC_RUN_DIR}/" + PLUGIN_DIR, "effect": "allow"}],
    }
    agent = (inputs.options or {}).get("agent") or "build"
    if agent != "build":
        config["default_agent"] = agent
    if inputs.model:
        config["model"] = f"{ENDPOINT_PROVIDER}/{inputs.model}" if inputs.endpoint else inputs.model
    return config


def _endpoint_provider(config: dict, endpoint: dict) -> dict:
    """A Rynhult model endpoint as OpenCode's custom provider `acc` (its key from $ACC_ENDPOINT_API_KEY)."""
    model = str(config.get("model") or "").removeprefix(f"{ENDPOINT_PROVIDER}/")
    entry: dict = {"name": model}
    if endpoint.get("context_tokens"):
        entry["limit"] = {"context": int(endpoint["context_tokens"])}
    return {**config, "providers": {ENDPOINT_PROVIDER: {
        "name": f"{endpoint['name']} (Rynhult model endpoint)", "env": ["ACC_ENDPOINT_API_KEY"],
        "package": ENDPOINT_PACKAGES[endpoint["style"]], "settings": {"baseURL": endpoint["url"]},
        "models": {model: entry}}}}


class OpenCode(Provider):
    def validate(self, inputs) -> None:
        super().validate(inputs)
        if inputs.model and not inputs.endpoint and not MODEL_ID.fullmatch(inputs.model):
            raise CompileError("OpenCode models are written provider/model (for example anthropic/claude-sonnet-4-5)")

    def compile(self, inputs, ctx: CompileContext, instructions: str) -> dict[str, str]:
        from ..server.compiler import safe_markdown
        catalog = ctx.catalog
        files = {CONFIG: json.dumps(_config(inputs), indent=2) + "\n", PLUGIN: plugin_source(ctx.hook_path),
                 PLUGIN_PACKAGE: json.dumps({"name": "acc-session-plugin", "private": True, "type": "module"}) + "\n",
                 SHELL: SHELL_SCRIPT}
        if instructions:
            files[INSTRUCTIONS] = instructions + "\n"
        for skill_id in inputs.skills:
            for name, content in catalog.skills[skill_id].files.items():
                files[f"{CONFIG_DIR}/skills/{skill_id}/{name}"] = safe_markdown(content) if name.endswith(".md") else content
        for agent_id in inputs.agents:
            files[f"{CONFIG_DIR}/agents/{agent_id}.md"] = _agent(catalog.agents[agent_id].files[f"{agent_id}.md"], agent_id)
        if catalog is not None and inputs.mcps:
            files[MCP] = json.dumps(_mcp(inputs, catalog), indent=2) + "\n"
        return files

    def args(self, inputs, *, run_dir, tmux_name, native_id, resume, files, extra):
        if not native_id:
            raise CompileError("OpenCode sessions need a pre-assigned session id")
        args = ["--standalone", "--session", native_id]  # a private server; the same flag starts or resumes it
        if self.option(inputs, "permissions") == "auto":
            args += ["--auto"]
        return args + list(extra)

    def env(self, inputs, catalog, run_dir="", files=None, endpoint_key=None, native_id=None):
        files = files or {}
        inline = {"plugins": [f"{run_dir}/{PLUGIN_DIR}"], "shell": f"{run_dir}/{SHELL}",
                  "permissions": PERMISSIONS[self.option(inputs, "permissions")]}
        env = {"OPENCODE_CONFIG_DIR": f"{run_dir}/{CONFIG_DIR}", "OPENCODE_CONFIG_CONTENT": json.dumps(inline),
               "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_CLAUDE_CODE": "1"}
        if MCP in files:
            team_permissions = json.loads(files[MCP]).get("permissions", [])
            if team_permissions:
                inline["permissions"] = [*inline["permissions"], *team_permissions]
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
            env["OPENCODE_CONFIG"] = f"{run_dir}/{MCP}"
        if native_id:
            env["ACC_OPENCODE_SESSION"] = native_id  # the plugin reports for this session (not its subagents')
        if endpoint_of(files):  # the acc provider reads its key from here; local servers ignore it
            env["ACC_ENDPOINT_API_KEY"] = endpoint_key or "acc-local-endpoint"
        return env

    def add_context(self, files, text):
        return {**files, INSTRUCTIONS: joined(files.get(INSTRUCTIONS), text)} if text else files

    def secret_files(self, files):
        return [name for name in (MCP,) if name in files]

    def materialize(self, files, catalog):
        """The files as written: a model endpoint (endpoint.json, added after compile) joins the config as the `acc`
        provider, and MCP secrets are filled in."""
        out = dict(files)
        endpoint = endpoint_of(files)
        if endpoint and CONFIG in files:
            out[CONFIG] = json.dumps(_endpoint_provider(json.loads(files[CONFIG]), endpoint), indent=2) + "\n"
        if MCP in files:
            config = json.loads(files[MCP])
            for mcp_id, server in config.get("mcp", {}).get("servers", {}).items():
                item = catalog.mcps.get(mcp_id) if catalog else None
                values = (item.meta.get("env") or {}) if item else {}
                env = server.get("environment") or {}
                for key, value in env.items():
                    if value == SECRET_PLACEHOLDER.format(mcp=mcp_id, key=key):
                        env[key] = str(values.get(key, ""))
            out[MCP] = json.dumps(config, indent=2) + "\n"
        return out

    def preview_native_id(self) -> str | None:
        return "ses_<session-id>"

    def new_native_id(self) -> str:
        """An id in OpenCode's own shape (ses_ + 12 hex digits of time, newest first + 14 random letters and digits)."""
        value = ~((int(time.time() * 1000) * 0x1000 + 1)) & ((1 << 48) - 1)
        return f"ses_{value:012x}" + "".join(secrets.choice(BASE62) for _ in range(14))

    def normalize_hook(self, hook: dict) -> dict:
        out = dict(hook)
        name = hook.get("hook_event_name")
        if isinstance(hook.get("sessionID"), str) and not isinstance(hook.get("session_id"), str):
            out["session_id"] = hook["sessionID"]
        if name == "permission.asked":
            action = str(hook.get("action") or "tool")
            resources = [r for r in hook.get("resources") or [] if isinstance(r, str)]
            what = ", ".join(resources)[:300]
            key = {"shell": "command", "edit": "file_path", "read": "file_path", "webfetch": "url"}.get(action, "description")
            out.update(tool_name=action, tool_input={key: what} if what else {})
        elif name == "form.created":
            form = hook.get("form") if isinstance(hook.get("form"), dict) else {}
            fields = form.get("fields") if isinstance(form.get("fields"), list) else []
            title = next((f.get("title") for f in fields if isinstance(f, dict) and isinstance(f.get("title"), str)), None)
            out.update(hook_event_name="Notification", notification_type="agent_needs_input",
                       message=f"asks: {title or form.get('title') or 'a question'}"[:300], session_id=form.get("sessionID"))
            return out
        elif name == "session.execution.failed":
            error = hook.get("error")
            out["error"] = (error.get("message") if isinstance(error, dict) else error) or "the turn failed"
        if isinstance(name, str):
            out["hook_event_name"] = EVENT_NAMES.get(name, name)
        return out

    # ------------------------------------------------------------------ broker side

    @staticmethod
    def _database(home: Path) -> Path:
        data = environment.get("XDG_DATA_HOME")
        return (Path(data) / "opencode" / "opencode.db") if data else home / DATABASE

    def transcript(self, home: Path, native_id: str) -> Path | None:
        path = self._database(home)
        return path if transcripts.opencode_has(path, native_id) else None

    def messages_source(self, home: Path, native_id: str) -> Path | None:
        return self.transcript(home, native_id)

    def session_messages(self, path: Path, native_id: str) -> list[dict]:
        return transcripts.opencode(path, native_id)

    def auth_health(self, exe: str, home: Path) -> dict:
        """No single vendor sign-in: "ready" when its server answers, with the providers it can reach (saved accounts
        and keys in the environment; free models need neither)."""
        health: dict = {"logged_in": False}
        try:
            result = run([exe, "auth", "list", "--standalone", "--format", "json"], timeout=40)
        except Exception as error:  # subprocess.TimeoutExpired, OSError
            return {**health, "error": type(error).__name__}
        try:
            rows = json.loads(result.stdout or "[]")
        except ValueError:
            rows = None
        if result.returncode or not isinstance(rows, list):
            text = ANSI.sub("", result.stderr or result.stdout).strip().splitlines()
            return {**health, "error": (text[-1] if text else f"auth list failed ({result.returncode})")[:200]}
        connected = []
        for row in rows:
            if isinstance(row, dict) and row.get("connections"):
                kinds = {str(c.get("type")) for c in row["connections"] if isinstance(c, dict)}
                label = str(row.get("name") or row.get("id"))[:40]
                connected.append(f"{label} (key from the environment)" if kinds == {"env"} else label)
        health.update(logged_in=True, providers=connected[:20])
        version = (run([exe, "--version"], timeout=20).stdout.strip().splitlines() or [""])[0]
        if version:
            health["version"] = version.removeprefix("opencode").strip().lstrip("v")[:40]
        return health

    def models(self, exe: str, home: Path) -> list[dict]:
        """`opencode models` (provider/model, one per line): free models, and those of the account's saved providers."""
        result = run([exe, "models", "--standalone"], timeout=60)
        if result.returncode:
            return []
        rows, seen = [], set()
        for line in ANSI.sub("", result.stdout).splitlines():
            model = line.strip()
            if MODEL_ID.fullmatch(model) and model not in seen:
                seen.add(model)
                rows.append({"id": model, "label": model, "description": "", "source": "opencode models"})
        return rows[:300]


PROVIDER = OpenCode(
    id="opencode", label="OpenCode", short="OpenCode", executable="opencode", color="#56b6c2", tier="full",
    preview=True, install="OpenCode's npm release, pinned (Providers → Agent software)",
    homepage="https://opencode.ai/docs", usage_url="", login=None, prompt_flag="--prompt", session_id="assigned",
    start_verify="hook", interrupt_hook=True, interrupt_keys=("Escape", "Escape"),
    quiet_idle_seconds=120,  # only if its events stop (a future version renaming them): the screen still ends a turn
    prompt_patterns=(r"Permission required\s*\n(?:\s*┃?\s*\n)*\s*┃?\s*\$\s*(?P<what>[^\n]+)",
                     r"Allow once\s+Always allow\s+Reject", r"enter submit\s+esc dismiss"),
    model_format="provider/model",
    preview_note="Rynhult was checked against OpenCode 2.1.3 with a stand-in model, not yet with real provider accounts.",
    login_hint="Give it models with vault keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY, …), a model "
               "endpoint, or /connect once in an OpenCode session's terminal. Its free models need no sign-in.",
    supports=frozenset({"resume", "needs_you", "instructions", "models", "skills", "agents", "mcp", "endpoints"}),
    endpoint_styles=("openai", "anthropic"),
    options=(
        Option("permissions", "Permissions", choices=(
            ("commands", "Ask before commands"), ("all", "Ask before commands and edits"),
            ("auto", "Approve everything not denied ⚠")), default="commands", dangerous=("auto",),
            help="Rynhult's guardrails apply in every mode"),
        Option("agent", "Primary agent", choices=(("build", "Build"), ("plan", "Plan (read-only)")), default="build"),
    ),
    hook_events=HOOK_EVENTS,
)
