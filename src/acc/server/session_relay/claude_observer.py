"""Dormant L1 presentation and nonmutating native ownership observations."""
import asyncio
import secrets
import time

from sqlalchemy import select

from acc.session_relay import protocol as p

from ..models import RelayDecision, RelayOperation, RelayOwnership
from . import authority, claude_public, store, writers


def presentation(ctx, db, row, actor, attachment_id=None):
    if not claude_public.supported(ctx, row):
        return {}
    owner = db.get(RelayOwnership, row.session_id)
    if not owner:
        return {}
    run = ctx.relay_runtime.runs[row.session_id]
    stream = run.terminal_grants.record
    mine = owner.authority.get('actor') == p.to_wire(actor)
    input_mine = stream is None or (stream.value.attachment_id == attachment_id
        and stream.phase in ('claiming', 'active') and stream.expires > time.monotonic())
    epoch = stream.value.epoch if stream else max(row.control_revision, run.terminal_grants.last_epoch) + 1
    active = db.get(RelayOperation, row.active_operation_id) if row.active_operation_id else None
    active_id = active.id if active and active.actor_key == store.actor_key(actor) and active.run_id == row.current_run_id else None
    origin = run.turn_origins.get(active_id) if active_id else None
    if origin and getattr(origin['resolve'], 'attachment_id', None) != attachment_id:
        active_id = None
    # Presentation only: never start or stop an owner to discover readiness.
    first_chat = db.scalar(select(RelayOperation.id).where(
        RelayOperation.session_id == row.session_id,
        RelayOperation.receipt['method'].as_string() == 'turn.submit',
        RelayOperation.receipt['state'].as_string() == 'accepted').limit(1))
    return {'ownershipRevision': row.revision, 'execution': {
        'view': owner.view, 'phase': owner.phase, 'holder': history_actor(owner.authority.get('actor')),
        'mine': mine, 'inputEpoch': epoch, 'activeOperationId': active_id,
        'waitingForFirstChat': first_chat is None,
        'available': owner.phase == 'owned' and writers.public_ready(ctx, row) and mine and input_mine},
        'selector': {'shared_live': False, 'owner': owner.view, 'may_control': False,
            'blockers': [], 'switch_state': owner.phase, 'idle_verified': False}}


def history_actor(actor):
    from .history import public_actors
    return public_actors(actor)


def verify(run, token):
    # The peer was created during a mutating launch request. Its context must
    # not turn a presentation callback into an exclusive SQLite writer.
    from ..db import WORKER_WRITE, WRITE_INTENT
    writer, worker = WRITE_INTENT.set(False), WORKER_WRITE.set(False)
    try:
        return _verify(run, token)
    finally:
        WORKER_WRITE.reset(worker)
        WRITE_INTENT.reset(writer)


def _verify(run, token):
    record = run.owner_observations.get(token)
    if record is None:
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    if record['deadline'] <= time.monotonic():
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    with run.ctx.db() as db:
        row, current = run.check_run(db, resolve=record['resolve'], action='switch')
        authority.recheck(record['authority'], current)
        if not claude_public.ready(run.ctx, row):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    return True


def input_activity(run):
    """Activity only; never cache or substitute grant authorization.

    Input reservation precedes the native hook's durable turn registration, so
    row.revision alone cannot invalidate an idle sample across that gap.
    """
    stream = run.terminal_grants.record
    return (stream.value, stream.sequence, stream.processed, stream.pending, stream.entered_sequence) if stream else None


