"""Person-only selection ledger, bounded overrides and owner allow-lists (slice 3)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import func, select

from ..models import Session, Setting, Team
from ..orchestrator import selection as core
from ..supervisor_common import LaunchRefused
from .context import Context


class Override(BaseModel):
    model_config = ConfigDict(extra='forbid')
    choice: StrictInt = Field(ge=0,le=11)
    reason: str = Field(min_length=1,max_length=1000)


def router(ctx: Context) -> APIRouter:
    api=APIRouter()

    def person(request:Request,who=Depends(ctx.principal)):
        if not core.enabled():
            raise HTTPException(404,'Not Found')
        if who.user_id is None or not getattr(request.state,'session_hash',None):
            raise HTTPException(403,'selection needs a signed-in person')
        return who

    def owner(who=Depends(person)):
        if not who.is_owner:
            raise HTTPException(403,'only an owner edits selection allow-lists')
        return who

    def team_for(db,who,lead):
        session=ctx.session_for(db,who,lead)
        team=db.get(Team,lead)
        if team is None or session.team_id!=lead:
            raise HTTPException(404,'no such team')
        return team

    @api.get('/api/teams/{lead}/selections')
    def selections(lead:str,assignment_offset:int=Query(0,ge=0,le=1000),assignment_limit:int=Query(25,ge=1,le=100),who=Depends(person)):
        with ctx.db() as db:
            team_for(db,who,lead)
            rows=db.scalars(select(core.Selection).where(core.Selection.team_id==lead)
                            .order_by(core.Selection.created_at.desc(),core.Selection.id).limit(100))
            from ..orchestrator import assignments
            total = db.scalar(select(func.count()).select_from(assignments.Assignment).where(assignments.Assignment.team_id == lead))
            result = {'selections':[core.output(row) for row in rows], 'assignments_total':total,
                    'assignments':[assignments.output(db, row) for row in db.scalars(select(assignments.Assignment)
                        .where(assignments.Assignment.team_id == lead).order_by(assignments.Assignment.created_at.desc(), assignments.Assignment.id.desc()).offset(assignment_offset).limit(assignment_limit))]}
            return ctx.supervisor.vault.redactor(db, db.get(Session, lead).vault_keys).deep(result)

    @api.post('/api/teams/{lead}/attempts/{attempt_id}/fail')
    def fail_attempt(lead: str, attempt_id: str, body: dict, who=Depends(ctx.principal)):
        """The person marks an uncertain attempt failed after inspecting it (review C-L3). Never one that is running:
        stop its helper first. A start that never created its session gives its count back."""
        if not core.enabled():
            raise HTTPException(404, 'Not Found')
        from ..orchestrator import assignments
        with ctx.db() as db:
            team_for(db, who, lead)  # ownership first: others get 404 whatever they send
            attempt = db.get(assignments.Attempt, attempt_id) if isinstance(attempt_id, str) else None
            assignment = db.get(assignments.Assignment, attempt.assignment_id) if attempt else None
            if assignment is None or assignment.team_id != lead:
                raise HTTPException(404, 'no such attempt')
            reason = body.get('reason')
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                raise HTTPException(422, 'say what you inspected (1-1000 characters)')
            session = db.get(Session, attempt.session_id or attempt.id)
            if attempt.state in ('failed', 'cancelled', 'reported'):
                raise HTTPException(409, f'this attempt is already {attempt.state}')
            if session is not None and (session.desired_state == 'running' or session.team_stop_pending):
                raise HTTPException(409, 'its helper is running; stop it and wait for the stop to be confirmed first')
            from ..nodes import node_lines
            assignments.fail_attempt(db, attempt, who.username, 'marked failed by the person: ' + node_lines(reason, 1000))
            db.commit()
            return assignments.output(db, assignment)

    @api.post('/api/teams/{lead}/selections/{selection_id}/override')
    async def override(lead:str,selection_id:str,body:Override,who=Depends(person)):
        with ctx.db() as db:
            team_for(db,who,lead)
            row=db.get(core.Selection,selection_id)
            if row is None or row.team_id!=lead:
                raise HTTPException(404,'no such selection')
            if row.state not in ('proposed','overridden') or body.choice>=len(row.candidates):
                raise HTTPException(409,'selection is refused, consumed or unavailable')
            candidate=dict(row.candidates[body.choice])
            if not candidate['eligible']:
                raise HTTPException(409,'this candidate was filtered; request a fresh selection')
            caller=db.get(Session,row.requested_by)
            account=caller.run_as
        ready=await core.readiness(ctx.supervisor,account,[candidate['machine']],{candidate['agent']})
        lock=ctx.supervisor.__dict__.setdefault('_team_lock',asyncio.Lock())
        async with lock:
            with ctx.db() as db:
                team_for(db,who,lead)
                row=db.scalar(select(core.Selection).where(core.Selection.id==selection_id).with_for_update())
                if (row is None or row.team_id!=lead or row.state not in ('proposed','overridden')
                        or body.choice>=len(row.candidates) or row.candidates[body.choice]!=candidate):
                    raise HTTPException(409,'selection changed; reload')
                try:
                    caller,team=core.authority(db,ctx.supervisor,row.requested_by)
                    core.check_candidate(db,caller,team,row,candidate,ready,default_account=ctx.supervisor.broker.default_account,catalog=ctx.supervisor.catalog)
                except LaunchRefused as error:
                    raise HTTPException(409,str(error)) from None
                reason=ctx.supervisor.vault.redactor(db,caller.vault_keys).text(body.reason.strip())
                if not reason:
                    raise HTTPException(422,'enter a reason for the override')
                row.profile_digest=core.profile_digest(ctx.supervisor.catalog, team.limits['agents'][candidate['grant_index']])
                row.catalog_revision=ctx.supervisor.catalog.revision
                row.chosen=body.choice
                row.state='overridden'
                row.override_by=who.username
                row.override_reason=reason
                core.record_event(db,row,who.username,'overridden')
                db.commit()
                return core.output(row)

    @api.get('/api/settings/orchestrator/selection')
    def get_preferences(who=Depends(owner)):
        with ctx.db() as db:
            return core.preferences(db)

    @api.put('/api/settings/orchestrator/selection')
    async def put_preferences(body:dict,who=Depends(owner)):
        try:
            value=core.validate_preferences(body)
        except (ValueError,TypeError,KeyError):
            raise HTTPException(422,'invalid selection allow-lists or quota settings') from None
        lock=ctx.supervisor.__dict__.setdefault('_team_lock',asyncio.Lock())
        async with lock:
            with ctx.db() as db:
                from ..models import AuditLog
                db.merge(Setting(key=core.POLICY_KEY,value=value,updated_by=who.username))
                db.add(AuditLog(actor=who.username,action='selection.preferences_changed',target=core.POLICY_KEY,
                                detail={'rules':value['rules'], 'quota':value.get('quota', core.quota_usage.DEFAULTS)}))
                db.commit()
        return value

    return api
