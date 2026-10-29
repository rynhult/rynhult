"""Codex (`codex`): hooks from a static ~/.codex/hooks.json, ids learned from SessionStart, device-code login."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

from . import transcripts
from .base import CompileContext, CompileError, Option, Provider, endpoint_of, guard_path, joined, read_json, run
from .rpc import RPC

OVERRIDES = "codex-overrides.json"
HOOK_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PermissionRequest", "Stop", "Interrupt",
               "PreToolUse", "PostToolUse")


# Reviewed against rust-v0.163.0 and rust-v0.164.0 only. See docs/research/codex-hook-trust.md.
HOOK_TRUST_VERSIONS = {"codex-cli 0.163.0", "codex-cli 0.164.0"}
HOOK_LABELS = dict(zip(HOOK_EVENTS, ("session_start", "session_end", "user_prompt_submit", "permission_request",
                                    "stop", "interrupt", "pre_tool_use", "post_tool_use")))

HOOK_REVIEW_PROMPT = (r"^\s*Hooks need review\s*\n\s*\d+ hooks? (?:is|are) new or changed\.\s*\n(?:.*\n)*?"
                      r"\s*(?:›\s*)?(?:\d+\.\s+)?Trust all and continue\s*\n"
                      r"\s*(?:›\s*)?(?:\d+\.\s+)?Continue without trusting\b")
PENDING_QUESTION_PROMPT = (r"^[ \t]*\?[ \t]+[1-9]\d* questions?[ \t]*\n"
                           r"[ \t]*shift\+← to answer[ \t]*(?:\n[ \t]*›[^\n]*(?:\n(?![ \t]*[■•›])[^\n]*){0,2})?\Z")
QUESTION_ANSWER_ROW = r"^[ \t]*(?:›[ \t]*)?Type your answer[ \t]*$"
OPEN_QUESTION_PROMPT = (QUESTION_ANSWER_ROW + r"\n(?:[^\n]*\n){0,2}"
                        r"[ \t]*enter submit[ \t]+\^\] skip[ \t]+shift\+→ main prompt[ \t]*\Z")


def acc_hook_hash(event: str, command: str) -> str:
    """Codex's normalized identity for ACC's *minimal command group* only, never arbitrary user hooks."""
    identity = {"event_name": HOOK_LABELS[event], "hooks": [{
        "type": "command", "command": command, "async": False,
        "timeout": 1 if event in ("SessionEnd", "Interrupt") else 600,
    }]}
    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(serialized).hexdigest()


def _trust_acc_hooks(path: Path, entries: dict[str, str], exe: str) -> None:
    """Preserve config text, like trust(); refuse unfamiliar versions/layouts rather than guessing.

    Only ordinary per-key state tables can be edited. A semantic comparison guards against
    header-like text inside multiline strings and any unintended change to other settings.
    """
    try:
        version = run([exe, "--version"], timeout=5)
        if version.returncode or version.stdout.strip() not in HOOK_TRUST_VERSIONS:
            return
        text = path.read_text() if path.exists() else ""
        original = tomllib.loads(text)
        expected = copy.deepcopy(original)
        state = expected.setdefault("hooks", {}).setdefault("state", {})
        if not isinstance(state, dict):
            return
        candidate = text
        for key, digest in entries.items():
            if key not in state:
                candidate += f'\n[hooks.state.{json.dumps(key)}]\ntrusted_hash = "{digest}"\n'
                state[key] = {"trusted_hash": digest}
                continue
            entry = state[key]
            if not isinstance(entry, dict) or set(entry) - {"enabled", "trusted_hash", "trusted_content_hash"}:
                return
            if "enabled" in entry and not isinstance(entry["enabled"], bool):
                return
            if any(field in entry and not isinstance(entry[field], str)
                   for field in ("trusted_hash", "trusted_content_hash")):
                return
            if entry.get("trusted_hash") == digest:
                continue
            # Codex writes these canonical quoted headers. Other valid TOML layouts stay untouched.
            header = "[hooks.state." + json.dumps(key) + "]"
            pattern = r"(?m)^" + re.escape(header) + r"[ \t]*(?:#[^\n]*)?\n(?P<body>(?:(?!^\[)[^\n]*\n?)*)"
            match = re.search(pattern, candidate)
            if not match:
                return
            body = match["body"]
            if "trusted_hash" in entry:
                body, count = re.subn(r'(?m)^(trusted_hash[ \t]*=[ \t]*)(?:"[^"\n]*"|\'[^\'\n]*\')',
                                      lambda m: m[1] + json.dumps(digest), body)
                if count != 1:
                    return
            else:
                body = body.rstrip("\n") + f'\ntrusted_hash = "{digest}"\n'
            candidate = candidate[:match.start("body")] + body + candidate[match.end("body"):]
            entry["trusted_hash"] = digest
        if candidate == text or tomllib.loads(candidate) != expected:
            return
        # Do not overwrite a concurrent user edit. Private temporary file also avoids a predictable symlink target.
        if (path.read_text() if path.exists() else "") != text:
            return
        # A symlinked config (dotfiles) is updated where it points, and the file keeps its mode. A dangling link is not
        # followed: that would create a file wherever it points.
        if path.is_symlink() and not path.exists():
            return
        target = path.resolve() if path.is_symlink() else path
        mode = target.stat().st_mode & 0o7777 if target.exists() else 0o600
        with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, prefix=".acc-hook-trust-", delete=False) as tmp:
            temporary = Path(tmp.name)
            try:
                tmp.write(candidate)
                tmp.close()
                temporary.chmod(mode)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError, AttributeError, TypeError, subprocess.TimeoutExpired):
        return  # Fail closed: hooks still need the person's review.


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(v) for v in values) + "]"


def hidden_skills(inputs, catalog, agent_home: str, plugins: dict | None) -> list[str]:
    """SKILL.md paths a Codex session must not see: unselected catalog skills, policy built-ins, installed plugins."""
    policy = catalog.codex_policy
    hidden = [f"{agent_home}/.codex/skills/acc-{s}/SKILL.md" for s, item in catalog.skills.items()
              if "codex" in item.providers and s not in inputs.skills]
    hidden += [f"{agent_home}/.codex/skills/.system/{s}/SKILL.md" for s in policy.get("hide_system_skills", [])]
    if policy.get("disable_installed_plugins"):
        hidden += list((plugins or {}).get("skill_paths", []))
    return hidden


def skills_config(hidden: list[str]) -> str:
    entries = ", ".join(f"{{path = {json.dumps(path)}, enabled = false}}" for path in hidden)
    return f"skills.config=[{entries}]"


def mcp_secrets(inputs, catalog) -> dict[str, str]:
    """MCP environment values, passed as the session's environment (Codex hands them to servers by name)."""
    values: dict[str, str] = {}
    for mcp_id in inputs.mcps if catalog else ():
        item = catalog.mcps.get(mcp_id)
        for key, value in ((item.meta.get("env") or {}) if item else {}).items():
            if key in values and values[key] != value:
                raise CompileError(f"two MCP servers set {key} differently; Codex can only pass one")
            values[key] = str(value)
    return values


