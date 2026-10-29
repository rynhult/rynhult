"""Per-session containers (owners only): each agent account's limits, and a project's switch to containers.

Containers are a Preview (docs/CONTAINERS.md, "Release status"): off by default, switched on per project by an owner,
and only on a host that has passed the container qualification (acc.container_host_record).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ... import container_host_record
from .. import containers
from ..models import AuditLog, User
from .context import Context
from .schemas import project_out


class LimitsIn(BaseModel):
    """None (empty in Settings) is "no limit" for that value."""
    cpus: int | None = Field(None, ge=1, le=256)
    memory_mib: int | None = Field(None, ge=256, le=4 << 20)
    pids: int | None = Field(None, ge=64, le=4 << 20)
    disk_gib: int | None = Field(None, ge=1, le=1 << 14)
    sessions: int | None = Field(None, ge=1, le=64)


class ContainersIn(BaseModel):
    enabled: bool = Field(strict=True)


def router(ctx: Context) -> APIRouter:
    api = APIRouter()
    db, settings, broker = ctx.db, ctx.settings, ctx.broker

    def accounts() -> list[str]:
        return [a for a in broker.accounts() if a != broker.services_account or a == broker.default_account]

    def qualification() -> dict:
        result = container_host_record.check(settings.containers_record)
        return {key: result[key] for key in ("passed", "reason", "report_sha256", "recorded_at") if key in result}

    @api.get("/api/containers")
    def get_containers(who=Depends(ctx.owner)):
        with db() as s:
            people: dict[str, list[str]] = {}
            for username, account in s.execute(select(User.username, User.account)):
                people.setdefault(account or broker.default_account, []).append(username)
            rows = []
            for account in accounts():
                limits, source = containers.limits_for(s, account, broker.default_account)
                rows.append({"account": account, "limits": limits, "source": source,
                             "owner": containers.owner_account(s, account, broker.default_account),
                             "people": sorted(people.get(account, []))})
        return {"enabled": settings.containers_enabled, "release": "preview",
                "qualification": qualification(), "policy": settings.containers_policy,
                "member_defaults": containers.MEMBER_DEFAULTS, "ranges": containers.RANGES, "accounts": rows}

    @api.put("/api/containers/limits/{account}")
    def put_limits(account: str, body: LimitsIn, who=Depends(ctx.owner)):
        if account not in accounts():
            raise HTTPException(404, f"There's no agent account {account!r} on this machine. Reload the page.")
        with db() as s:
            before, source = containers.limits_for(s, account, broker.default_account)
            try:
                value = containers.save_limits(s, account, body.model_dump(), who.username)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            s.add(AuditLog(actor=who.username, action="containers.limits", target=account,
                           detail={"limits": value, "before": before, "was": source}))
            s.commit()
        return {"account": account, "limits": value, "source": "set"}

    @api.post("/api/projects/{project_id}/containers")
    def set_containers(project_id: int, body: ContainersIn, who=Depends(ctx.owner)):
        """New sessions in the project run in containers (on) or as today (off). Running sessions keep how they
        started."""
        if body.enabled and not settings.containers_enabled:
            raise HTTPException(409, "Containers per session is turned off in Admin features or server configuration.")
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            if body.enabled and (project.host or "local") != "local":
                raise HTTPException(409, "For now, containers only run on the machine Rynhult is installed on, and this project is on another one.")
            if body.enabled and project.builtin:
                raise HTTPException(409, "Local workspaces don't run in containers.")
            switching_on, passed = body.enabled and not project.containers, {}
            if switching_on:  # switching off, or a project already on, is never refused
                passed = qualification()
                if not passed["passed"]:
                    s.add(AuditLog(actor=who.username, action="project.containers_refused", target=project.slug,
                                   detail={"reason": "host_not_qualified"}))
                    s.commit()
                    raise HTTPException(409, f"containers are a Preview that needs a qualified host: {passed['reason']}")
            project.containers = body.enabled
            s.add(AuditLog(actor=who.username, action="project.containers", target=project.slug,
                           detail={"enabled": body.enabled,
                                   **({"qualification": passed.get("report_sha256")} if switching_on else {})}))
            s.commit()
            return project_out(project)

    return api
