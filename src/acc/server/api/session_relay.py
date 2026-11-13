"""Authenticated hub HTTP binding. Provider capabilities require native qualification."""
import hashlib
import json
import threading
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from acc.session_relay import protocol as p

from ..admin_features import enabled, require
from ..db import WRITE_INTENT, write_intent
from ..models import RelayPreference, RelaySession, Session, SessionRun, User
from ..session_relay import authority, claude_public, history, store
from ..session_relay.connections import Connections
from ..vault import Cipher
from .project_briefs import PrivateJSONResponse

RPC_RATE_LIMIT = 240
RPC_RATE_WINDOW = 1.0


class RpcRateLimit:
    """Bound authenticated RPC dispatch per session/actor, with bounded memory."""
    def __init__(self, *, clock=time.monotonic, capacity=4096):
        self.clock, self.capacity = clock, capacity
        self.entries = {}
        self.lock = threading.Lock()

    def admit(self, session_id, actor):
        key = (session_id, p.canonical_json(p.to_wire(actor)))
        with self.lock:
            now = self.clock()
            if key not in self.entries and len(self.entries) >= self.capacity:
                self.entries = {k: v for k, v in self.entries.items() if now - v[0] < RPC_RATE_WINDOW}
                if len(self.entries) >= self.capacity:
                    return False
            start, count = self.entries.get(key, (now, 0))
            if now - start >= RPC_RATE_WINDOW:
                start, count = now, 0
            if count >= RPC_RATE_LIMIT:
                return False
            self.entries[key] = (start, count + 1)
            return True


class PreferenceIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    view: Literal['chat', 'terminal']
    expectedRevision: int = Field(ge=0, strict=True)


