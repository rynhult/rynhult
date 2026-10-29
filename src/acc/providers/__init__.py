"""The agent providers ACC can run, by id. See base.py for what each supplies and docs/PROVIDERS.md for the plan."""
from __future__ import annotations

from .antigravity import PROVIDER as ANTIGRAVITY
from .base import CompileContext, CompileError, Option, Provider  # noqa: F401 (re-exported)
from .claude import PROVIDER as CLAUDE
from .codex import PROVIDER as CODEX
from .copilot import PROVIDER as COPILOT
from .cursor import PROVIDER as CURSOR
from .opencode import PROVIDER as OPENCODE

PROVIDERS: dict[str, Provider] = {p.id: p for p in (CLAUDE, CODEX, COPILOT, ANTIGRAVITY, CURSOR, OPENCODE)}
ID_PATTERN = r"^[a-z][a-z0-9-]{1,23}$"


def get(provider_id: str) -> Provider:
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        raise CompileError(f"unknown provider {provider_id!r}") from None


def ids() -> list[str]:
    return list(PROVIDERS)


def executables(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Provider id -> command (ACC_PROVIDERS can point any of them elsewhere; `gh` is GitHub's CLI, not a provider)."""
    return {**{p.id: p.executable for p in PROVIDERS.values()}, "gh": "gh", **(overrides or {})}
