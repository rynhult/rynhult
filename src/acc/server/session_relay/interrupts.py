"""Exact-target public interrupts over the existing durable grant path.

An accepted receipt means requested, not confirmed cancellation. Native completion
is observed separately. No observer/alternate view can acquire cancellation power.
"""
from dataclasses import dataclass

from acc.session_relay import protocol as p

from ..db import write_in_thread, write_intent
from ..models import AuditLog, RelayOperation, RelayOwnership
from . import store, writers

PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED = False


@dataclass(frozen=True)
class Target:
    operation_id: str
    control_revision: int
    view: str


def public_ready(ctx, row):
    runtime = getattr(ctx, 'relay_runtime', None)
    run = runtime.runs.get(row.session_id) if runtime else None
    if run is not None and run.provider == 'claude':
        from .claude_public import ready
        return (PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED is True and ready(ctx, row)
                and row.capabilities.get('interrupt') == 'qualified')
    return (writers.public_ready(ctx, row) and row.capabilities.get('interrupt') == 'qualified'
            and run is not None and run.provider == 'codex')


def check_grant(run, db, grant):
    row, current = run.check_run(db, resolve=grant.resolve)
    from .release_scope import supported_actor
    supported_actor(current.actor)
    target = grant.native_interrupt
    owner = db.get(RelayOwnership, run.session_id)
    attachment = getattr(grant.resolve, 'attachment_id', None)
    if (not public_ready(run.ctx, row) or not isinstance(target, Target)
            or current.actor.kind is not p.ActorKind.PERSON or not attachment):
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    if run.provider == 'claude':
        origin = run.turn_origins.get(target.operation_id)
        active = db.get(RelayOperation, target.operation_id)
        if (not owner or owner.mode != 'exclusive_idle' or owner.phase != 'owned'
                or owner.view != target.view or target.view != 'chat'
                or owner.authority.get('actor') != p.to_wire(current.actor)
                or row.control_revision != target.control_revision
                or not origin or origin['actor'] != current.actor or origin['runId'] != str(run.run_id)
                or getattr(origin['resolve'], 'attachment_id', None) != attachment
                or row.active_operation_id != target.operation_id or active is None
                or active.session_id != run.session_id or active.run_id != run.run_id
                or active.receipt['method'] != 'turn.submit' or active.receipt['state'] != 'uncertain'
                or active.receipt['actor'] != p.to_wire(current.actor)):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return
    if (not owner or owner.mode != 'exclusive_writer' or owner.phase != 'owned'
            or target.view not in ('chat', 'terminal') or owner.view != target.view
            or row.controller != p.to_wire(current.actor)
            or owner.authority.get('writerAttachment') != attachment
            or row.control_revision != target.control_revision):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    active = db.get(RelayOperation, target.operation_id)
    if (row.active_operation_id != target.operation_id or active is None
            or active.session_id != run.session_id or active.run_id != run.run_id
            or active.receipt['method'] != 'turn.submit' or active.receipt['state'] != 'accepted'
            or active.receipt['actor'] != p.to_wire(current.actor)):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)


@write_intent
def issue(run, resolve, params):
    with run.ctx.db() as db:
        row, current = run.check_run(db, resolve=resolve)
        if not public_ready(run.ctx, row):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        operation = store.operation(db, run.session_id, params['operationId'], current.actor)
        if operation.receipt['method'] != 'turn.interrupt':
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        owner = db.get(RelayOwnership, run.session_id)
        view = owner.view if owner else None
    token = run.issue(resolve, params['operationId'], {key: value for key, value in params.items() if key != 'operationId'})
    if isinstance(token, str):
        grant = run.grants[token]
        revision = params.get('expectedControlRevision')
        grant.native_interrupt = Target(params['turnId'], revision, view)
        try:
            if type(revision) is not int:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            with run.ctx.db() as db:
                check_grant(run, db, grant)
        except BaseException:
            run.fail_queued(token)
            run.grants.pop(token, None)
            raise
    return token


@write_intent
def claim(run, token):
    run.resolve_grant(token)
    grant = run.grants[token]
    with run.ctx.db() as db:
        check_grant(run, db, grant)
        row = store.lock(db, run.session_id)
        record = store.operation(db, run.session_id, grant.operation.operation_id, grant.operation.actor)
        if record.receipt['state'] != 'queued' or record.deadline <= store.utcnow() or not record.confirmed:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        # Never replace active_operation_id: this receipt requests cancellation
        # of that turn, and cannot become another active turn or FIFO entry.
        receipt = store.transition(db, row, record, p.ReceiptState.SUBMITTING)
        db.add(AuditLog(actor=grant.operation.actor.principal, action='relay.turn.interrupt', target=run.session_id,
            detail={'operation_id': record.id, 'run_id': run.run_id, 'control_revision': row.control_revision}))
        db.commit()
        return receipt


@write_intent
def abandon(run, token):
    run.fail_queued(token)
    grant = run.grants.get(token)
    if grant is None:
        return
    with run.ctx.db() as db:
        record = store.operation(db, run.session_id, grant.operation.operation_id, grant.operation.actor)
        submitted = record.receipt['state'] == 'submitting'
    if submitted:
        run.settle(token, {'outcome': 'unknown'})


async def interrupt(run, resolve, params):
    async with run.submit_lock:
        token = await write_in_thread(lambda: issue(run, resolve, params))
        if not isinstance(token, str):
            return token  # observe the existing receipt; never resend
        try:
            await run.peer.call('stage', {'grant': token})
            await write_in_thread(lambda: claim(run, token))
            return await run.manager.dispatch(run, token)
        except BaseException:
            # The claim's worker transaction can commit after cancellation.
            # fail_queued only fails a definitely queued receipt; any submitted
            # operation stays uncertain and may be observed, never retried.
            await write_in_thread(lambda: abandon(run, token))
            raise
        finally:
            if token in run.grants:
                await run.manager.discard(run, token)
