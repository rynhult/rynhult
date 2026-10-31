"""Sessions: list, preview, launch, external registration, lifecycle actions, events; observations."""
from __future__ import annotations

import asyncio
import base64
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from ... import container_host_record, providers
from ...broker import ops
from .. import budgets, explanations, export, gitbackup, guardrails, history, local_workspace, vault
from ..broker_client import BrokerError, BrokerUnavailable
from ..compiler import CompileError, build_args, compile_files, not_delivered
from ..issue_sources import IssueSourceError, issue_prompt, source_key
from ..models import AuditLog, Event, Node, Observation, Project, Session, Team
from ..orchestrator import delivery
from ..public_errors import public_error, unknown_method
from ..reconcile import host_of
from ..supervisor import LaunchRefused
from ..teams import default_team_agents, team_files, validate_grant
from .context import Context
from .schemas import (
    DeleteWorkspaceIn,
    DevelopmentWorkspaceIn,
    ExternalSessionIn,
    MessageIn,
    PromoteIn,
    RenameIn,
    json_list,
    offline_machines,
    project_out,
    session_out,
)
from .schemas import (
    SessionIn as BaseSessionIn,
)

# Until the terminal attach path is qualified, a container session would start an agent nobody can see.
CONTAINER_ATTACH_UNQUALIFIED = "container sessions are not available yet: the terminal attach path is not qualified"
ARTIFACT_DEADLINE = 60.0  # seconds for a whole artifact, however many replies it takes

# what each artifact is served as: text of every kind (html included) as plain text, never as a page
ARTIFACT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                  ".webp": "image/webp", ".mp4": "video/mp4", ".webm": "video/webm",
                  ".json": "application/json", ".csv": "text/csv; charset=utf-8",
                  **{ext: "text/plain; charset=utf-8" for ext in (".txt", ".log", ".md", ".xml", ".html")}}


class SessionIn(BaseSessionIn):
    """Slice-owned launch additions; shared schemas remain owned by their slices."""
    task: str | None = Field(None, max_length=65536)

    @model_validator(mode='after')
    def validate_target_task(self):
        if self.topic is not None and self.project_id is not None:
            raise ValueError('topic is for local workspaces; did you mean task?')
        if self.task is not None:
            if not delivery.enabled():
                raise ValueError('launch task delivery is disabled')
            try:
                delivery.validate_text(self.task)
            except LaunchRefused as error:
                raise ValueError(str(error)) from None
            if self.initial_prompt or self.issue_source or self.automation:
                raise ValueError('task cannot be combined with initial_prompt, issue_source or automation')
        return self

    def inputs(self, catalog=None):
        return BaseSessionIn.model_validate(self.model_dump(exclude={'task'}, exclude_unset=True)).inputs(catalog)


class ExplainIn(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f]+$")
    regenerate: bool = False
    revision: str | None = None


