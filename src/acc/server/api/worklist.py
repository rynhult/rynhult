"""Orchestrator v2, slice 1: the person's side of a team's working list (docs/ORCHESTRATOR-V2.md, section 6).

Whoever can see the lead session reads and edits its list. Every write names the revision it was made against;
a stale card answers 409 with that card and the current list. The routes default on; explicit Off overrides disable them.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..db import WRITE_INTENT
from ..models import Team
from ..orchestrator import delivery, worklist
from .context import Context


class ItemIn(BaseModel):
    kind: str
    title: str
    steps: str = ""
    options: list | None = None
    recommendation: dict | None = None
    default: dict | None = None
    links: list | None = None
    blocks: list[str] | None = None


class DiscussIn(BaseModel):
    revision: int = Field(strict=True, ge=1)
    text: str = Field(min_length=1, max_length=6000)


class NowIn(BaseModel):
    text: str
    revision: int
    now_revision: int | None = None  # the NOW the person started from (break-it review 2026-10-26)


def router(ctx: Context) -> APIRouter:
    api = APIRouter()
    worklist.DB["factory"] = ctx.db

    def team_of(s, who, lead: str) -> str:
        session = ctx.session_for(s, who, lead)
        if s.get(Team, lead) is None or session.team_id != lead:
            raise HTTPException(404, "no such team")
        return lead

    def answer(s, team_id: str, error: worklist.Refused, item_id: int | None = None):
        s.rollback()
        if error.status == 409:
            current = worklist.snapshot(s, team_id)
            return JSONResponse(jsonable({"detail": str(error), "worklist": current,
                **({"item": next((i for i in current["items"] if i["id"] == item_id), None)} if item_id is not None else {})}), 409)
        raise HTTPException(error.status, str(error))

    def jsonable(value):
        from fastapi.encoders import jsonable_encoder
        return jsonable_encoder(value)

    @api.get("/api/teams/{lead}/worklist")
    def read(lead: str, who=Depends(ctx.principal)):
        # This read also writes (sweep, report needs, the run row): reserve the writer up front, or a commit by the lead
        # between its first read and first write fails it with "database is locked" (db.WRITE_INTENT).
        token = WRITE_INTENT.set(True)
        try:
            with ctx.db() as s:
                team_id = team_of(s, who, lead)
                worklist.sweep(s, team_id)
                worklist.ingest_reports(s, team_id)  # slice 4's report needs, in this transaction (outbox contract)
                out = worklist.snapshot(s, team_id)
                s.commit()
                return out
        finally:
            WRITE_INTENT.reset(token)

    @api.post("/api/teams/{lead}/worklist/items", status_code=201)
    def create(lead: str, body: ItemIn, who=Depends(ctx.principal)):
        with ctx.db() as s:
            team_id = team_of(s, who, lead)
            try:
                item = worklist.add(s, team_id, who.username, worklist.person_source(who.username),
                                    body.model_dump(exclude_none=True))
            except worklist.Refused as error:
                return answer(s, team_id, error)
            s.commit()
            return worklist.item_out(item)

    @api.patch("/api/teams/{lead}/worklist/items/{item_id}")
    def change(lead: str, item_id: int, body: dict, who=Depends(ctx.principal)):
        with ctx.db() as s:
            team_id = team_of(s, who, lead)
            try:
                item = worklist.person_update(s, team_id, item_id, who.username, body)
            except worklist.Refused as error:
                return answer(s, team_id, error, item_id)
            nudged = ("answer" in body or "answer_text" in body or body.get("state") == "done" and item.kind == "need") and \
                delivery.nudge_lead(s, team_id, "answer")
            s.commit()
            if nudged:
                delivery.wake(ctx.supervisor)
            return worklist.item_out(item)

    @api.post('/api/teams/{lead}/worklist/items/{item_id}/discuss', status_code=202)
    def discuss(lead: str, item_id: int, body: DiscussIn, who=Depends(ctx.principal)):
        from ..models import Session, utcnow
        from ..supervisor_common import LaunchRefused
        with ctx.db() as s:
            team_id = team_of(s, who, lead)
            item = s.get(worklist.WorkItem, item_id)
            if item is None or item.team_id != team_id:
                raise HTTPException(404, 'no such item')
            if item.revision != body.revision:
                return answer(s, team_id, worklist.Refused('the item changed since you loaded it', 409))
            session, team = s.get(Session, lead), s.get(Team, lead)
            if item.kind != 'decision' or item.state != 'open':
                raise HTTPException(409, 'Discuss an open decision card.')
            if not delivery.enabled() or team.stopped or (team.expires_at is not None and team.expires_at <= utcnow()) or session.desired_state != 'running':
                raise HTTPException(409, 'Resume an active team before sending a discussion.')
            if len(item.links or []) >= 20:
                raise HTTPException(422, 'This card already has 20 links.')
            try:
                delivery.validate_text(body.text)
                # Card titles are agent-authored evidence, never part of the person's origin label or message.
                text = (f'[Discussion from your person about working-list card #{item.id}, revision {item.revision}] '
                        f'{body.text} Read the card with worklist. Keep this card open. Reply on this card with worklist_update(item={item.id}, note=your reply).')
                row = delivery.enqueue_in_transaction(s, ctx.supervisor, session, who.username, text)
            except LaunchRefused as error:
                raise HTTPException(422, str(error)) from None
            item.links = [*(item.links or []), {'kind': 'delivery', 'id': row.id}]
            item.revision += 1
            worklist._bump(worklist.run_for(s, lead))
            worklist._audit(s, who.username, lead, 'discuss', item)
            s.commit()
            result = {'item': worklist.item_out(item), 'delivery': delivery.public(row)}
        delivery.wake(ctx.supervisor)
        return result

    @api.put("/api/teams/{lead}/worklist/now")
    def set_now(lead: str, body: NowIn, who=Depends(ctx.principal)):
        with ctx.db() as s:
            team_id = team_of(s, who, lead)
            try:
                run = worklist.person_now(s, team_id, who.username, body.text, body.revision, body.now_revision)
            except worklist.Refused as error:
                return answer(s, team_id, error)
            nudged = delivery.nudge_lead(s, team_id, "now")
            s.commit()
            if nudged:
                delivery.wake(ctx.supervisor)
            return {"now": run.now, "revision": run.revision}

    return api
