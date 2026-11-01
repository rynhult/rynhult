"""Personal launch placement, using enrolled accounts and the existing unprivileged team checkout operations."""
import platform
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select

from .. import providers
from .broker_client import BrokerError
from .group_remote import reserved_for_other
from .models import Node, Placement, Project, Session


def scope(ctx, who, project_id=None, machine=None):
    with ctx.db() as db:
        project = ctx.project_for(db, who, project_id) if project_id is not None else None
        account = project.account if project else who.account
        host = machine or (project.host if project else None) or "local"
        if machine is None:
            return account, host
    if not who.is_owner and account != who.account:
        raise HTTPException(404, "That machine account wasn't found.")
    if host == "local":
        accounts = ctx.broker.accounts()
    else:
        link = ctx.machine(host, who)
        accounts = link.accounts
        with ctx.db() as db:
            if not who.is_owner and reserved_for_other(db, host, account, who.username):
                raise HTTPException(404, "That machine account wasn't found.")
    if account not in accounts:
        raise HTTPException(422, "This machine has no matching agent account for the project. Choose another machine.")
    return account, host


def repository(ctx, who, project_id, machine):
    """Only recorded, accessible checkouts; never a client-supplied path or URL."""
    with ctx.db() as db:
        project = ctx.project_for(db, who, project_id)
        if machine == (project.host or "local"):
            return project.workspace_path
        prior = db.scalar(select(Session.team_repo).join(Placement, Session.placement_id == Placement.id).where(
            Session.project_id == project_id, Session.owner == who.username, Session.run_as == project.account,
            Placement.host == machine, Session.team_repo.is_not(None)).order_by(Session.created_at.desc()).limit(1))
        if prior:
            return prior
        if project.remote_url:
            return db.scalar(ctx.scope(select(Project.workspace_path), Project, who).where(
                Project.host == machine, Project.account == project.account, Project.archived.is_(False),
                Project.trusted.is_(True), Project.import_mode == "managed",
                Project.remote_url == project.remote_url).limit(1))
    return None


async def inventory(ctx, who, project_id=None):
    account, _ = scope(ctx, who, project_id)
    with ctx.db() as db:
        rows = [("local", "This Rynhult (main)", True, platform.system(), ctx.broker.accounts())]
        for node in db.scalars(select(Node).where(Node.status == "active").order_by(Node.name)):
            if not who.is_owner and reserved_for_other(db, node.name, account, who.username):
                continue
            link = ctx.broker.hub.links.get(node.name) if ctx.broker.hub else None
            accounts = link.accounts if link and not link.closed else (node.info or {}).get("accounts", [])
            if not who.is_owner and account not in accounts:
                continue
            from .api.nodes import describe_system
            rows.append((node.name, node.name, bool(link and not link.closed),
                         describe_system(node.info or {})["label"], accounts))
    result = []
    for name, label, connected, system, accounts in rows:
        reason = "Offline. Reconnect this machine." if not connected else (
            "The project's agent account is not available on this machine." if account not in accounts else None)
        installed = {}
        if reason is None:
            try:
                installed = await ctx.broker.call("installed_providers", account=account, host=name)
            except BrokerError:
                reason = "Could not check agents. Reconnect the machine and try again."
        result.append({"name": name, "label": label, "connected": connected, "system": system,
                       "available": reason is None, "reason": reason,
                       "providers": [{"id": p.id, "label": p.label,
                                      "installed": bool(installed.get(p.id)) if installed else None}
                                     for p in providers.PROVIDERS.values()]})
    return result


async def checkout(ctx, who, project, machine, name, branch, *, clone=False):
    repo = repository(ctx, who, project.id, machine)
    if repo is None:
        if not clone:
            raise HTTPException(409, f"Choose Clone on {machine} before launching this project's first session there.")
        source_host = project.host or "local"
        root = str(ctx.settings.account_root(project.account)) if source_host == "local" else str(Path(project.workspace_path).parent)
        transfer = await ctx.broker.call("make_worktree", account=project.account, host=source_host,
            repo=project.workspace_path, root=root, name=f"transfer-{uuid.uuid4().hex[:12]}", branch=branch)
        try:
            bundle = await ctx.broker.call("team_bundle", account=project.account, host=source_host,
                                          path=transfer["path"], branch=transfer["branch"])
        finally:
            await ctx.broker.call("remove_worktree", account=project.account, host=source_host,
                                  repo=project.workspace_path, path=transfer["path"])
        made = await ctx.broker.call("team_checkout", account=project.account, host=machine, name=name, branch=branch, **bundle)
        return made, made["repo"]
    layout = await ctx.broker.call("ping", account=project.account, host=machine)
    roots = layout.get("roots") or []
    if not roots or not isinstance(roots[0], str) or not roots[0].startswith("/"):
        raise HTTPException(409, "Update this machine's Rynhult before creating a checkout there.")
    made = await ctx.broker.call("make_worktree", account=project.account, host=machine,
                                 repo=repo, root=roots[0], name=name, branch=branch)
    return made, repo
