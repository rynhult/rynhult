"""Local workspaces: sessions that need no project (Researcher, Council: is this niche worth pursuing?).

Workspaces stay on this host: each is a new folder, local/<topic>-<date> under the workspace root, with local git
history only (no remote, no CI), trusted by the broker and grouped under a built-in "Local workspaces" project.
Projects are the other kind: a GitHub repository with the protect-main ruleset and CI/CD. Promoting a workspace
creates that repository, pushes the workspace's history and registers it as a project.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from sqlalchemy import select

from .models import Project

SLUG = "local"
NAME = "Local workspaces"
BUILTIN = "local"  # Project.builtin


def topic_folder(topic: str, existing: set[str], today: dt.date | None = None) -> str:
    base = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", topic.lower())).strip("-")[:50] or "workspace"
    name = f"{base}-{(today or dt.date.today()).strftime('%Y%m%d')}"
    candidate, n = name, 2
    while candidate in existing:
        candidate, n = f"{name}-{n}", n + 1
    return candidate


def root(workspaces_root: Path) -> Path:
    return workspaces_root / "local"


class LocalProjectError(Exception):
    """The built-in project ACC would use doesn't match whose and where it should be: refused, never used."""


def reserved_slug(slug: str) -> bool:
    """"local" and "local-<anything>" are the built-in projects' slugs: nobody registers, imports or promotes to them."""
    return slug == SLUG or slug.startswith(f"{SLUG}-")


def find_project(db, account: str) -> Project | None:
    """An account's built-in project on this machine: by its identity (account and host), not by slug."""
    return db.scalar(select(Project).where(Project.builtin == BUILTIN, Project.account == account,
                                           Project.host == "local").order_by(Project.id))


def check_project(project: Project, account_root: Path, account: str, owns) -> Project:
    """Refuse a built-in project that isn't this account's, on this machine, in its local folder, or whose owner isn't
    the caller (`owns(project)`: theirs, or the caller is an owner)."""
    if (project.builtin != BUILTIN or project.account != account or (project.host or "local") != "local"
            or project.workspace_path != str(root(account_root)) or not owns(project)):
        raise LocalProjectError(f"the local workspaces project {project.slug!r} doesn't belong to you or this account; "
                                "an owner has to sort it out")
    return project


def ensure_project(db, account_root: Path, account: str, owner: str | None, default_account: str, owns) -> Project:
    """The built-in project that groups an account's local workspaces (created on first use): "local" for the default
    account, "local-<account>" for others, owned by the person who first uses it. Found by account and host; one that
    doesn't match (another account's, another machine, another folder, not the caller's) is refused."""
    project = find_project(db, account)
    if project is None:
        slug = SLUG if account == default_account else f"{SLUG}-{account}"
        if db.scalar(select(Project.id).where(Project.slug == slug)) is not None:
            raise LocalProjectError(f"a project already uses the slug {slug!r}, which is for local workspaces; an owner "
                                    "has to rename it")
        project = Project(slug=slug, name=NAME, workspace_path=str(root(account_root)), trusted=True, account=account,
                          owner=owner, import_mode="managed", builtin=BUILTIN, host="local",
                          notes="Built in: local workspaces (one folder each, local git only). Promote one to make it a project with GitHub and CI.")
        db.add(project)
        db.flush()
        return project
    check_project(project, account_root, account, owns)
    if project.archived:
        project.archived = False
    return project