def router(ctx: Context) -> APIRouter:
    settings, db, supervisor, broker, refused = ctx.settings, ctx.db, ctx.supervisor, ctx.broker, ctx.refused
    api = APIRouter(lifespan=delivery.lifespan(supervisor, ctx.writers))

    @api.get("/api/sessions")
    def list_sessions(request: Request, who=Depends(ctx.principal)):
        # Polled every few seconds with every session: one query per relationship, machines resolved once, and a
        # direct JSON encoding (rc performance check: 2,000 sessions took about 6 s, mostly per-row work).
        with db() as s:
            offline = offline_machines(s)
            rows = s.scalars(ctx.scope(select(Session), Session, who).order_by(Session.created_at.desc()).options(
                selectinload(Session.project), selectinload(Session.launch_spec), selectinload(Session.placement)))
            return json_list([session_out(x, offline_hosts=offline) for x in rows], request.headers.get("if-none-match"))

    def standalone_profile(body: SessionIn) -> None:
        profile = supervisor.catalog.profiles.get(body.profile or "")
        if not profile or not profile.get("standalone"):
            names = ", ".join(p["name"] for p in supervisor.catalog.profiles.values() if p.get("standalone"))
            raise HTTPException(422, f"A local workspace needs a profile that works without a project. Choose one of: {names}.")

    def local_root(who):
        """This person's local workspaces: under their agent account's root."""
        return local_workspace.root(settings.account_root(who.account))

    def local_project(s, who) -> int:
        try:
            return local_workspace.ensure_project(s, settings.account_root(who.account), who.account, who.username,
                                                  settings.default_account, lambda p: ctx.owns(p, who)).id
        except local_workspace.LocalProjectError as error:
            raise HTTPException(409, str(error))

    def existing_workspace(s, name: str, who) -> str:
        """A folder an earlier session of theirs created (so the next session, e.g. Council after Researcher, shares it)."""
        path = str(local_root(who) / name)
        if s.scalar(ctx.scope(select(Session.id), Session, who).where(Session.workdir == path).limit(1)) is None:
            raise HTTPException(404, f"There's no local workspace called {name}.")
        return path

    def workspace_folders(who) -> set[str]:
        root = local_root(who)
        try:
            return {p.name for p in root.iterdir()}
        except OSError:
            return set()

    def development_target(body, who):
        if body.workspace:
            with db() as s:
                project = s.scalar(ctx.scope(select(Project), Project, who).where(
                    Project.private_workspace.is_(True), Project.account == who.account,
                    Project.workspace_path == str(local_root(who) / body.workspace)))
                if project:
                    return body.model_copy(update={"workspace": None, "topic": None, "project_id": project.id})
        return body

    def no_worktree_for_workspace(body: SessionIn) -> None:
        """Checked before anything is made or recorded: a refused launch must not leave a topic folder behind."""
        if body.worktree and (body.topic or body.workspace):
            raise HTTPException(422, "Own worktree is for project sessions: a local workspace is already its own folder. Turn off Own worktree.")

    async def issue_inputs(body: SessionIn, who, *, frozen: bool = False):
        try:
            inputs = body.inputs(None if frozen else supervisor.catalog)
        except (CompileError, KeyError) as error:
            raise HTTPException(422, str(error)) from None
        if body.issue_source is None:
            return inputs
        if not body.project_id or body.topic or body.workspace:
            raise HTTPException(422, "Issues can only be attached to a project session. Choose a project.")
        ref = body.issue_source
        with db() as s:
            project = ctx.project_for(s, who, body.project_id)
            try:
                config, token = source_key(s, project, ref.source, who.username, supervisor.vault)
            except (IssueSourceError, vault.VaultError) as error:
                raise HTTPException(422, str(error)) from None
            remote = project.remote_url
        try:
            issue = await supervisor.issue_sources.detail(ref.source, config, remote, token, ref.kind, ref.id)
        except IssueSourceError as error:
            raise HTTPException(422, str(error)) from None
        from dataclasses import replace
        return replace(inputs, initial_prompt=issue_prompt(inputs.initial_prompt, issue))

    async def default_team_grant(body, who):
        if isinstance(body.orchestrator, dict) and 'agents' not in body.orchestrator and body.project_id:
            with db() as s:
                project = ctx.project_for(s, who, body.project_id)
                account, host = project.account, project.host or 'local'
            agents = await default_team_agents(broker, account, host)
            body = body.model_copy(update={'orchestrator': {**body.orchestrator, 'agents': agents}})
        return body

    @api.post("/api/sessions/preview")
    async def preview_session(body: SessionIn, who=Depends(ctx.principal)):
        """Exactly what Launch would run (paths that depend on the new id shown as <run-dir>)."""
        if body.topic is not None and body.project_id is not None:
            raise HTTPException(422, 'topic is for local workspaces; did you mean task?')
        from ..admin_features import launch_refusal, require
        if problem := launch_refusal(ctx.settings, automation=body.automation, orchestrator=body.orchestrator):
            raise HTTPException(409, problem)
        if body.issue_source:
            require(ctx.settings, 'issue_sources')
        if body.endpoint:
            require(ctx.settings, 'local_models')
        body = development_target(body, who)
        no_worktree_for_workspace(body)
        body = await default_team_grant(body, who)
        prepared = await issue_inputs(body, who)
        with db() as s:
            local_only = False
            keys_project, remote, host = None, False, "local"  # local workspaces: their own project, made at the first launch
            if body.workspace:
                standalone_profile(body)
                cwd, trusted, import_mode = existing_workspace(s, body.workspace, who), True, "managed"
                account = who.account
            elif body.topic:
                standalone_profile(body)
                cwd = str(local_root(who) / local_workspace.topic_folder(body.topic, workspace_folders(who)))
                trusted, import_mode = True, "managed"
                account = who.account
            else:
                if not body.project_id:
                    raise HTTPException(404, "Choose a project.")
                project = ctx.project_for(s, who, body.project_id)
                cwd, trusted, import_mode = project.workspace_path, project.trusted, project.import_mode
                local_only = project.private_workspace
                if body.worktree or local_only:  # made at launch: <root>/.worktrees/<slug>-<id>, on branch acc/<name>-<id>
                    cwd = str(settings.account_root(project.account) / ".worktrees" / f"{project.slug}-<id>")
                account, host = project.account, project.host or "local"
                keys_project, remote = project.id, host != "local"
            if body.workspace or body.topic:
                found = local_workspace.find_project(s, who.account)
                if found is not None:
                    try:
                        keys_project = local_workspace.check_project(found, settings.account_root(who.account),
                                                                     who.account, lambda p: ctx.owns(p, who)).id
                    except local_workspace.LocalProjectError as error:
                        raise HTTPException(409, str(error))
            try:
                inputs = prepared
                from ..orchestrator import planning, project_briefs
                preview_grant = body.orchestrator
                if preview_grant is not None and body.project_id:
                    try:
                        preview_grant = project_briefs.launch_grant(s, body.project_id, preview_grant)
                        inputs = project_briefs.launch_inputs(s, body.project_id, inputs, preview_grant.get('brief_version'))
                        from ..orchestrator.kickoff import initial_inputs
                        inputs = initial_inputs(s, body.project_id, inputs, preview_grant)
                    except (project_briefs.Refused, LaunchRefused) as error:
                        raise CompileError(str(error)) from None
                if preview_grant and planning.enabled() and preview_grant.get('run_mode') == 'prepare':
                    try:
                        inputs = planning.controls.readonly_inputs(inputs)
                    except LaunchRefused as error:
                        raise CompileError(str(error)) from None
                keys = vault.plan(s, keys_project, who.username, remote, supervisor.catalog, inputs.mcps)
                where = supervisor.known_layout(host, account) or supervisor.known_layout("local", account)
                compiled = compile_files(inputs, where["hook_path"], where["statusline_path"],
                                         catalog=supervisor.catalog, agent_home=where["home"],
                                         codex_plugins=supervisor.codex_plugins(account),
                                         endpoint=supervisor.endpoint_info(s, inputs.endpoint),
                                         guardrails=guardrails.policy(s, cwd, who.username, keys_project, local_only))
                preview_limits = None
                if body.orchestrator is not None:
                    if not body.worktree or body.topic or body.workspace:
                        raise CompileError("An Orchestrator needs a project and its own worktree. Choose a project and turn on Own worktree.")
                    try:
                        preview_limits = validate_grant(preview_grant, supervisor.catalog, host, where["hook_path"], body.confirm_dangerous)
                    except LaunchRefused as error:
                        raise CompileError(str(error)) from None
                    from dataclasses import replace
                    compiled = replace(compiled, files=team_files(compiled.files, inputs.provider, where["hook_path"], lead=True, inputs=inputs))
                from .. import agent_contract, image_capabilities
                files = providers.get(inputs.provider).add_context(compiled.files, vault.context(keys))
                files = agent_contract.apply(files, inputs, agent_contract.for_preview(
                    s, inputs, s.get(Project, body.project_id) if body.project_id else None,
                    preview_limits, host, supervisor.catalog, run_mode=(preview_limits or {}).get('run_mode', 'build')))
                # Preview never performs a paid probe or certifies a future runtime.
                files = image_capabilities.context_files(files, inputs.provider, {'image_input': None})
                args = build_args(inputs, run_dir="<run-dir>", tmux_name="<tmux-name>",
                                  native_id=providers.get(inputs.provider).preview_native_id(), resume=False,
                                  files=files)
            except (CompileError, vault.VaultError) as error:
                raise HTTPException(422, str(error))
            return {"command": [providers.get(inputs.provider).executable, *args], "cwd": cwd, "files": files,
                    "keys": keys.public(),
                    "digest": compiled.digest, "trusted": trusted, "import_mode": import_mode,
                    "inputs": inputs.as_dict(), "standalone": bool(body.topic or body.workspace),
                    "not_delivered": not_delivered(inputs, supervisor.catalog)}

    async def launch_session(body: SessionIn, who, schedule_run_id: str | None = None,
                             multirun_entry: tuple[str, int] | None = None):
        host, folder = 'local', None
        if body.workspace:
            path = str(local_root(who) / body.workspace)
        elif body.project_id:
            with db() as s:
                project = ctx.project_for(s, who, body.project_id)
                path, host = project.workspace_path, project.host or 'local'
        elif body.topic:
            folder = local_workspace.topic_folder(body.topic, workspace_folders(who))
            path = str(local_root(who) / folder)
        else:
            return await _launch_session(body, who, schedule_run_id, multirun_entry)
        return await supervisor.workspace_operations.run(path,
            lambda: _launch_session(body, who, schedule_run_id, multirun_entry, folder), host, kind="launch")

    async def _launch_session(body: SessionIn, who, schedule_run_id: str | None = None,
                             multirun_entry: tuple[str, int] | None = None, workspace_name: str | None = None):
        from ..admin_features import launch_refusal
        if problem := launch_refusal(ctx.settings, automation=body.automation, orchestrator=body.orchestrator,
                                     schedule=bool(schedule_run_id), multirun=bool(multirun_entry)):
            raise HTTPException(409, problem)
        if body.topic is not None and body.project_id is not None:
            raise HTTPException(422, 'topic is for local workspaces; did you mean task?')
        from ..admin_features import launch_refusal, require
        if problem := launch_refusal(ctx.settings, automation=body.automation, orchestrator=body.orchestrator):
            raise HTTPException(409, problem)
        if body.issue_source:
            require(ctx.settings, 'issue_sources')
        if body.endpoint:
            require(ctx.settings, 'local_models')
        body = development_target(body, who)
        no_worktree_for_workspace(body)
        try:
            # Scheduled launches retain the resolved choices saved by the person.
            inputs = await issue_inputs(body, who, frozen=bool(schedule_run_id))
        except (CompileError, KeyError) as error:
            raise HTTPException(422, str(error))
        dangerous = providers.get(inputs.provider).dangerous(inputs)
        if dangerous and not body.confirm_dangerous:
            raise HTTPException(409, "This launch lets the agent run commands without asking. Tick the confirmation to go ahead.")
        if dangerous:
            with db() as s:
                s.add(AuditLog(actor=who.username, action="session.dangerous_confirmed", target=body.name,
                               detail=body.model_dump(exclude={"initial_prompt", "task"})))
                s.commit()
        with db() as s:
            paused = budgets.paused(s, who.username, inputs.provider)
        if paused and not body.budget_ok:
            raise HTTPException(409, f"Your budget pauses new {providers.get(inputs.provider).label} launches ({', '.join(paused)}). "
                                     "Confirm to launch anyway.")
        limits = None
        if body.orchestrator is not None:
            if not {"mcp", "instructions"}.issubset(providers.get(inputs.provider).supports):
                raise HTTPException(422, "An Orchestrator needs an agent that supports MCP tools and per-session instructions. Choose another agent.")
            if body.topic or body.workspace or not body.project_id or not body.worktree:
                raise HTTPException(422, "An Orchestrator needs a project and its own worktree. Choose a project and turn on Own worktree.")
            if schedule_run_id is None:
                body = await default_team_grant(body, who)
            with db() as s:
                project = ctx.project_for(s, who, body.project_id)
                try:
                    limits = validate_grant(body.orchestrator, supervisor.catalog, project.host or "local",
                                            settings.hook_path, body.confirm_dangerous)
                    known = {"local"} | set(s.scalars(select(Node.name).where(Node.status == "active")))
                    if set(limits["machines"]) - known:
                        raise LaunchRefused("One of the team's machines isn't enrolled. Choose machines listed in Host → Machines.")
                except LaunchRefused as error:
                    raise HTTPException(422, str(error))
        from .. import automation
        project_id, workdir = body.project_id, None
        own_worktree, container = body.worktree, False
        try:
            if body.workspace:
                standalone_profile(body)
                with db() as s:
                    workdir = existing_workspace(s, body.workspace, who)
                    project_id = local_project(s, who)
                    s.commit()
            elif body.topic:
                standalone_profile(body)
                with db() as s:  # first: a refused project must not leave a topic folder behind
                    project_id = local_project(s, who)
                    s.commit()
                made = await broker.call("make_workspace", account=who.account, parent=str(local_root(who)),
                                         name=workspace_name, title=body.topic)
                workdir = made["path"]
                await broker.call("trust_workspace", account=who.account, path=workdir)
            elif project_id is None:
                raise HTTPException(422, "Choose a project, or give a topic for a local workspace.")
            else:
                with db() as s:
                    project = ctx.project_for(s, who, project_id)  # only into your own projects
                    if project.private_workspace:
                        own_worktree, workdir = True, project.workspace_path
                    slug, repo, account, host = project.slug, project.workspace_path, project.account, project.host or "local"
                    container = project.containers
                if container:
                    # An owner switched this project to containers: every session gets a private checkout and runs
                    # in a container through the root launcher, or doesn't start (never a native fallback).
                    if not settings.containers_enabled:
                        raise HTTPException(409, "This project runs sessions in containers, but containers are turned off on this Rynhult. "
                                                 "An owner can switch the project off containers in its settings.")
                    if not settings.containers_attach_qualified:
                        raise HTTPException(409, CONTAINER_ATTACH_UNQUALIFIED)
                    # Containers are a Preview (docs/CONTAINERS.md, "Release status"): a new container session starts
                    # only while this host's qualification record passes. Sessions already running, and resumes of
                    # existing ones, are left alone.
                    qualified = container_host_record.check(settings.containers_record)
                    if not qualified["passed"]:
                        with db() as s:
                            s.add(AuditLog(actor=who.username, action="container.launch_refused", target=slug,
                                           detail={"reason": "host_not_qualified", "provider": inputs.provider}))
                            s.commit()
                        raise HTTPException(409, "this project runs sessions in containers, a Preview that needs a "
                                                 f"qualified host: {qualified['reason']}")
                    if host != "local":
                        raise HTTPException(409, "Container sessions only run on the main Rynhult machine for now, and this project is on another machine.")
                    if limits is not None and limits["machines"] != ["local"]:
                        raise HTTPException(422, "Container teams need local helpers only. Choose the main Rynhult machine for every helper.")
                    own_worktree = True
            # Identity is initialized by create_session after launch approval.
            # Worktree creation needs no author; avoid a pre-approval repository probe.
            worktree = None
            if own_worktree and container:
                suffix = uuid.uuid4().hex[:8]
                name = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", body.name.lower())).strip("-")[:40] or "session"
                if multirun_entry:
                    name = f"{name}-{inputs.provider}"
                worktree = await broker.call("make_private_checkout", account=account, host=host, repo=repo,
                                             root=str(settings.account_root(account)), name=f"{slug}-{suffix}",
                                             branch=f"acc/{name}-{suffix}" + ("/work" if limits is not None else ""))
            elif own_worktree:
                suffix = uuid.uuid4().hex[:8]
                name = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", body.name.lower())).strip("-")[:40] or "session"
                if multirun_entry:
                    name = f"{name}-{inputs.provider}"
                # on another machine: next to the project (Rynhult doesn't know that machine's workspace root)
                root = str(settings.account_root(account)) if host == "local" else str(Path(repo).parent)
                worktree = await broker.call("make_worktree", account=account, host=host, repo=repo,
                                             root=root, name=f"{slug}-{suffix}",
                                             branch=f"acc/{name}-{suffix}" + ("/work" if limits is not None else ""))
            try:
                acc_id = await supervisor.create_session(project_id, inputs, body.name, who.username, workdir=workdir,
                                                         owner=who.username, worktree=worktree, orchestrator=limits,
                                                         schedule_run_id=schedule_run_id, multirun_entry=multirun_entry,
                                                         container=container, automation_policy=body.automation)
            except Exception:
                if worktree:  # nothing will use it: don't leave an empty worktree behind
                    await broker.call("remove_worktree", account=account, host=host, repo=repo, path=worktree["path"])
                raise
        except (LaunchRefused, CompileError, BrokerError) as error:
            if isinstance(error, BrokerError) and "isn't a git repository with a commit yet" in str(error):
                raise HTTPException(409, "This project has no Git commit yet. Open the project page and choose Make the first commit, then start the session or team again.") from None
            raise refused(error)
        with db() as s:
            session = s.get(Session, acc_id)
            if body.issue_source:
                session.issue_source = {**body.issue_source.model_dump(), "actor": who.username}
                s.commit()
            if body.automation:
                automation.configure(s, session, body.automation, who.username)
                s.commit()
            result = session_out(session, detail=True, ssh_host=settings.editor_host)
        task = getattr(body, 'task', None)
        if task is not None:
            result['delivery'] = await supervisor.send_text(acc_id, who.username, task)
        return result

    ctx.launch_session = launch_session

    @api.post("/api/sessions", status_code=201)
    async def create_session(body: SessionIn, who=Depends(ctx.principal)):
        return await launch_session(body, who)

    @api.post("/api/sessions/external", status_code=201)
    def register_external(body: ExternalSessionIn, who=Depends(ctx.owner)):
        """Observe-only registration; the owner's launcher writes hooks into run/<id>/events.jsonl."""
        try:
            acc_id = supervisor.register_external(body.project_id, body.provider, body.name, body.native_id,
                                                  body.run_as, body.unit, who.username)
        except LaunchRefused as error:
            raise refused(error)
        with db() as s:
            session = s.get(Session, acc_id)
            out = session_out(session, detail=True, ssh_host=settings.editor_host)
            out["run_dir"] = str(settings.session_run_dir(session))
        return out

    @api.get("/api/sessions/{acc_id}")
    def get_session(acc_id: str, who=Depends(ctx.principal)):
        with db() as s:
            return session_out(ctx.session_for(s, who, acc_id), detail=True, ssh_host=settings.editor_host)

    @api.get("/api/sessions/{acc_id}/team")
    async def get_team(acc_id: str, who=Depends(ctx.principal)):
        from ..orchestrator.reservations import Reservation
        from ..orchestrator.reservations import view as reservation_view
        with db() as s:
            lead = ctx.session_for(s, who, acc_id)
            team = s.get(Team, acc_id)
            if not team or lead.team_id != acc_id:
                raise HTTPException(404, "no such team")
            members = list(s.scalars(select(Session).where(Session.team_id == acc_id).order_by(Session.created_at)))
            decisions = list(s.scalars(select(AuditLog).where(AuditLog.target == acc_id, AuditLog.action.like("team.%"))
                                      .order_by(AuditLog.id)))
            return {"limits": team.limits, "expires_at": team.expires_at, "stopped": team.stopped, "wrapup_until": team.wrapup_until,
                    "used": {"started": team.started, "running": sum(x.parent_session_id is not None and (x.desired_state == "running" or x.team_stop_pending) for x in members)},
                    "reservations": [reservation_view(row) for row in s.scalars(select(Reservation).where(
                        Reservation.team_id == acc_id).order_by(Reservation.id))],
                    "members": [await supervisor.team_summary(x) for x in members],
                    "decisions": [{"action": x.action, "detail": x.detail, "ts": x.ts} for x in decisions]}

    @api.post("/api/sessions/{acc_id}/team/stop")
    async def stop_team(acc_id: str, who=Depends(ctx.principal)):
        with db() as s:
            ctx.session_for(s, who, acc_id)
        try:
            return await supervisor.stop_team(acc_id, who.username)
        except LaunchRefused as error:
            raise refused(error)

    @api.get("/api/sessions/{acc_id}/events")
    def session_events(acc_id: str, after: int = 0, limit: int = 200, who=Depends(ctx.principal)):
        with db() as s:
            ctx.session_for(s, who, acc_id)
            rows = s.scalars(select(Event).where(Event.session_id == acc_id, Event.id > after)
                             .order_by(Event.id.desc() if after == 0 else Event.id).limit(max(1, min(limit, 1000))))
            events = [{"id": e.id, "ts": e.ts, "source": e.source, "kind": e.kind, "verified": e.verified,
                       "payload": e.payload} for e in rows]
            return sorted(events, key=lambda e: e["id"])

    @api.get("/api/search")
    def search_history(q: str = "", limit: int = 30, project_id: int | None = None,
                       provider: str | None = None, since: date | None = None, who=Depends(ctx.principal)):
        """Search every conversation you can see (your sessions; owners: all), archived ones included."""
        with db() as s:
            scope = ctx.scope(select(Session.id), Session, who)
            if project_id is not None:
                scope = scope.where(Session.project_id == project_id)
            if provider:
                scope = scope.where(Session.provider == provider)
            if since:
                scope = scope.where(Session.created_at >= datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc))
            allowed = set(s.scalars(scope))
            return {"query": q, "results": history.search(s, q[:200], allowed, max(1, min(limit, 50))),
                    "indexed_sessions": history.indexed_sessions(s, allowed)}

    @api.get("/api/sessions/{acc_id}/changes")
    async def session_changes(acc_id: str, who=Depends(ctx.principal)):
        """The workspace's current-branch changes: files and the diff, using the same baseline as the row badge."""
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            account, path, base, host = session.run_as, session.cwd, session.base_commit, session.host
        try:
            changes = await broker.call("workspace_changes", account=account, host=host, path=path, base=base, detail=True)
        except BrokerError as error:
            raise refused(error)
        with db() as s:  # the diff as the UI shows it: with the session's keys redacted
            changes = supervisor.vault.redactor(s, ctx.session_for(s, who, acc_id).vault_keys).deep(changes)
            return {**changes, "revision": explanations.revision(changes)}

    def explanation_key(acc_id, changes):
        return f"explain:{acc_id}:{explanations.revision(changes)[:36]}"

    @api.get("/api/sessions/{acc_id}/explanation")
    async def get_explanation(acc_id: str, who=Depends(ctx.principal)):
        changes = await session_changes(acc_id, who)
        with db() as s:
            row = s.get(Observation, explanation_key(acc_id, changes))
            value = row.value if row else {"status": "missing", "revision": explanations.revision(changes), "steps": []}
            return supervisor.vault.redactor(s, ctx.session_for(s, who, acc_id).vault_keys).deep(value)

    @api.get("/api/sessions/{acc_id}/explanation/agents")
    async def explanation_agents(acc_id: str, who=Depends(ctx.principal)):
        with db() as s:
            ctx.session_for(s, who, acc_id)
        try:
            health = await broker.call("auth_health", account=who.account, host="local")
        except BrokerError as error:
            raise refused(error)
        return {"agents": [{"id": p.id, "label": p.label} for p in providers.PROVIDERS.values()
                           if (health.get(p.id) or {}).get("logged_in")]}

    @api.post("/api/sessions/{acc_id}/explanation")
    async def create_explanation(acc_id: str, body: ExplainIn, who=Depends(ctx.principal)):
        changes = await session_changes(acc_id, who)
        if not changes.get("git") or not changes.get("files"):
            raise HTTPException(409, "There are no changes to explain.")
        if body.revision and body.revision != explanations.revision(changes):
            raise HTTPException(409, "The changes have moved on since you opened them. Refresh Changes, then explain them again.")
        key = explanation_key(acc_id, changes)
        async with supervisor._lock:
            with db() as s:
                cached = s.get(Observation, key)
                if cached and (cached.value["status"] == "running"
                               or cached.value["status"] == "ready" and not body.regenerate):
                    return supervisor.vault.redactor(s, ctx.session_for(s, who, acc_id).vault_keys).deep(cached.value)
                paused = budgets.paused(s, who.username, body.provider)
                if paused:
                    raise HTTPException(409, f"Your budget pauses new {providers.get(body.provider).label} launches ({', '.join(paused)}). Choose another agent.")
            agents = await explanation_agents(acc_id, who)
            if body.provider not in {p["id"] for p in agents["agents"]}:
                raise HTTPException(422, "Choose an agent that's installed and signed in on this machine.")
            with db() as s:
                project_id = local_project(s, who)
                s.commit()
            try:
                return await supervisor.launch_explanation(project_id, who.username, body.provider, body.model,
                                                            key, changes, acc_id)
            except (LaunchRefused, CompileError, BrokerError) as error:
                raise refused(error)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None

    def artifact_place(acc_id: str, who) -> dict:
        """Where a session's artifacts are read from: its folder, through the broker of its account and machine (the
        same as its Changes)."""
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            return {"account": session.run_as, "host": host_of(session), "path": session.cwd,
                    **({"helper_id": session.id} if "explanation.json" in session.launch_spec.files else {})}

    @api.get("/api/sessions/{acc_id}/artifacts")
    async def session_artifacts(acc_id: str, who=Depends(ctx.principal)):
        """Files the agent left for its reviewer in .acc/artifacts: screenshots, short videos, logs and reports."""
        try:
            return await broker.call("list_artifacts", **artifact_place(acc_id, who))
        except BrokerError as error:
            raise refused(error)

    @api.get("/api/sessions/{acc_id}/artifacts/{name:path}")
    async def session_artifact(acc_id: str, name: str, who=Depends(ctx.principal)):
        """One artifact, served so a browser can't run it: a fixed type from the extension allowlist, no sniffing, a
        sandboxing CSP, and inline only for images and video (anything else downloads)."""
        place = artifact_place(acc_id, who)
        kind = ops.artifact_kind(name)
        if kind is None:
            raise HTTPException(404, "That artifact wasn't found.")
        limit = ops.ARTIFACT_LIMITS[kind]
        data = bytearray()
        size = mtime = None

        async def read_all() -> None:
            # A machine's broker answers for itself: every reply is checked here, against this Rynhult's own limits.
            nonlocal size, mtime
            while True:
                part = await broker.call("read_artifact", **place, name=name, offset=len(data))
                if not isinstance(part, dict) or part.get("kind") != kind or part.get("offset") != len(data):
                    raise ValueError("malformed reply")
                if size is None:
                    size, mtime = part.get("size"), part.get("mtime_ns")
                    if not isinstance(size, int) or not 0 <= size <= limit:
                        raise ValueError("size out of range")
                elif (part.get("size"), part.get("mtime_ns")) != (size, mtime):
                    raise HTTPException(409, "The artifact changed while it was being read. Try again.")
                encoded = part.get("data")
                if not isinstance(encoded, str) or len(encoded) > 4 * -(-ops.ARTIFACT_CHUNK // 3):
                    raise ValueError("chunk too large")
                chunk = base64.b64decode(encoded, validate=True)
                if len(data) + len(chunk) > size:
                    raise ValueError("more data than its size")
                data.extend(chunk)
                if len(data) == size:
                    return
                if not chunk:
                    raise ValueError("ended early")

        try:
            await asyncio.wait_for(read_all(), ARTIFACT_DEADLINE)
        except BrokerError as error:
            message = str(error)
            if "no such artifact" in message:
                raise HTTPException(404, "That artifact wasn't found.")
            if "too large" in message:
                raise HTTPException(413, message.split(": ", 1)[-1])
            raise refused(error)
        except (ValueError, TypeError):
            raise HTTPException(502, "The machine holding this artifact sent a reply Rynhult couldn't read.") from None
        except asyncio.TimeoutError:
            raise HTTPException(504, "Reading the artifact took too long. Try again.") from None
        filename = name.rsplit("/", 1)[-1]
        disposition = "inline" if kind in ("image", "video") else "attachment"
        return Response(bytes(data), media_type=ARTIFACT_TYPES[Path(name).suffix.lower()], headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; media-src 'self'; sandbox",
            "Cache-Control": "private, no-cache",
        })

    async def _action(acc_id: str, who, start):
        """Run a supervisor action on one of your sessions (`start` builds the coroutine once access is checked)."""
        with db() as s:
            ctx.session_for(s, who, acc_id)
        try:
            await start()
        except (LaunchRefused, BrokerError) as error:
            raise refused(error)
        with db() as s:
            return session_out(s.get(Session, acc_id))

    @api.post("/api/sessions/{acc_id}/stop")
    async def stop(acc_id: str, who=Depends(ctx.principal)):
        return await _action(acc_id, who, lambda: supervisor.stop(acc_id, who.username))

    @api.post("/api/sessions/{acc_id}/archive")
    async def archive(acc_id: str, who=Depends(ctx.principal)):
        return await _action(acc_id, who, lambda: supervisor.stop(acc_id, who.username, archive=True))

    @api.post("/api/sessions/{acc_id}/stop-held")
    async def stop_held(acc_id: str, request: Request, who=Depends(ctx.owner)):
        """Replacement recovery: an owner, freshly verified, stops the pane the old main left running on a machine."""
        from .. import passkeys
        from ..models import User
        with db() as s:
            ctx.session_for(s, who, acc_id)
            actor = s.get(User, who.user_id) if who.user_id is not None else None
            try:
                passkeys.recently_verified(s, getattr(request.state, "session_hash", None), actor)
            except HTTPException:
                s.add(AuditLog(actor=who.username, action="session.replacement_held_stop", target=acc_id,
                               detail={"outcome": "refused", "reason": "verification_required"}))
                s.commit()
                raise
        return await _action(acc_id, who, lambda: supervisor.stop_held(acc_id, who.username))

    @api.post("/api/sessions/{acc_id}/resume")
    async def resume(acc_id: str, who=Depends(ctx.principal)):
        return await _action(acc_id, who, lambda: supervisor.resume(acc_id, who.username))

    def development_record(s, path, name, account, owner):
        project = Project(slug=f"dev-{uuid.uuid4().hex[:16]}", name=name, workspace_path=path,
                          account=account, owner=owner, trusted=True, private_workspace=True)
        s.add(project)
        s.flush()
        return project

    @api.post("/api/workspaces", status_code=201)
    async def create_development(body: DevelopmentWorkspaceIn, who=Depends(ctx.principal)):
        folder = local_workspace.topic_folder(body.name.strip(), workspace_folders(who))
        path = str(local_root(who) / folder)
        return await supervisor.workspace_operations.run(path, lambda: _create_development(body, who, folder))

    async def _create_development(body: DevelopmentWorkspaceIn, who=Depends(ctx.principal), workspace_name=None):
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "Give the workspace a name.")
        try:
            made = await broker.call("make_workspace", account=who.account, parent=str(local_root(who)),
                                     name=workspace_name, title=name)
            await broker.call("prepare_private_workspace", account=who.account, path=made["path"])
            await broker.call("trust_workspace", account=who.account, path=made["path"])
        except BrokerError as error:
            raise refused(error)
        with db() as s:
            project = development_record(s, made["path"], name, who.account, who.username)
            s.add(AuditLog(actor=who.username, action="workspace.create_development", target=project.slug))
            s.commit()
            return project_out(project)

    @api.post("/api/workspaces/{folder}/development")
    async def convert_development(folder: str, session: str | None = None, who=Depends(ctx.owner)):
        with db() as s:
            account = ctx.session_for(s, who, session).run_as if session else who.account
        path = str(local_workspace.root(settings.account_root(account)) / folder)
        return await supervisor.workspace_operations.run(path, lambda: _convert_development(folder, session, who), kind="convert")

    async def _convert_development(folder: str, session: str | None = None, who=Depends(ctx.owner)):
        with db() as s:
            # Find by accessible sessions, never trust an arbitrary path supplied by the caller.
            account = ctx.session_for(s, who, session).run_as if session else who.account
            path = str(local_workspace.root(settings.account_root(account)) / folder)
            sessions = s.scalars(ctx.scope(select(Session), Session, who).where(
                Session.workdir == path, Session.run_as == account)).all()
            if not sessions:
                raise HTTPException(404, "That workspace wasn't found.")
            path, account, owner = sessions[0].workdir, sessions[0].run_as, sessions[0].owner
            if s.scalar(select(Project.id).where(Project.workspace_path == path)):
                raise HTTPException(409, "This workspace is already a project.")
            if any(x.desired_state == "running" for x in sessions):
                raise HTTPException(409, "Stop the workspace's sessions before converting it to Development.")
            ids = [x.id for x in sessions]
        try:
            await broker.call("prepare_private_workspace", account=account, path=path)
        except BrokerError as error:
            raise HTTPException(409, str(error))
        with db() as s:
            current = s.scalars(select(Session).where(Session.workdir == path, Session.run_as == account)).all()
            if any(x.desired_state == "running" for x in current) or s.scalar(select(Project.id).where(Project.workspace_path == path)):
                raise HTTPException(409, "The workspace changed while it was being converted. Stop its sessions and try again.")
            project = development_record(s, path, folder, account, owner)
            for session in s.scalars(select(Session).where(Session.id.in_(ids))):
                session.project_id = project.id
            s.add(AuditLog(actor=who.username, action="workspace.convert_development", target=project.slug))
            s.commit()
            return project_out(project)

    @api.get("/api/workspaces")
    async def list_workspaces(who=Depends(ctx.principal)):
        """Local workspaces: one per folder, with its sessions, report counts and the project it became, if any."""
        with db() as s:
            sessions = s.scalars(ctx.scope(select(Session), Session, who).where(Session.workdir.is_not(None))
                                 .order_by(Session.created_at)).all()
            promoted = {p.workspace_path: {"id": p.id, "name": p.name, "repo": p.repo, "private_workspace": p.private_workspace}
                        for p in s.scalars(ctx.scope(select(Project), Project, who).where(Project.workspace_path.in_({x.workdir for x in sessions})))}
            folders: dict[str, dict] = {}
            for x in sessions:
                entry = folders.setdefault(x.workdir, {
                    "folder": x.workdir.rsplit("/", 1)[1], "path": x.workdir, "created_at": x.created_at,
                    "kind": "development" if (promoted.get(x.workdir) or {}).get("private_workspace") else "research",
                    "project": promoted.get(x.workdir), "sessions": [], "account": x.run_as, "local": True})
                entry["local"] = entry["local"] and x.host == "local"
                entry["sessions"].append({"id": x.id, "name": x.name, "provider": x.provider,
                                          "profile": x.launch_spec.inputs.get("profile"), "observed_state": x.observed_state,
                                          "desired_state": x.desired_state, "last_output_at": x.last_output_at})
            # Development workspaces exist independently of sessions.
            for p in s.scalars(ctx.scope(select(Project), Project, who).where(Project.private_workspace.is_(True), Project.archived.is_(False))):
                folders.setdefault(p.workspace_path, {"folder": Path(p.workspace_path).name, "path": p.workspace_path,
                    "created_at": p.created_at, "kind": "development", "project": {"id": p.id, "name": p.name,
                    "repo": None, "private_workspace": True}, "sessions": [], "account": p.account, "local": p.host == "local"})
        for entry in folders.values():
            account, local = entry.pop("account"), entry.pop("local")
            entry["folder_removed"] = False
            # Only a definite absence on this machine is a ghost. Permission errors,
            # remote paths and dangling symlinks must never be described as removed.
            if local and Path(entry["path"]).parent == local_root(who) and not local_root(who).is_symlink():
                try:
                    Path(entry["path"]).lstat()
                except FileNotFoundError:
                    entry["folder_removed"] = True
                except OSError:
                    pass
            if entry["folder_removed"]:
                entry["docs"] = None
                continue
            try:
                docs = await broker.call("scan_docs", account=account, path=entry["path"])
                entry["docs"] = {kind: len(items) for kind, items in docs.items()}
            except BrokerError:
                entry["docs"] = None
        return sorted(folders.values(), key=lambda e: e["created_at"], reverse=True)

    @api.post("/api/workspaces/{folder}/delete")
    async def delete_workspace(folder: str, body: DeleteWorkspaceIn, who=Depends(ctx.principal)):
        path = str(local_root(who) / folder)
        return await supervisor.workspace_operations.run(path, lambda: _delete_workspace(folder, body, who), kind="delete")

    async def _delete_workspace(folder: str, body: DeleteWorkspaceIn, who=Depends(ctx.principal)):
        """Delete a local workspace: archive its sessions, then remove the folder and its local git history.

        Refused for a workspace that became a project (its folder is the project's checkout). The sessions stay in
        the history as archived; they no longer point at the folder."""
        if not ops.FOLDER.fullmatch(folder):
            raise HTTPException(409, "Choose a direct local workspace folder with a valid folder name.")
        if body.confirm != folder:
            raise HTTPException(409, "Type the folder name to confirm. Deleting removes its files for good.")
        root = local_root(who)
        path = str(root / folder)
        with db() as s:
            sessions = s.scalars(ctx.scope(select(Session), Session, who).where(Session.workdir == path)).all()
            if not sessions:
                raise HTTPException(404, f"There's no local workspace called {folder}.")
            project = s.scalar(select(Project).where(Project.workspace_path == path))
            if project is not None:
                raise HTTPException(409, f"This workspace is now the project {project.name}, so it can't be deleted here.")
            if any(x.host != "local" for x in sessions):
                raise HTTPException(409, "This workspace has sessions on another machine. Ask the owner to remove it on that machine.")
            ids = [x.id for x in sessions]
            # Historical sessions may have run as the human/services account.
            # Cleanup authority comes from the caller's agent account, never that history.
            account = who.account
            legacy_active = {x.run_as for x in sessions if x.run_as != account and (
                x.desired_state == "running" or x.observed_state not in {"archived", "stopped", "exited", "error"})}
        try:
            # A services broker cannot stop old agent processes. Never turn that
            # failure into permission to delete their working directory.
            for previous_account in legacy_active:
                status = await broker.call("ping", account=previous_account)
                if status.get("role") != "agents":
                    raise HTTPException(409, "Stop the workspace's older sessions on their original account, then archive them and retry. Ask the owner if that account is unavailable.")
            for acc_id in ids:
                await supervisor.stop(acc_id, who.username, archive=True)
            removed = await broker.call("delete_workspace", account=account, parent=str(root), name=folder)
        except BrokerError as error:
            if unknown_method(error) or isinstance(error, BrokerUnavailable):
                raise HTTPException(409, "Cannot safely remove this workspace with the current agent service. Ask the owner to update or repair that service, or remove the folder using its original account and retry.") from error
            raise HTTPException(409, public_error(error)) from error
        except LaunchRefused as error:
            raise refused(error)
        with db() as s:
            for x in s.scalars(select(Session).where(Session.id.in_(ids))):
                x.workdir = None
            s.add(AuditLog(actor=who.username, action="workspace.delete", target=folder, detail={"path": path, "sessions": ids, "already_removed": bool(removed.get("already_removed"))}))
            s.commit()
        return {"deleted": folder, "sessions_archived": len(ids), "already_removed": bool(removed.get("already_removed"))}

    def doc_folders(s, who, acc_id: str) -> tuple[list[tuple[str, str]], str, str]:
        """Where a session's docs are, in order: its own folder (worktree, local workspace or the shared checkout),
        then, for a worktree session, the project's checkout, where its notes land once the worktree is cleaned up."""
        session = ctx.session_for(s, who, acc_id)
        own = "worktree" if session.worktree else "workspace" if session.workdir else "project"
        folders = [(own, session.cwd)]
        if session.worktree and session.project and session.project.workspace_path:
            folders.append(("project", session.project.workspace_path))
        return folders, session.run_as, session.host

    @api.get("/api/sessions/{acc_id}/knowledge")
    async def session_knowledge(acc_id: str, who=Depends(ctx.principal)):
        """The docs in the folder the session works in (UX review SE-2: never the main checkout for a worktree session)."""
        with db() as s:
            folders, account, host = doc_folders(s, who, acc_id)
        for i, (folder, path) in enumerate(folders):
            try:
                docs = await broker.call("scan_docs", account=account, host=host, path=path)
                return {"docs": docs, "repo": None, "folder": folder}
            except BrokerError as error:
                if i == len(folders) - 1:
                    raise refused(error)

    @api.get("/api/sessions/{acc_id}/knowledge/doc")
    async def session_doc(acc_id: str, path: str, who=Depends(ctx.principal)):
        with db() as s:
            folders, account, host = doc_folders(s, who, acc_id)
        for i, (_, cwd) in enumerate(folders):
            try:
                return await broker.call("read_doc", account=account, host=host, path=cwd, rel=path)
            except BrokerError as error:
                if i == len(folders) - 1:
                    raise refused(error)

    @api.post("/api/sessions/{acc_id}/promote", status_code=201)
    async def promote(acc_id: str, body: PromoteIn, who=Depends(ctx.principal)):
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            if session.project is None:
                raise HTTPException(409, "This project was deleted; its sessions remain in history.")
            path, host = session.workdir or session.project.workspace_path, session.host
        return await supervisor.workspace_operations.run(path, lambda: _promote(acc_id, body, who), host, kind="convert")

    async def _promote(acc_id: str, body: PromoteIn, who=Depends(ctx.principal)):
        """Worth pursuing: turn the local workspace into a project. With github (the default), create a private
        repository, push the workspace's history to it, then apply protect-main so changes land through PRs."""
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            if not session.workdir:
                raise HTTPException(404, "Only sessions in a local workspace can be turned into a project.")
            if local_workspace.reserved_slug(body.slug):
                raise HTTPException(422, "The names local and local-… are kept for local workspaces. Choose another.")
            if s.scalar(select(Project).where(Project.slug == body.slug)) is not None:
                raise HTTPException(409, f"A project named {body.slug} already exists. Choose another name.")
            existing = s.scalar(select(Project).where(Project.workspace_path == session.workdir))
            if existing is not None and not existing.private_workspace:
                raise HTTPException(409, "This workspace is already a project.")
            existing_id = existing.id if existing else None
            path, session_name, account = session.workdir, session.name, session.run_as
        repo = None
        if body.github and not who.is_owner:
            raise HTTPException(403, "Only an owner can create GitHub repositories from Rynhult. Turn it into a project without GitHub, or ask an owner.")
        if body.github:
            if not body.confirm:
                raise HTTPException(409, "Confirm that Rynhult should create the GitHub repository.")
            try:
                created = await supervisor.github.create_repo(body.slug, body.description, settings.ruleset_path,
                                                              auto_init=False, protect=False)
                repo = created["full_name"]
                await broker.call("push_workspace", account=account, path=path, repo=repo)
                await supervisor.github.apply_ruleset(repo, settings.ruleset_path)
            except BrokerError as error:
                raise refused(error)
            except Exception as error:  # GitHub API
                raise HTTPException(502, f"GitHub: {error}" + (f" (repository {repo} was created)" if repo else ""))
        with db() as s:
            project = Project(slug=body.slug, name=body.name.strip(), workspace_path=path, trusted=True, repo=repo,
                              import_mode="managed", account=account, owner=who.username,
                              notes=f"Promoted from local workspace session {session_name} ({acc_id}).")
            if existing_id:
                project = ctx.project_for(s, who, existing_id)
                project.backup_identity = gitbackup.identity(project)
                project.slug, project.name, project.repo = body.slug, body.name.strip(), repo
                project.private_workspace = False
            s.add(project)
            s.add(AuditLog(actor=who.username, action="workspace.promote", target=body.slug,
                           detail={"session": acc_id, "path": path, "repo": repo}))
            s.commit()
            return project_out(project)

    @api.post("/api/sessions/{acc_id}/rename")
    def rename(acc_id: str, body: RenameIn, who=Depends(ctx.principal)):
        """Display name only: the tmux name and native conversation id never change."""
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            old, session.name = session.name, body.name.strip()
            s.add(AuditLog(actor=who.username, action="session.rename", target=acc_id, detail={"from": old, "to": session.name}))
            s.commit()
            return session_out(session)

    @api.post("/api/sessions/{acc_id}/interrupt")
    async def interrupt(acc_id: str, who=Depends(ctx.principal)):
        return await _action(acc_id, who, lambda: supervisor.interrupt(acc_id, who.username))

    @api.put("/api/sessions/{acc_id}/automation")
    async def edit_automation(acc_id: str, body: dict, who=Depends(ctx.principal)):
        from .. import automation
        async with supervisor._lock:
            with db() as s:
                session = ctx.session_for(s, who, acc_id)
                try:
                    automation.configure(s, session, body, who.username)
                except ValueError as error:
                    raise HTTPException(422, str(error))
                s.commit()
                return session_out(session)

    @api.post("/api/sessions/{acc_id}/message")
    async def message(acc_id: str, body: MessageIn, wait: float = Query(0, ge=0, le=8), who=Depends(ctx.principal)):
        with db() as s:
            ctx.session_for(s, who, acc_id)
        if not delivery.enabled():
            return await _action(acc_id, who, lambda: supervisor.send_text(acc_id, who.username, body.text))
        try:
            result = await supervisor.send_text(acc_id, who.username, body.text)
        except LaunchRefused as error:
            raise refused(error) from None
        deadline = asyncio.get_running_loop().time() + wait
        while wait and result['state'] not in delivery.TERMINAL and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.1)
            with db() as s:
                result = delivery.public(s.get(delivery.MessageDelivery, result['id']))
        from fastapi.encoders import jsonable_encoder
        return JSONResponse({'delivery': jsonable_encoder(result)}, status_code=200 if result['state'] == 'acknowledged' else 202)

    @api.get('/api/sessions/{acc_id}/deliveries')
    def session_deliveries(acc_id: str, include_text: bool = False, who=Depends(ctx.principal)):
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            if not delivery.enabled():
                return {'deliveries': []}
            rows = s.scalars(select(delivery.MessageDelivery).where(
                delivery.MessageDelivery.session_id == acc_id).order_by(
                    delivery.MessageDelivery.created_at.desc()).limit(20))
            return {'deliveries': supervisor.vault.redactor(s, session.vault_keys).deep(
                [{**delivery.public(row), **({'text': row.body or row.preview,
                  'text_truncated': not bool(row.body) and len(row.preview.encode()) < row.bytes}
                  if include_text else {})} for row in rows])}

    @api.post('/api/deliveries/{identifier}/retry')
    def retry_delivery(identifier: str, who=Depends(ctx.principal)):
        if not delivery.enabled():
            raise HTTPException(404, 'delivery queue is disabled')
        with db() as s:
            row = s.get(delivery.MessageDelivery, identifier)
            if row is None:
                raise HTTPException(404, 'no such delivery')
            ctx.session_for(s, who, row.session_id)
            try:
                result = delivery.retry(s, row)
            except LaunchRefused as error:
                raise refused(error) from None
        delivery.wake(supervisor)
        return {'delivery': result}

    @api.get('/api/deliveries/{identifier}')
    def get_delivery(identifier: str, who=Depends(ctx.principal)):
        if not delivery.enabled():
            raise HTTPException(404, 'delivery queue is disabled')
        with db() as s:
            row = s.get(delivery.MessageDelivery, identifier)
            if row is None:
                raise HTTPException(404, 'no such delivery')
            session = ctx.session_for(s, who, row.session_id)
            return {'delivery': supervisor.vault.redactor(s, session.vault_keys).deep(delivery.public(row))}

    @api.get('/api/sessions/{acc_id}/report')
    def get_report(acc_id: str, who=Depends(ctx.principal)):
        if not delivery.enabled():
            raise HTTPException(404, 'structured reports are disabled')
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            row = delivery.latest_report(s, acc_id)
            return {'report': supervisor.vault.redactor(s, session.vault_keys).deep(delivery.report_public(row)) if row else None}

    @api.get("/api/sessions/{acc_id}/export")
    async def export_session(acc_id: str, who=Depends(ctx.principal)):
        """The session as a Markdown file: details, conversation (from the history index), handoffs or findings, and
        the command it was launched with."""
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            cwd, account, host, standalone = session.cwd, session.run_as, session.host, bool(session.workdir)
            launched = s.scalar(select(Event.payload).where(Event.session_id == acc_id, Event.kind.like("run_%"))
                                .order_by(Event.id).limit(1))
        docs: list[dict] | None = []
        try:  # a workspace's findings are all its docs; a project session's handoffs are the ones named for it
            found = await broker.call("scan_docs", account=account, host=host, path=cwd)
            for kind, entries in found.items():
                for entry in entries:
                    if standalone or (kind == "handoffs" and acc_id in entry["path"]):
                        docs.append(await broker.call("read_doc", account=account, host=host, path=cwd, rel=entry["path"]))
        except BrokerError:
            docs = None
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            messages = [tuple(row) for row in s.execute(text(
                "SELECT role, text FROM transcript_fts WHERE session_id = :id ORDER BY seq"), {"id": acc_id})]
            body = supervisor.vault.redactor(s, session.vault_keys).text(
                export.markdown(session, messages, docs, (launched or {}).get("command")))
            name = export.filename(session, datetime.now(timezone.utc))
        return Response(body, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @api.get("/api/observations")
    def observations(who=Depends(ctx.owner)):  # host-wide readings
        with db() as s:
            return [{"key": o.key, "value": o.value, "source": o.source, "observed_at": o.observed_at}
                    for o in s.scalars(select(Observation))]

    from . import multiruns
    api.include_router(multiruns.router(ctx, launch_session, preview_session, session_changes))
    return api
