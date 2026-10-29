"""What every agent provider supplies to Rynhult.

A provider is one coding-agent CLI that runs natively in tmux (Claude Code, Codex, …). Rynhult never re-implements a
CLI: it compiles the files and command line for a launch, reads the CLI's own hooks to know its state, resumes the
same conversation after a crash, and drives the CLI's own login. Everything that differs between CLIs lives in the
provider's module; the rest of Rynhult asks the registry (acc.providers) instead of naming providers.

Two halves, both pure functions of their inputs:
- server side (accd): option schema, validation, compiled files, argv, environment, hook normalisation;
- broker side (runs as the agent account): transcripts, workspace trust, login health, login argv, models, usage.
"""
from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the server's types; providers never import server modules at run time
    from ..server.catalog import Catalog
    from ..server.compiler import LaunchInputs


class CompileError(ValueError):
    pass


# Fields LaunchInputs has always carried for Claude and Codex. Other providers keep their options in
# LaunchInputs.options, so launch specs stored before the registry existed resume unchanged.
LEGACY_OPTION_FIELDS = ("permission_mode", "sandbox", "approval", "remote_control")
ENDPOINT_FILE = "endpoint.json"  # a launch on a model endpoint: {"id", "name", "style", "url"} (never the key)


def endpoint_of(files: dict[str, str] | None) -> dict | None:
    """The model endpoint a launch uses (compiled into its files), or None for the vendor's own models."""
    raw = (files or {}).get(ENDPOINT_FILE)
    return json.loads(raw) if raw else None


@dataclass(frozen=True)
class Option:
    """One launch setting the New session dialog shows for a provider (and the API validates)."""
    key: str
    label: str
    kind: str = "select"  # select | bool
    choices: tuple[tuple[str, str], ...] = ()  # (value, label)
    default: Any = None
    help: str = ""
    dangerous: tuple[Any, ...] = ()  # values that let the agent act without asking: launch needs confirming

    def public(self) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind, "default": self.default, "help": self.help,
                "choices": [{"value": v, "label": l} for v, l in self.choices], "dangerous": list(self.dangerous)}


