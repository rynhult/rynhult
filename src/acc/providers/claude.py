"""Claude Code (`claude`): hooks per session in --settings, pre-assigned session ids, paste-code login."""
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
    read_json,
    run,
    write_json_0600,
)

HOOK_EVENTS = (
    "SessionStart", "SessionEnd", "UserPromptSubmit", "Notification",
    "PermissionRequest", "PermissionDenied", "PreToolUse", "PostToolUse", "Stop", "StopFailure",
)
PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
PLUGIN = "plugin"
SECRET_PLACEHOLDER = "acc-secret:{mcp}/{key}"

BUILTIN_MODELS = [
    {"id": "fable", "label": "Fable (latest)", "description": "Alias for the latest Fable model", "source": "cli alias"},
    {"id": "opus", "label": "Opus (latest)", "description": "Alias for the latest Opus model", "source": "cli alias"},
    {"id": "sonnet", "label": "Sonnet (latest)", "description": "Alias for the latest Sonnet model", "source": "cli alias"},
    {"id": "haiku", "label": "Haiku (latest)", "description": "Alias for the latest Haiku model", "source": "cli alias"},
    {"id": "claude-fable-5-1", "label": "Fable 5.1", "description": "", "source": "known model"},
    {"id": "claude-opus-5-5", "label": "Opus 5.5", "description": "", "source": "known model"},
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5", "description": "", "source": "known model"},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5", "description": "", "source": "known model"},
]


