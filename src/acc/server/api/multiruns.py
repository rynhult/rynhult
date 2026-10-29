"""Independent, person-launched alternatives to one task; never an Orchestrator team."""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from ... import providers
from ..broker_client import BrokerError
from ..compiler import CompileError
from ..models import API_PRINCIPAL, AuditLog, MultiRun, MultiRunEntry, Observation, Project, Session, User
from ..supervisor import LaunchRefused
from .context import Context
from .schemas import SessionIn, session_out

log = logging.getLogger(__name__)


class RunChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    profile: str | None = None
    model: str | None = None
    effort: str | None = None
    endpoint: str | None = None
    options: dict[str, str | bool] = Field(default_factory=dict, max_length=20)
    confirm_dangerous: bool = False


class MultiRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int
    name: str = Field(min_length=1, max_length=40)
    initial_prompt: str = Field(min_length=1, max_length=20000)
    runs: list[RunChoice] = Field(min_length=2, max_length=5)

    @model_validator(mode="after")
    def validate_launches(self):
        if not self.name.strip() or not self.initial_prompt.strip():
            raise ValueError("Give the multi-run a name and a task.")
        self.launches()  # use ordinary input validation, including provider options and prompt flag protection
        return self

    def launches(self) -> list[SessionIn]:
        return [SessionIn.model_validate({
            **choice.model_dump(exclude_unset=True), "name": self.name.strip(),
            "project_id": self.project_id, "initial_prompt": self.initial_prompt, "worktree": True,
        }) for choice in self.runs]


class KeepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    archive_others: bool = False
    confirm: bool = False


