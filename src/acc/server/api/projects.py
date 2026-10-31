"""Projects: registry, trust, knowledge, GitHub views, repo creation and import, compose runtime; audit log."""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, or_, select, update

from .. import attention, audit_search, compose, local_workspace
from ..agent_setup import valid_agent_account
from ..broker_client import BrokerError
from ..issue_sources import IssueSourceError, check_remote, github_remote
from ..models import API_PRINCIPAL, AuditLog, Event, Node, Observation, Project, Session, User
from ..orchestrator import project_briefs as brief
from .context import SLUG, Context, refresh_read
from .schemas import CreateRepoIn, ImportRepoIn, NewProjectIn, ProjectIn, RuntimeIn, project_out

GITHUB_REPO = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")


def router(ctx: Context) -> APIRouter:
    settings, db, supervisor, broker, refused = ctx.settings, ctx.db, ctx.supervisor, ctx.broker, ctx.refused
    api = APIRouter()
    from .project_delete import register
    register(api, ctx)

    @api.get("/api/projects")
    def list_projects(who=Depends(ctx.principal)):
        with db() as s:
            return [project_out(p) for p in s.scalars(ctx.scope(select(Project), Project, who).order_by(Project.slug))]

    def project_account(s, body: ProjectIn | NewProjectIn, who) -> str:
        """The agent account a project's sessions run as. Here: yours. On another machine: one of the accounts it
        reported: yours if it has it, else (owners only) the one you name, or its only one; members always run as their
        own account, so a machine without it is refused with what it has."""
        if body.host == "local":
            if body.account not in (None, who.account):
                raise HTTPException(422, "Projects on this machine run as your own agent account, so you can't choose another one.")
            return who.account
        node = s.scalar(select(Node).where(Node.name == body.host, Node.status == "active"))
        if node:
            s.execute(update(Node).where(Node.id == node.id).values(status=Node.status))
        link = ctx.broker.hub.links.get(body.host) if ctx.broker.hub else None
        accounts = [a for a in (link.accounts if link else (node.info or {}).get("accounts", []))
                    if valid_agent_account(a)]
        wanted = body.account or who.account
        if not who.is_owner:
            from ..group_remote import reserved_for_other
            if reserved_for_other(s, body.host, wanted, who.username):
                raise HTTPException(404, 'That machine account wasn\'t found.')
        if wanted in accounts and (wanted == who.account or who.is_owner):
            return wanted
        if body.account is None and who.is_owner and len(accounts) == 1:
            return accounts[0]
        have = ", ".join(accounts) or "none reported yet (check that it's connected in Host → Machines)"
        if not who.is_owner:  # which accounts a machine has (other people's) isn't a member's to learn
            raise HTTPException(422, f"{body.host} has no agent account for you ({who.account}). Ask an owner to run that "
                                     f"machine's installer again with --agent {who.account}.")
        raise HTTPException(422, f"Choose which of {body.host}'s agent accounts runs this project's sessions under Agent "
                                 f"account. It has: {have}.")

    def free_slug(slug: str) -> None:
        if not SLUG.fullmatch(slug):
            raise HTTPException(422, "Use only lowercase letters, digits and dashes in the slug.")
        if local_workspace.reserved_slug(slug):
            raise HTTPException(422, "The slugs local and local-… are kept for local workspaces. Choose another slug.")

    @api.post("/api/projects", status_code=201)
    async def create_project(body: ProjectIn, who=Depends(ctx.principal)):
        if body.host == "local":
            if not Path(body.workspace_path).is_absolute():
                raise HTTPException(422, "Enter the workspace path as a full path that starts with /.")
            with db() as s:
                account = project_account(s, body, who)
            try:
                identity = await broker.call("workspace_identity", account=account, path=body.workspace_path)
            except BrokerError as error:
                raise HTTPException(409, "Cannot safely identify this workspace. Ask the owner to update or repair its agent service before registering it.") from error
            body = body.model_copy(update={"workspace_path": identity["path"]})
        return await supervisor.workspace_operations.run(body.workspace_path,
            lambda: _create_project(body, who), body.host)

    async def _create_project(body: ProjectIn, who):
        free_slug(body.slug)
        try:
            check_remote(body.remote_url)
        except IssueSourceError as error:
            raise HTTPException(422, str(error)) from None
        if not Path(body.workspace_path).is_absolute():
            raise HTTPException(422, "Enter the workspace path as a full path that starts with /.")
        with db() as s:
            if body.host != "local" and s.scalar(select(Node.id).where(Node.name == body.host, Node.status == "active")) is None:
                raise HTTPException(422, f"There's no machine named {body.host}. Check the name in Host → Machines." if who.is_owner else
                                    f"{body.host} has no agent account for you ({who.account}). Ask an owner to run that "
                                    f"machine's installer again with --agent {who.account}.")
            account = project_account(s, body, who)
        if body.host == 'local':
            canonical = supervisor.workspace_operations.key(body.workspace_path)[1]
            root = Path(supervisor.workspace_operations.key(local_workspace.root(settings.account_root(account)))[1])
            if Path(canonical).parent == root:
                body = body.model_copy(update={'workspace_path': canonical})
                try:
                    await broker.call('scan_docs', account=account, path=canonical)
                except BrokerError as error:
                    raise HTTPException(409, "That local workspace is no longer available. Reload Workspaces before registering it as a project.") from error
        # A registration can reference a child of a deletion target, whose exact
        # operation key differs. Resolve aliases through the owning broker and
        # refuse that new reference while its ancestor is leased for removal.
        operations = supervisor.workspace_operations
        if operations.other_busy(body.workspace_path, body.host):
            with db() as s:
                account = project_account(s, body, who)
            try:
                identity = await broker.call('workspace_identity', account=account, host=body.host, path=body.workspace_path)
            except BrokerError as error:
                raise HTTPException(409, "Cannot safely register this folder during another workspace operation. Wait and retry.") from error
            canonical = identity['path']
            alias = operations.key(canonical, body.host) != operations.key(body.workspace_path, body.host)

            def deleting():
                return operations.busy_ancestor(canonical, body.host) or (alias and operations.busy(canonical, body.host, kind='delete'))

            if deleting():
                if not await operations.wait_for_deletions(canonical, body.host, include_self=alias):
                    raise HTTPException(409, "An enclosing folder has a workspace operation in progress. Wait for it to finish before registering this folder.")
                try:
                    # The ancestor may now be gone. Only the owning broker may
                    # inspect it; accd must not probe an account's filesystem.
                    await broker.call('scan_docs', account=account, host=body.host, path=canonical)
                except BrokerError as error:
                    raise HTTPException(409, "That folder is no longer available after the enclosing workspace operation. Reload before registering it.") from error
                # No await between this check and the Project commit below.
                if deleting():
                    raise HTTPException(409, "An enclosing folder has a workspace operation in progress. Wait for it to finish before registering this folder.")
        with db() as s:
            if s.scalar(select(Project).where(Project.slug == body.slug)):
                raise HTTPException(409, "A project with that slug already exists. Choose another slug.")
            if body.host != "local" and s.scalar(select(Node.id).where(Node.name == body.host, Node.status == "active")) is None:
                raise HTTPException(422, f"There's no machine named {body.host}. Check the name in Host → Machines." if who.is_owner else
                                    f"{body.host} has no agent account for you ({who.account}). Ask an owner to run that "
                                    f"machine's installer again with --agent {who.account}.")
            account = project_account(s, body, who)
            project = Project(**body.model_dump(exclude={"account"}), account=account, owner=who.username)
            s.add(project)
            s.add(AuditLog(actor=who.username, action="project.create", target=body.slug, detail=body.model_dump()))
            s.commit()
        # Local workspace existence was checked by its agents broker under the
        # operation lock. Ordinary registration still records a path without probing.
        # The first launch checks the repository and fills missing identity via
        # the owning account's broker, just like other workspace operations.
        return project_out(project)

    @api.get("/api/projects/{project_id}/first-commit")
    async def first_commit_status(project_id: int, who=Depends(ctx.principal)):
        with db() as s:
            project = ctx.project_for(s, who, project_id)
        try:
            return await broker.call('project_first_commit', account=project.account, host=project.host or 'local',
                                     path=project.workspace_path)
        except BrokerError as error:
            raise refused(error)

    @api.post("/api/projects/{project_id}/first-commit")
    async def first_commit(project_id: int, who=Depends(ctx.principal)):
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            if project.import_mode != 'managed' or project.group_id is not None or project.archived:
                raise HTTPException(409, 'Make the first commit only in an active managed personal project.')
        async def commit():
            with db() as s:
                person = s.scalar(select(User).where(User.username == project.owner)) if project.owner else None
                name = person.username if person else 'Rynhult'
                email = f'{person.username if person else "rynhult"}@noreply.rynhult.invalid'
            result = await broker.call('project_first_commit', account=project.account, host=project.host or 'local',
                                       path=project.workspace_path, title=project.name, create=True, name=name, email=email)
            with db() as s:
                if result['created']:
                    s.add(AuditLog(actor=who.username, action='project.first_commit', target=project.slug,
                                   detail={'pushed': False}))
                    s.commit()
            return result
        try:
            return await supervisor.workspace_operations.run(project.workspace_path, commit, project.host or 'local')
        except BrokerError as error:
            if "README.md already exists." in str(error):
                raise HTTPException(409, "README.md already exists. Commit it manually; Rynhult will not overwrite it.") from None
            raise refused(error)

    @api.get("/api/projects/{project_id}/git-identity")
    async def get_git_identity(project_id: int, who=Depends(ctx.principal)):
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            path, account, host = project.workspace_path, project.account, project.host or 'local'
        try:
            return await broker.call('git_identity', account=account, host=host, path=path)
        except BrokerError as error:
            raise refused(error)

    @api.put("/api/projects/{project_id}/git-identity")
    async def put_git_identity(project_id: int, body: dict, who=Depends(ctx.principal)):
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            if project.import_mode != 'managed' or project.group_id is not None:
                raise HTTPException(409, 'Git author settings can only be changed for a managed personal project.')
            path, account, host, slug = project.workspace_path, project.account, project.host or 'local', project.slug
        if set(body) != {'name', 'email'} or any(not isinstance(body[k], str) or not body[k].strip()
                or len(body[k]) > 200 or any(ord(c) < 32 or ord(c) == 127 or c in '<>' for c in body[k]) for k in body):
            raise HTTPException(422, 'Enter a Git author name and email without control characters.')
        if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', body['email']):
            raise HTTPException(422, 'Enter a valid Git author email address.')
        try:
            result = await broker.call('git_identity', account=account, host=host, path=path, replace=True, **body)
        except BrokerError as error:
            raise refused(error)
        if not result['supported']:
            raise HTTPException(409, 'This folder does not have a Git repository yet.')
        with db() as s:
            s.add(AuditLog(actor=who.username, action='project.git_identity', target=slug,
                           detail={**result, 'source': 'project_settings', 'scope': 'repository-local'}))
            s.commit()
        return result

    @api.post("/api/projects/{project_id}/trust")
    async def trust_project(project_id: int, who=Depends(ctx.principal)):
        """the owner confirms the workspace is trusted; Rynhult records it for both CLIs (M1 finding F1)."""
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            path, account, host = project.workspace_path, project.account, project.host or "local"
            s.rollback()  # no transaction (and no write lock) while the broker works
        try:
            result = await broker.call("trust_workspace", account=account, host=host, path=path)
        except BrokerError as error:
            raise refused(error)
        with db() as s:
            project = s.get(Project, project_id)
            project.trusted = True
            s.add(AuditLog(actor=who.username, action="project.trust", target=project.slug, detail=result))
            s.commit()
            return project_out(project)

    @api.post("/api/projects/{project_id}/archive")
    def archive_project(project_id: int, who=Depends(ctx.principal)):
        """Hide a project from launch lists. Refused while it has live Rynhult sessions; files are untouched."""
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            live = s.scalars(select(Session).where(Session.project_id == project_id,
                                                   Session.desired_state == "running")).first()
            if live is not None:
                raise HTTPException(409, f"This project still has running sessions ({live.name}). Stop or archive them first.")
            project.archived = True
            project.compose_autostart = False
            s.add(AuditLog(actor=who.username, action="project.archive", target=project.slug))
            s.commit()
            return project_out(project)

    @api.get("/api/projects/{project_id}/knowledge")
    async def project_knowledge(project_id: int, who=Depends(ctx.principal)):
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            path, repo, account, host = project.workspace_path, project.repo, project.account, project.host or "local"
        try:
            docs = await broker.call("scan_docs", account=account, host=host, path=path)
        except BrokerError as error:
            raise refused(error)
        return {"docs": docs, "repo": repo}

    @api.get("/api/projects/{project_id}/knowledge/doc")
    async def project_doc(project_id: int, path: str, who=Depends(ctx.principal)):
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            workspace, account, host = project.workspace_path, project.account, project.host or "local"
        try:
            return await broker.call("read_doc", account=account, host=host, path=workspace, rel=path)
        except BrokerError as error:
            raise refused(error)

    @api.get("/api/audit")
    def audit(limit: int = 200, action: str | None = None, actor: str | None = None, since: datetime.date | None = None,
              q: str = Query("", max_length=256), before_id: int | None = Query(None, gt=0, le=9223372036854775807), page: bool = False,
              who=Depends(ctx.owner)):
        with db() as s:
            query = select(AuditLog)
            if action:
                query = query.where(AuditLog.action.like(f"{action}%"))
            if actor:
                query = query.where(AuditLog.actor == actor)
            if since:  # a day, from its start (the Audit page's "since" filter)
                query = query.where(AuditLog.ts >= datetime.datetime.combine(since, datetime.time(), datetime.timezone.utc))
            if q.strip():
                # Literal substring matching: '%' and '_' are text, not wildcard operators.
                query = query.where(or_(*(column.icontains(q.strip(), autoescape=True) for column in (
                    AuditLog.actor, AuditLog.action, AuditLog.target, cast(AuditLog.detail, String))),
                    cast(AuditLog.detail, String).icontains(json.dumps(q.strip(), ensure_ascii=True)[1:-1], autoescape=True)))
            if before_id is not None:
                query = query.where(AuditLog.id < before_id)
            size = max(1, min(limit, 1000))
            try:
                with audit_search.budget(s):
                    found = list(s.scalars(query.order_by(AuditLog.id.desc()).limit(size + 1 if page else size)))
            except audit_search.SearchBudgetExceeded:
                raise HTTPException(503, "Audit search reached its time or work limit. Narrow Type, Person or Since, then retry.") from None
            more = page and len(found) > size
            items = [{"id": a.id, "ts": a.ts, "actor": a.actor, "action": a.action, "target": a.target, "detail": a.detail}
                     for a in found[:size]]
            return {"items": items, "has_more": more, "next_before": items[-1]["id"] if more else None} if page else items

    @api.get("/api/projects/{project_id}/github")
    async def project_github(project_id: int, refresh: int = 0, who=Depends(ctx.principal)):
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            if project.private_workspace:
                raise HTTPException(409, "Private workspaces have no repository to show.")

            if not github_remote(project.remote_url) or (project.issue_sources or {}).get("gitlab"):
                return {"error": "This project's repository isn't on GitHub. Use Issue sources to see its issues."}
            owners = project.owner in (None, API_PRINCIPAL) or \
                s.scalar(select(User.role).where(User.username == project.owner)) == "owner"
        if not owners:  # Rynhult's GitHub token is the owner's: it must not read repos for members' (or removed people's) projects
            return {"error": "GitHub views are available for owners' projects: they use the owner's GitHub token."}
        if refresh:
            with refresh_read():
                await supervisor.refresh_github(project_id)
        with db() as s:
            observation = s.get(Observation, f"github.{project_id}")
            return observation.value if observation else {}

    async def machine_root(host: str, account: str) -> Path:
        """Where `account`'s new projects go on `host`: the first of its broker's allowed roots (its ping says, from
        Rynhult 0.x with New project on machines), else the node installers' defaults (Linux /srv/acc/workspaces, a Mac
        ~/acc-workspaces)."""
        try:
            info = await broker.call("ping", account=account, host=host)
        except BrokerError:
            raise HTTPException(409, "The machine didn't answer. Check that it's connected in Host → Machines, then try again.") from None
        roots = info.get("roots") if isinstance(info, dict) else None
        if isinstance(roots, list) and roots and isinstance(roots[0], str) and roots[0].startswith("/"):
            return Path(roots[0])
        with db() as s:
            node = s.scalar(select(Node).where(Node.name == host, Node.status == "active"))
        if (node.info or {}).get("os") == "darwin" if node else False:
            home = info.get("home") if isinstance(info, dict) else None
            if isinstance(home, str) and home.startswith("/"):
                return Path(home) / "acc-workspaces"
        return Path("/srv/acc/workspaces")

    def where(body: NewProjectIn, who) -> tuple[str, str]:
        """(host, account) a new project is made on: this machine as yours, or a connected machine as one of its
        accounts (the same rule as registering a folder there)."""
        if body.host == "local":
            if body.account not in (None, who.account):
                raise HTTPException(422, "Projects on this machine run as your own agent account, so you can't choose another one.")
            return "local", who.account
        ctx.machine(body.host, who)  # enrolled and connected now (404 or 409 otherwise)
        with db() as s:
            return body.host, project_account(s, body, who)

    async def _register_new(slug: str, name: str, trust: bool, who, notes: str, make, *, repo: str | None = None,
                            remote_url: str | None = None, action: str = "project.import", host: str = "local",
                            account: str | None = None, description: str = "") -> dict:
        """A new folder in the account's own workspace root on that machine (`make(workspace)` creates it through its
        broker there), trusted if asked, then registered as the caller's project. Never a folder they name: that's
        Register folder."""
        free_slug(slug)
        account = account or who.account
        with db() as s:
            if s.scalar(select(Project).where(Project.slug == slug)):
                raise HTTPException(409, f"A project named {slug} already exists. Choose another name or slug.")
        root = settings.account_root(account) if host == "local" else await machine_root(host, account)
        workspace = str(root / slug)
        async def register():
            with db() as s:
                if s.scalar(select(Project.id).where((Project.slug == slug) | (
                        (Project.workspace_path == workspace) & (Project.host == host)))):
                    raise HTTPException(409, "A project already uses that name or folder. Reload Projects and try again.")
            try:
                await make(workspace)
                if trust:
                    await broker.call("trust_workspace", account=account, host=host, path=workspace)
            except BrokerError:
                raise HTTPException(502, "Couldn't create the project. Check that the machine is connected and the repository can be reached, then try again.") from None
            snapshot = None
            with db() as s:
                project = Project(slug=slug, name=name, workspace_path=workspace, repo=repo, remote_url=remote_url,
                                  trusted=trust, notes=description if description.strip() else notes,
                                  account=account, owner=who.username, host=host, managed_checkout=action == "project.create")
                s.add(project)
                if description.strip():
                    s.flush()
                    value = brief.save(s, project.id, who.username, {'product': description, 'run_mode': 'prepare'}, 0)
                    snapshot = brief.version_out(s, value)
                s.add(AuditLog(actor=who.username, action=action, target=slug,
                               detail={"repo": repo or remote_url, "trusted": trust,
                                       **({"machine": host, "account": account} if host != "local" else {})}))
                s.commit()
            from ..git_identity import ensure
            warning = await ensure(supervisor, project, who.username)
            if snapshot:
                from .project_briefs import export_snapshot
                await export_snapshot(ctx, project, snapshot['version'], snapshot['markdown'], 'docs/PROJECT-BRIEF.md', None)
            return {**project_out(project), **({'git_identity_warning': warning} if warning else {})}

        return await supervisor.workspace_operations.run(workspace, register, host)

    async def _register_clone(repo: str, slug: str, name: str, trust: bool, who, notes: str, host: str = "local",
                              account: str | None = None, *, description: str = "", created: bool = False) -> dict:
        account = account or who.account

        async def clone(workspace: str) -> None:  # with the account's own gh, on that machine
            await broker.call("clone_repo", account=account, host=host, repo=repo, dest=workspace)
        return await _register_new(slug, name, trust, who, notes, clone, repo=repo, host=host, account=account,
                                   description=description, action="project.create" if created else "project.import")

    @api.post("/api/projects/new", status_code=201)
    async def new_project(body: NewProjectIn, who=Depends(ctx.principal)):
        """A first project without a server shell: an empty git folder, or a clone of a repository the caller's agent
        account can reach, in their own workspace root (never a path they pick). An empty folder is trusted for agents unless
        they say not; a clone isn't unless they say so, as trust lets agents run with the repository's own config and hooks."""
        slug = (body.slug or re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", body.name.lower())).strip("-"))[:64]
        if not slug:
            raise HTTPException(422, "Give the project a name with letters or digits, or enter a slug.")
        free_slug(slug)
        host, account = where(body, who)
        trust = body.trust if body.trust is not None else body.source == "empty"
        if body.source == "empty":
            async def empty(workspace: str) -> None:
                await broker.call("make_workspace", account=account, host=host, parent=str(Path(workspace).parent),
                                  name=slug, title=body.name, project=True)
            return await _register_new(slug, body.name, trust, who, "Started from Rynhult as an empty folder", empty,
                                       action="project.create", host=host, account=account, description=body.description)
        url = (body.url or "").strip()
        if GITHUB_REPO.match(url):  # owner/name: the account's own gh login, as Import from GitHub
            return await _register_clone(url, slug, body.name, trust, who, f"Imported from github.com/{url}", host,
                                         account, description=body.description)
        try:
            check_remote(url or None)
        except IssueSourceError:
            raise HTTPException(422, "Use an HTTPS or SSH repository URL without a password, or owner/name for GitHub.") from None
        if not url:
            raise HTTPException(422, "Enter the repository's URL, or owner/name for GitHub.")
        github = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?", url)

        async def clone(workspace: str) -> None:
            await broker.call("clone_url", account=account, host=host, url=url, dest=workspace)
        return await _register_new(slug, body.name, trust, who, f"Cloned from {url}", clone,
                                   repo=github.group(1) if github else None, remote_url=url, host=host,
                                   account=account, description=body.description)

    @api.get("/api/projects/new/folder")
    async def new_project_folder(host: str = "local", account: str | None = None, who=Depends(ctx.principal)):
        """Where New project would put a folder on that machine (the dialog shows it before anything is made)."""
        body = NewProjectIn(name="x", host=host, account=account)
        host, account = where(body, who)
        root = settings.account_root(account) if host == "local" else await machine_root(host, account)
        return {"host": host, "account": account, "root": str(root)}

    @api.post("/api/projects/import", status_code=201)
    async def import_project(body: ImportRepoIn, who=Depends(ctx.principal)):
        """Clone an existing repository into a new workspace and register it."""
        slug = (body.slug or body.repo.split("/")[1]).lower()
        free_slug(slug)
        return await _register_clone(body.repo, slug, body.repo.split("/")[1], body.trust, who,
                                     f"Imported from github.com/{body.repo}")

    @api.post("/api/github/repos", status_code=201)
    async def create_repo(body: CreateRepoIn, who=Depends(ctx.owner)):  # uses the dashboard's GitHub token
        """Create a private repo (with the protect-main ruleset), clone it and register the project."""
        if not body.confirm:
            raise HTTPException(409, "Tick the box to confirm Rynhult should create the GitHub repository, then try again.")
        free_slug(body.name)  # before the repository exists
        try:
            repo = await supervisor.github.create_repo(body.name, body.description[:300], settings.ruleset_path)
        except Exception as error:
            raise HTTPException(502, f"GitHub: {error}")
        with db() as s:
            s.add(AuditLog(actor=who.username, action="github.create_repo", target=repo["full_name"], detail={"private": True}))
            s.commit()
        return await _register_clone(repo["full_name"], body.name, body.name, True, who,
                                     f"Created on GitHub by Rynhult ({repo['html_url']})", description=body.description, created=True)

    @api.get("/api/projects/{project_id}/runtime")
    async def project_runtime(project_id: int, who=Depends(ctx.principal)):
        """The compose stack's containers, and for owners whether its compose files are still what an owner approved
        (D4, acc.server.compose). It's asked as the services account, which can look at any folder, so for a member only
        once an owner has made the stack Rynhult's (Autostart on), and with no broker error passed on: a member's project
        may name any folder (prerelease review D11)."""
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            path, managed, approved = project.workspace_path, project.compose_autostart, project.compose_pin
        if not who.is_owner and not managed:
            return {"containers": [], "not_started": True,
                    "note": "Rynhult doesn't run this project's Docker stack; an owner can turn on Autostart"}
        try:
            out = {"containers": await broker.call("compose_ps", account=settings.services_owner, path=path)}
        except BrokerError as error:
            out = {"containers": [], "note": str(error) if who.is_owner else "the stack's status is unavailable"}
        if who.is_owner:  # only owners approve what autostart runs
            try:
                current = await broker.call("compose_digest", account=settings.services_owner, path=path)
                out["compose"] = compose.review(approved, current)
            except BrokerError:
                pass  # no compose file, or the services broker is down (the note above says which)
        return out

    @api.post("/api/projects/{project_id}/runtime")
    async def set_runtime(project_id: int, body: RuntimeIn, who=Depends(ctx.owner)):  # Docker stacks are the owner's
        """Autostart on or off. On approves the compose files as they are now, which the owner reviewed: `digest` is
        the one the review showed, and a stack that changed since is refused (review it again). Approving a stack
        autostart held brings it up."""
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            path, host = project.workspace_path, project.host or "local"
        return await supervisor.workspace_operations.run(path, lambda: _set_runtime(project_id, body, who), host)

    async def _set_runtime(project_id, body, who):
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            if project.archived and body.compose_autostart:
                raise HTTPException(409, "This project is archived; its compose stack cannot be started.")
            path, slug = project.workspace_path, project.slug
        pinned = None
        if body.compose_autostart:
            if not body.digest:
                raise HTTPException(409, "Review the stack's compose files and approve them to turn autostart on.")
            try:
                current = await broker.call("compose_digest", account=settings.services_owner, path=path)
            except BrokerError as error:
                raise ctx.refused(error) from None
            if current["digest"] != body.digest:
                raise HTTPException(409, "The compose files changed since you reviewed them. Review them again, then turn autostart on.")
            pinned = compose.pin(current, who.username)
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            before = (project.compose_pin or {}).get("digest")
            project.compose_autostart = body.compose_autostart
            if pinned is not None:
                project.compose_pin = pinned
                attention.resolve(s, f"compose:{project_id}")
            s.add(AuditLog(actor=who.username, action="project.autostart", target=project.slug,
                           detail={"compose_autostart": body.compose_autostart,
                                   **({"approved": pinned["digest"][:12], "before": (before or "")[:12],
                                       "files": sorted(pinned["files"])} if pinned else {})}))
            s.commit()
            out = project_out(project)
        if pinned is not None and supervisor.release_autostart(project_id):
            try:
                await broker.call("compose_up", account=settings.services_owner, path=path, expect=pinned["digest"])
                with db() as s:
                    s.add(Event(source="api", kind="project_autostart", payload={"project": slug, "by": who.username}))
                    s.commit()
            except BrokerError as error:
                out["note"] = f"approved, but bringing the stack up failed: {error}"
        return out

    return api
