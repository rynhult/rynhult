"""Admission, worker lifetime and explicit owner handover for manual HA."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from .db import WRITE_INTENT
from .ha import Fenced
from .ha_automatic import Policy, evaluate
from .models import AuditLog
from .node_tasks import NodeTasks


class Change(BaseModel):
    expected_epoch: int = Field(ge=0)
    old_main_fenced: bool = False


class Runtime:
    def __init__(self, authority, ctx):
        self.authority, self.ctx = authority, ctx
        self.tasks = []
        self._workers = NodeTasks("ha-workers")
        self.sockets = set()
        self.transition = asyncio.Lock()
        self._last_evaluation = None
        self.authority.auto_evaluate = ctx.settings.ha_auto_evaluate

    def automatic_status(self, actor):
        # No observation adapter: simulated grants never enter production authority.
        # Public health polling cannot create audit records.
        with self.authority.lock:
            decision = evaluate(Policy(enabled=self.ctx.settings.ha_auto_evaluate), None)
            result = {'evaluation_enabled': self.ctx.settings.ha_auto_evaluate,
                      'would_promote': decision.would_promote, 'reason': decision.reason,
                      'promotion_wired': False}
            if result != self._last_evaluation:
                epoch = self.authority.status()['epoch']
                with self.authority.control.begin() as connection:
                    connection.execute(AuditLog.__table__.insert().values(
                        actor=actor, action='ha.automatic_evaluation', target=self.authority.node_id,
                        detail={**result, 'epoch': epoch}))
                self._last_evaluation = result
            return result

    async def worker(self, work):
        from .admin_features import CURRENT
        CURRENT.set(getattr(self.ctx.settings, "features", None))
        with self.authority.activity():
            await work()

    def start(self):
        if features := getattr(self.ctx.settings, "features", None):
            with self.ctx.db() as db:
                features.load(db)
        # Promotion runs inside POST middleware. Workers must retain the normal
        # deferred transaction policy, not inherit the API's serialized lock.
        token = WRITE_INTENT.set(False)
        try:
            self.ctx.broker.hub.closed = False
            self.tasks = [self._workers.track(asyncio.create_task(self.worker(work))) for work in
                          (self.ctx.supervisor.run, self.ctx.updates.run, self.ctx.backup_jobs.run)]
        finally:
            WRITE_INTENT.reset(token)

    async def stop(self):
        self.authority.accepting = False
        sockets = list(self.sockets)
        for ws in sockets:
            ws.cancel()
        supervisor = self.ctx.supervisor
        if any(not task.done() for task in (*supervisor.tools.tasks, *self.ctx.publisher.tasks)):
            self.authority.mark_uncertain('background publish/tool effect')
        # Do not await socket transport completion: a peer may not consume its
        # close frame until this HTTP demotion returns. Activity/transaction
        # locks still refuse the epoch change until handlers have drained.
        tasks = [*self.tasks, *supervisor.tools.tasks, *self.ctx.publisher.tasks]
        if supervisor._backup_task:
            tasks.append(supervisor._backup_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.ctx.broker.close()
        self.tasks.clear()

    @asynccontextmanager
    async def lifespan(self):
        async def monitor():
            while True:
                await asyncio.sleep(1)
                async with self.transition:
                    if self.authority.epoch is not None:
                        try:
                            self.authority.check()
                        except Fenced:
                            await self.stop()
        watcher = self._workers.track(asyncio.create_task(monitor()))
        try:
            yield
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await self.stop()
            self.authority.release()

    def router(self):
        api = APIRouter()

        @api.get('/api/ha/status')
        def status(who=Depends(self.ctx.owner)):
            try:
                result = self.authority.status()
                result['automatic_evaluation'] = self.automatic_status(who.username)
                return result
            except SQLAlchemyError:
                raise HTTPException(503, "Rynhult can't reach its database, so it can't tell which main is active. Check the database, then reload.") from None

        @api.post('/api/ha/promote')
        async def promote(body: Change, who=Depends(self.ctx.owner)):
            async with self.transition:
                self.authority.promote(expected_epoch=body.expected_epoch, old_main_fenced=body.old_main_fenced, actor=who.username)
                try:
                    from . import auth
                    with self.authority.activity(), self.ctx.db() as db:
                        auth.ensure_setup_token(db, self.ctx.settings.setup_token_path)
                    self.start()
                except Exception:
                    # Never leave an active epoch without its workers: hand it back cleanly
                    # so the owner can retry here or promote the other main.
                    await self.stop()
                    try:
                        self.authority.demote(expected_epoch=self.authority.epoch, actor=who.username)
                    except (Fenced, SQLAlchemyError):
                        self.authority.release()  # durable holder remains: fenced recovery decides
                    raise HTTPException(503, "This main couldn't start after promotion, so it handed control back. "
                                             'Check the status on this page before trying again.') from None
                return self.authority.status()

        @api.post('/api/ha/demote')
        async def demote(body: Change, who=Depends(self.ctx.owner)):
            async with self.transition:
                # A wrong/stale operator request must not interrupt the current epoch.
                if self.authority.epoch != body.expected_epoch:
                    raise HTTPException(409, 'This main is not the active one for that change. Reload the page and check the status.')
                await self.stop()
                self.authority.demote(expected_epoch=body.expected_epoch, actor=who.username)
                return self.authority.status()

        return api


class Gate:
    """Protect all API entry points, including GETs with refresh/file side effects.

    A standby offers health, owner status and the static status screen. It does not
    serve cached workload pages that could be mistaken for live machine state.
    """
    def __init__(self, app, authority, runtime=None):
        self.app, self.authority, self.runtime = app, authority, runtime

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        path = scope['path']
        method = scope.get('method', '')
        is_preview = (HTTPConnection(scope).url.hostname or '').startswith('preview-')
        exempt = (not is_preview and scope['type'] == 'http' and
                  ((method == 'GET' and (path in ('/api/health', '/api/auth/state', '/api/ha/status')
                                        or not path.startswith('/api/')))
                   or (method == 'POST' and path in ('/api/ha/promote', '/api/ha/demote'))))
        if exempt:
            return await self.app(scope, receive, send)
        task = asyncio.current_task()
        if scope['type'] == 'websocket' and self.runtime:
            self.runtime.sockets.add(task)
        started = False
        async def output(message):
            nonlocal started
            # A disconnected leadership connection must stop even existing streams.
            self.authority.check()
            if message['type'] in ('http.response.start', 'websocket.accept'):
                started = True
            await send(message)
        async def input_message():
            self.authority.check()
            message = await receive()
            self.authority.check()
            return message
        try:
            with self.authority.activity():
                await self.app(scope, input_message, output)
        except (Fenced, SQLAlchemyError):
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1013})
            elif not started:
                await JSONResponse({'detail': 'Standby or authority unavailable; use the active main'},
                                   status_code=503, headers={'Retry-After': '1'})(scope, receive, send)
            else:
                raise
        except asyncio.CancelledError:
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1012})
            else:
                raise
        finally:
            if scope['type'] == 'websocket' and self.runtime:
                self.runtime.sockets.discard(task)
