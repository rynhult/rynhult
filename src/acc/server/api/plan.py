"""Orchestrator v2, slice 6: the person's view of a team's plan and roadmap (docs/ORCHESTRATOR-V2.md).

Whoever can see the lead session reads the plan and can edit it. Every edit names the version it was made against; a
stale one answers 409 with the current plan. Each edit is a new version under the person's name. The routes exist
by default unless Orchestrator v2 or its planning slice is explicitly off.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..db import WRITE_INTENT
from ..models import Team
from ..orchestrator import delivery, planning
from ..supervisor_common import LaunchRefused
from .context import Context


def router(ctx: Context) -> APIRouter:
    api = APIRouter()

    def team_of(s, who, lead: str) -> str:
        session = ctx.session_for(s, who, lead)
        if s.get(Team, lead) is None or session.team_id != lead:
            raise HTTPException(404, "no such team")
        return lead

    @api.get("/api/teams/{lead}/plan")
    def read(lead: str, who=Depends(ctx.principal)):
        token = WRITE_INTENT.set(True)  # the view may write (its run row): reserve the writer first, as for the list
        try:
            with ctx.db() as s:
                out = planning.view(s, team_of(s, who, lead))
                s.commit()
                return out
        finally:
            WRITE_INTENT.reset(token)

    @api.get("/api/teams/{lead}/plan/versions/{number}")
    def read_version(lead: str, number: int, who=Depends(ctx.principal)):
        with ctx.db() as s:
            found = planning.version(s, team_of(s, who, lead), number)
            if found is None:
                raise HTTPException(404, "no such plan version")
            return found

    @api.patch("/api/teams/{lead}/plan")
    def edit(lead: str, body: dict, who=Depends(ctx.principal)):
        with ctx.db() as s:
            team_id = team_of(s, who, lead)
            if 'run_mode' in body and who.user_id is None:
                raise HTTPException(403, 'Sign in as a person to change Team > Plan > Run mode.')
            previous_mode = planning.plan_for(s, team_id).run_mode
            try:
                planning.person_update(s, team_id, who.username, body)
                out = planning.view(s, team_id)
                if 'run_mode' in body and previous_mode != out['run_mode'] and delivery.enabled():
                    notice = delivery.run_mode_notice(s, ctx.supervisor, team_id, out['run_mode'], out['version'])
                    out['run_mode_delivery'] = delivery.public(notice)
            except planning.Refused as error:
                s.rollback()
                if error.status == 409:
                    return JSONResponse({"detail": str(error), "plan": jsonable_encoder(planning.view(s, team_id))}, 409)
                raise HTTPException(error.status, str(error))
            except LaunchRefused as error:
                raise HTTPException(409, f'Run mode was not changed: {error}') from None
            nudged = delivery.nudge_lead(s, team_id, "plan")
            s.commit()
            if nudged or out.get('run_mode_delivery'):
                delivery.wake(ctx.supervisor)
            return out

    return api
