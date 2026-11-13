"""Authority-bound coordinator transitions. M1 supplies no runtime dispatch transport.

Every entry opens a short fresh transaction; asynchronous preparation finishes
before reauthorization/claim. M2 must bind a successful claim to B's one-operation
broker grant, journal and final admission barrier. Claiming is never acceptance.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select

from acc.session_relay import protocol as p

from ..db import write_in_thread, write_intent
from ..models import AuditLog, RelayDecision, RelayOperation, RelayOwnership
from . import authority, store


class Hub:
    def __init__(self, ctx):
        self.ctx = ctx

    def _audit(self, db, actor, session_id, action, **detail):
        db.add(AuditLog(actor=actor.principal, action='relay.' + action, target=session_id,
                        detail={'channel': actor.channel.value, **detail}))

    @write_intent
    def _write(self, resolve, session_id, work, *, action='control', expected_run=None, attached=False, decision_id=None, queue_pass=False):
        actor = None
        try:
            _, actor = resolve()
            with self.ctx.db() as db:
                decision = None
                # Authorize resource before loading/validating decision metadata.
                row, grant = authority.current(self.ctx, db, resolve, session_id, action=action if action != 'decide' else 'control',
                                                expected_run=expected_run)
                actor = grant.actor
                if action == 'decide':
                    record = db.get(RelayDecision, (session_id, decision_id))
                    if record is None or record.session_id != session_id:
                        raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
                    decision = store.decision_from(record.decision)
                    row, grant = authority.current(self.ctx, db, resolve, session_id, action='decide',
                                                    expected_run=expected_run, attached=attached, decision=decision)
                if queue_pass:
                    store.expire_queued(db, row)
                    db.flush()
                    try:
                        # Keep the all-person expiry sweep even when this caller's
                        # requested action refuses. Only that action rolls back.
                        with db.begin_nested():
                            result = work(db, row, grant)
                    except (p.RelayError, HTTPException):
                        db.commit()
                        raise
                else:
                    result = work(db, row, grant)
                self._audit(db, grant.actor, session_id, action, run_id=grant.run_id, authority_epoch=grant.epoch,
                            operation_id=getattr(result, 'operation_id', None),
                            decision_id=getattr(result, 'decision_id', None))
                db.commit()
                return result
        except (p.RelayError, HTTPException) as error:
            # Separate transaction: a refused attempt must survive rollback. No
            # payload, native ID or foreign record is included in audit details.
            if actor:
                with self.ctx.db() as db:
                    self._audit(db, actor, session_id, 'refused', code=error.code.value if isinstance(error, p.RelayError) else 'NOT_FOUND_OR_DENIED', method=action)
                    db.commit()
            raise

    def register(self, resolve, session_id, expected_run, key, method, digest):
        if isinstance(key, str) and key.startswith('stock-frame-'):
            from .stock_approval_frames import Resolver as StockResolver
            if not isinstance(resolve, StockResolver):
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        def work(db, row, grant):
            if grant.delivery_scope is not None:
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)  # atomic service enqueue is the only registrar
            from ..admin_features import enabled
            if not enabled(self.ctx.settings, 'session_relay'):
                # Registration only carries a digest. Reconstruct the bounded
                # set of currently valid deny payloads from authoritative records;
                # a label, caller assertion or an allow digest grants nothing.
                denied = False
                if method == 'decision.reply':
                    records = db.scalars(select(RelayDecision).where(RelayDecision.session_id == session_id,
                                             RelayDecision.run_id == row.current_run_id,
                                             RelayDecision.decision['status'].as_string() == 'pending'))
                    for record in records:
                        decision = store.decision_from(record.decision)
                        if (decision.status is not p.DecisionStatus.PENDING or
                                decision.expires_at_ms <= int(store.utcnow().timestamp() * 1000) or
                                decision.policy_revision != grant.policy_revision or
                                decision.kind is p.DecisionKind.PRIVILEGED):
                            continue
                        for choice in decision.choices:
                            if choice.effect is p.ChoiceEffect.DENY:
                                expected = p.operation_digest('decision.reply', {
                                    'sessionId': session_id, 'expectedRunId': str(grant.run_id),
                                    'decisionId': decision.decision_id, 'choiceId': choice.id,
                                    'expectedRevision': decision.revision, 'actionDigest': decision.action_digest})
                                denied |= digest == expected
                if not denied:
                    raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            # Answer repeats current authority, effect and CAS checks.
            if method=='switch.recover':
                from .exclusive_recovery import ready
                owner=db.get(RelayOwnership,session_id)
                if not ready(self.ctx,row) or not owner or owner.phase!='recovery_required':
                    raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            return store.register(db, row, grant.actor, key, method, digest, grant.stamp(),recovery=method=='switch.recover')
        return self._write(resolve, session_id, work, expected_run=expected_run,
            action='recover' if method=='switch.recover' else 'switch' if method in ('switch.prepare','switch.commit','switch.cancel') else 'operation.register')

    def queue(self, resolve, session_id, identifier, *, expected_run, digest, if_busy='reject'):
        def work(db, row, grant):
            from ..admin_features import enabled
            if not enabled(self.ctx.settings, 'session_relay'):
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            op = store.operation(db, session_id, identifier, grant.actor)
            if grant.delivery_scope is not None:
                from .delivery import check_operation
                check_operation(grant.delivery_scope, op)
            receipt = store.receipt_from(op.receipt)
            p.check_registered(receipt, actor=grant.actor, session_id=session_id, run_id=str(grant.run_id),
                               method='turn.submit', payload_digest=digest)
            if receipt.actor != grant.actor:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            if receipt.state is not p.ReceiptState.QUEUED or op.queue_seq is not None:
                return receipt  # same ID observes its receipt, never resubmits
            if op.deadline <= store.utcnow():
                return store.transition(db, row, op, p.ReceiptState.FAILED, reason=p.FailureReason.EXPIRED)
            queued = [r for r in db.scalars(select(RelayOperation).where(RelayOperation.session_id == session_id))
                      if r.queue_seq is not None and r.receipt['state'] == 'queued']
            busy = row.active_operation_id is not None or any(entry.confirmed for entry in queued)
            if if_busy not in ('reject', 'queue'):
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            if busy and if_busy == 'reject':
                raise p.RelayError(p.ErrorCode.BUSY)
            if len(queued) >= 8 or any(r.actor_key == op.actor_key for r in queued):
                raise p.RelayError(p.ErrorCode.LIMIT)
            authority.recheck(grant, authority.Authority(store.actor_from(op.authority['actor']), op.authority['runId'],
                              op.authority['epoch'], op.authority['bindingId'], op.authority['policyRevision'], op.authority.get('delivery')))
            op.queue_seq = row.event_seq + 1
            store.append(db, row, 'queue.changed', {'receipt': op.receipt}, operation_id=op.id)
            return receipt
        return self._write(resolve, session_id, work, expected_run=expected_run, action='turn.submit', queue_pass=True)

    def claim(self, resolve, session_id, identifier, *, previous=None):
        def work(db, row, grant):
            from ..admin_features import enabled
            if not enabled(self.ctx.settings, 'session_relay'):
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            if previous:
                authority.recheck(previous, grant)
            op = store.operation(db, session_id, identifier, grant.actor)
            if grant.delivery_scope is not None:
                from .delivery import check_operation
                check_operation(grant.delivery_scope, op)
            receipt = store.receipt_from(op.receipt)
            if receipt.state is not p.ReceiptState.QUEUED:
                return receipt
            if op.deadline <= store.utcnow():
                return store.transition(db, row, op, p.ReceiptState.FAILED, reason=p.FailureReason.EXPIRED)
            if op.authority != grant.stamp():
                return store.transition(db, row, op, p.ReceiptState.FAILED, reason=p.FailureReason.AUTHORITY_ENDED)
            if not op.confirmed or op.configuration_revision != row.configuration_revision:
                raise p.RelayError(p.ErrorCode.REVISION_CONFLICT)
            pending = [r for r in db.scalars(select(RelayOperation).where(RelayOperation.session_id == session_id))
                       if r.queue_seq is not None and r.confirmed and r.receipt['state'] == 'queued']
            if op.queue_seq is None or row.active_operation_id or min(pending, key=lambda r: r.queue_seq).id != op.id:
                raise p.RelayError(p.ErrorCode.BUSY)
            row.active_operation_id = op.id
            return store.transition(db, row, op, p.ReceiptState.SUBMITTING)
        return self._write(resolve, session_id, work, queue_pass=True)

    async def prepare_claim(self, resolve, session_id, identifier, prepare):
        # Do not retain a DB session/snapshot across the awaited preparation.
        with self.ctx.db() as db:
            _, previous = authority.current(self.ctx, db, resolve, session_id, action='control')
        await prepare()
        return await write_in_thread(lambda: self.claim(resolve, session_id, identifier, previous=previous))

    def cancel(self, resolve, session_id, identifier, revision):
        def work(db, row, grant):
            op = store.operation(db, session_id, identifier, grant.actor)
            if grant.delivery_scope is not None:
                from .delivery import check_operation
                check_operation(grant.delivery_scope, op)
            receipt = store.receipt_from(op.receipt)
            if receipt.revision != revision or receipt.state is not p.ReceiptState.QUEUED:
                raise p.RelayError(p.ErrorCode.REVISION_CONFLICT)
            return store.transition(db, row, op, p.ReceiptState.FAILED, reason=p.FailureReason.CANCELLED)
        return self._write(resolve, session_id, work, action='operation.cancel')

    def disconnect(self, resolve, session_id):
        def work(db, row, grant):
            for op in db.scalars(select(RelayOperation).where(RelayOperation.session_id == session_id,
                                                             RelayOperation.actor_key == store.actor_key(grant.actor))):
                if op.receipt['state'] == 'queued':
                    op.confirmed = False
        return self._write(resolve, session_id, work, action='disconnect')

    def reconfirm(self, resolve, session_id, identifier):
        def work(db, row, grant):
            op = store.operation(db, session_id, identifier, grant.actor)
            if op.authority != grant.stamp() or op.receipt['state'] != 'queued':
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            op.confirmed = True
        return self._write(resolve, session_id, work)

    def answer(self, resolve, session_id, decision_id, *, expected_run, attached, choice_id,
               revision, digest, operation_id):
        def work(db, row, grant):
            from ..admin_features import enabled
            if not enabled(self.ctx.settings, 'session_relay'):
                decision = store.decision_from(db.get(RelayDecision, (session_id, decision_id)).decision)
                if not any(choice.id == choice_id and choice.effect is p.ChoiceEffect.DENY for choice in decision.choices):
                    raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            op = store.operation(db, session_id, operation_id, grant.actor)
            receipt = store.receipt_from(op.receipt)
            payload_digest = p.operation_digest('decision.reply', {
                'sessionId': session_id, 'expectedRunId': str(expected_run), 'decisionId': decision_id,
                'choiceId': choice_id, 'expectedRevision': revision, 'actionDigest': digest})
            p.check_registered(receipt, actor=grant.actor, session_id=session_id, run_id=str(grant.run_id),
                               method='decision.reply', payload_digest=payload_digest)
            if op.authority != grant.stamp() or op.deadline <= store.utcnow():
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if receipt.state is not p.ReceiptState.QUEUED:
                raise p.RelayError(p.ErrorCode.DECISION_RESOLVED)
            answer = store.reserve_decision(db, row, decision_id, grant.actor, choice_id=choice_id,
                                            expected_revision=revision, action_digest=digest)
            store.transition(db, row, op, p.ReceiptState.SUBMITTING)
            return answer
        return self._write(resolve, session_id, work, action='decide', expected_run=expected_run,
                           attached=attached, decision_id=decision_id)

    def take(self, resolve, session_id, *, revision, reason, drain_settled):
        """Reserve controller metadata without touching a terminal draft or minting a grant.

        M2 supplies a verified drained/correlated boundary from TerminalHost. No
        API can call this until that proof and native attribution are qualified.
        """
        def work(db, row, grant):
            from ..models import RelayOwnership
            owner = db.get(RelayOwnership, session_id)
            if owner and owner.mode == 'exclusive_writer':
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            from ..admin_features import enabled
            if not enabled(self.ctx.settings, 'session_relay'):
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            if not drain_settled:
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            if revision != row.control_revision:
                raise p.RelayError(p.ErrorCode.REVISION_CONFLICT)
            if not isinstance(reason, str) or not 0 < len(reason.strip()) <= p.MAX_REASON_CHARS:
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            previous = row.controller
            row.controller = p.to_wire(grant.actor)
            row.control_revision += 1
            store.append(db, row, 'control.changed', {'holder': row.controller, 'revision': row.control_revision})
            safe_reason = '[withheld]'
            if getattr(self.ctx, 'supervisor', None):
                from ..models import Session
                session = db.get(Session, session_id)
                safe_reason = self.ctx.supervisor.vault.redactor(db, session.vault_keys or []).text(reason)[:p.MAX_REASON_CHARS]
            self._audit(db, grant.actor, session_id, 'control.take', previous=previous,
                        control_revision=row.control_revision, reason=safe_reason)
            return row.control_revision
        return self._write(resolve, session_id, work)


@write_intent
def seal_for_stop(ctx, session_id):
    from ..models import RelaySession
    with ctx.db() as db:
        if db.get(RelaySession, session_id):
            store.seal(db, store.lock(db, session_id))
            db.commit()


def refuse_legacy(db, session_id):
    from ..models import RelaySession
    from ..supervisor import LaunchRefused
    if db.get(RelaySession, session_id):
        raise LaunchRefused('This relay session requires explicit qualified relay recovery; legacy resume is unavailable.')


def refuse_raw_input(db, session_id):
    from ..models import RelaySession
    from ..supervisor import LaunchRefused
    if db.get(RelaySession, session_id):
        raise LaunchRefused('This relay session requires qualified hub input; legacy text and interrupts are unavailable. Stop remains available.')
