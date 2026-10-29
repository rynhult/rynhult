"""Browser terminal websocket (single writer per session)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from .. import auth, group_board
from ..broker_client import BrokerError, BrokerUnavailable
from ..db import WRITE_INTENT
from ..models import AuditLog, Session
from ..tty import bridge
from .context import Context

log = logging.getLogger("acc.server.terminal")
TEXT_LIMIT = 200_000  # characters of a text snapshot (the broker applies the same cap)


def router(ctx: Context) -> APIRouter:
    settings, db, writers = ctx.settings, ctx.db, ctx.writers
    api = APIRouter()

    @api.get("/api/sessions/{acc_id}/terminal-output")
    async def terminal_output(acc_id: str, who=Depends(ctx.principal)):
        with db() as s:
            session = ctx.session_for(s, who, acc_id)
            if session.management != "acc" or not session.tmux_name:
                raise HTTPException(409, "This session has no Rynhult terminal.")
            name, account, host = session.tmux_name, session.run_as, session.host
        # Each read captures up to ~10,000 rows of up to 1,000 columns on the broker: bound how often one person can.
        if ctx.terminal_reads.blocked(who.username):
            raise HTTPException(429, "Too many terminal snapshots; try again in a minute.")
        ctx.terminal_reads.fail(who.username)
        token = WRITE_INTENT.set(True)  # like terminal.attach: reading the text is access to the terminal
        try:
            with db() as s:
                s.add(AuditLog(actor=who.username, action="terminal.read", target=acc_id))
                s.commit()
        except OperationalError:
            log.warning("terminal.read audit for %s skipped: database busy", acc_id)
        finally:
            WRITE_INTENT.reset(token)
        try:
            result = await ctx.broker.call("terminal_output", account=account, host=host, tmux_name=name)
        except BrokerError as error:
            raise ctx.refused(error) from None
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise HTTPException(502, "The session's terminal sent a snapshot Rynhult couldn't read. Try again.")
        # The broker caps it; an enrolled machine answers for its own broker, so cap it here as well.
        text = result["text"]
        truncated = bool(result.get("truncated")) or len(text) > TEXT_LIMIT
        text = text[-TEXT_LIMIT:]
        if truncated and "\n" in text:  # the first row may begin mid-way through a key, where redaction can't see it
            text = text[text.index("\n") + 1:]
        with db() as s:  # like transcripts, diffs and exports: the values of the session's keys never leave as text
            text = ctx.supervisor.vault.redactor(s, ctx.session_for(s, who, acc_id).vault_keys).text(text)
        # Like the live terminal, output may contain secrets; never let an HTTP cache retain it.
        return JSONResponse({"text": text, "truncated": truncated}, headers={"Cache-Control": "no-store"})

    @api.websocket("/api/sessions/{acc_id}/tty")
    async def terminal(ws: WebSocket, acc_id: str, mode: str = "write", take: int = 0, rows: int = 40, cols: int = 120):
        if not ws.headers.get("origin") or ws.headers.get("origin") not in settings.allowed_origins:
            await ws.close(code=4403)
            return
        with db() as s:  # read: who is asking, and which tmux session
            token = ws.cookies.get(ctx.session_cookie)
            record = auth.lookup_session(s, token, edge_fingerprint=ws.scope.get('state', {}).get('edge_fingerprint'),
                native=ws.scope.get('state', {}).get('edge_native', False))
            session = s.get(Session, acc_id)
            username = record.user.username if record else None
            if record is not None and record.user.role != "owner" and session is not None and session.owner != username:
                session = None  # someone else's session: as if it didn't exist
            if session and session.project.group_id and record and record.user.role != 'owner' and not group_board.authorized(s, session, ctx.settings.groups_remote_enabled):
                session = None
            tmux_name = session.tmux_name if session and session.management == "acc" else None
            if session and mode == "write":
                from ...backup_activation import held
                if held(s, session):  # a restored session the old main left running: inspect, never type into it
                    mode, take = "read", 0
            account = session.run_as if session else None
            host = session.host if session else "local"
            s.rollback()  # no last-seen write here; the HTTP requests around it keep that current
        if username and tmux_name:
            WRITE_INTENT.set(True)  # websockets skip the HTTP middleware that sets it
            try:
                with db() as s:
                    s.add(AuditLog(actor=username, action="terminal.attach", target=acc_id,
                                   detail={"mode": mode, "take": bool(take)}))
                    s.commit()
            except OperationalError:
                log.warning("terminal.attach audit for %s skipped: database busy", acc_id)
        if username is None:
            await ws.close(code=4401)
            return
        if tmux_name is None:
            await ws.close(code=4404)
            return
        async def signed_in() -> bool:
            with db() as s:
                current = auth.lookup_session(s, token, edge_fingerprint=ws.scope.get('state', {}).get('edge_fingerprint'),
                native=ws.scope.get('state', {}).get('edge_native', False))
                target = s.get(Session, acc_id)
                alive = (current is not None and target is not None and target.management == "acc"
                         and (current.user.role == "owner" or (target.owner == current.user.username
                             and (not target.project.group_id or group_board.authorized(s, target, ctx.settings.groups_remote_enabled)))))
                s.rollback()
            return alive

        await ws.accept()
        key = auth.digest(token or "")
        ctx.terminals.setdefault(key, set()).add(ws)  # logout or a revoke closes it at once (see auth routes)
        try:
            if not await signed_in():  # role/ownership may have changed while accepting the socket
                await ws.close(code=4403)
                return
            await bridge(ws, lambda: ctx.broker.open_stream(account, host), acc_id, tmux_name, writers,
                         want_write=mode == "write", take=bool(take), rows=rows, cols=cols,
                         signed_in=signed_in, recheck=ctx.terminal_recheck,
                         on_input=lambda: ctx.supervisor.cancel_automation(acc_id))
        except WebSocketDisconnect:
            pass  # the browser may leave before the broker handshake finishes
        except (OSError, ConnectionError, BrokerUnavailable):
            try:
                await ws.close(code=4503)
            except (OSError, RuntimeError):
                pass  # the edge guard already ended this public stream
        finally:
            open_ = ctx.terminals.get(key)
            if open_ is not None:
                open_.discard(ws)
                if not open_:
                    ctx.terminals.pop(key, None)

    return api
