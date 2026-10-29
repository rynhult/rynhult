"""Launch compiler: pure function from launch inputs (+ the catalog) to generated files and provider argv.

The compiled files are stored in the launch spec and reused verbatim on every resume, because the CLIs don't
restore their per-launch settings themselves (M1 qualification, test 2). Everything a resume needs is therefore in
the files: build_args() derives flags from them. What differs per CLI lives in acc.providers.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import yaml

from acc.env import PROTECTED_SESSION_VARIABLES, Environment

from .. import providers
from ..providers.base import ENDPOINT_FILE, CompileContext, CompileError  # noqa: F401 (CompileError re-exported)
from ..providers.claude import HOOK_EVENTS as CLAUDE_HOOK_EVENTS  # noqa: F401
from ..providers.claude import SECRET_PLACEHOLDER  # noqa: F401 (compatibility re-export)
from ..providers.codex import mcp_secrets as _codex_secrets  # noqa: F401
from .catalog import Catalog
from .catalog_import import INLINE_SHELL, allowed_frontmatter, parse_frontmatter


@dataclass(frozen=True)
class LaunchInputs:
    provider: str
    model: str | None = None
    effort: str | None = None
    permission_mode: str = "acceptEdits"  # Claude; always passed explicitly (M1 finding F6)
    sandbox: str = "workspace-write"  # Codex
    approval: str = "on-request"  # Codex
    remote_control: bool = False  # Claude only
    append_prompt: str | None = None  # extra instructions, appended after the profile's
    initial_prompt: str | None = None  # first launch only, never on resume
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    profile: str | None = None
    skills: tuple[str, ...] = field(default_factory=tuple)
    agents: tuple[str, ...] = field(default_factory=tuple)
    mcps: tuple[str, ...] = field(default_factory=tuple)
    options: dict = field(default_factory=dict)  # every other provider's launch options (see its option schema)
    endpoint: str | None = None  # a model endpoint (local LLM server or compatible API) instead of the vendor's models

    def as_dict(self) -> dict:
        data = self.__dict__.copy()
        for key in ("extra_args", "skills", "agents", "mcps"):
            data[key] = list(data[key])
        if not data["options"]:
            data.pop("options")  # launch specs (and their digests) from before the registry stay identical
        else:
            data["options"] = dict(data["options"])
        if data["endpoint"] is None:
            data.pop("endpoint")
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "LaunchInputs":
        data = dict(data)
        for key in ("extra_args", "skills", "agents", "mcps"):
            data[key] = tuple(data.get(key) or ())
        data["options"] = dict(data.get("options") or {})
        return cls(**data)


@dataclass(frozen=True)
class Compiled:
    files: dict[str, str]
    digest: str


def safe_markdown(content: str) -> str:
    """A skill or agent file as sessions get it: the frontmatter is parsed (catalog_import.parse_frontmatter) and
    rebuilt from ALLOWED_FRONTMATTER alone, so settings that would widen the session (hooks, pre-approved tools,
    MCP servers, permission mode) never pass, however they are spelled; a block that can't be read safely becomes
    an empty one. `!`cmd`` (run when a skill loads) is defused."""
    parsed = parse_frontmatter(content)
    if parsed.present:
        meta = allowed_frontmatter(parsed.meta)
        block = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1 << 30) if meta else ""
        content = f"---\n{block}---\n{parsed.body}"
    return INLINE_SHELL.sub(lambda match: "! " + match.group(0)[1:], content)


def _check_catalog(inputs: LaunchInputs, catalog: Catalog | None) -> None:
    if not (inputs.profile or inputs.skills or inputs.agents or inputs.mcps):
        return
    if catalog is None:
        raise CompileError("catalog is not loaded")
    if inputs.profile and inputs.profile not in catalog.profiles:
        raise CompileError(f"unknown profile {inputs.profile!r}")
    profile_providers = catalog.profiles.get(inputs.profile or "", {}).get("providers")
    if profile_providers and inputs.provider not in profile_providers:
        raise CompileError(f"profile {inputs.profile!r} is not available for {inputs.provider}")
    for kind, pool in (("skills", catalog.skills), ("agents", catalog.agents), ("mcps", catalog.mcps)):
        for item_id in getattr(inputs, kind):
            item = pool.get(item_id)
            if item is None:
                raise CompileError(f"unknown {kind[:-1]} {item_id!r}")
            reason = item.unavailable(inputs.provider)
            if reason:
                raise CompileError(f"{kind[:-1]} {item_id!r} is not available for {inputs.provider}: {reason}")


def not_delivered(inputs: LaunchInputs, catalog: Catalog | None) -> list[dict]:
    """What the launch's profile holds that this provider won't receive, each with the reason (shown before launch).

    The API fills a profile launch with only the items the provider can use, and a provider without the
    "instructions" capability gets no per-session instructions: without this list both would vanish unseen."""
    profile = catalog.profiles.get(inputs.profile or "") if catalog else None
    if profile is None:
        return []
    provider = providers.get(inputs.provider)
    left = []
    if profile.get("instructions", "").strip() and "instructions" not in provider.supports:
        left.append({"kind": "instructions", "id": profile["id"],
                     "reason": f"{provider.label} has no per-session way to receive Rynhult's profile instructions"})
    for kind in ("skills", "agents", "mcps"):
        pool = getattr(catalog, kind)
        for item_id in profile.get(kind, []):
            reason = pool[item_id].unavailable(inputs.provider) if item_id in pool else "not in the catalog"
            if reason:
                left.append({"kind": kind[:-1], "id": item_id, "reason": reason})
    return left


def _instructions(inputs: LaunchInputs, catalog: Catalog | None) -> str:
    parts = []
    if inputs.profile and catalog:
        profile = catalog.profiles[inputs.profile]
        parts.append(f"# Rynhult profile: {profile['name']}\n\n{profile.get('instructions', '').strip()}")
    if inputs.append_prompt:
        parts.append(inputs.append_prompt.strip())
    return "\n\n".join(parts)


def _context(hook_path: str, statusline_path: str, catalog: Catalog | None, agent_home: str,
             plugins: dict | None) -> CompileContext:
    return CompileContext(hook_path=hook_path, statusline_path=statusline_path, catalog=catalog, agent_home=agent_home,
                          plugins=plugins)


def _endpoint_file(inputs: LaunchInputs, provider, endpoint: dict | None) -> dict[str, str]:
    """endpoint.json for a launch on a model endpoint: which URL and API style this provider uses there."""
    if not inputs.endpoint:
        return {}
    if not endpoint or endpoint.get("id") != inputs.endpoint:
        raise CompileError(f"unknown model endpoint {inputs.endpoint!r}")
    style = next((s for s in provider.endpoint_styles if (endpoint.get("styles") or {}).get(s)), None)
    if style is None:
        speaks = " or ".join(sorted(endpoint.get("styles") or {})) or "no known"
        raise CompileError(f"{provider.label} can't use {endpoint['name']}: it speaks {speaks} APIs")
    if not inputs.model:
        raise CompileError(f"choose one of {endpoint['name']}'s models")
    info = {"id": endpoint["id"], "name": endpoint["name"], "style": style, "url": endpoint["styles"][style]}
    if endpoint.get("context_tokens"):
        info["context_tokens"] = int(endpoint["context_tokens"])
    return {ENDPOINT_FILE: json.dumps(info, indent=2) + "\n"}


def compile_files(inputs: LaunchInputs, hook_path: str, statusline_path: str, catalog: Catalog | None = None,
                  agent_home: str = "/home/agent", codex_plugins: dict | None = None,
                  endpoint: dict | None = None, guardrails: dict | None = None) -> Compiled:
    provider = providers.get(inputs.provider)
    _check_catalog(inputs, catalog)
    provider.validate(inputs)
    instructions = _instructions(inputs, catalog)
    if guardrails and guardrails.get("local_only"):
        instructions += "\nPrivate development workspace: local git only. Commit locally. Do not push, add or change git remotes, or create remote repositories. Promotion to a project is a separate explicit action in Rynhult. Rynhult manages its own internal git backup.\n"
    if guardrails is not None:
        instructions += ("\n\nUse installed tools freely; dependencies inside this session's project are allowed. "
                         "Account or machine tool installation, upgrades and removal need approval unless your person "
                         "or project is exempt: acc-request tool --manager <manager> --name <package> "
                         '[--version <v>] --reason "<why>" (add --action remove for removal). Wait for the person\'s '
                         "Inbox decision; never work around a refusal.")
    files = provider.compile(inputs, _context(hook_path, statusline_path, catalog, agent_home, codex_plugins),
                             "")
    from .agent_contract import CORE
    # Use the provider's context delivery even without a catalog. Put the platform
    # core ahead of optional profile text (some CLIs cap instruction length).
    files = provider.add_context(files, CORE + ('\n\n' + instructions.strip() if instructions.strip() else ''))
    files.update(_endpoint_file(inputs, provider, endpoint))
    if guardrails is not None:  # read by bin/acc-guard before each tool call
        files["guardrails.json"] = json.dumps(guardrails, indent=2, sort_keys=True) + "\n"
    digest = hashlib.sha256(json.dumps({"inputs": inputs.as_dict(), "files": files}, sort_keys=True).encode()).hexdigest()
    return Compiled(files=files, digest=digest)


def resume_skills_override(inputs: LaunchInputs, files: dict[str, str], catalog: Catalog | None, agent_home: str,
                           codex_plugins: dict | None) -> list[str]:
    """Extra arguments a resume needs that the stored files can't carry (Codex: skills added since launch)."""
    return providers.get(inputs.provider).resume_extra(inputs, files, _context("", "", catalog, agent_home, codex_plugins))


