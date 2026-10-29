"""A session as one Markdown document: what it was, what was said, what it wrote down, and how it was started."""
from __future__ import annotations

import re
from datetime import datetime

from .models import Session

ROLES = {"user": "You", "assistant": "Agent"}


def filename(session: Session, day: datetime) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", session.name).strip("-.") or "session"
    return f"{name}-{day:%Y-%m-%d}.md"


def markdown(session: Session, messages: list[tuple[str, str]], docs: list[dict] | None, command: str | None) -> str:
    """`messages`: (role, text) from the history index, in order; `docs`: {"path", "content"}, or None if unreadable."""
    branch = (session.changes or {}).get("branch") or session.worktree_branch
    state = session.observed_state + (" (archived)" if session.desired_state == "archived" else "")
    rows = [("Project", (session.project.slug if session.project else (session.deleted_project or {}).get("slug", "Deleted project"))), ("Agent", session.provider),
            ("Model", session.launch_spec.inputs.get("model") or "default"), ("Created", f"{session.created_at:%Y-%m-%d %H:%M} UTC"),
            ("State", state), ("Branch", branch), ("Worktree", session.worktree), ("Session id", session.id)]
    out = [f"# {session.name}", "", "| | |", "|---|---|"]
    out += [f"| {label} | {_cell(value)} |" for label, value in rows if value]
    out += ["", "## Conversation", ""]
    if not messages:
        out += ["_Not indexed yet: Rynhult indexes a session's conversation shortly after it does something._", ""]
    for role, text in messages:
        out += [f"### {ROLES.get(role, role or 'Message')}", "", text.strip(), ""]
    title = "Findings" if session.workdir else "Handoffs"
    out += [f"## {title}", ""]
    if docs is None:
        out += ["_The session's folder couldn't be read._", ""]
    elif not docs:
        out += ["_None._", ""]
    for doc in docs or []:
        out += [f"### {doc['path']}", "", _fence(doc["content"]), ""]
    out += ["## Launch command", "", _fence(command, "sh") if command else "_Not recorded._", ""]
    return "\n".join(out)


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _fence(text: str, lang: str = "markdown") -> str:
    """A code fence longer than any run of backticks inside, so the content can't close it."""
    ticks = "`" * max(3, max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"