@dataclass(frozen=True)
class CompileContext:
    hook_path: str
    statusline_path: str
    catalog: "Catalog | None"
    agent_home: str
    plugins: dict | None = None  # provider-specific discovery from the account (Codex: installed plugins)


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    executable: str  # default command; the broker's ACC_PROVIDERS can point it elsewhere
    color: str = "#8a8f9c"
    short: str = ""  # badge name ("Claude"); defaults to the label
    tier: str = "full"  # full: hooks for every state incl. "needs you"; standard: no reliable waiting signal; basic
    preview: bool = False  # not yet verified against the real CLI (docs/PROVIDERS.md checklist)
    install: str = ""  # how to install it, for the UI and setup
    homepage: str = ""
    usage_url: str = ""
    usage_key: str = ""  # Observation the plan usage is stored under (default "<id>.usage")
    login: str | None = None  # "paste" (URL, then a code pasted back) | "device" (code entered on another device) |
    #                           "link" (a URL to approve in a browser; completes on its own)
    login_tty: bool = False  # sign-in reads the code only from a terminal (a pipe on stdin makes it refuse)
    prompt_flag: str | None = None  # how the CLI takes a first prompt and stays interactive (None: as an argument)
    session_id: str = "learned"  # "assigned": Rynhult picks the id up front | "learned": from the SessionStart hook
    start_verify: str = "hook"  # "hook": wait for SessionStart | "settle": treat as idle after a few seconds
    interrupt_hook: bool = False  # does the CLI report an interrupt itself?
    # For CLIs that don't always report the end of a turn: a working session whose screen hasn't changed for this long
    # is idle (their TUIs redraw constantly while working). None: hooks alone decide.
    quiet_idle_seconds: float | None = None
    # For CLIs with no permission hook: regexes for their approval prompt at the bottom of the screen, so Rynhult can still
    # show "needs you". A named group `what` (e.g. the command) becomes the inbox item's detail.
    prompt_patterns: tuple[str, ...] = ()
    screen_prompt_command: bool = True  # False: a non-command dialog; show its plain message.
    # The CLI sends PermissionRequest for every tool, even ones it then approves by itself (Copilot), and nothing
    # until a long tool finishes: the hook can't tell, so "needs you" comes from the prompt on screen instead.
    advisory_asks: bool = False
    interrupt_keys: tuple[str, ...] = ("Escape",)  # what interrupts a turn (tmux key names, pressed a moment apart)
    login_hint: str = ""  # for a CLI with no sign-in Rynhult drives (login None): how to give it models instead
    model_format: str = ""  # how its model ids are written, when that isn't obvious (the dialog's placeholder)
    preview_note: str = ""  # what has and hasn't been verified, for a preview (default: not against the real CLI yet)
    supports: frozenset[str] = field(default_factory=frozenset)  # resume, needs_you, remote_control, effort, agents,
    #                                                   models, usage, mcp, skills, instructions, endpoints, statusline
    options: tuple[Option, ...] = ()
    endpoint_styles: tuple[str, ...] = ()  # API styles it can use on a model endpoint, preferred first
    hook_events: tuple[str, ...] = ()  # events Rynhult listens to (for docs and per-session hook files)

    # ------------------------------------------------------------------ metadata

    @property
    def usage_observation(self) -> str:
        return self.usage_key or f"{self.id}.usage"

    def public(self, installed: bool | None = None) -> dict:
        return {"id": self.id, "label": self.label, "short": self.short or self.label, "color": self.color, "tier": self.tier, "preview": self.preview,
                "install": self.install, "homepage": self.homepage, "usage_url": self.usage_url, "login": self.login,
                **({"login_hint": self.login_hint} if self.login_hint else {}),
                **({"model_format": self.model_format} if self.model_format else {}),
                **({"preview_note": self.preview_note} if self.preview_note else {}),
                "supports": sorted(self.supports), "options": [o.public() for o in self.options],
                "endpoint_styles": list(self.endpoint_styles),
                **({"installed": installed} if installed is not None else {})}

    # ------------------------------------------------------------------ options

    def option(self, inputs: "LaunchInputs", key: str) -> Any:
        spec = next((o for o in self.options if o.key == key), None)
        if key in LEGACY_OPTION_FIELDS:
            return getattr(inputs, key)
        value = (inputs.options or {}).get(key)
        return spec.default if value is None and spec else value

    def validate(self, inputs: "LaunchInputs") -> None:
        for spec in self.options:
            value = self.option(inputs, spec.key)
            if spec.kind == "select" and value not in {v for v, _ in spec.choices}:
                raise CompileError(f"unknown {spec.label.lower()} {value!r}")
            if spec.kind == "bool" and not isinstance(value, bool):
                raise CompileError(f"{spec.label} must be true or false")
        unknown = set(inputs.options or {}) - {o.key for o in self.options}
        if unknown:
            raise CompileError(f"{self.label} has no option {sorted(unknown)[0]!r}")
        if inputs.remote_control and "remote_control" not in self.supports:
            raise CompileError(f"Remote Control is not supported for {self.label} sessions")

    def dangerous(self, inputs: "LaunchInputs") -> bool:
        return any(self.option(inputs, o.key) in o.dangerous for o in self.options if o.dangerous)

    # ------------------------------------------------------------------ launch (server side)

    def compile(self, inputs: "LaunchInputs", ctx: CompileContext, instructions: str) -> dict[str, str]:
        """Files written to the session's run folder (stored in the launch spec and reused on every resume)."""
        return {}

    def args(self, inputs: "LaunchInputs", *, run_dir: str, tmux_name: str, native_id: str | None, resume: bool,
             files: dict[str, str], extra: list[str]) -> list[str]:
        raise NotImplementedError

    def env(self, inputs: "LaunchInputs", catalog: "Catalog | None", run_dir: str = "",
            files: dict[str, str] | None = None, endpoint_key: str | None = None,
            native_id: str | None = None) -> dict[str, str]:
        """Environment for the session, passed through a 0600 file (MCP secrets, settings that point at its files, a
        model endpoint and its key; `native_id`: the conversation it runs, when known)."""
        return {}

    def materialize(self, files: dict[str, str], catalog: "Catalog | None") -> dict[str, str]:
        """The stored files as written to the run folder (secret placeholders filled in)."""
        return files

    def secret_files(self, files: dict[str, str]) -> list[str]:
        """The run folder's files that materialize fills with secret values (D21: removed when the session stops,
        written again at resume)."""
        return []

    def add_context(self, files: dict[str, str], text: str) -> dict[str, str]:
        """The stored files with `text` added to the session's instructions (where the profile's go), for this run
        only: Rynhult's key vault lists the variables a session has at every launch and resume, and they can change."""
        return files

    def resume_extra(self, inputs: "LaunchInputs", files: dict[str, str], ctx: CompileContext) -> list[str]:
        """Extra arguments a resume needs that the stored files can't carry (e.g. catalog changes since launch)."""
        return []

    def new_native_id(self) -> str:
        """The id Rynhult assigns a new conversation up front (session_id "assigned")."""
        return str(uuid.uuid4())

    def preview_native_id(self) -> str | None:
        return "<session-uuid>" if self.session_id == "assigned" else None

    # ------------------------------------------------------------------ hooks (server side)

    def normalize_hook(self, hook: dict) -> dict:
        """The hook in Rynhult's vocabulary (Claude Code's event names and snake_case fields): hook_event_name is one of
        SessionStart, SessionEnd, UserPromptSubmit, PostToolUse, PermissionRequest, PermissionDenied, Notification
        (notification_type), Stop, StopFailure, Interrupt; session_id is the native conversation id."""
        return hook

    # ------------------------------------------------------------------ broker side (runs as the agent account)

    def transcript(self, home: Path, native_id: str) -> Path | None:
        """Where the conversation is stored, if it exists (a resume must never start a new conversation)."""
        return None

    def trust(self, home: Path, workspace: str, exe: str | None = None) -> None:
        """Record that the user trusts this folder, so the CLI doesn't stop at its own trust prompt (`exe`: the CLI,
        when it's installed for this account)."""

    def screen_prompt(self, screen: str) -> str | None:
        """What the CLI needs input for when its screen shows a live prompt; None otherwise."""
        tail = "\n".join([line.rstrip() for line in screen.splitlines() if line.strip()][-15:])
        for pattern in self.prompt_patterns:
            if match := re.search(pattern, tail, re.MULTILINE):
                return (match.groupdict().get("what") or "").strip()[:300]  # "": a prompt, but not what it's for
        return None

    def screen_working(self, screen: str) -> bool:
        """A positive working indicator, when the CLI exposes one."""
        return False

    def screen_idle(self, screen: str) -> str | None:
        """A terminal turn error in the live footer, when the CLI exposes one."""
        return None

    def input_pending(self, screen: str, text: str) -> bool:
        """Inspect only the cursor's input row, never a past message in the transcript."""
        return bool(re.search(r"\[(?:Pasted\b|Paste #\d+)", screen, re.IGNORECASE)
                    or text[-32:] in screen)

    def prompt_args(self, prompt: str) -> list[str]:
        return [self.prompt_flag, prompt] if self.prompt_flag else [prompt]

    def messages_source(self, home: Path, native_id: str) -> Path | None:
        """The file history search reads (its stamp says when to read it again)."""
        path = self.transcript(home, native_id)
        return path if path and path.is_file() else None

    def messages(self, path: Path) -> list[dict]:
        """The conversation as [{role: user|assistant, text, ts}] (providers.transcripts)."""
        return []

    def session_messages(self, path: Path, native_id: str) -> list[dict]:
        """messages() for a source that holds more than one conversation (OpenCode's database)."""
        return self.messages(path)

    def auth_health(self, exe: str, home: Path) -> dict:
        return {"logged_in": False}

    def login_argv(self, exe: str) -> list[str]:
        raise CompileError(f"{self.label} has no login Rynhult can drive")

    def before_login(self, home: Path) -> None:
        """Settings the CLI's login needs on a headless server (runs as the agent account)."""

    def bootstrap(self, home: Path, hook_path: str, exe: str | None = None) -> None:
        """Account-level setup the CLI needs before a launch (e.g. its static hooks file); idempotent, as the account."""

    def models(self, exe: str, home: Path) -> list[dict]:
        return []

    def usage(self, exe: str, home: Path) -> dict | None:
        return None


