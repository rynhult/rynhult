"""Dormant service deliveries over an existing person-owned Chat writer.

The durable binding is created by trusted delivery enqueue, never an RPC actor
field. A service receipt retains its real origin; it does not acquire a writer.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select

from acc.session_relay import protocol as p

from .. import budgets
from ..api.context import Principal
from ..models import AuditLog, RelayOperation, RelayOwnership, RelaySession, Session, SessionRun, Team, User, utcnow
from . import store, writers

DELIVERY_QUALIFIED = False
CLAUDE_SERVICE_QUALIFIED = False
ACTIONS = frozenset({'operation.register', 'turn.submit', 'control', 'operation.cancel'})


def runtime(supervisor):
    manager = getattr(supervisor, 'relay_manager', None)
    return manager


def team_scope(db, session):
    if not session.team_id:
        return None
    team = db.get(Team, session.team_id)
    if not team or team.stopped or team.expires_at is not None and team.expires_at <= utcnow():
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    if session.id != team.id:
        from ..compiler import LaunchInputs
        limits = team.limits
        if not isinstance(limits, dict) or session.host not in limits.get('machines', []):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        try:
            inputs = LaunchInputs.from_dict(session.launch_spec.inputs)
            permitted = [LaunchInputs.from_dict(entry['inputs']) for entry in limits.get('agents', [])
                         if isinstance(entry, dict) and entry.get('agent') == session.provider
                         and entry.get('profile') == inputs.profile]
        except (KeyError, TypeError, ValueError):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED) from None
        fields = ('provider', 'profile', 'sandbox', 'approval', 'permission_mode', 'remote_control',
                  'extra_args', 'options', 'skills', 'agents', 'mcps', 'append_prompt', 'endpoint')
        if not any(all(getattr(entry, key) == getattr(inputs, key) for key in fields) for entry in permitted):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    return {'id': team.id, 'limits': team.limits,
            'expires': team.expires_at.isoformat() if team.expires_at else None}


def actor_for(source):
    if source == '@api':
        channel = p.Channel.API
    elif source == 'supervisor':
        channel = p.Channel.SCHEDULE
    elif source.startswith('session:') and source[8:]:
        channel = p.Channel.TEAM
    else:
        # Legacy person text entry has no current cookie/attachment grant.
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    return p.Actor(p.ActorKind.SERVICE, source, channel)


def ready(ctx, row):
    return DELIVERY_QUALIFIED is True and writers.public_ready(ctx, row)


def capture(ctx, db, message):
    from ..orchestrator import assignments, delivery
    session = db.get(Session, message.session_id)
    row = store.lock(db, message.session_id)
    run = getattr(ctx, 'relay_runtime', None)
    run = run.runs.get(message.session_id) if run else None
    owner = db.get(RelayOwnership, message.session_id)
    actor = actor_for(message.actor)
    from .release_scope import supported_actor
    supported_actor(actor)
    claude = run is not None and run.provider == 'claude'
    writer = owner.authority.get('actor') if claude and owner else row.controller
    if (not ready(ctx, row) or run is None or run.provider not in ('codex', 'claude') or not owner
            or session.provider != run.provider
            or claude and (CLAUDE_SERVICE_QUALIFIED is not True or run.frozen_policy.get('authMode') != 'api_key')
            or owner.mode != ('exclusive_idle' if claude else 'exclusive_writer')
            or owner.phase != 'owned' or owner.view != 'chat'
            or not writer or writer.get('kind') != 'person'
            or message.expires_at <= utcnow() or session.desired_state != 'running'
            or not delivery.authorized(db, session, message.actor)):
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    # Reauthenticate the existing holder, including its login/device attachment.
    from .authority import current
    _, holder = current(ctx, db, getattr(run, 'writer_resolve', None) or run.resolve, session.id, action='control', expected_run=run.run_id)
    if p.to_wire(holder.actor) != writer:
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    person = db.scalar(select(User).where(User.username == session.owner))
    if person is None:
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    attempt = assignments.current(db, session.id)
    if (message.attempt_id != (attempt.id if attempt else None) or attempt and (
            attempt.state != 'running' or assignments.replaced(db, attempt))):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    assignment = db.get(assignments.Assignment, attempt.assignment_id) if attempt else None
    team = team_scope(db, session)
    if team and session.id != session.team_id and attempt is None:
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    if assignment and (assignment.team_id != session.team_id or assignment.state != 'running'):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    principals = {session.owner, holder.actor.principal}
    source_run = credential = None
    if actor.channel is p.Channel.TEAM:
        caller = db.get(Session, message.actor[8:])
        principals.add(caller.owner)
        source = db.scalar(select(SessionRun).where(SessionRun.session_id == caller.id)
                           .order_by(SessionRun.id.desc()).limit(1))
        if source is None or source.verified_at is None or source.ended_at is not None:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        source_run = source.id
    elif actor.channel is p.Channel.API:
        token = getattr(ctx, 'token', None)
        if not isinstance(token, str) or not token:
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        credential = p.sha256_digest(token.encode())
    notice = None
    if actor.channel is p.Channel.SCHEDULE:
        notice = notice_scope(db, message)
        if notice is None:
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    for principal in principals:
        if budgets.paused(db, principal, session.provider, project_id=session.project_id,
                          machine=session.host, account=session.run_as):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    if message.body and hashlib.sha256(message.body.encode()).hexdigest() != message.text_sha256:
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    scope = {'id': message.id, 'source': message.actor, 'target': session.id, 'owner': session.owner,
             'run': row.current_run_id, 'binding': row.binding_id, 'text': message.text_sha256,
             'configurationRevision': row.configuration_revision,
             'attempt': message.attempt_id, 'assignment': ({'id': assignment.id, 'plan': assignment.plan_version,
                 'brief': assignment.brief_version, 'need': p.sha256_digest(p.canonical_json(assignment.need)),
                 'goal': p.sha256_digest(assignment.goal.encode())} if assignment else None),
             'sourceRun': source_run, 'credential': credential, 'notice': notice,
             'team': team, 'writer': writer, 'controlRevision': row.control_revision,
             'expires': message.expires_at.isoformat()}
    return session, actor, scope


@dataclass(frozen=True)
class DeliveryResolver:
    ctx: object
    identifier: str
    scope: dict

    def in_transaction(self, db):
        from ..orchestrator.delivery import MessageDelivery
        message = db.get(MessageDelivery, self.identifier)
        if message is None:
            raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
        session, actor, scope = capture(self.ctx, db, message)
        if scope != self.scope:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return Principal(actor.principal, 'service', session.run_as), actor

    def __call__(self):
        with self.ctx.db() as db:
            return self.in_transaction(db)


def bound(ctx, db, resolve, session_id, action):
    if (type(resolve) is not DeliveryResolver or resolve.ctx is not ctx or action not in ACTIONS
            or resolve.scope['target'] != session_id):
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    resolve.in_transaction(db)
    return db.get(Session, session_id), resolve.scope


def registration(ctx, db, message):
    session, actor, scope = capture(ctx, db, message)
    row = store.lock(db, session.id)
    from .authority import current
    resolve = DeliveryResolver(ctx, message.id, scope)
    _, grant = current(ctx, db, resolve, session.id, action='operation.register')
    payload = {'sessionId': session.id, 'expectedRunId': str(row.current_run_id), 'text': message.body,
               'expectedConfigurationRevision': row.configuration_revision,
               'expectedControlRevision': row.control_revision, 'ifBusy': 'queue'}
    result = store.register(db, row, actor, 'delivery-' + message.id, 'turn.submit',
                            p.operation_digest('turn.submit', payload), grant.stamp(), deadline=message.expires_at)
    from .hub import Hub
    Hub(ctx)._audit(db, actor, session.id, 'delivery.register', operation_id=result.operation_id,
                    delivery_id=message.id, previous=scope['writer'], control_revision=scope['controlRevision'])
    return result


def operation(db, identifier):
    from ..orchestrator.delivery import MessageDelivery
    return db.scalar(select(RelayOperation).join(MessageDelivery,
        MessageDelivery.session_id == RelayOperation.session_id).where(MessageDelivery.id == identifier,
        RelayOperation.registration_key == 'delivery-' + identifier,
        RelayOperation.authority['delivery']['id'].as_string() == identifier))


async def deliver(supervisor, identifier):
    """Observe or admit one registered delivery; never wait for turn completion."""
    from ..db import write_in_thread, write_intent
    from ..orchestrator import delivery as queue
    manager = runtime(supervisor)
    @write_intent
    def bind_notice():
        with supervisor.db() as db:
            message = db.get(queue.MessageDelivery, identifier)
            if (message is None or message.actor != 'supervisor' or message.state != 'queued'
                    or not db.get(RelaySession, message.session_id) or operation(db, identifier) is not None):
                return False
            try:
                # A coalescer can supersede this row while we wait for the
                # session lock. Never register from the pre-lock ORM snapshot.
                store.lock(db, message.session_id)
                db.refresh(message)
                if message.state != 'queued' or operation(db, identifier) is not None:
                    return False
                from .release_scope import supported_actor
                supported_actor(actor_for(message.actor))
                intent = notice_scope(db, message)
                if intent is None or manager is None or not ready(manager.ctx, store.get(db, message.session_id)):
                    return False
                if message.session_id not in manager.runs:
                    return True
                registration(manager.ctx, db, message)
                db.commit()
                return False
            except (p.RelayError, HTTPException) as error:
                db.rollback()
                message = db.get(queue.MessageDelivery, identifier)
                # Only the closed, fixed RC message may reach the public queue.
                reason = ('relay_not_supported_in_release' if isinstance(error, p.RelayError)
                    and error.details.get('reason') == 'not_supported_in_release' else 'relay_authority_ended')
                queue.transition(db, message, 'failed', error=reason)
                db.commit()
                return True
    if await write_in_thread(bind_notice):
        return True
    with supervisor.db() as db:
        message = db.get(queue.MessageDelivery, identifier)
        if message is None or not db.get(RelaySession, message.session_id):
            return False
        if message.state != 'queued':
            return True
        op = operation(db, identifier)
        if op is None:
            return False  # Old retained queue takes the existing fail-closed fence.
        scope = op.authority.get('delivery')
        if not isinstance(scope, dict):
            return False
        resolve = DeliveryResolver(manager.ctx, identifier, scope) if manager else None
        receipt = store.receipt_from(op.receipt)
        session_id = message.session_id
        submitted = op.queue_seq is not None
        params = {'sessionId': session_id, 'expectedRunId': str(op.run_id), 'text': message.body,
                  'expectedConfigurationRevision': op.configuration_revision,
                  'expectedControlRevision': scope['controlRevision'], 'ifBusy': 'queue', 'operationId': op.id}
    await reap_failed_pending(supervisor, session_id)
    failure = None
    waiting_for_sdk = manager is not None and (run := manager.runs.get(session_id)) is not None and run.provider == 'claude'
    if receipt.state is p.ReceiptState.QUEUED:
        try:
            if resolve is None:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            # Expiry/assignment/budget/holder changes also invalidate a waiting
            # operation. Validation never authorizes a replay of a submitted one.
            await write_in_thread(resolve)
            if not submitted:
                receipt = await manager.submit(resolve, session_id, params)
        except (p.RelayError, HTTPException) as error:
            failure = error.code.value if isinstance(error, p.RelayError) else 'NOT_FOUND_OR_DENIED'
    @write_intent
    def finish():
        with supervisor.db() as db:
            message = db.get(queue.MessageDelivery, identifier)
            op = operation(db, identifier)
            row = store.lock(db, session_id)
            if message.state != 'queued':
                return
            if failure and op.receipt['state'] == 'queued':
                store.transition(db, row, op, p.ReceiptState.FAILED, reason=p.FailureReason.AUTHORITY_ENDED)
            current = store.receipt_from(op.receipt)
            if current.state is p.ReceiptState.ACCEPTED:
                queue.transition(db, message, 'acknowledged', ack='relay_admitted')
            elif current.state is p.ReceiptState.UNCERTAIN and waiting_for_sdk:
                message.error = 'relay_admission_unconfirmed'
                message.deliver_after = utcnow() + queue.timedelta(seconds=1)
            elif current.state in (p.ReceiptState.FAILED, p.ReceiptState.UNCERTAIN):
                queue.transition(db, message, 'failed', error='relay_' + (current.reason.value if current.reason else 'outcome_unknown'))
            else:
                message.error = 'relay_queued'
                message.deliver_after = utcnow() + queue.timedelta(seconds=1)
            if message.state in queue.TERMINAL and manager is not None:
                from .hub import Hub
                Hub(manager.ctx)._audit(db, store.actor_from(op.receipt['actor']), session_id,
                    'delivery.settled', operation_id=op.id, delivery_id=message.id, state=current.state.value,
                    previous=scope['writer'], control_revision=scope['controlRevision'])
            db.commit()
    await write_in_thread(finish)
    # Reclaim invalidated stages through the existing failed-receipt authority.
    if failure and manager:
        run = manager.runs.get(session_id)
        if run:
            async with run.submit_lock:
                for token, grant in list(run.grants.items()):
                    if grant.operation.operation_id == params['operationId']:
                        await manager.discard(run, token)
    return True


def idle_for_assignment(supervisor, db, session, actor):
    """A team assignment may change only at the existing qualified Chat boundary."""
    from ..orchestrator import delivery as queue
    manager = runtime(supervisor)
    row = store.get(db, session.id)
    owner = db.get(RelayOwnership, session.id)
    run = manager.runs.get(session.id) if manager else None
    if (not manager or not ready(manager.ctx, row) or run is None or not owner
            or owner.view != 'chat' or owner.phase != 'owned' or row.active_operation_id
            or actor is None or actor_for(actor).channel is not p.Channel.TEAM
            or not queue.authorized(db, session, actor)):
        return False
    from ..models import RelayDecision
    if db.scalar(select(RelayOperation.id).where(RelayOperation.session_id == session.id,
            RelayOperation.receipt['state'].as_string().in_(('queued', 'submitting', 'uncertain'))).limit(1)):
        return False
    if db.scalar(select(RelayDecision.id).where(RelayDecision.session_id == session.id,
            RelayDecision.decision['status'].as_string().in_(('pending', 'reserved', 'outcome_unknown'))).limit(1)):
        return False
    run.check_run(db, resolve=getattr(run, 'writer_resolve', None) or run.resolve)
    return True


def end_waiting(db, message, reason):
    op = operation(db, message.id)
    if op is not None:
        row = store.lock(db, message.session_id)
        db.refresh(op)
        if op.receipt['state'] == 'queued':
            store.transition(db, row, op, p.ReceiptState.FAILED, reason=reason)


async def reap_failed(supervisor, identifier):
    manager = runtime(supervisor)
    if manager is None:
        return
    with supervisor.db() as db:
        op = operation(db, identifier)
        if op is None or op.receipt['state'] != 'failed':
            return
        session_id, operation_id = op.session_id, op.id
    run = manager.runs.get(session_id)
    if run:
        async with run.submit_lock:
            for token, grant in list(run.grants.items()):
                if grant.operation.operation_id == operation_id:
                    await manager.discard(run, token)


def check_operation(scope, op, params=None):
    if (op.registration_key != 'delivery-' + scope['id'] or op.session_id != scope['target']
            or op.authority.get('delivery') != scope or op.receipt['method'] != 'turn.submit'):
        raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
    if params is not None and (not isinstance(params.get('text'), str)
            or hashlib.sha256(params['text'].encode()).hexdigest() != scope['text']
            or params.get('expectedConfigurationRevision') != scope['configurationRevision']
            or params.get('expectedControlRevision') != scope['controlRevision']):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)


NOTICE_FAMILIES = frozenset({'lead', 'parent', 'run_mode', 'project_brief'})


def seal_notice(db, message, family):
    """Server-only intent, independent of any current process or input grant."""
    if family not in NOTICE_FAMILIES or message.actor != 'supervisor':
        raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    target = db.get(Session, message.session_id)
    if not db.get(RelaySession, target.id):
        return
    from ..orchestrator.assignments import current
    attempt = current(db, target.id)
    message.attempt_id = attempt.id if attempt else None
    db.add(AuditLog(actor='supervisor', action='relay.notice.intent', target=target.id,
        detail={'delivery': message.id, 'digest': message.text_sha256, 'family': family,
                'team': team_scope(db, target), 'channel': 'schedule'}))


def notice_scope(db, message):
    intent = db.scalar(select(AuditLog).where(AuditLog.action == 'relay.notice.intent',
        AuditLog.actor == 'supervisor', AuditLog.target == message.session_id,
        AuditLog.detail['delivery'].as_string() == message.id).order_by(AuditLog.id).limit(1))
    if intent is None:
        return None
    value = intent.detail
    if (value.get('family') not in NOTICE_FAMILIES or value.get('digest') != message.text_sha256
            or value.get('team') != team_scope(db, db.get(Session, message.session_id))):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    return {'audit': intent.id, 'family': value['family'], 'digest': value['digest'], 'team': value['team']}


def coalescible(db, message):
    store.lock(db, message.session_id)
    op = operation(db, message.id)
    if op is not None:
        db.refresh(op)
    return op is None or op.receipt['state'] == 'queued'


def supersede_notice(db, message):
    from ..orchestrator import delivery as queue
    if not coalescible(db, message):
        return False
    end_waiting(db, message, p.FailureReason.CANCELLED)
    queue.transition(db, message, 'expired', error='notice_superseded')
    db.add(AuditLog(actor='supervisor', action='relay.notice.superseded', target=message.session_id,
                   detail={'delivery': message.id, 'channel': 'schedule'}))
    return True


async def reap_failed_pending(supervisor, session_id):
    manager = runtime(supervisor)
    run = manager.runs.get(session_id) if manager else None
    if run is None:
        return
    async with run.submit_lock:
        with supervisor.db() as db:
            tokens = [token for token, grant in run.grants.items()
                      if (op := db.get(RelayOperation, grant.operation.operation_id)) is not None
                      and op.receipt['state'] == 'failed']
        for token in tokens:
            await manager.discard(run, token)
