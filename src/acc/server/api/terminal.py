"""Browser terminal websocket (single writer per session)."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import object_session

from .. import auth
from ..broker_client import BrokerError, BrokerUnavailable
from ..db import WRITE_INTENT
from ..models import AuditLog, Session, SessionRun
from ..terminal_fencing import fenced_bridge
from ..tty import bridge
from .context import Context, Principal

log = logging.getLogger("acc.server.terminal")
TEXT_LIMIT = 200_000  # characters of a text snapshot (the broker applies the same cap)


def router(ctx: Context) -> APIRouter:
    settings, db, writers = ctx.settings, ctx.db, ctx.writers
    api = APIRouter()

    def run_identity(target):
        if target is None or target.management != 'acc' or target.desired_state != 'running':
            return None
        run = object_session(target).scalar(select(SessionRun).where(SessionRun.session_id == target.id).order_by(SessionRun.id.desc()).limit(1))
        return str(run.id) if run and run.ended_at is None else None

    @api.get("/api/sessions/{acc_id}/terminal-reference")
    async def terminal_reference(acc_id: str, who=Depends(ctx.principal)):
        with db() as s:
            target = ctx.session_for(s, who, acc_id)
            from ..models import RelaySession
            if s.get(RelaySession, acc_id):
                raise HTTPException(409, 'Relay Terminal mediation is not qualified for this session.')
            run = run_identity(target)
            if run is None or not target.tmux_name:
                raise HTTPException(409, "This session has no current terminal run.")
            return JSONResponse({'protocol': 'run-observer-v1', 'installation': ctx.terminal_instance, 'sessionId': acc_id, 'runId': run,
                                 'person': who.username, 'node': target.host, 'account': target.run_as},
                                headers={'Cache-Control': 'no-store'})

    def run_reference(session):
        run = session.runs[-1] if session.runs else None
        if session.management != 'acc' or not session.tmux_name or not run or run.ended_at is not None:
            return None
        return {'sessionId': session.id, 'runId': str(run.id), 'node': session.host or 'local', 'account': session.run_as}


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
            result = await ctx.broker.call("terminal_output", account=account, host=host, tmux_name=name, watching=True)
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
    async def terminal(ws: WebSocket, acc_id: str, mode: str = "write", take: int = 0, rows: int = 40, cols: int = 120, run_id: str | None = None, run: str | None = None, instance: str | None = None, take_reason: str | None = None, confirm_partial: bool = False):
        if not ws.headers.get("origin") or ws.headers.get("origin") not in settings.allowed_origins:
            await ws.close(code=4403)
            return
        with db() as s:  # read: who is asking, and which tmux session
            token = ws.cookies.get(ctx.session_cookie)
            record = auth.lookup_session(s, token, edge_fingerprint=ws.scope.get('state', {}).get('edge_fingerprint'),
                native=ws.scope.get('state', {}).get('edge_native', False))
            session = s.get(Session, acc_id)
            username = record.user.username if record else None
            user_id = record.user.id if record else None
            if record is not None:
                try:
                    session = ctx.session_for(s, Principal(username, record.user.role,
                        record.user.account or settings.default_account, record.user.id), acc_id)
                except HTTPException:
                    session = None
            if run is not None and (run_id is not None or instance != ctx.terminal_instance or mode != 'read' or take or run_identity(session) != run):
                await ws.accept()
                await ws.close(code=4404)
                return
            from ..models import RelaySession
            if session and s.get(RelaySession, acc_id):
                # Even a read attach can emit native replies/resize. Until the
                # proxy/TerminalHost is qualified, never enter the raw bridge.
                await ws.accept()
                await ws.close(code=4409, reason='Relay Terminal mediation is unavailable')
                return
            tmux_name = session.tmux_name if session and session.management == "acc" else None
            if session and mode == "write":
                from ...backup_activation import held
                if held(s, session):  # a restored session the old main left running: inspect, never type into it
                    mode, take = "read", 0
            account = session.run_as if session else None
            host = session.host if session else "local"
            bound = run_reference(session) if session else None
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
            if run is not None:
                await ws.accept()
            await ws.close(code=4401)
            return
        if tmux_name is None:
            if run is not None:
                await ws.accept()
            await ws.close(code=4404)
            return
        if run_id is not None and (bound is None or bound['runId'] != run_id or mode != 'read' or take):
            await ws.close(code=4409)
            return
        def authorized_now() -> bool:
            intent = WRITE_INTENT.set(False)
            try:
                with db() as s:
                    current = auth.lookup_session(s, token, edge_fingerprint=ws.scope.get('state', {}).get('edge_fingerprint'),
                        native=ws.scope.get('state', {}).get('edge_native', False))
                    if current is None:
                        return False
                    try:
                        target = ctx.session_for(s, Principal(current.user.username, current.user.role,
                            current.user.account or settings.default_account, current.user.id), acc_id)
                    except HTTPException:
                        return False
                    from ..models import RelaySession
                    if s.get(RelaySession, acc_id):
                        return False
                    return bool(target.management == 'acc' and (run_id is None or (run_reference(target) == bound and target.tmux_name == tmux_name)) and (run is None or (
                        instance == ctx.terminal_instance and run_identity(target) == run
                        and target.tmux_name == tmux_name and target.run_as == account and target.host == host)))
            finally:
                WRITE_INTENT.reset(intent)

        async def signed_in():
            return await asyncio.to_thread(authorized_now)

        async def control_allowed():
            if not await signed_in():
                return False
            from ...backup_activation import held
            with db() as s:
                target = s.get(Session, acc_id)
                return target is not None and not held(s, target)

        async def audit_control(event):
            with db() as s:
                s.add(AuditLog(actor=username, action='terminal.control', target=acc_id,
                               detail={'run_id': run_id or (bound or {}).get('runId'), **event}))
                s.commit()

        await ws.accept()
        key = auth.digest(token or "")
        ctx.terminals.setdefault(key, set()).add(ws)  # logout or a revoke closes it at once (see auth routes)
        try:
            if not await signed_in():  # role/ownership may have changed while accepting the socket
                await ws.close(code=4403)
                return
            if bound:
                writers.bind_run(acc_id, bound['runId'])
            if run_id is not None:
                await fenced_bridge(ws, lambda: ctx.broker.open_stream(account, host), acc_id, tmux_name, writers,
                    run_id=run_id, rows=rows, cols=cols, valid=signed_in, can_control=control_allowed,
                    recheck=ctx.terminal_recheck, on_input=lambda: ctx.supervisor.cancel_automation(acc_id), on_control=audit_control, actor=username, actor_id=user_id)
            else:
                if run is not None:
                    # An old broker must refuse BEFORE attach can resize its program.
                    try:
                        geometry = await ctx.broker.call('terminal_observer_geometry', account=account, host=host, tmux_name=tmux_name)
                    except BrokerError:
                        await ws.close(code=4404)
                        return
                    if (not isinstance(geometry, dict) or geometry.get('geometry') != 'manual-v1'
                            or type(geometry.get('rows')) is not int or not 5 <= geometry['rows'] <= 500
                            or type(geometry.get('cols')) is not int or not 20 <= geometry['cols'] <= 1000):
                        await ws.close(code=4404)
                        return
                    rows, cols = geometry['rows'], geometry['cols']
                await bridge(ws, lambda: ctx.broker.open_stream(account, host), acc_id, tmux_name, writers,
                             want_write=mode == "write", take=bool(take), rows=rows, cols=cols,
                             signed_in=signed_in, recheck=ctx.terminal_recheck,
                             on_input=lambda: ctx.supervisor.cancel_automation(acc_id), bound_run=run, bound_instance=instance,
                             observer_geometry=(rows, cols) if run is not None else None, take_reason=take_reason,
                             confirm_partial=confirm_partial, actor=username, actor_id=user_id, on_control=audit_control)
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