# ---------------------------------------------------------------------- helpers shared by providers

def guard_path(hook_path: str) -> str:
    """bin/acc-guard, installed next to acc-hook: every CLI's pre-tool hook (guardrails, and the "doing now" line)."""
    return str(Path(hook_path).with_name("acc-guard"))


def run(argv: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    """A CLI's status or list command. stdin is closed so none of them can sit waiting for input (a sign-in code)."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json_0600(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".acc-tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def mcp_servers_json(inputs: "LaunchInputs", catalog: "Catalog | None", placeholder: str) -> dict:
    """The selected MCP servers in the common `mcpServers` shape (Claude Code, Copilot CLI, Gemini CLI, …), with
    secret values as placeholders filled in only when the files are written."""
    servers = {}
    for mcp_id in inputs.mcps if catalog else ():
        meta = catalog.mcps[mcp_id].meta
        if meta.get("url"):
            servers[mcp_id] = {"type": "http", "url": meta["url"]}
        else:
            servers[mcp_id] = {"command": meta["command"], "args": list(meta.get("args", [])),
                               "env": {key: placeholder.format(mcp=mcp_id, key=key) for key in meta.get("env", {})}}
    return {"mcpServers": servers}


def joined(existing: str | None, text: str) -> str:
    """Instructions with another paragraph after them."""
    return f"{existing.rstrip()}\n\n{text}\n" if existing and existing.strip() else f"{text}\n"


def fill_placeholders(config: dict, catalog: "Catalog | None", placeholder: str) -> dict:
    for mcp_id, server in config.get("mcpServers", {}).items():
        item = catalog.mcps.get(mcp_id) if catalog else None
        env = server.get("env") or {}
        for key, value in env.items():
            if value == placeholder.format(mcp=mcp_id, key=key):
                env[key] = str(((item.meta.get("env") or {}) if item else {}).get(key, ""))
    return config