class Claude(Provider):
    def screen_working(self, screen: str) -> bool:
        # Only the live footer/task rows, not old transcript output (captured 2026-10-24).
        tail = "\n".join([line.rstrip() for line in screen.splitlines() if line.strip()][-5:])
        return bool(re.search(
            r"^\s*(?:✻[^\n]*·\s*[1-9]\d* shells? still running\b"
            r"|⏵⏵[^\n]*[·,]\s*[1-9]\d* (?:shells?|monitors?)\b"
            r"|◯\s+\S+[^\S\n]{2,}\S)", tail, re.MULTILINE))

    def compile(self, inputs, ctx: CompileContext, instructions: str) -> dict[str, str]:
        from ..server.compiler import safe_markdown  # catalog content rules live with the compiler
        hook = {"hooks": [{"type": "command", "command": f"{shlex.quote(ctx.hook_path)} claude"}]}
        guard = {"hooks": [{"type": "command", "command": f"{shlex.quote(guard_path(ctx.hook_path))} claude PreToolUse"}]}
        # disableAllHooks: false here (command-line settings outrank user, project and local ones) so a settings file
        # the agent can write doesn't switch Rynhult's hooks off
        settings = {"hooks": {event: [guard if event == "PreToolUse" else hook] for event in HOOK_EVENTS},
                    "disableAllHooks": False, "statusLine": {"type": "command", "command": shlex.quote(ctx.statusline_path)}}
        if inputs.permission_mode == "bypassPermissions":
            # Rynhult already made the person confirm this unrestricted launch (audited); without this Claude Code stops at
            # its own one-time "Bypass Permissions mode" dialog and the session waits there unseen (overnight 2026-10-22)
            settings["skipDangerousModePermissionPrompt"] = True
        files = {"settings.json": json.dumps(settings, indent=2) + "\n"}
        if instructions:
            files["append-prompt.md"] = instructions + "\n"
        catalog = ctx.catalog
        if inputs.skills or inputs.agents:
            files[f"{PLUGIN}/.claude-plugin/plugin.json"] = json.dumps({
                "name": "acc-session", "version": "1.0.0",
                "description": "Skills and agents selected in Rynhult for this session (vendored, pinned; see catalog/manifest.toml)",
            }, indent=2) + "\n"
            for skill_id in inputs.skills:
                for name, content in catalog.skills[skill_id].files.items():
                    files[f"{PLUGIN}/skills/{skill_id}/{name}"] = safe_markdown(content) if name.endswith(".md") else content
            for agent_id in inputs.agents:
                files[f"{PLUGIN}/agents/{agent_id}.md"] = safe_markdown(catalog.agents[agent_id].files[f"{agent_id}.md"])
        if catalog is not None:  # least privilege: only the MCP servers chosen for this session (--strict-mcp-config)
            files["mcp.json"] = json.dumps(mcp_servers_json(inputs, catalog, SECRET_PLACEHOLDER), indent=2) + "\n"
        return files

    def validate(self, inputs) -> None:
        super().validate(inputs)
        if inputs.endpoint and inputs.remote_control:
            raise CompileError("Remote Control needs Anthropic's own models; turn it off to use a model endpoint")

    def env(self, inputs, catalog, run_dir="", files=None, endpoint_key=None, native_id=None):
        """On a model endpoint (Anthropic-style API): every model Claude Code would pick is the chosen one."""
        endpoint = endpoint_of(files)
        if not endpoint:
            return {}
        model = inputs.model or ""
        return {"ANTHROPIC_BASE_URL": endpoint["url"], "ANTHROPIC_AUTH_TOKEN": endpoint_key or "acc-local-endpoint",
                "ANTHROPIC_API_KEY": "", "ANTHROPIC_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                "CLAUDE_CODE_SUBAGENT_MODEL": model, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                **({"CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(endpoint["context_tokens"])} if endpoint.get("context_tokens") else {})}

    def add_context(self, files, text):
        return {**files, "append-prompt.md": joined(files.get("append-prompt.md"), text)} if text else files

    def secret_files(self, files):
        return [name for name in ("mcp.json",) if name in files]

    def materialize(self, files, catalog):
        if "mcp.json" not in files:
            return files
        config = fill_placeholders(json.loads(files["mcp.json"]), catalog, SECRET_PLACEHOLDER)
        return {**files, "mcp.json": json.dumps(config, indent=2) + "\n"}

    def args(self, inputs, *, run_dir, tmux_name, native_id, resume, files, extra):
        if not native_id:
            raise CompileError("Claude sessions need a pre-assigned session id")
        args = ["--settings", f"{run_dir}/settings.json"]
        args += ["--resume", native_id] if resume else ["--session-id", native_id]
        args += ["--permission-mode", inputs.permission_mode]
        if inputs.model:
            args += ["--model", inputs.model]
        if inputs.effort:
            args += ["--effort", inputs.effort]
        if inputs.remote_control:
            args += ["--remote-control", tmux_name]
        if "append-prompt.md" in files or (inputs.append_prompt and not files):
            args += ["--append-system-prompt-file", f"{run_dir}/append-prompt.md"]
        if f"{PLUGIN}/.claude-plugin/plugin.json" in files:
            args += ["--plugin-dir", f"{run_dir}/{PLUGIN}"]
        if "mcp.json" in files:
            args += ["--mcp-config", f"{run_dir}/mcp.json", "--strict-mcp-config"]
        return args

    # ------------------------------------------------------------------ broker side

    def transcript(self, home: Path, native_id: str) -> Path | None:
        return next((home / ".claude/projects").glob(f"*/{native_id}.jsonl"), None)

    def messages(self, path: Path) -> list[dict]:
        return transcripts.claude(path)

    def trust(self, home: Path, workspace: str, exe: str | None = None) -> None:
        path = home / ".claude.json"
        data = read_json(path)
        data.setdefault("projects", {}).setdefault(workspace, {})["hasTrustDialogAccepted"] = True
        write_json_0600(path, data)

    def auth_health(self, exe: str, home: Path) -> dict:
        health: dict = {"logged_in": False}
        try:
            data = json.loads(run([exe, "auth", "status", "--json"]).stdout or "{}")
            health.update(logged_in=bool(data.get("loggedIn")), method=data.get("authMethod"),
                          email=data.get("email"), subscription=data.get("subscriptionType"))
        except (OSError, ValueError) as error:
            health["error"] = type(error).__name__
        except Exception as error:  # subprocess.TimeoutExpired
            health["error"] = type(error).__name__
        oauth = read_json(home / ".claude/.credentials.json").get("claudeAiOauth", {})
        if oauth:
            health.update(access_expires_at=(oauth.get("expiresAt") or 0) / 1000 or None,
                          login_expires_at=(oauth.get("refreshTokenExpiresAt") or 0) / 1000 or None,
                          rate_limit_tier=oauth.get("rateLimitTier"))
        return health

    def login_argv(self, exe: str) -> list[str]:
        return [exe, "auth", "login", "--claudeai"]

    def models(self, exe: str, home: Path) -> list[dict]:
        models = [dict(m) for m in BUILTIN_MODELS]
        cached = read_json(home / ".claude.json").get("additionalModelOptionsCache") or []
        known = {m["id"] for m in models}
        for option in cached if isinstance(cached, list) else []:
            value = option.get("value") if isinstance(option, dict) else None
            if isinstance(value, str) and value not in known and len(value) < 80:
                models.append({"id": value, "label": str(option.get("label") or value)[:60],
                               "description": str(option.get("description") or "")[:200], "source": "claude cli cache"})
        return models

    def usage(self, exe: str, home: Path) -> dict:
        """Claude Code's own cached plan utilisation (~/.claude.json, internal and undocumented: a labelled fallback;
        the statusline is the official source and arrives through sessions)."""
        cache = read_json(home / ".claude.json").get("cachedUsageUtilization") or {}
        usage = cache.get("utilization") or {}
        pick = lambda w: {"utilization": w.get("utilization"), "resets_at": w.get("resets_at")} if isinstance(w, dict) else None
        return {"fetched_at": (cache.get("fetchedAtMs") or 0) / 1000 or None, "five_hour": pick(usage.get("five_hour")),
                "seven_day": pick(usage.get("seven_day")), "source": "claude-cli-cache (internal)"}


PROVIDER = Claude(
    id="claude", label="Claude Code", short="Claude", executable="claude", color="#d97757", tier="full",
    install="Anthropic's apt repository (deploy/setup.sh)", homepage="https://claude.com/claude-code",
    usage_url="https://claude.ai/settings/usage", usage_key="claude.usage_cache", login="paste", session_id="assigned", start_verify="hook",
    supports=frozenset({"resume", "instructions", "needs_you", "remote_control", "effort", "agents", "models", "usage", "mcp", "skills",
                        "endpoints", "statusline"}),
    endpoint_styles=("anthropic",),
    options=(
        Option("permission_mode", "Permissions", choices=(
            ("acceptEdits", "Accept edits (asks before commands)"), ("default", "Default (asks for everything)"),
            ("plan", "Plan only (read-only)"), ("auto", "Auto (classifier decides)"),
            ("dontAsk", "Don't ask (denies what isn't pre-approved)"),
            ("bypassPermissions", "Bypass all permissions ⚠")),
            default="acceptEdits", dangerous=("bypassPermissions",)),
        Option("remote_control", "Remote Control", kind="bool", default=True,
               help="claude.ai and the app"),
    ),
    hook_events=HOOK_EVENTS,
)
