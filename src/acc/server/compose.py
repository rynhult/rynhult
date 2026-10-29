"""Compose stacks an owner approved (D4, docs/wiki/Internal/Security.md "Project stacks").

Agents can edit a project's compose files, and the stack runs under the services account's rootless Docker, which is
the human owner on a standard install: a volume mount in compose.yaml reaches the owner's home. So autostart only runs
a stack whose files an owner approved. Turning autostart on approves the files as they are then (pin); when they
change, autostart holds the stack and the project card shows the change for an owner to approve (review).
"""
from __future__ import annotations

import difflib

from .supervisor_common import now

MAX_DIFF = 64 * 1024  # characters of a change shown
CHANGED = "changed since they were approved"  # in the broker's compose_up refusal (acc.broker.ops)


class ComposeChanged(Exception):
    """The stack isn't what an owner approved: autostart holds it."""


def pin(current: dict, who: str) -> dict:
    """What the project records when an owner approves its stack (current: the broker's compose_digest)."""
    return {"digest": current["digest"], "files": current.get("files") or {}, "at": now().isoformat(), "by": who}


def review(approved: dict | None, current: dict) -> dict:
    """Whether the stack still is what an owner approved, and if not, the change (a unified diff of the files)."""
    if approved and approved.get("digest") == current["digest"]:
        return {"approved": True, "digest": current["digest"], "at": approved.get("at"), "by": approved.get("by")}
    before, after = (approved or {}).get("files") or {}, current.get("files") or {}
    diff = []
    for name in sorted(set(before) | set(after)):
        if before.get(name) != after.get(name):
            diff += difflib.unified_diff((before.get(name) or "").splitlines(keepends=True),
                                         (after.get(name) or "").splitlines(keepends=True),
                                         fromfile=f"approved/{name}" if name in before else "/dev/null",
                                         tofile=f"now/{name}" if name in after else "/dev/null")
    text = "".join(line if line.endswith("\n") else line + "\n" for line in diff)
    note = None
    if approved is None:
        note = "Never approved: autostart runs this stack only after an owner approves its compose files."
    elif not text:
        note = ("The files shown are unchanged, but what compose resolves changed: .env, an included or extended file, "
                "an env_file, or the stack can't be read." + ("" if current.get("resolved") else
                                                               " docker compose config failed on it now."))
    return {"approved": False, "digest": current["digest"], "diff": text[:MAX_DIFF], "truncated": len(text) > MAX_DIFF,
            "note": note, "at": (approved or {}).get("at"), "by": (approved or {}).get("by")}
