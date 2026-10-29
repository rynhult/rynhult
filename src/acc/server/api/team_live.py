"""Person-only live team reads. Waiting polls own no database session/transaction."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, select

from ..models import Event, Session, Team
from ..orchestrator import live
from .context import Context

POLL_INTERVAL = 0.5
PAGE_SIZE = 200
WAITING_PER_PERSON = 4


def router(ctx: Context) -> APIRouter:
    routes = APIRouter()
    live.install(ctx)
    # Per person, the waiting polls oldest first. A fifth one ends the oldest at once (it may belong to a tab that was
    # closed or reloaded; nothing tells the server) instead of refusing the newest, so leaving a view never locks the
    # person out of live updates (security review 2026-10-25, OV-10). At most four still wait.
    waiting: dict[int, list[asyncio.Event]] = {}

    def read(request, lead_id, after=None):
        # Re-evaluate the actual cookie, including logout/role changes, every poll.
        who = ctx.principal(request)
        if who.user_id is None:
            raise HTTPException(403, 'Sign in as a person to view a team')
        with ctx.db() as db:
            lead = db.get(Session, lead_id)
            if (lead is None or lead.team_id != lead_id or db.get(Team, lead_id) is None
                    or (not who.is_owner and lead.owner != who.username)):
                raise HTTPException(404, 'Team not found')
            rows = live.members(db, lead)
            ids = [row.id for row in rows]
            scope = (Event.source == 'orchestrator', Event.session_id.in_(ids), Event.kind.in_(live.KINDS))
            ceiling = db.scalar(select(func.max(Event.id)).where(*scope)) or 0
            if after is not None:
                if after > ceiling:
                    raise HTTPException(409, 'Refresh the team snapshot before resuming updates')
                events = list(db.scalars(select(Event).where(*scope, Event.id > after).order_by(Event.id).limit(PAGE_SIZE + 1)))
                by_id = {row.id: row for row in rows}
                result = []
                for entry in events[:PAGE_SIZE]:
                    redact = ctx.supervisor.vault.redactor(db, by_id[entry.session_id].vault_keys).text
                    data = live.payload(entry.kind, entry.payload, redact)
                    if entry.kind.startswith('team.member.'):
                        data['id'] = entry.session_id
                    result.append({'id': entry.id, 'session': entry.session_id, 'kind': entry.kind,
                                   'ts': entry.ts.isoformat(), 'payload': data})
                return who.user_id, {'events': result, 'cursor': result[-1]['id'] if result else after,
                                     'has_more': len(events) > PAGE_SIZE}
            redactors = {row.id: ctx.supervisor.vault.redactor(db, row.vault_keys).text for row in rows}
            extra = live.details(db, rows, scope, redactors)
            members = [dict(live.member_data(row, redactors[row.id]), **extra[row.id]) for row in rows]
            # Snapshot and cursor share one transaction/snapshot; no write can fall
            # between them. Metadata is projected, never a launch spec/task/grant.
            return who.user_id, {'members': members, 'cursor': ceiling}

    @routes.get('/api/teams/{lead_id}/tree')
    async def tree(request: Request, lead_id: str):
        _, result = await asyncio.to_thread(read, request, lead_id)
        return result

    @routes.get('/api/teams/{lead_id}/events')
    async def events(request: Request, lead_id: str,
                     after: int = Query(0, ge=0, le=2**63-1), wait: float = Query(25, ge=0, le=25)):
        user_id, result = await asyncio.to_thread(read, request, lead_id, after)
        if result['events'] or not wait:
            return result
        polls = waiting.setdefault(user_id, [])
        while len(polls) >= WAITING_PER_PERSON:
            polls.pop(0).set()
        evicted = asyncio.Event()
        polls.append(evicted)
        deadline = time.monotonic() + wait
        try:
            while time.monotonic() < deadline:
                try:
                    await asyncio.wait_for(evicted.wait(), min(POLL_INTERVAL, max(0, deadline - time.monotonic())))
                except TimeoutError:
                    pass
                if evicted.is_set():  # a newer view of this person took the slot: answer now, with no events
                    return {**result, 'evicted': True}
                _, result = await asyncio.to_thread(read, request, lead_id, after)
                if result['events']:
                    return result
            return result
        finally:
            if evicted in polls:
                polls.remove(evicted)
            if not polls:
                waiting.pop(user_id, None)

    return routes