def _jsonrpc(exe: str, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
    """One request to a short-lived `codex app-server` (initialize, initialized, the call)."""
    with RPC([exe, "app-server"], timeout) as rpc:
        rpc.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "acc", "version": "1"}}})
        rpc.receive(1)
        rpc.send({"jsonrpc": "2.0", "method": "initialized"})
        rpc.send({"jsonrpc": "2.0", "id": 2, "method": method, **({"params": params} if params is not None else {})})
        return rpc.receive(2)["result"] or {}


class Codex(Provider):
    def screen_idle(self, screen: str) -> str | None:
        tail = "\n".join([line.rstrip() for line in screen.splitlines() if line.strip()][-8:])
        if "esc to interrupt" in tail:
            return None
        # Codex draws up to two footer rows (model/folder, "? for shortcuts") under the empty prompt.
        match = re.search(r"^[ \t]*■[ \t]*(?P<what>\S[^\n]*)\n[ \t]*›[ \t]*Ask Codex to do anything[ \t]*"
                          r"(?:\n(?![ \t]*[■•›])[^\n]*){0,2}\Z", tail, re.MULTILINE)
        return match["what"].strip() if match else None

    def screen_prompt(self, screen: str) -> str | None:
        # The question controls must be the live footer, not an example printed in the transcript.
        tail = "\n".join([line.rstrip() for line in screen.splitlines() if line.strip()][-8:])
        if re.search(OPEN_QUESTION_PROMPT, tail, re.MULTILINE):
            answer_row = list(re.finditer(QUESTION_ANSWER_ROW, screen, re.MULTILINE))[-1]
            before_answer = screen[:answer_row.start()].rstrip()
            question = re.split(r"\n\s*\n", before_answer)[-1]
            return " ".join(question.split())[:300] or "Codex is asking a question; open the session to answer"
        if re.search(PENDING_QUESTION_PROMPT, tail, re.MULTILINE):
            return "Codex has a question; open the session and press Shift+Left to answer"
        if re.search(HOOK_REVIEW_PROMPT, screen, re.MULTILINE):
            return "Codex is asking to trust hooks; open the session to review"
        return None

    def compile(self, inputs, ctx: CompileContext, instructions: str) -> dict[str, str]:
        # Codex hooks come from the static ~/.codex/hooks.json (M1 finding F5), kept up to date by bootstrap().
        catalog = ctx.catalog
        if catalog is None:
            if instructions:
                raise CompileError("Codex instructions need the catalog")
            return {}
        overrides = []
        if instructions:
            overrides.append(f"developer_instructions={json.dumps(instructions)}")
        for mcp_id in inputs.mcps:
            meta = catalog.mcps[mcp_id].meta
            if meta.get("url"):  # streamable HTTP server
                overrides.append(f"mcp_servers.{mcp_id}.url={json.dumps(meta['url'])}")
                continue
            overrides.append(f"mcp_servers.{mcp_id}.command={json.dumps(meta['command'])}")
            overrides.append(f"mcp_servers.{mcp_id}.args={_toml_array(list(meta.get('args', [])))}")
            if meta.get("env"):  # by name only: Codex hands these from the session's environment to the server
                overrides.append(f"mcp_servers.{mcp_id}.env_vars={_toml_array(sorted(meta['env']))}")
        # Catalog skills are linked into ~/.codex/skills/acc-<id> (ops.sync_codex_skills); hide the ones not chosen
        # (M1 test 6b), plus the Codex built-ins the catalog policy names.
        hidden = hidden_skills(inputs, catalog, ctx.agent_home, ctx.plugins)
        if hidden:
            overrides.append(skills_config(hidden))
        if catalog.codex_policy.get("disable_installed_plugins"):
            for plugin in sorted((ctx.plugins or {}).get("plugins", [])):
                overrides.append(f"plugins.{json.dumps(plugin)}.enabled=false")
        return {OVERRIDES: json.dumps(overrides, indent=2) + "\n"}

    def args(self, inputs, *, run_dir, tmux_name, native_id, resume, files, extra):
        if resume:
            if not native_id:
                raise CompileError("cannot resume a Codex session whose id was never reported")
            args = ["resume", native_id]
        else:
            args = []
        args += ["--sandbox", inputs.sandbox, "--ask-for-approval", inputs.approval]
        # ACC pins and updates Codex (acc-packages). Codex's own startup update prompt would take ACC's first
        # Enter as "Update now", run npm install -g as the agent account, fail, and exit before the session starts.
        args += ["-c", "check_for_update_on_startup=false"]
        if inputs.model:
            args += ["--model", inputs.model]
        if inputs.effort:
            args += ["-c", f'model_reasoning_effort="{inputs.effort}"']
        endpoint = endpoint_of(files)
        if endpoint:  # a model provider of its own for this session (OpenAI-style API)
            name = "acc_" + "".join(c if c.isalnum() else "_" for c in endpoint["id"])
            args += ["-c", f"model_provider={json.dumps(name)}",
                     "-c", f"model_providers.{name}.name={json.dumps(endpoint['name'])}",
                     "-c", f"model_providers.{name}.base_url={json.dumps(endpoint['url'])}",
                     "-c", f'model_providers.{name}.wire_api="responses"',
                     "-c", f'model_providers.{name}.env_key="ACC_ENDPOINT_API_KEY"']
            if endpoint.get("context_tokens"):
                args += ["-c", f"model_context_window={int(endpoint['context_tokens'])}"]
        for override in [*json.loads(files.get(OVERRIDES, "[]")), *extra]:
            args += ["-c", override]
        # A repository's .codex/config.toml can set [features] hooks=false (or codex_hooks), even in an untrusted
        # folder, and Codex then skips the guard. Only the command line outranks that layer, so this goes last.
        return args + ["-c", "features.hooks=true"]

    def env(self, inputs, catalog, run_dir="", files=None, endpoint_key=None, native_id=None):
        env = mcp_secrets(inputs, catalog)
        if endpoint_of(files):  # Codex reads the endpoint's key from this variable (env_key); local servers ignore it
            env["ACC_ENDPOINT_API_KEY"] = endpoint_key or "acc-local-endpoint"
        return env

    def add_context(self, files, text):
        """developer_instructions is one -c override: the paragraph joins the profile's instructions there."""
        if not text:
            return files
        overrides = json.loads(files.get(OVERRIDES, "[]"))
        prefix = "developer_instructions="
        found = next((i for i, o in enumerate(overrides) if o.startswith(prefix)), None)
        if found is None:
            overrides.insert(0, prefix + json.dumps(text))
        else:
            overrides[found] = prefix + json.dumps(joined(json.loads(overrides[found][len(prefix):]), text).rstrip("\n"))
        return {**files, OVERRIDES: json.dumps(overrides, indent=2) + "\n"}

    def resume_extra(self, inputs, files, ctx: CompileContext) -> list[str]:
        """Hide skills added to the catalog after launch too (later -c wins).

        The launch spec is reused verbatim, but its hide-list only knows the catalog of launch day; a skill added
        since would otherwise appear in an old session. Returns [] when nothing new needs hiding."""
        if ctx.catalog is None or OVERRIDES not in files:
            return []
        original = next((o for o in json.loads(files[OVERRIDES]) if o.startswith("skills.config=")), "")
        hidden = hidden_skills(inputs, ctx.catalog, ctx.agent_home, ctx.plugins)
        combined = list(dict.fromkeys([*(p for p in hidden if p not in original), *[
            part.split('"')[1] for part in original.split("path = ")[1:]]]))
        if all(p in original for p in hidden):
            return []
        return [skills_config(combined)]

    # ------------------------------------------------------------------ broker side

    def transcript(self, home: Path, native_id: str) -> Path | None:
        return next((home / ".codex/sessions").rglob(f"rollout-*-{native_id}.jsonl"), None)

    def messages(self, path: Path) -> list[dict]:
        return transcripts.codex(path)

    def trust(self, home: Path, workspace: str, exe: str | None = None) -> None:
        path = home / ".codex/config.toml"
        path.parent.mkdir(exist_ok=True)
        text = path.read_text() if path.exists() else ""
        if tomllib.loads(text).get("projects", {}).get(workspace, {}).get("trust_level") != "trusted":
            quoted = json.dumps(workspace)  # TOML basic strings share JSON escaping for paths
            text = text.rstrip("\n") + f'\n\n[projects.{quoted}]\ntrust_level = "trusted"\n'
            tomllib.loads(text)  # refuse to write a file Codex could not parse
            path.write_text(text)

    def auth_health(self, exe: str, home: Path) -> dict:
        from ..broker.logins import _jwt_claims
        health: dict = {"logged_in": False}
        try:
            status = run([exe, "login", "status"])
            text = (status.stdout + status.stderr).strip()
            health.update(logged_in=status.returncode == 0 and "Logged in" in text, status=text[:120])
        except (OSError, subprocess.TimeoutExpired) as error:
            health["error"] = type(error).__name__
        auth = read_json(home / ".codex/auth.json")
        if auth:
            claims = _jwt_claims((auth.get("tokens") or {}).get("id_token", ""))
            health.update(mode=auth.get("auth_mode"), last_refresh=auth.get("last_refresh"),
                          plan=(claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type"),
                          email=claims.get("email"))
        return health

    def login_argv(self, exe: str) -> list[str]:
        return [exe, "login", "--device-auth"]

    def models(self, exe: str, home: Path) -> list[dict]:
        result = _jsonrpc(exe, "model/list", {})
        rows = result.get("data") or result.get("models") or []
        return [{"id": m.get("model") or m.get("id"), "label": m.get("displayName") or m.get("model"),
                 "description": m.get("description") or "",
                 "efforts": [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or []],
                 "default_effort": m.get("defaultReasoningEffort"), "source": "codex app-server"}
                for m in rows if not m.get("hidden") and (m.get("model") or m.get("id"))]

    def bootstrap(self, home: Path, hook_path: str, exe: str | None = None) -> None:
        """Install and trust only ACC's exact minimal command groups; preserve every other hook."""
        path = home / ".codex" / "hooks.json"
        try:
            config = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            return
        if not isinstance(config, dict) or set(config) - {"hooks", "description"}:
            return
        hooks = config.get("hooks", {})
        if not isinstance(hooks, dict):
            return
        entries = {}
        changed = False
        for event in HOOK_EVENTS:
            command = f"{shlex.quote(guard_path(hook_path))} codex PreToolUse" if event == "PreToolUse" else f"{shlex.quote(hook_path)} codex"
            groups = hooks.get(event, [])
            if not isinstance(groups, list) or any(not isinstance(g, dict) for g in groups):
                return  # Do not shift indices or bless an unfamiliar hooks format.
            ours = {"hooks": [{"type": "command", "command": command}]}
            if ours not in groups:
                groups = hooks[event] = [*groups, ours]
                changed = True
            for index, group in enumerate(groups):
                if group == ours:
                    entries[f"{path.absolute()}:{HOOK_LABELS[event]}:{index}:0"] = acc_hook_hash(event, command)
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.acc-tmp")
            tmp.write_text(json.dumps({**config, "hooks": hooks}, indent=2) + "\n")
            tmp.replace(path)
        _trust_acc_hooks(home / ".codex/config.toml", entries, exe or self.executable)

    def usage(self, exe: str, home: Path) -> dict:
        """Official Codex plan usage via a short-lived app-server (account/rateLimits/read; M1 test 6a)."""
        result = _jsonrpc(exe, "account/rateLimits/read")
        limits = result.get("rateLimits") or {}
        window = lambda w: {"used_percent": w.get("usedPercent"), "window_minutes": w.get("windowDurationMins"),
                            "resets_at": w.get("resetsAt")} if w else None
        return {"plan": limits.get("planType"), "primary": window(limits.get("primary")),
                "secondary": window(limits.get("secondary")), "reached": limits.get("rateLimitReachedType"),
                "credits": (limits.get("credits") or {}).get("balance"), "observed_at": time.time()}


PROVIDER = Codex(
    id="codex", label="Codex", executable="codex", color="var(--fg)", tier="full",
    install="npm @openai/codex (pinned by deploy/install.sh)", homepage="https://developers.openai.com/codex/cli",
    usage_url="https://chatgpt.com/codex/settings/usage", usage_key="codex.rate_limits", login="device", session_id="learned", start_verify="settle",
    supports=frozenset({"resume", "instructions", "needs_you", "effort", "models", "usage", "mcp", "skills", "endpoints"}),
    endpoint_styles=("openai",),
    options=(
        # Words, not Codex's flags (UX review R6); the values stay Codex's own.
        Option("sandbox", "What it can change", choices=(
            ("workspace-write", "Can edit the project"), ("read-only", "Read only"), ("danger-full-access", "Full access (no sandbox) ⚠")),
            default="workspace-write", dangerous=("danger-full-access",)),
        Option("approval", "When it asks", choices=(
            ("on-request", "Asks when it needs to"), ("untrusted", "Asks before most commands"), ("never", "Never asks ⚠")),
            default="on-request", dangerous=("never",)),
    ),
    # The whole startup review dialog (tui/src/startup_hooks_review.rs): the heading on its own line, the count, then
    # its choices. A line a session prints that merely mentions the heading is not it.
    prompt_patterns=(HOOK_REVIEW_PROMPT, PENDING_QUESTION_PROMPT, OPEN_QUESTION_PROMPT), screen_prompt_command=False,
    hook_events=HOOK_EVENTS,
)