async def observe(run, resolve, expected_run):
    from ..db import WRITE_INTENT
    async def read_in_thread(work):
        def call():
            scope = WRITE_INTENT.set(False)
            try:
                return work()
            finally:
                WRITE_INTENT.reset(scope)
        return await asyncio.to_thread(call)
    # Reads never hold a write-intent transaction across native IO.
    def capture():
        with run.ctx.db() as db:
            row, current = run.check_run(db, resolve=resolve, action='switch')
            if str(row.current_run_id) != expected_run or not claude_public.ready(run.ctx, row):
                raise p.RelayError(p.ErrorCode.STALE_RUN)
            stream = run.terminal_grants.record
            return {'resolve': resolve, 'authority': current, 'revision': row.revision, 'input_activity': input_activity(run), 'deadline': time.monotonic() + 10,
                'input': (stream, stream.sequence, stream.processed) if stream is not None else (None, 0, 0)}
    if len(run.owner_observations) >= 16 or len(run.owner_observation_tasks) >= 16:
        raise p.RelayError(p.ErrorCode.LIMIT)
    record = await read_in_thread(capture)
    if len(run.owner_observations) >= 16 or len(run.owner_observation_tasks) >= 16:
        raise p.RelayError(p.ErrorCode.LIMIT)
    token = secrets.token_hex(32)
    run.owner_observations[token] = record
    try:
        task = asyncio.create_task(run.peer.call('owner.inspect', {'observation': token}))
        run.owner_observation_tasks.add(task)
        def finished(done):
            run.owner_observation_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        value = await asyncio.wait_for(asyncio.shield(task), 5)
        def finish():
            verify(run, token)
            with run.ctx.db() as db:
                row, grant = authority.current(run.ctx, db, resolve, run.session_id, expected_run=expected_run)
                if (not isinstance(value, dict) or type(value.get('idle')) is not bool
                        or not isinstance(value.get('blockers'), list) or len(value['blockers']) > 7
                        or any(blocker not in p.SWITCH_BLOCKERS for blocker in value['blockers'])):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                if value.get('runId') != expected_run:
                    raise p.RelayError(p.ErrorCode.STALE_RUN)
                result = presentation(run.ctx, db, row, grant.actor, resolve.attachment_id)
                if result['execution']['view'] != value.get('view'):
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                blockers = set(value['blockers'])
                if row.active_operation_id:
                    blockers.add('active_turn')
                for op in db.scalars(select(RelayOperation).where(RelayOperation.session_id == row.session_id,
                        RelayOperation.run_id == row.current_run_id)):
                    if op.queue_seq is not None and op.receipt['state'] == 'queued':
                        blockers.add('queued_send')
                    if op.id != row.active_operation_id and op.receipt['state'] in ('submitting', 'uncertain'):
                        blockers.add('uncertain_operation')
                if any(decision.decision['status'] in ('pending', 'reserved', 'outcome_unknown') for decision in
                        db.scalars(select(RelayDecision).where(RelayDecision.session_id == row.session_id,
                            RelayDecision.run_id == row.current_run_id))):
                    blockers.add('pending_decision')
                if (row.revision != record['revision'] or input_activity(run) != record['input_activity']
                        or run.terminal_grants.frame_lock.locked()):
                    blockers.add('uncertain_operation')
                result['selector'].update(may_control=True, blockers=sorted(blockers), idle_verified=value['idle'] and not blockers)
                return result
        return await read_in_thread(finish)
    except (ConnectionError, OSError, TimeoutError):
        # Missing native idle facts cannot mint authority. Preserve a current
        # reader's presentation during an in-flight input race, conservatively
        # unavailable, rather than treating a lost observation as lost access.
        def unavailable():
            verify(run, token)
            if run.peer.closed:
                raise p.RelayError(p.ErrorCode.OFFLINE)
            with run.ctx.db() as db:
                row, grant = authority.current(run.ctx, db, resolve, run.session_id, expected_run=expected_run)
                stream, admitted, processed = record['input']
                current_stream = run.terminal_grants.record
                input_changed = (stream is not None and current_stream is stream
                    and (stream.sequence, stream.processed) != (admitted, processed))
                if row.revision == record['revision'] and not input_changed:
                    raise p.RelayError(p.ErrorCode.OFFLINE)
                result = presentation(run.ctx, db, row, grant.actor, resolve.attachment_id)
                if not result:
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                result['execution']['available'] = False
                result['selector'].update(may_control=False, blockers=['uncertain_operation'], idle_verified=False)
                return result
        return await read_in_thread(unavailable)
    finally:
        run.owner_observations.pop(token, None)