def router(ctx: Context, launch_session, preview_session, session_changes) -> APIRouter:
    api = APIRouter()
    db, supervisor = ctx.db, ctx.supervisor

    def group_for(s, who, group_id):
        group = s.get(MultiRun, group_id)
        if not ctx.owns(group, who):
            raise HTTPException(404, "That multi-run wasn't found.")
        return group

    def entries(s, group_id):
        return list(s.scalars(select(MultiRunEntry).where(MultiRunEntry.group_id == group_id)
                              .order_by(MultiRunEntry.position)))

    def group_out(s, group, who):
        project = s.get(Project, group.project_id) if group.project_id is not None else None
        owners_project = project is not None and (project.owner in (None, API_PRINCIPAL) or
            s.scalar(select(User.role).where(User.username == project.owner)) == "owner")
        github = s.get(Observation, f"github.{project.id}") if owners_project and not project.private_workspace else None
        runs = []
        for entry in entries(s, group.id):
            session = ctx.session_for(s, who, entry.session_id) if entry.session_id else None
            usage = s.get(Observation, f"claude.statusline.{session.id}") if session else None
            branch = ((session.changes or {}).get("branch") or session.worktree_branch) if session else None
            ci = None
            if github and branch:
                ci = {"source": github.source, "observed_at": github.observed_at,
                      "error": github.value.get("error"),
                      "run": next((r for r in github.value.get("runs", []) if r.get("branch") == branch), None)}
            runs.append({"position": entry.position, "choice": entry.choice, "error": entry.error,
                         "session": session_out(session) if session else None,
                         "usage": {"cost": usage.value.get("cost"), "context_window": usage.value.get("context_window"),
                                   "source": usage.source, "observed_at": usage.observed_at} if usage else None,
                         "ci": ci})
        return {"id": group.id, "name": group.name, "project_id": group.project_id, "project": project.slug if project else "Deleted project",
                "owner": group.owner, "created_at": group.created_at, "winner_session_id": group.winner_session_id,
                "runs": runs}

    @api.get("/api/multiruns")
    def list_groups(who=Depends(ctx.principal)):
        with db() as s:
            groups = s.scalars(ctx.scope(select(MultiRun), MultiRun, who).order_by(MultiRun.created_at.desc()))
            # A transferred session is not made visible through its former group.
            return [group_out(s, g, who) for g in groups
                    if all(not e.session_id or ctx.owns(s.get(Session, e.session_id), who)
                           for e in entries(s, g.id))]

    @api.get("/api/multiruns/{group_id}")
    def get_group(group_id: str, who=Depends(ctx.principal)):
        with db() as s:
            return group_out(s, group_for(s, who, group_id), who)

    @api.post("/api/multiruns", status_code=201)
    async def start_group(body: MultiRunIn, who=Depends(ctx.principal)):
        launches = body.launches()
        with db() as s:
            project = ctx.project_for(s, who, body.project_id)
            if project.archived or not project.trusted or project.import_mode != "managed":
                raise HTTPException(409, "Choose a project that is trusted, managed and not archived.")
        # Confirm every dangerous choice before the first worktree or session is created.
        for launch in launches:
            try:
                inputs = launch.inputs(supervisor.catalog)
                await preview_session(launch, who)
            except (CompileError, KeyError, ValueError) as error:
                raise HTTPException(422, str(error)) from None
            if providers.get(inputs.provider).dangerous(inputs) and not launch.confirm_dangerous:
                raise HTTPException(409, "This launch lets the agent run commands without asking. Tick the confirmation to go ahead.")
        group_id = uuid.uuid4().hex[:12]
        with db() as s:
            s.add(MultiRun(id=group_id, project_id=body.project_id, name=body.name.strip(), owner=who.username))
            s.flush()
            for i, choice in enumerate(body.runs):
                s.add(MultiRunEntry(group_id=group_id, position=i, choice=choice.model_dump()))
            s.add(AuditLog(actor=who.username, action="multirun.start", target=group_id,
                           detail={"project_id": body.project_id, "runs": [c.model_dump() for c in body.runs]}))
            s.commit()
        results = await asyncio.gather(*(launch_session(launch, who, multirun_entry=(group_id, i))
                                         for i, launch in enumerate(launches)), return_exceptions=True)
        with db() as s:
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    if isinstance(result, HTTPException):
                        error = str(result.detail)
                    else:
                        log.error("multi-run %s choice %s failed", group_id, i,
                                  exc_info=(type(result), result, result.__traceback__))
                        error = "This run couldn't start because of an unexpected error. An owner can find the cause in the server log."
                    s.get(MultiRunEntry, (group_id, i)).error = error
            s.commit()
            return group_out(s, s.get(MultiRun, group_id), who)

    def selected(s, who, group_id, session_id):
        group = group_for(s, who, group_id)
        if not any(e.session_id == session_id for e in entries(s, group.id)):
            raise HTTPException(404, "That run isn't part of this multi-run.")
        return ctx.session_for(s, who, session_id)

    @api.get("/api/multiruns/{group_id}/compare")
    async def compare(group_id: str, left: str, right: str, who=Depends(ctx.principal)):
        with db() as s:
            selected(s, who, group_id, left)
            selected(s, who, group_id, right)
        if left == right:
            raise HTTPException(422, "Choose two different runs.")
        a, b = await asyncio.gather(session_changes(left, who), session_changes(right, who))
        return {"left": a, "right": b}

    @api.post("/api/multiruns/{group_id}/keep")
    async def keep(group_id: str, body: KeepIn, who=Depends(ctx.principal)):
        if body.archive_others and not body.confirm:
            raise HTTPException(409, "Confirm stopping and archiving the other runs. Their branches are kept.")
        async with supervisor.__dict__.setdefault("_multirun_lock", asyncio.Lock()):
            with db() as s:
                selected(s, who, group_id, body.session_id)
                others = [e.session_id for e in entries(s, group_id)
                          if e.session_id and e.session_id != body.session_id]
                for acc_id in others:
                    ctx.session_for(s, who, acc_id)
                group_for(s, who, group_id).winner_session_id = body.session_id
                s.add(AuditLog(actor=who.username, action="multirun.keep", target=group_id,
                               detail=body.model_dump(exclude={"confirm"})))
                s.commit()
            errors = []
            if body.archive_others:
                for acc_id in others:
                    try:
                        await supervisor.stop(acc_id, who.username, archive=True)
                    except (LaunchRefused, BrokerError) as error:
                        errors.append({"session_id": acc_id, "error": str(error)})
            with db() as s:
                return {**group_out(s, group_for(s, who, group_id), who), "archive_errors": errors}

    return api
