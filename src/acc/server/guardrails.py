"""Guardrails: hard limits on what agents' tools may do, enforced by bin/acc-guard (each CLI's pre-tool hook).

Owners choose the rules in Settings; each session gets them frozen into its launch files (guardrails.json), so a
change applies to sessions started afterwards. Rules are on by default: they only block what ACC's own workflow never
needs (agents push branches and open pull requests).
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session as DbSession

from .models import Setting

KEY = "guardrails"
RULES = {
    "force_push": ("No force pushes", "git push --force / -f / --force-with-lease, and +refspecs."),
    "push_default": ("No pushes to main or master", "Agents push a branch and open a pull request instead."),
    "merge_pr": ("No merging pull requests", "gh pr merge in any form, and gh api calls that merge a pull request or "
                 "turn on auto-merge. A person merges: in the session's Pull request tab or on GitHub."),
    "rm_outside": ("No recursive deletes outside the workspace", "rm -r on paths outside the session's folder, "
                   "including ~, $HOME and /; agents may still clean up their own files in /tmp."),
    "secrets": ("Keep out of secrets", ".env files (not .env.example), private keys, the agent CLIs' sign-in "
                "tokens and other credential files, read or written by any tool or command."),
}
MAX_CUSTOM = 50


def load(db: DbSession) -> dict:
    """The owner's choices, with defaults for anything not set: every rule on, no custom patterns."""
    row = db.get(Setting, KEY)
    value = row.value if row else {}
    rules = {key: bool((value.get("rules") or {}).get(key, True)) for key in RULES}
    custom = [c for c in value.get("custom") or [] if isinstance(c, dict) and c.get("pattern")]
    from .security_settings import BUILTINS, resolve
    effective = resolve(db)
    rules.update({key: effective['switches'][key] for key in BUILTINS})
    return {"rules": rules, "custom": custom}


def validate(data: dict) -> dict:
    rules = data.get("rules") or {}
    unknown = set(rules) - set(RULES)
    if unknown:
        raise ValueError(f"unknown rules: {', '.join(sorted(unknown))}")
    custom = []
    for entry in (data.get("custom") or [])[:MAX_CUSTOM + 1]:
        pattern = str(entry.get("pattern") or "").strip()
        if not pattern:
            continue
        if len(pattern) > 300:
            raise ValueError("patterns are limited to 300 characters")
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(f"{pattern!r} is not a valid regular expression: {error}") from None
        custom.append({"pattern": pattern, "reason": str(entry.get("reason") or "").strip()[:200]})
    if len(custom) > MAX_CUSTOM:
        raise ValueError(f"at most {MAX_CUSTOM} custom patterns")
    return {"rules": {key: bool(rules.get(key, True)) for key in RULES}, "custom": custom}


def save(db: DbSession, data: dict, actor: str) -> dict:
    value = validate(data)
    db.merge(Setting(key=KEY, value=value, updated_by=actor))
    return value


def policy(db: DbSession, workspace: str, owner: str | None = None, project_id: int | None = None,
           local_only: bool = False, *, machine: str | None = None, account: str | None = None) -> dict:
    """What a new session's guardrails.json holds."""
    from .security_settings import BUILTINS, lock_revision, resolve
    lock_revision(db)
    effective = resolve(db, project_id=project_id, owner=owner, machine=machine, account=account)
    value = load(db)
    return {"rules": {key: effective['switches'][key] for key in BUILTINS},
            "custom": value['custom'] if effective['switches']['custom_rules'] else [],
            "workspace": workspace, "tool_approval": effective['switches']['tool_install_check'],
            "local_only": local_only, "security": effective}