def router(ctx):
    api = APIRouter(default_response_class=PrivateJSONResponse)
    connections = Connections()
    rpc_limits = RpcRateLimit()
    pages = history.History(Cipher(ctx.settings.data_dir / 'secrets/session_relay_cursor.key').key(create=True))

    def resolve(request, who):
        # Reuse authentication within this synchronous transaction; no await.
        actor = authority.authenticated_actor(who,
            cookie_authenticated=getattr(request.state, 'cookie_authenticated', False),
            native=getattr(request.state, 'edge_native', False),
            device=getattr(request.state, 'edge_fingerprint', None))
        return who, actor

    def snapshot_value(db, row, actor, attachment_id=None):
        value = p.to_wire(history.snapshot(db, row, actor))
        from ..session_relay.native_prompt import current
        prompt = current(db, row.session_id)
        if prompt is not None:
            value['nativePrompt'] = prompt
        from ..session_relay.exclusive_public import presentation
        l1=presentation(ctx,db,row,actor)
        if l1 is not None:value['l1']=l1
        from ..session_relay.retained_inputs import snapshot as retained_inputs
        retained=retained_inputs(ctx,row,actor)
        if retained is not None:
            value['retainedInputs']=retained
        from ..models import RelayDecision
        outcomes=db.scalars(select(RelayDecision).where(RelayDecision.session_id==row.session_id,
            RelayDecision.run_id==row.current_run_id).order_by(RelayDecision.id).limit(16)).all()
        value['decisionOutcomes']=[record.decision for record in outcomes
            if record.decision.get('status') in ('answered','denied','cancelled','expired','invalidated')]
        from ..session_relay.writers import public_ready
        if value.get('writer'):
            value['writer']['available'] = public_ready(ctx, row)
            from ..models import RelayOwnership
            owner = db.get(RelayOwnership, row.session_id)
            bound = owner.authority.get('writerAttachment') if owner else None
            value['writer']['mine'] &= bool(bound) and bound == attachment_id
        from ..session_relay.claude_observer import presentation
        value.update(presentation(ctx, db, row, actor, attachment_id))
        if row.active_operation_id and not row.sealed:
            value['activeTurnId'] = row.active_operation_id
        if row.sealed:
            native = db.get(SessionRun, row.current_run_id)
            reasons = {'native_exit_normal': 'normal', 'native_exit_abnormal': 'abnormal', 'native_exit_unknown': 'unknown'}
            value['exitOutcome'] = reasons.get(native.end_reason, 'unknown') if native else 'unknown'
            value['finalEventsDrained'] = history.final_events_drained(db, row)
        value['terminalMediated'] = False  # all native/Host/R1 proofs still required
        return value

    def read(request, who, acc_id, work):
        try:
            with ctx.db() as db:
                row, grant = authority.current(ctx, db, lambda: resolve(request, who), acc_id)
                session = ctx.session_for(db, who, acc_id)
                mask = ctx.supervisor.vault.redactor(db, session.vault_keys or []).text
                scope = hashlib.sha256(p.canonical_json([p.to_wire(grant.actor),
                                      getattr(request.state, 'session_hash', None)])).hexdigest()
                return history.public_actors(work(db, row, grant.actor, scope, mask))
        except p.RelayError as error:
            return PrivateJSONResponse({'error': error.to_wire()}, status_code=404 if
                                       error.code is p.ErrorCode.NOT_FOUND_OR_DENIED else 409)

    @api.get('/api/sessions/{acc_id}/relay')
    def snapshot(request: Request, acc_id: str, who=Depends(ctx.principal)):
        return read(request, who, acc_id, lambda db, row, actor, scope, mask:
                    history.safe_display(snapshot_value(db, row, actor), mask))

    @api.get('/api/sessions/{acc_id}/relay/events')
    def events(request: Request, acc_id: str, cursor: str | None = Query(None, max_length=2048),
               limit: int = Query(100, ge=1, le=200),
               runId: int | None = Query(None, ge=1, le=(1 << 63) - 1), who=Depends(ctx.principal)):
        return read(request, who, acc_id, lambda db, row, actor, scope, mask:
                    pages.page(db, row, scope, actor=actor, cursor=cursor, limit=limit, mask=mask, run_id=runId))

    @api.get('/api/sessions/{acc_id}/relay/runs')
    def runs(request: Request, acc_id: str, who=Depends(ctx.principal)):
        return read(request, who, acc_id, lambda db, row, actor, scope, mask: history.retained_runs(db, row))

    @api.get('/api/sessions/{acc_id}/relay/operations/{operation_id}')
    async def operation(request: Request, acc_id: str, operation_id: str, who=Depends(ctx.principal)):
        def stored():
            return read(request, ctx.principal(request), acc_id, lambda db, row, actor, scope, mask:
                store.operation(db, acc_id, operation_id, actor).receipt)
        value = await run_in_threadpool(stored)
        if not isinstance(value, dict):
            return value
        if not enabled(ctx.settings, 'session_relay'):
            return value  # authenticated stored history only; no broker polling/recovery
        runtime = getattr(ctx, 'relay_runtime', None)
        if value.get('state') == 'uncertain' and value.get('method') == 'turn.interrupt' and value.get('actor', {}).get('kind') == 'person' and runtime:
            from ..session_relay.runtime import RequestResolver
            await runtime.observe_operation(RequestResolver(ctx, request), acc_id, operation_id)
            return await run_in_threadpool(stored)
        if value.get('method') == 'decision.reply' and value.get('state') in ('submitting', 'uncertain'):
            def eligible():
                return read(request, ctx.principal(request), acc_id, lambda db, row, actor, scope, mask:
                    db.get(Session, acc_id).provider == 'claude')
            if await run_in_threadpool(eligible) is True:
                from ..session_relay.runtime import RequestResolver, manager
                await manager(ctx).recover_application(RequestResolver(ctx, request), acc_id, operation_id)
                return await run_in_threadpool(stored)
        return value

    @api.get('/api/sessions/{acc_id}/relay/terminal')
    async def terminal_read(request: Request, acc_id: str,
                            expectedRunId: str = Query(..., max_length=128),
                            attachmentId: str = Query(..., max_length=128)):
        # HTTP observer binding, not a new JSON-RPC operation/capability. Keep
        # the design's exact operation allowlist and input negotiation unchanged.
        from types import SimpleNamespace

        from ..session_relay.runtime import RequestResolver, manager
        def attached(db, actor):
            row = store.get(db, acc_id)
            scope = hashlib.sha256(p.canonical_json([p.to_wire(actor),
                getattr(request.state, 'session_hash', None), row.enrollment, row.history_epoch,
                row.current_run_id])).hexdigest()
            connections.use(request.headers.get('x-acc-relay-connection'), scope,
                SimpleNamespace(method='session.snapshot', params={'attachmentId': attachmentId}))
        current = RequestResolver(ctx, request, attached, attachment_id=attachmentId)
        def authorize():
            who = ctx.principal(request)
            with ctx.db() as db:
                ctx.session_for(db, who, acc_id)
                require(ctx.settings, 'session_relay')
                row, _ = authority.current(ctx, db, current, acc_id)
                return row.sealed or str(row.current_run_id) != expectedRunId
        try:
            retained = await run_in_threadpool(authorize)
            if retained:
                from ..session_relay.final_screen import read as read_final
                before, _ = await run_in_threadpool(read_final, ctx, current, acc_id, expectedRunId)
                after, result = await run_in_threadpool(read_final, ctx, current, acc_id, expectedRunId)
                if before != after:
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                return PrivateJSONResponse(result)
            result = await manager(ctx).read_terminal(current, acc_id, {'expectedRunId': expectedRunId})
            await run_in_threadpool(current)
            return PrivateJSONResponse(result)
        except p.RelayError as error:
            return PrivateJSONResponse({'error': error.to_wire()}, status_code=404 if
                error.code is p.ErrorCode.NOT_FOUND_OR_DENIED else 409)

    @api.get('/api/sessions/{acc_id}/relay/ownership')
    async def ownership_read(request: Request, acc_id: str,
                             expectedRunId: str = Query(..., max_length=128),
                             attachmentId: str = Query(..., max_length=128)):
        from types import SimpleNamespace

        from ..session_relay import claude_observer
        from ..session_relay.runtime import RequestResolver
        def attached(db, actor):
            row = store.get(db, acc_id)
            scope = hashlib.sha256(p.canonical_json([p.to_wire(actor),
                getattr(request.state, 'session_hash', None), row.enrollment, row.history_epoch,
                row.current_run_id])).hexdigest()
            connections.use(request.headers.get('x-acc-relay-connection'), scope,
                SimpleNamespace(method='session.snapshot', params={'attachmentId': attachmentId}))
        current = RequestResolver(ctx, request, attached, attachment_id=attachmentId)
        try:
            await run_in_threadpool(current)
            run = getattr(ctx, 'relay_runtime', None)
            run = run.runs.get(acc_id) if run else None
            if run is None or run.provider != 'claude':
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            return PrivateJSONResponse(await claude_observer.observe(run, current, expectedRunId))
        except p.RelayError as error:
            return PrivateJSONResponse({'error': error.to_wire()}, status_code=404 if
                error.code is p.ErrorCode.NOT_FOUND_OR_DENIED else 409)

    def offered(row):
        from ..session_relay.exclusive_public import public_ready as l1_ready
        from ..session_relay.exclusive_recovery import ready as recovery_ready
        from ..session_relay.native_decisions import public_ready as decisions_ready
        from ..session_relay.writers import public_ready
        return frozenset(({'queue'} if row.capabilities.get('text') == 'qualified' else set())
                         | ({'writer', 'terminal'} if public_ready(ctx, row) else set())
                         | ({'decisions'} if decisions_ready(ctx, row) else set())
                         | claude_public.offered(ctx, row)
                         | ({'switch'} if l1_ready(ctx,row) or recovery_ready(ctx,row) else set()))

    def rpc_work(request, acc_id, raw, recovered=False):
        # Body read has finished. Authenticate afresh after that await, including
        # CSRF/Origin, then keep all authorization and DB work synchronous.
        who = ctx.principal(request)
        command = None
        read_token = WRITE_INTENT.set(False)  # HTTP POST carries observer reads, not a global writer lock
        try:
            with ctx.db() as db:
                row, grant = authority.current(ctx, db, lambda: resolve(request, who), acc_id)
                mask = None  # Mutation frames carry no display payload to redact.
                scope = hashlib.sha256(p.canonical_json([p.to_wire(grant.actor),
                    getattr(request.state, 'session_hash', None), row.enrollment, row.history_epoch,
                    row.current_run_id])).hexdigest()
                command = p.parse_command(raw)
                params, method = command.params, command.method
                if method != 'initialize' and params['sessionId'] != acc_id:
                    raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
                if method == 'initialize':
                    owner_scope = hashlib.sha256(p.canonical_json([p.to_wire(grant.actor),
                        getattr(request.state, 'session_hash', None), acc_id])).hexdigest()
                    result = connections.initialize(scope, params, owner_scope=owner_scope,
                        offered=offered(row))
                else:
                    connection = connections.use(request.headers.get('x-acc-relay-connection'), scope, command)
                    if method == 'session.attach':
                        result = {'attachmentId': connection['attachment'], 'snapshot': snapshot_value(db, row, grant.actor, connection['attachment'])}
                    elif method == 'session.snapshot':
                        result = snapshot_value(db, row, grant.actor, connection['attachment'])
                    elif method == 'session.detach':
                        result = {'detached': True}
                    elif method == 'events.read':
                        session = ctx.session_for(db, who, acc_id)
                        mask = ctx.supervisor.vault.redactor(db, session.vault_keys or []).text
                        result = pages.page(db, row, scope, actor=grant.actor, mask=mask,
                                            cursor=params.get('cursor'), limit=params.get('limit', 100))
                    elif method == 'operation.get':
                        result = store.operation(db, acc_id, params['operationId'], grant.actor).receipt
                        if not recovered and result['method'] == 'decision.reply' and result['state'] in ('submitting', 'uncertain') and db.get(Session, acc_id).provider == 'claude':
                            return command, scope, connection['attachment'], 'application'
                        if result['method'] == 'turn.interrupt' and result['state'] == 'uncertain' and result.get('actor', {}).get('kind') == 'person' and getattr(ctx, 'relay_runtime', None):
                            return command, scope, connection['attachment']
                    else:
                        if method == 'turn.interrupt' or (method == 'operation.register' and params['method'] == 'turn.interrupt'):
                            from ..session_relay.interrupts import public_ready as interrupt_ready
                            if not interrupt_ready(ctx, row):
                                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
                        claude_switch = (claude_public.admit(ctx, row, command, connection['negotiated'])
                            if claude_public.supported(ctx, row) else False)
                        if not claude_switch:
                            p.admit(command, connection['negotiated'], {key: p.Support(value) for key, value in row.capabilities.items()})
                        if not claude_switch and method not in {'operation.register', 'turn.submit', 'operation.cancel', 'writer.take', 'control.claim', 'terminal.input', 'terminal.drain', 'decision.reply', 'turn.interrupt', 'switch.prepare', 'switch.commit', 'switch.cancel', 'switch.recover'}:
                            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
                        # Finish the observer transaction before any mutation or IO.
                        return command, scope, connection['attachment']
                if mask is None:
                    session = ctx.session_for(db, who, acc_id)
                    mask = ctx.supervisor.vault.redactor(db, session.vault_keys or []).text
                result = history.safe_display(history.public_actors(result), mask)
                return PrivateJSONResponse(json.loads(p.result_frame(command.request_id, result)))
        except (p.RelayError, p.RpcError) as error:
            return PrivateJSONResponse(json.loads(p.error_frame(error, command.request_id if command else None)))
        finally:
            WRITE_INTENT.reset(read_token)

    @api.post('/api/sessions/{acc_id}/relay')
    async def rpc(request: Request, acc_id: str):
        def authenticate():
            from ..session_relay.runtime import RequestResolver
            read_token = WRITE_INTENT.set(False)
            try:
                with ctx.db() as db:
                    who, actor = RequestResolver(ctx, request).authenticate_in_transaction(db)
                    ctx.session_for(db, who, acc_id)
                    if not rpc_limits.admit(acc_id, actor):
                        raise HTTPException(429, 'Too many relay requests. Try again shortly.',
                                            headers={'Retry-After': '1'})
            finally:
                WRITE_INTENT.reset(read_token)
        await run_in_threadpool(authenticate)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > p.MAX_FRAME_BYTES:
                raise HTTPException(413, 'Relay request is too large.')
        # Authentication/rate admission above is read-only and precedes syntax
        # parsing, the frame lock and every mutation. Reservation and post-IO
        # transactions still authenticate afresh; no authority crosses an await.
        fast_frame = None
        try:
            parsed = p.parse_command(bytes(raw)) if len(raw) <= 16 << 10 else None
            runtime = getattr(ctx, 'relay_runtime', None)
            run = runtime.runs.get(acc_id) if runtime else None
            if parsed and parsed.method == 'terminal.input' and run and run.provider == 'codex':
                fast_frame = parsed
        except (p.RelayError, p.RpcError):
            pass
        if fast_frame is not None:
            if fast_frame.params['sessionId'] != acc_id:
                # Preserve the ordinary auth-first refusal for a foreign URL.
                response = await run_in_threadpool(rpc_work, request, acc_id, bytes(raw))
            else:
                response = fast_frame, None, fast_frame.params['attachmentId']
        else:
            response = await run_in_threadpool(rpc_work, request, acc_id, bytes(raw))
        if not isinstance(response, tuple):
            return response
        command, _, attachment_id = response[:3]
        if len(response) == 4 and response[3] == 'application':
            from ..session_relay.runtime import RequestResolver, manager
            await manager(ctx).recover_application(RequestResolver(ctx, request), acc_id, command.params['operationId'])
            return await run_in_threadpool(rpc_work, request, acc_id, bytes(raw), True)
        from dataclasses import replace
        attachment_check = replace(command, params={'attachmentId': attachment_id})
        def attached(db, actor):
            row = db.get(RelaySession, acc_id)
            if row is None:
                raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
            scope = hashlib.sha256(p.canonical_json([p.to_wire(actor),
                getattr(request.state, 'session_hash', None), row.enrollment, row.history_epoch,
                row.current_run_id])).hexdigest()
            connection = connections.use(request.headers.get('x-acc-relay-connection'), scope, attachment_check)
            if fast_frame is not None:
                p.admit(command, connection['negotiated'], {key: p.Support(value) for key, value in row.capabilities.items()})
        from ..session_relay.runtime import RequestResolver
        current = RequestResolver(ctx, request, attached, attachment_id=attachment_id)
        from ..db import write_in_thread
        from ..session_relay.hub import Hub
        from ..session_relay.runtime import manager
        params = command.params
        try:
            switch_method = params.get('method') if command.method == 'operation.register' else command.method
            if command.method == 'operation.get':
                result = await ctx.relay_runtime.observe_operation(current, acc_id, params['operationId'])
            elif switch_method in claude_public.METHODS and claude_public.PUBLIC_CLAUDE_QUALIFIED:
                result = await claude_public.dispatch(ctx, current, acc_id, command)
            elif command.method == 'operation.register':
                runtime = getattr(ctx, 'relay_runtime', None)
                run = runtime.runs.get(acc_id) if runtime else None
                if (params['method'] == 'decision.reply' and run and run.provider == 'claude'
                        and claude_public.PUBLIC_CLAUDE_DECISIONS_QUALIFIED):
                    current = claude_public.DecisionResolver(current, acc_id)
                result = await write_in_thread(lambda: Hub(ctx).register(current, acc_id, params['expectedRunId'],
                    params['registrationKey'], params['method'], params['payloadDigest']))
            elif command.method == 'control.claim':
                result = await manager(ctx).claim_terminal(current, acc_id, params)
            elif command.method == 'terminal.input':
                result = await manager(ctx).input_terminal(current, acc_id, params)
            elif command.method == 'terminal.drain':
                result = await manager(ctx).drain_terminal(current, acc_id, params)
            elif command.method == 'writer.take':
                result = await manager(ctx).take_writer(current, acc_id, params)
            elif command.method == 'decision.reply':
                result = await manager(ctx).answer_decision(current, acc_id, params)
            elif command.method == 'turn.interrupt':
                result = await manager(ctx).interrupt(current, acc_id, params)
            elif command.method=='switch.recover':
                from ..session_relay.exclusive_recovery import recover
                result=await recover(manager(ctx).runs.get(acc_id),current,params)
            elif command.method in ('switch.prepare','switch.commit','switch.cancel'):
                from ..session_relay import exclusive_public
                from ..session_relay.exclusive_runtime import ExclusiveLaunch
                run=manager(ctx).runs.get(acc_id)
                if not isinstance(run,ExclusiveLaunch):raise p.RelayError(p.ErrorCode.UNSUPPORTED)
                action={'switch.prepare':exclusive_public.prepare,'switch.commit':exclusive_public.commit,
                    'switch.cancel':exclusive_public.cancel}[command.method]
                result=await action(run,current,params)
            elif command.method == 'operation.cancel':
                result = await manager(ctx).cancel(current, acc_id, params)
            else:
                result = await manager(ctx).submit(current, acc_id, params)
            if command.method in ('switch.commit','switch.recover') and result.get('phase')=='owned' and result.get('changed'):
                def finish_switch():
                    who=ctx.principal(request)
                    with ctx.db() as db:
                        row,grant=authority.current(ctx,db,lambda:resolve(request,who),acc_id,
                            expected_run=result['runId'])
                        from ..models import RelayOwnership
                        owner=db.get(RelayOwnership,acc_id)
                        op=store.operation(db,acc_id,params['operationId'],grant.actor)
                        if (not owner or owner.phase!='owned' or owner.commit_operation_id!=op.id
                                or op.receipt['state']!='accepted'):
                            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                        scope=hashlib.sha256(p.canonical_json([p.to_wire(grant.actor),
                            getattr(request.state,'session_hash',None),acc_id])).hexdigest()
                        connections.finish(request.headers.get('x-acc-relay-connection'),scope,attachment_id)
                await run_in_threadpool(finish_switch)
            elif command.method not in ('switch.commit', 'terminal.input'):
                await run_in_threadpool(current)
            return PrivateJSONResponse(json.loads(p.result_frame(command.request_id, history.public_actors(p.to_wire(result)))))
        except p.RelayError as error:
            return PrivateJSONResponse(json.loads(p.error_frame(error, command.request_id)))

    def person(request, who):
        _, actor = resolve(request, who)
        if actor.kind is not p.ActorKind.PERSON or who.user_id is None:
            raise HTTPException(403, 'A person sign-in is required.')
        return who

    @api.get('/api/session-relay/preference')
    def preference(request: Request, who=Depends(ctx.principal)):
        person(request, who)
        with ctx.db() as db:
            row = db.get(RelayPreference, who.username)
            return {'view': row.view if row else 'terminal', 'revision': row.revision if row else 0}

    @api.put('/api/session-relay/preference')
    def set_preference(request: Request, body: PreferenceIn, who=Depends(ctx.principal)):
        person(request, who)
        return save_preference(who, body)

    @write_intent
    def save_preference(who, body):
        with ctx.db() as db:
            # Lock the existing parent too: first-write CAS must serialize on PG.
            user = db.scalar(select(User).where(User.id == who.user_id).with_for_update())
            if not user or user.username != who.username:
                raise HTTPException(403, 'A current person sign-in is required.')
            row = db.get(RelayPreference, who.username)
            if body.expectedRevision != (row.revision if row else 0):
                raise HTTPException(409, 'The view preference changed. Reload and try again.')
            if row is None:
                row = RelayPreference(owner=who.username, view=body.view, revision=1)
                db.add(row)
            else:
                row.view, row.revision = body.view, row.revision + 1
            db.commit()
            return {'view': row.view, 'revision': row.revision}

    return api
