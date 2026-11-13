"""Short, caller-owned transactions. Callers authorize first and commit event + state together.

All writers lock the session row before reading mutable state. No transaction in
this module waits for provider IO. Registration tombstones are never evicted.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, select

from acc.session_relay import protocol as p

from ..models import RelayDecision, RelayEvent, RelayOperation, RelayRun, RelaySession, Session, SessionRun, utcnow

MAX_REGISTRATIONS = 4096
MAX_EVENTS = 65536
MAX_EVENT_BYTES = 128 << 10
MAX_RETAINED_BYTES = 256 << 20
# Reserve space for receipt/decision finalization, revocation and Stop.
FINALIZATION_RESERVE = 64 << 20


def actor_key(actor):
    # Device and channel are included in check_registered by this store too.
    return json.dumps([actor.kind.value, actor.principal], separators=(',', ':'))


def actor_from(value):
    return p.Actor(p.ActorKind(value['kind']), value['principal'], p.Channel(value['channel']), value.get('device'))


def receipt_from(value):
    return p.Receipt(value['operationId'], value['sessionId'], value['runId'], value['method'], value['payloadDigest'],
                     actor_from(value['actor']), p.ReceiptState(value['state']), value['revision'],
                     p.FailureReason(value['reason']) if value.get('reason') else None)


def decision_from(value):
    return p.Decision(value['decisionId'], value['sessionId'], value['runId'], p.DecisionKind(value['kind']),
                      value['actionDigest'], value['description'],
                      tuple(p.DecisionChoice(c['id'], c['label'], p.ChoiceEffect(c['effect'])) for c in value['choices']),
                      value['expiresAtMs'], value['policyRevision'], value['revision'], p.DecisionStatus(value['status']),
                      actor_from(value['winner']) if value.get('winner') else None, value.get('choiceId'))


def get(db, session_id, *, for_update=False):
    # A frame's joined load already acquired this exact root FOR UPDATE.
    # Reuse only inside that same synchronous transaction, never after commit
    # or an await. No absent row or authorization result is cached.
    locked = db.info.get('relay_frame_root')
    if locked and locked[0] is db.get_transaction() and locked[1] == session_id:
        return locked[2]
    query = select(RelaySession).where(RelaySession.session_id == session_id)
    row = db.scalar(query.with_for_update() if for_update else query)
    if row is None:
        raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
    return row


def lock(db, session_id):
    return get(db, session_id, for_update=True)


def coordinates(session):
    return {'host': session.host, 'account': session.run_as, 'workspace': session.cwd,
            'placementId': session.placement_id, 'launchSpecId': session.launch_spec_id,
            'launchDigest': session.launch_spec.digest, 'nativeId': session.native_id}


def enroll(db, session_id, run_id, binding_id, *, identity, frozen_policy, policy_revision):
    """Internal reviewed-launch integration seam; never called by an attach/read."""
    session, run = db.get(Session, session_id), db.get(SessionRun, run_id)
    if (not session or session.provider != 'codex' or session.management != 'acc' or not run
            or run.session_id != session_id or run.verified_at is None or run.ended_at is not None):
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    if db.get(RelaySession, session_id):
        raise p.RelayError(p.ErrorCode.REVISION_CONFLICT)
    row = RelaySession(session_id=session_id, enrollment=uuid.uuid4().hex, current_run_id=run_id,
                       binding_id=binding_id)
    db.add(row)
    db.flush()
    db.add(RelayRun(run_id=run_id, session_id=session_id, binding_id=binding_id,
                    identity={**identity, 'coordinates': coordinates(session)},
                    frozen_policy=frozen_policy, policy_revision=policy_revision))
    db.flush()
    return row


def append(db, row, kind, payload, *, source_key=None, operation_id=None, decision_id=None, priority=False):
    """Only safe metadata or complete normalized/redacted blocks may enter here.

    There is deliberately no raw provider event importer. history.message is the
    complete-block ingress for transcript content; operation events hold no prompt.
    """
    if kind not in p.DURABLE_EVENTS:
        raise ValueError('not a durable event')
    if source_key is not None:
        previous = db.scalar(select(RelayEvent).where(RelayEvent.session_id == row.session_id,
                                                    RelayEvent.source_key == source_key))
        if previous:
            return previous
    data = p.to_wire(p.Event(kind, row.session_id, payload, event_seq=row.event_seq + 1,
                            history_epoch=row.history_epoch, run_id=str(row.current_run_id) if row.current_run_id else None,
                            operation_id=operation_id, decision_id=decision_id, redacted=True))
    p.encode_frame(data)  # validate the bounded wire representation too
    # Stable application encoding, independent of SQLAlchemy private dialects.
    size = len(p.canonical_json(data))
    if size > MAX_EVENT_BYTES:
        raise p.RelayError(p.ErrorCode.LIMIT)
    # Hard storage cap before committing state; pruning is explicit and never drops receipts.
    byte_limit = MAX_RETAINED_BYTES if priority else MAX_RETAINED_BYTES - FINALIZATION_RESERVE
    event_limit = MAX_EVENTS + 8 * MAX_REGISTRATIONS if priority else MAX_EVENTS
    if row.event_count >= event_limit or row.used_bytes + size > byte_limit:
        raise p.RelayError(p.ErrorCode.LIMIT)
    row.event_seq += 1
    row.event_count += 1
    row.used_bytes += size
    event = RelayEvent(session_id=row.session_id, seq=row.event_seq, event=data, source_key=source_key, byte_size=size)
    db.add(event)
    db.flush()
    return event


def register(db, row, actor, key, method, digest, authority, *, deadline=None, methods=p.R1_METHODS, recovery=False):
    if not p.valid_opaque(key) or not p.valid_digest(digest) or method not in p.OPERATIONS or method not in methods:
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    if p.OPERATIONS[method].correlation is not p.Correlation.OPERATION:
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    existing = db.scalar(select(RelayOperation).where(RelayOperation.session_id == row.session_id,
                         RelayOperation.actor_key == actor_key(actor), RelayOperation.registration_key == key))
    if existing:
        receipt = receipt_from(existing.receipt)
        p.check_registered(receipt, actor=actor, session_id=row.session_id, run_id=str(row.current_run_id),
                           method=method, payload_digest=digest)
        if receipt.actor != actor:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        return p.RegistrationResult(existing.id, receipt, True)
    if (row.sealed and not (recovery is True and method=='switch.recover')) or row.current_run_id is None:
        raise p.RelayError(p.ErrorCode.STALE_RUN)
    count = db.scalar(select(func.count()).select_from(RelayOperation).where(RelayOperation.session_id == row.session_id))
    if count >= MAX_REGISTRATIONS:
        raise p.RelayError(p.ErrorCode.LIMIT)
    receipt = p.Receipt(uuid.uuid4().hex, row.session_id, str(row.current_run_id), method, digest, actor)
    db.add(RelayOperation(id=receipt.operation_id, session_id=row.session_id, run_id=row.current_run_id,
                         actor_key=actor_key(actor), registration_key=key, receipt=p.to_wire(receipt),
                         authority=authority, deadline=deadline or utcnow() + timedelta(seconds=60),
                         configuration_revision=row.configuration_revision))
    append(db, row, 'operation.changed', {'receipt': p.to_wire(receipt)}, operation_id=receipt.operation_id)
    return p.RegistrationResult(receipt.operation_id, receipt)


def operation(db, session_id, identifier, actor):
    op = db.get(RelayOperation, identifier)
    if op is None or op.session_id != session_id or op.actor_key != actor_key(actor):
        raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
    return op


def transition(db, row, op, state, *, reason=None, evidence=None):
    receipt = p.advance_receipt(receipt_from(op.receipt), state, reason=reason, evidence=evidence)
    op.receipt = p.to_wire(receipt)
    append(db, row, 'operation.changed', {'receipt': op.receipt}, operation_id=op.id, priority=True)
    return receipt


def reserve_decision(db, row, identifier, actor, *, choice_id, expected_revision, action_digest):
    record = db.get(RelayDecision, (row.session_id, identifier))
    if record is None or record.session_id != row.session_id:
        raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
    run = db.get(RelayRun, row.current_run_id)
    answer = p.reserve_answer(decision_from(record.decision), actor=actor, run_id=str(row.current_run_id),
                              policy_revision=run.policy_revision, choice_id=choice_id,
                              expected_revision=expected_revision, action_digest=action_digest,
                              now_ms=int(utcnow().timestamp() * 1000))
    record.decision = p.to_wire(answer)
    append(db, row, 'decision.changed', {'decision': record.decision}, decision_id=identifier)
    return answer


def add_decision(db, row, decision):
    if decision.session_id != row.session_id or decision.run_id != str(row.current_run_id) or row.sealed:
        raise p.RelayError(p.ErrorCode.STALE_RUN)
    all_decisions = db.scalars(select(RelayDecision).where(RelayDecision.session_id == row.session_id)).all()
    if len(all_decisions) >= MAX_REGISTRATIONS or len(p.encode_frame(p.to_wire(decision))) > 8 << 10:
        raise p.RelayError(p.ErrorCode.LIMIT)
    pending = [r for r in all_decisions
               if r.run_id == row.current_run_id and r.decision['status'] in ('pending', 'reserved', 'outcome_unknown')]
    if len(pending) >= p.MAX_PENDING_DECISIONS:
        raise p.RelayError(p.ErrorCode.LIMIT)
    db.add(RelayDecision(id=decision.decision_id, session_id=row.session_id, run_id=row.current_run_id,
                         decision=p.to_wire(decision)))
    append(db, row, 'decision.requested', {'decision': p.to_wire(decision)}, decision_id=decision.decision_id)


def seal(db, row):
    """Fence before lifecycle IO; possibly sent operations remain uncertain, never resendable."""
    if row.sealed:
        return
    row.sealed = True
    row.authority_epoch += 1
    row.controller = None
    row.control_revision += 1
    for op in db.scalars(select(RelayOperation).where(RelayOperation.session_id == row.session_id)):
        state = p.ReceiptState(op.receipt['state'])
        if state is p.ReceiptState.QUEUED:
            transition(db, row, op, p.ReceiptState.FAILED, reason=p.FailureReason.RUN_ENDED)
        elif state is p.ReceiptState.SUBMITTING:
            transition(db, row, op, p.ReceiptState.UNCERTAIN)
    for record in db.scalars(select(RelayDecision).where(RelayDecision.session_id == row.session_id)):
        decision = decision_from(record.decision)
        if decision.status is p.DecisionStatus.PENDING:
            record.decision = p.to_wire(p.advance_decision(decision, p.DecisionStatus.CANCELLED))
        elif decision.status is p.DecisionStatus.RESERVED:
            record.decision = p.to_wire(p.advance_decision(decision, p.DecisionStatus.OUTCOME_UNKNOWN))
        else:
            continue
        append(db, row, 'decision.changed', {'decision': record.decision}, decision_id=record.id, priority=True)
    append(db, row, 'session.stateChanged', {'state': 'sealed'}, priority=True)


def prune(db, row, *, before=None):
    before = before or utcnow() - timedelta(days=30)
    cutoff = db.scalar(select(func.max(RelayEvent.seq)).where(RelayEvent.session_id == row.session_id,
                                                           RelayEvent.created_at < before))
    if cutoff is not None:
        count, used = db.execute(select(func.count(), func.coalesce(func.sum(RelayEvent.byte_size), 0))
                                .where(RelayEvent.session_id == row.session_id, RelayEvent.seq <= cutoff)).one()
        db.execute(delete(RelayEvent).where(RelayEvent.session_id == row.session_id, RelayEvent.seq <= cutoff))
        row.event_count -= count
        row.used_bytes -= used
        row.floor_seq = max(row.floor_seq, cutoff)
        append(db, row, 'history.gap', {'after': row.floor_seq}, priority=True)


def recover(db, row):
    """After coordinator restart, reconcile possible writes; never dispatch them again."""
    row.authority_epoch += 1
    for op in db.scalars(select(RelayOperation).where(RelayOperation.session_id == row.session_id)):
        if op.receipt['state'] == 'submitting':
            transition(db, row, op, p.ReceiptState.UNCERTAIN)
        elif op.receipt['state'] == 'queued':
            op.confirmed = False
    for record in db.scalars(select(RelayDecision).where(RelayDecision.session_id == row.session_id)):
        decision = decision_from(record.decision)
        if decision.status is p.DecisionStatus.RESERVED:
            record.decision = p.to_wire(p.advance_decision(decision, p.DecisionStatus.OUTCOME_UNKNOWN))
            append(db, row, 'decision.changed', {'decision': record.decision}, decision_id=record.id, priority=True)


def settle(db, row, op, *, evidence, accepted):
    """Called only with qualified broker/provider evidence, never an HTTP/RPC success.

    Late evidence may settle its old receipt; it cannot clear a replacement run's
    active operation. Turn completion is distinct from prompt admission.
    """
    receipt = transition(db, row, op, p.ReceiptState.ACCEPTED if accepted else p.ReceiptState.FAILED,
                         evidence=evidence, reason=None if accepted else p.FailureReason.REJECTED)
    # A conclusive rejection cannot produce a later native completion event.
    # Release only this exact current turn's claim, after validating its proof;
    # admission/uncertainty and late evidence for another run retain their fences.
    if (not accepted and receipt.method == 'turn.submit' and row.current_run_id == op.run_id
            and row.active_operation_id == op.id):
        row.active_operation_id = None
    return receipt


def complete_turn(db, row, operation_id, *, outcome='completed'):
    if outcome not in ('completed', 'interrupted', 'failed'):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    op = db.get(RelayOperation, operation_id)
    if op is None or op.session_id != row.session_id:
        raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
    if row.current_run_id == op.run_id and row.active_operation_id == op.id:
        row.active_operation_id = None
        append(db, row, 'turn.' + outcome, {}, operation_id=op.id)


def expire_queued(db, row):
    """Sweep all actors under the session mutation lock, including disconnected people."""
    for op in db.scalars(select(RelayOperation).where(RelayOperation.session_id == row.session_id,
                          RelayOperation.deadline <= utcnow(), RelayOperation.receipt['state'].as_string() == 'queued')):
        transition(db, row, op, p.ReceiptState.FAILED, reason=p.FailureReason.EXPIRED)
