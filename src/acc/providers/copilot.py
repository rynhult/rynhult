"""GitHub Copilot CLI (`copilot`, npm @github/copilot).

Checked against the CLI's own help (1.0.102, 2026-10-21) and GitHub's hooks reference:
- `--session-id <uuid>` starts a new session with that id or resumes it, so ACC assigns ids up front (like Claude);
- hooks: user-level files in ~/.copilot/hooks/*.json (written by acc-agent-add); PascalCase events carry Claude-style
  snake_case fields (session_id, hook_event_name); `notification` and `errorOccurred` exist only in camelCase. Verified
  live: the interactive TUI sends SessionStart + UserPromptSubmit with the first prompt (not at startup), with the
  session id ACC assigned; hooks only run in a trusted folder; `errorOccurred` repeats on retries (`recoverable`);
- instructions: an AGENTS.md in a folder named by COPILOT_CUSTOM_INSTRUCTIONS_DIRS (verified with `instruction list`);
- skills: `--add-dir` loads that folder's .github/skills; MCP servers: `--additional-mcp-config @file`;
- folder trust: `copilot config trustedFolders <dir>`; login: `copilot login --device-code`.
Not yet run against a real Copilot account: labelled preview until the docs/PROVIDERS.md checklist passes.
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
    endpoint_of,
    fill_placeholders,
    guard_path,
    joined,
    mcp_servers_json,
    run,
)
from .rpc import RPC

SECRET_PLACEHOLDER = "acc-secret:{mcp}/{key}"
HOOK_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "Stop",
               "notification", "errorOccurred")
EVENT_NAMES = {  # Copilot's names (both spellings) -> ACC's (Claude Code's)
    "sessionStart": "SessionStart", "sessionEnd": "SessionEnd", "userPromptSubmitted": "UserPromptSubmit",
    "preToolUse": "PreToolUse", "postToolUse": "PostToolUse", "postToolUseFailure": "PostToolUse", "PostToolUseFailure": "PostToolUse",
    "permissionRequest": "PermissionRequest", "agentStop": "Stop", "notification": "Notification",
}
FIELDS = {"sessionId": "session_id", "toolName": "tool_name", "toolInput": "tool_input", "toolArgs": "tool_input",
          "transcriptPath": "transcript_path", "stopReason": "stop_reason", "hookEventName": "hook_event_name"}
INSTRUCTIONS_DIR = "instructions"  # <run dir>/instructions/AGENTS.md
SKILLS_DIR = "copilot"  # <run dir>/copilot/.github/skills/<id>/


def _read_jsonc(path: Path) -> dict:
    """Copilot's config.json starts with `//` comment lines (seen with 1.0.102)."""
    try:
        text = "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("//"))
        data = json.loads(text)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class Copilot(Provider):
    def screen_working(self, screen: str) -> bool:
        # Owner-reported status-line capture (2026-10-23). A long tool may stop redrawing,
        # but the interrupt hint remains until the turn finishes. Ignore old transcript lines.
        tail = "\n".join([line.rstrip() for line in screen.splitlines() if line.strip()][-5:])
        return bool(re.search(r"^\s*[◉○]\s+\S[^\n]*\besc\s+esc\s+interrupt\s*$", tail, re.MULTILINE))

    def compile(self, inputs, ctx: CompileContext, instructions: str) -> dict[str, str]:
        from ..server.compiler import safe_markdown
        files: dict[str, str] = {}
        if instructions:
            files[f"{INSTRUCTIONS_DIR}/AGENTS.md"] = instructions + "\n"
        catalog = ctx.catalog
        for skill_id in inputs.skills:
            for name, content in catalog.skills[skill_id].files.items():
                files[f"{SKILLS_DIR}/.github/skills/{skill_id}/{name}"] = safe_markdown(content) if name.endswith(".md") else content
        if catalog is not None and inputs.mcps:
            config = mcp_servers_json(inputs, catalog, SECRET_PLACEHOLDER)
            for server in config["mcpServers"].values():  # Copilot's shape: local|http, and which tools to expose
                server.setdefault("type", "local")
                server["tools"] = ["*"]
            files["mcp.json"] = json.dumps(config, indent=2) + "\n"
        return files

    def add_context(self, files, text):
        name = f"{INSTRUCTIONS_DIR}/AGENTS.md"
        return {**files, name: joined(files.get(name), text)} if text else files

    def secret_files(self, files):
        return [name for name in ("mcp.json",) if name in files]

    def materialize(self, files, catalog):
        if "mcp.json" not in files:
            return files
        config = fill_placeholders(json.loads(files["mcp.json"]), catalog, SECRET_PLACEHOLDER)
        return {**files, "mcp.json": json.dumps(config, indent=2) + "\n"}

    def args(self, inputs, *, run_dir, tmux_name, native_id, resume, files, extra):
        if not native_id:
            raise CompileError("Copilot sessions need a pre-assigned session id")
        args = ["--session-id", native_id, "--no-auto-update"]  # same flag starts or resumes that session
        if inputs.model:
            args += ["--model", inputs.model]
        if inputs.effort:
            args += ["--reasoning-effort", inputs.effort]
        mode = self.option(inputs, "mode")
        if mode and mode != "interactive":
            args += ["--mode", mode]
        permissions = self.option(inputs, "permissions")
        if permissions == "tools":
            args += ["--allow-all-tools"]
        elif permissions == "all":
            args += ["--allow-all"]
        if any(name.startswith(f"{SKILLS_DIR}/") for name in files):
            args += ["--add-dir", f"{run_dir}/{SKILLS_DIR}"]
        if "mcp.json" in files:
            args += ["--additional-mcp-config", f"@{run_dir}/mcp.json"]
        return args + list(extra)

    def env(self, inputs, catalog, run_dir="", files=None, endpoint_key=None, native_id=None):
        env = {"COPILOT_CUSTOM_INSTRUCTIONS_DIRS": f"{run_dir}/{INSTRUCTIONS_DIR}"} \
            if f"{INSTRUCTIONS_DIR}/AGENTS.md" in (files or {}) else {}
        endpoint = endpoint_of(files)
        if endpoint:  # BYOK: Copilot uses this server instead of GitHub's models (no GitHub login needed)
            env.update(COPILOT_PROVIDER_BASE_URL=endpoint["url"], COPILOT_PROVIDER_TYPE=endpoint["style"],
                       COPILOT_MODEL=inputs.model or "")
            if endpoint_key:
                env["COPILOT_PROVIDER_API_KEY"] = endpoint_key
            if endpoint.get("context_tokens"):
                env["COPILOT_PROVIDER_MAX_PROMPT_TOKENS"] = str(endpoint["context_tokens"])
        return env

    def normalize_hook(self, hook: dict) -> dict:
        out = {FIELDS.get(key, key): value for key, value in hook.items()}
        name = out.get("hook_event_name")
        if isinstance(name, str):
            out["hook_event_name"] = EVENT_NAMES.get(name, name)
        if name in ("errorOccurred", "ErrorOccurred"):  # retries report recoverable errors: only a final one fails the turn
            error = out.get("error")
            out["error"] = error.get("message") if isinstance(error, dict) else error
            out["hook_event_name"] = "ErrorOccurred" if out.get("recoverable") else "StopFailure"
        if isinstance(out.get("tool_input"), str):  # toolArgs may arrive as a JSON string
            try:
                out["tool_input"] = json.loads(out["tool_input"])
            except ValueError:
                out["tool_input"] = {"description": out["tool_input"][:200]}
        return out

    # ------------------------------------------------------------------ broker side

    def transcript(self, home: Path, native_id: str) -> Path | None:
        """~/.copilot/session-state/<id>/events.jsonl (seen with 1.0.102); anything else named after the session id
        under ~/.copilot as a fallback, since the layout isn't documented."""
        root = home / ".copilot"
        events = root / "session-state" / native_id / "events.jsonl"
        if events.is_file():
            return events
        if not root.is_dir():
            return None
        for depth in ("*", "*/*", "*/*/*"):
            found = next((p for p in root.glob(depth) if native_id in p.name), None)
            if found:
                return found
        return None

    def messages(self, path: Path) -> list[dict]:
        return transcripts.copilot(path)

    def trust(self, home: Path, workspace: str, exe: str | None = None) -> None:
        """Copilot keeps trusted folders in ~/.copilot/config.json (`trustedFolders`; `copilot config` refuses that
        key). The file starts with `//` comment lines, which are kept."""
        if not exe:
            return
        path = home / ".copilot" / "config.json"
        try:
            lines = path.read_text().splitlines()
        except OSError:
            lines = ["// User settings belong in settings.json.", "// This file is managed automatically."]
        header = [line for line in lines if line.lstrip().startswith("//")]
        config = _read_jsonc(path)
        folders = config.get("trustedFolders") if isinstance(config.get("trustedFolders"), list) else []
        if workspace in folders:
            return
        config["trustedFolders"] = [*folders, workspace]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.acc-tmp")
        tmp.write_text("\n".join([*header, json.dumps(config, indent=2)]) + "\n")
        tmp.chmod(0o600)
        tmp.replace(path)

    def _account(self, home: Path) -> dict | None:
        """GitHub's Copilot account record for the signed-in user (plan, organisations, quotas). Internal and
        undocumented (`/copilot_internal/user`, what Copilot's own /usage shows): read defensively and labelled. The
        token is read here, as the agent account, and only sent to api.github.com."""
        import urllib.request
        config = _read_jsonc(home / ".copilot" / "config.json")
        user = config.get("lastLoggedInUser") or config.get("last_logged_in_user") or {}
        tokens = config.get("authTokens") or {}
        login = user.get("login") if isinstance(user, dict) else None
        entry = tokens.get(f"https://github.com:{login}") or next(iter(tokens.values()), None) if isinstance(tokens, dict) else None
        token = entry.get("token") if isinstance(entry, dict) else None
        if not token:
            return None
        request = urllib.request.Request("https://api.github.com/copilot_internal/user", headers={
            "Authorization": f"token {token}", "Accept": "application/json", "User-Agent": "acc"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.load(response)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def usage(self, exe: str, home: Path) -> dict | None:
        """Plan and AI-credit usage for this billing cycle (premium requests), as GitHub's settings page shows it."""
        import time
        data = self._account(home)
        if data is None:
            return None
        snapshots = data.get("quota_snapshots") or {}
        credits = snapshots.get("premium_interactions") or {}
        pick = lambda q: {"unlimited": bool(q.get("unlimited"))} if isinstance(q, dict) else None
        return {
            "plan": data.get("copilot_plan"), "sku": data.get("access_type_sku"),
            "organizations": [o for o in (data.get("organization_login_list") or []) if isinstance(o, str)][:10],
            "resets_at": data.get("quota_reset_date_utc") or data.get("quota_reset_date"),
            "credits": {"used": credits.get("credits_used"), "entitlement": credits.get("entitlement"),
                        "remaining": credits.get("remaining"), "percent_remaining": credits.get("percent_remaining"),
                        "unlimited": bool(credits.get("unlimited")), "overage_permitted": credits.get("overage_permitted"),
                        "overage_count": credits.get("overage_count")} if credits else None,
            "chat": pick(snapshots.get("chat")), "completions": pick(snapshots.get("completions")),
            "observed_at": time.time(), "source": "github copilot_internal/user (internal)",
        }

    def auth_health(self, exe: str, home: Path) -> dict:
        """No status command: a stored login is either in the system keyring or ~/.copilot's config file."""
        health: dict = {"logged_in": False}
        config = _read_jsonc(home / ".copilot" / "config.json")
        user = config.get("last_logged_in_user") or config.get("lastLoggedInUser") or \
            next(iter(config.get("logged_in_users") or config.get("loggedInUsers") or []), None)
        if isinstance(user, dict):
            health.update(logged_in=True, login=user.get("login"), email=user.get("login"))
            account = self._account(home)
            if account:  # the plan behind the login (e.g. an enterprise seat through an organisation)
                health.update(plan=account.get("copilot_plan"), sku=account.get("access_type_sku"),
                              organizations=(account.get("organization_login_list") or [])[:10])
        version = (run([exe, "--version"], timeout=20).stdout.strip().splitlines() or [""])[0]
        if version:
            health["version"] = version.removeprefix("GitHub Copilot CLI ").rstrip(".")[:40]
        return health

    def login_argv(self, exe: str) -> list[str]:
        return [exe, "login", "--device-code"]

    def bootstrap(self, home: Path, hook_path: str, exe: str | None = None) -> None:
        """~/.copilot/hooks/acc.json: Copilot reads user-level hooks from there. Each passes the event name to acc-hook,
        which records nothing outside an ACC session ($ACC_RUN_DIR unset)."""
        command = lambda event: f"{shlex.quote(guard_path(hook_path) if event == 'PreToolUse' else hook_path)} copilot {event}"
        hooks = {"version": 1, "hooks": {event: [{"type": "command", "bash": command(event), "timeoutSec": 10}]
                                         for event in HOOK_EVENTS}}
        path = home / ".copilot" / "hooks" / "acc.json"
        text = json.dumps(hooks, indent=2) + "\n"
        try:
            if path.read_text() == text:
                return
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.acc-tmp")
        tmp.write_text(text)
        tmp.replace(path)

    def before_login(self, home: Path) -> None:
        """A server has no system keychain, and login only offers plaintext storage on a terminal (ACC's login runs
        through pipes), so the token would be thrown away. `storeTokenPlaintext` in ~/.copilot/settings.json (which
        `copilot config` refuses to set) keeps it in a file only the agent account can read, like Claude's and
        Codex's credentials."""
        path = home / ".copilot" / "settings.json"
        settings = _read_jsonc(path)
        if settings.get("storeTokenPlaintext") is True:
            return
        settings["storeTokenPlaintext"] = True
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.acc-tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n")
        tmp.chmod(0o600)
        tmp.replace(path)

    def models(self, exe: str, home: Path) -> list[dict]:
        """The models this login may use (its plan and organisation policy decide), from the CLI's own Agent Client
        Protocol server (`copilot --acp`): a new session's `models.availableModels`; the probe session is closed."""
        with RPC([exe, "--acp", "--no-auto-update"], 60, cwd=str(home)) as rpc:
            rpc.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": 1, "clientCapabilities": {}}})
            rpc.receive(1)
            rpc.send({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(home), "mcpServers": []}})
            result = rpc.receive(2)["result"] or {}
            if result.get("sessionId"):
                rpc.send({"jsonrpc": "2.0", "id": 3, "method": "session/close",
                          "params": {"sessionId": result["sessionId"]}})
                try:
                    rpc.receive(3)
                except TimeoutError:
                    pass
        probe = result.get("sessionId")
        if isinstance(probe, str) and re.fullmatch(r"[0-9a-f-]{36}", probe):  # the probe's own (empty) session only
            import shutil
            shutil.rmtree(home / ".copilot" / "session-state" / probe, ignore_errors=True)
        listed = (result.get("models") or {})
        rows, seen = [], set()
        for model in listed.get("availableModels") or []:
            model_id = model.get("modelId") if isinstance(model, dict) else None
            if isinstance(model_id, str) and model_id not in seen and len(model_id) < 80:
                seen.add(model_id)
                rows.append({"id": model_id, "label": str(model.get("name") or model_id)[:60],
                             "description": str(model.get("description") or "")[:200], "source": "copilot acp",
                             **({"default": True} if model_id == listed.get("currentModelId") else {})})
        return rows


PROVIDER = Copilot(
    id="copilot", label="GitHub Copilot CLI", short="Copilot", executable="copilot", color="#8957e5", tier="full",
    preview=True, install="npm @github/copilot (deploy/setup.sh asks; needs Node 22+)",
    homepage="https://docs.github.com/en/copilot/how-tos/copilot-cli",
    usage_url="https://github.com/settings/copilot/features", usage_key="copilot.usage", login="device", prompt_patterns=(
        r"│ │ (?P<what>[^│\n]+?)\s*│?\s*\n│ ╰[^\n]*\n(?:│[^\n]*\n){0,3}│ Do you want to run this command\?",
        r"Do you want to (?:run this command|make (?:this|these) (?:edit|change)s?|[a-z][^\n?]{0,60})\?"),
    prompt_flag="-i", session_id="assigned", advisory_asks=True,
    start_verify="settle",  # interactive Copilot reports SessionStart with the first prompt, not at startup
    quiet_idle_seconds=60,  # a turn that fails after its retries ends without a hook (seen with 1.0.102)
    supports=frozenset({"resume", "instructions", "needs_you", "effort", "models", "usage", "mcp", "skills", "endpoints"}),
    endpoint_styles=("openai", "anthropic"),
    options=(
        Option("mode", "Mode", choices=(("interactive", "Interactive"), ("plan", "Plan first"),
                                        ("autopilot", "Autopilot (keeps going on its own)")), default="interactive"),
        Option("permissions", "Permissions", choices=(
            ("ask", "Ask before tools and paths"), ("tools", "Allow all tools ⚠"), ("all", "Allow everything ⚠")),
            default="ask", dangerous=("tools", "all")),
    ),
    hook_events=HOOK_EVENTS,
)
