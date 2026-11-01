"""Tool approvals, installed pins and owner-controlled person/project exemptions."""
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..models import InstalledTool, Project, Setting, ToolRequest, User
from ..tools import ToolRefused, out
from .context import Context


class ToolIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    manager: str
    name: str
    version: str | None = None
    action: str = "install"
    reason: str = Field(min_length=1, max_length=500)


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(pattern="^(person|project)$")
    target: str = Field(min_length=1, max_length=64)
    required: bool


def router(ctx: Context):
    from .security_settings import audited_router
    api = audited_router(ctx, audit_paths=('/api/tools/policy',), prefix='/api/tools')
    service = ctx.supervisor.tools

    def refused(error):
        return HTTPException(error.status if isinstance(error, ToolRefused) else 422, str(error))

    @api.get("")
    def list_tools(who=Depends(ctx.principal)):
        with ctx.db() as db:
            tools = [out(t) for t in db.scalars(select(InstalledTool).order_by(InstalledTool.name))
                     if service.can_see(db, t, who.username)]
            requests = [out(r) for r in db.scalars(select(ToolRequest).order_by(ToolRequest.created_at.desc()))
                        if service.can_see(db, r, who.username)][:200]
            return {"tools": tools, "requests": requests}

    @api.post("/requests", status_code=201)
    def request(body: ToolIn, who=Depends(ctx.principal)):
        with ctx.db() as db:
            ctx.session_for(db, who, body.session_id)
        try:
            return {"id": service.request_session(**body.model_dump())}
        except ValueError as error:
            raise refused(error) from None

    @api.post("/requests/{request_id}/approve")
    async def approve(request_id: str, who=Depends(ctx.principal)):
        try:
            service.decide(request_id, who.username, True)
        except ValueError as error:
            raise refused(error) from None
        return {"ok": True}

    @api.post("/requests/{request_id}/reject")
    async def reject(request_id: str, who=Depends(ctx.principal)):
        try:
            service.decide(request_id, who.username, False)
        except ValueError as error:
            raise refused(error) from None
        return {"ok": True}

    @api.post("/{tool_id}/remove", status_code=201)
    def remove(tool_id: str, who=Depends(ctx.principal)):
        try:
            return {"id": service.remove(tool_id, who.username)}
        except ValueError as error:
            raise refused(error) from None

    @api.get("/policy")
    def policies(who=Depends(ctx.owner)):
        from .security_settings import human
        human(who)
        with ctx.db() as db:
            return {"people": [{"target": p.username, "label": p.username,
                                "required": (db.get(Setting, f"tools.person.{p.username}").value.get("required", True)
                                             if db.get(Setting, f"tools.person.{p.username}") else True)}
                               for p in db.scalars(select(User).order_by(User.username))],
                    "projects": [{"target": str(p.id), "label": p.name,
                                  "required": (db.get(Setting, f"tools.project.{p.id}").value.get("required", True)
                                               if db.get(Setting, f"tools.project.{p.id}") else True)}
                                 for p in db.scalars(select(Project).where(Project.archived.is_(False)))]}

    @api.put("/policy")
    def policy(body: PolicyIn, request: Request, who=Depends(ctx.owner)):
        from .security_settings import SecurityRefusal, browser
        with ctx.db() as db:
            browser(db, request, who)
        raise SecurityRefusal(410, 'Tool policy changes moved to Settings → Security. Review a scoped Security change there.', 'retired_writer')

    return api
