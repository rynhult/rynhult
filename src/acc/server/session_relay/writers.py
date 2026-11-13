"""Person-initiated Codex writer changes over an existing private app-server.

Internal reviewed entry: public native take-over stays unavailable until its
retirement proof and TerminalHost transport qualify. Reads never call take().
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from acc.session_relay import protocol as p

from ..db import write_in_thread, write_intent
from ..models import RelayOperation, RelayOwnership, RelayRun
from . import store
from .hub import Hub

# No automatic promotion from fixture passes, database capabilities or CLI pins.
# Every native/placement/R1 release proof must be approved before this can change.
PUBLIC_WRITER_QUALIFIED = False
TAKE_METHODS = frozenset({'control.take', 'writer.take'})


class ProcessingLost(p.RelayError):
    def __init__(self):
        super().__init__(p.ErrorCode.INPUT_CONFLICT)


def public_ready(ctx, row):
    from ..admin_features import enabled
    return (PUBLIC_WRITER_QUALIFIED is True and enabled(ctx.settings, 'session_relay')
            and not row.sealed and row.current_run_id is not None
            and all(row.capabilities.get(key) == 'qualified' for key in ('text', 'terminal.input')))


@write_intent
def prepare(run, token):
    value = run.resolve_grant(token)
    grant = run.grants[token]
    op = grant.operation
    if op.method not in TAKE_METHODS or op.actor.kind is not p.ActorKind.PERSON:
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    reason = op.params.get('reason')
    if not isinstance(reason, str) or not 0 < len(reason.strip()) <= p.MAX_REASON_CHARS or any(c in reason for c in ('\n', '\r')):
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    target = op.params.get('view')
    if target not in ('chat', 'terminal'):
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    with run.ctx.db() as db:
        row, current = run.check_run(db, resolve=grant.resolve)
        if op.method == 'writer.take' and (not public_ready(run.ctx, row)
                or getattr(grant.resolve, 'attachment_id', None) != op.params.get('attachmentId')):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        owner = db.get(RelayOwnership, run.session_id)
        if not owner or owner.mode != 'exclusive_writer' or owner.phase != 'owned':
            raise p.RelayError(p.ErrorCode.BUSY)
        if type(op.params.get('expectedRevision')) is not int or row.control_revision != op.params.get('expectedRevision'):
            raise p.RelayError(p.ErrorCode.REVISION_CONFLICT)
        capability = 'text' if target == 'chat' else 'terminal.input'
        if row.capabilities.get(capability) != 'qualified':
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        stream = run.terminal_grants.record
        # Only an unsubmitted prefix may be retained; an Enter awaiting a
        # conclusive processing receipt still prevents retirement. The broker
        # separately proves permanent old-client fencing and fresh composition.
        if stream and (stream.pending or (stream.entered_sequence is not None
                and stream.processed < stream.entered_sequence)):
            # A finished admitted native turn without its processing ACK cannot
            # establish a reusable input boundary. Preserve that original receipt
            # but fence this run rather than leave the ambiguous stream live.
            native = db.get(RelayOperation, stream.native.operation_id) if stream.native and stream.native.operation_id else None
            if (not stream.pending and row.active_operation_id is None and native is not None
                    and native.receipt['method'] == 'turn.submit' and native.receipt['state'] == 'accepted'):
                raise ProcessingLost()
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        pending(db, row, op.operation_id)
        record = db.get(RelayOperation, op.operation_id)
        store.transition(db, row, record, p.ReceiptState.SUBMITTING)
        owner.previous_actor, owner.authority = row.controller, current.stamp()
        owner.phase, owner.plan_id, owner.target = 'draining', op.operation_id, target
        owner.digest = op.payload_digest
        owner.expires_at = record.deadline
        store.append(db, row, 'control.changed', {'state': 'draining', 'target': target})
        db.commit()
    return value


def pending(db, row, identifier, *, decisions=None):
    if row.active_operation_id:
        raise p.RelayError(p.ErrorCode.BUSY)
    records = db.scalars(select(RelayOperation).where(RelayOperation.session_id == row.session_id))
    if any(op.id != identifier and op.receipt['state'] in ('queued', 'submitting', 'uncertain') for op in records):
        raise p.RelayError(p.ErrorCode.BUSY)
    from ..models import RelayDecision
    if decisions is None:
        decisions = db.scalars(select(RelayDecision).where(RelayDecision.session_id == row.session_id))
    if any(item.decision['status'] in ('pending', 'reserved', 'outcome_unknown') for item in decisions):
        raise p.RelayError(p.ErrorCode.BUSY)


@write_intent
def finish(run, token, result):
    grant = run.grants[token]
    with run.ctx.db() as db:
        row, current = run.check_run(db, resolve=grant.resolve, action='switch')
        owner = db.get(RelayOwnership, run.session_id)
        op = db.get(RelayOperation, grant.operation.operation_id)
        if (owner.phase != 'draining' or owner.plan_id != op.id or owner.authority != current.stamp()
                or owner.expires_at <= store.utcnow() or result.get('view') != owner.target
                or result.get('epoch') != row.control_revision + 1 or result.get('runId') != str(run.run_id)
                or result.get('executionIdentity') != db.get(RelayRun, run.run_id).identity['processIdentity']):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        pending(db, row, op.id)
        if grant.operation.method == 'writer.take' and not public_ready(run.ctx, row):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        previous = row.controller
        owner.view, owner.phase = owner.target, 'owned'
        if grant.operation.method == 'writer.take':
            owner.authority = {**current.stamp(), 'writerAttachment': grant.operation.params['attachmentId']}
        row.controller, row.control_revision = p.to_wire(current.actor), result['epoch']
        store.append(db, row, 'control.changed', {'holder': row.controller, 'view': owner.view, 'revision': row.control_revision})
        receipt = store.settle(db, row, op, accepted=True,
            evidence=p.Evidence(p.EvidenceKind.HUB_COMMIT, op.id, str(op.run_id)))
        Hub(run.ctx)._audit(db, current.actor, run.session_id, grant.operation.method, previous=previous,
                           view=owner.view, control_revision=row.control_revision)
        db.commit()
        run.terminal_grants.invalidate()
        return receipt


@write_intent
def refuse(run, token, error, prepared):
    grant = run.grants[token]
    with run.ctx.db() as db:
        row = store.lock(db, run.session_id)
        record = db.get(RelayOperation, grant.operation.operation_id)
        if not prepared and record is not None and record.receipt['state'] == 'queued':
            reason = p.FailureReason.UNSUPPORTED if isinstance(error, p.RelayError) and error.code is p.ErrorCode.UNSUPPORTED else p.FailureReason.POLICY_REFUSED
            store.transition(db, row, record, p.ReceiptState.FAILED, reason=reason)
        Hub(run.ctx)._audit(db, grant.operation.actor, run.session_id, 'refused', method=grant.operation.method,
                           code=error.code.value if isinstance(error, p.RelayError) else 'OUTCOME_UNKNOWN')
        db.commit()


async def take(run, resolve, params):
    async with run.submit_lock:
        token = await write_in_thread(lambda: run.issue(resolve, params['operationId'],
            {key: value for key, value in params.items() if key != 'operationId'}))
        if not isinstance(token, str):
            return token
        prepared = False
        try:
            await write_in_thread(lambda: prepare(run, token))
            prepared = True
            result = await run.peer.call('writer.take', {'grant': token})
            receipt = await write_in_thread(lambda: finish(run, token, result))
            run.writer_resolve, run.writer_actor = resolve, run.grants[token].operation.actor
            return receipt
        except BaseException as error:
            try:
                await write_in_thread(lambda failure=error: refuse(run, token, failure, prepared))
            finally:
                # Audit/DB loss must not leave an ambiguous broker lease live.
                if (prepared or isinstance(error, asyncio.CancelledError)
                        or isinstance(error, ProcessingLost)):
                    try:
                        await write_in_thread(run.fence_lost)
                    finally:
                        run.peer.invalidate()
            if isinstance(error, (ConnectionError, OSError, TimeoutError)):
                raise p.RelayError(p.ErrorCode.OFFLINE) from None
            raise
        finally:
            run.grants.pop(token, None)