def session_environment(inputs: LaunchInputs, catalog: Catalog | None, run_dir: str = "",
                        files: dict[str, str] | None = None, endpoint_key: str | None = None,
                        native_id: str | None = None) -> dict[str, str]:
    """Environment the session gets through its 0600 env file (Codex: MCP secrets, passed to servers by name;
    Copilot: where its instructions are; OpenCode: its config and conversation; any provider: its model endpoint and
    key)."""
    values = providers.get(inputs.provider).env(inputs, catalog, run_dir, files, endpoint_key, native_id=native_id)
    for key in values:
        if key in PROTECTED_SESSION_VARIABLES:
            raise CompileError(f"{key} is reserved for Rynhult's session launcher")
    return dict(Environment(values))


def materialize(files: dict[str, str], catalog: Catalog | None, provider: str = "claude") -> dict[str, str]:
    """The launch files as written to the run folder: secret placeholders replaced by the catalog's values."""
    return providers.get(provider).materialize(files, catalog)


def build_args(inputs: LaunchInputs, *, run_dir: str, tmux_name: str, native_id: str | None, resume: bool,
               files: dict[str, str] | None = None, extra_overrides: list[str] | None = None) -> list[str]:
    """Provider argv (without the executable) for a first launch or a resume, from the compiled files."""
    args = providers.get(inputs.provider).args(inputs, run_dir=run_dir, tmux_name=tmux_name, native_id=native_id,
                                               resume=resume, files=files or {}, extra=list(extra_overrides or []))
    args += list(inputs.extra_args)
    if inputs.initial_prompt and not resume:
        args += providers.get(inputs.provider).prompt_args(inputs.initial_prompt)
    return args
