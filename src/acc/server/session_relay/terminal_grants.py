"""Opaque, attachment-bound main grants for an already owned TerminalHost.

No attach/read creates input authority. Public qualification remains closed.
The registry is run-local and holds at most one stream; restart cannot renew it.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import bindparam, event, select

from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import InputGrant, TerminalRun

from ..db import write_in_thread, write_intent
from ..models import (
    LaunchSpec,
    Placement,
    Project,
    RelayDecision,
    RelayOperation,
    RelayOwnership,
    RelayRun,
    RelaySession,
    SecurityRevision,
    Session,
    SessionRun,
)
from ..source_models import ExecutionHost, SessionTarget, WorkingLocation
from . import authority, store, writers
from .hub import Hub


@lru_cache(maxsize=1)
def _frame_facts_statement():
    """Reuse query structure only; every call executes against fresh locked rows."""
    return (select(RelaySession, Session, SessionRun, RelayRun,
        RelayOwnership, Project, Placement, LaunchSpec, SessionTarget, WorkingLocation, ExecutionHost, SecurityRevision)
        .join(Session, Session.id == RelaySession.session_id)
        .outerjoin(SessionRun, SessionRun.id == RelaySession.current_run_id)
        .outerjoin(RelayRun, RelayRun.run_id == RelaySession.current_run_id)
        .outerjoin(RelayOwnership, RelayOwnership.session_id == RelaySession.session_id)
        .outerjoin(Project, Project.id == Session.project_id)
        .outerjoin(Placement, Placement.id == Session.placement_id)
        .outerjoin(LaunchSpec, LaunchSpec.id == Session.launch_spec_id)
        .outerjoin(SessionTarget, SessionTarget.session_id == Session.id)
        .outerjoin(WorkingLocation, WorkingLocation.id == SessionTarget.location_id)
        .outerjoin(ExecutionHost, ExecutionHost.id == WorkingLocation.host_id)
        .outerjoin(SecurityRevision, SecurityRevision.id == 1)
        .where(RelaySession.session_id == bindparam('relay_frame_session_id'))
        .with_for_update(of=RelaySession))


@contextmanager
def frame_identity(db):
    """Keep ORM identities alive only inside one synchronous frame transaction.

    Session.get already uses its identity map. Weak references otherwise let
    repeated caller/grant checks reload the same rows after a temporary value
    dies. Retain loaded identities and one successful login check per resolver
    and transaction, never missing rows or a policy/grant decision. Every
    post-await stage opens its own DB session and reads current authority.
    """
    rows = []
    def retain(_session, row):
        rows.append(row)
    event.listen(db, 'loaded_as_persistent', retain)
    db.info['relay_frame_identity'] = True
    db.info['relay_frame_principals'] = {}
    try:
        yield
    finally:
        event.remove(db, 'loaded_as_persistent', retain)
        rows.clear()
        db.info.pop('relay_frame_principals', None)
        db.info.pop('relay_frame_identity', None)
        db.info.pop('relay_frame_root', None)


@dataclass(repr=False)
class Stream:
    value: InputGrant
    resolve: object
    authority: object
    operation_id: str
    expires: float
    phase: str = 'claiming'
    sequence: int = 0
    processed: int = 0
    pending: bool = False
    native: object = None
    private_draft: str = ''
    entered_sequence: int | None = None


@dataclass(repr=False)
class NativeSubmission:
    sequence: int
    params: dict
    operation_id: str | None = None


class NativeResolver:
    """Server-only delegation of the exact live stream; never a cached person."""
    def __init__(self, registry, record, submission):
        self.registry, self.record, self.submission = registry, record, submission
        self.attachment_id = record.value.attachment_id

    def __call__(self):
        with self.registry.run.ctx.db() as db:
            return self.in_transaction(db)

    def in_transaction(self, db):
        record, _ = self.registry._current(db, self.record.value.grant)
        if record is not self.record or record.native is not self.submission or record.sequence != self.submission.sequence:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        resolve = record.resolve
        return resolve.in_transaction(db) if hasattr(resolve, 'in_transaction') else resolve()


class TerminalGrants:
    def __init__(self, run):
        self.run = run
        self.record = None
        self.last_epoch = 0
        self.frame_lock = asyncio.Lock()
        from .stock_approval_frames import ApprovalFrames
        self.approvals = ApprovalFrames(self)

    def queued_dispatch_allowed(self, token):
        return True

    def invalidate(self):
        if self.record is not None:
            self.record.phase = 'revoked'
        self.record = None
        self.approvals.reset()

    def _current(self, db, token, *, resolve=None):
        # The current HTTP caller must authenticate before any private stream
        # lookup/refusal. A live input frame performs this inside its reservation
        # or acknowledgement transaction rather than a preliminary worker hop.
        if resolve is not None and hasattr(resolve, 'authenticate_in_transaction'):
            resolve.authenticate_in_transaction(db)
        record = self.record
        if (record is None or not p.valid_opaque(token) or not secrets.compare_digest(token, record.value.grant)
                or record.phase not in ('claiming', 'active') or record.expires <= time.monotonic()):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        if db.info.get('relay_frame_identity'):
            if resolve is None and hasattr(record.resolve, 'authenticate_in_transaction'):
                record.resolve.authenticate_in_transaction(db)
            # One indexed row per model; no collections or cached policy result.
            # Lock the same relay root that check_run locks, before loading its
            # mutable run/owner facts. FOR UPDATE OF excludes nullable join sides.
            facts = db.execute(_frame_facts_statement(),
                {'relay_frame_session_id': self.run.session_id}).first()
            if facts is not None:
                db.info['relay_frame_root'] = (db.get_transaction(), self.run.session_id, facts[0])
        if resolve is not None:
            # Validate the current caller/attachment before the original writer.
            # A revoked writer must not report HTTP401 for another valid caller.
            _, caller = authority.current(self.run.ctx, db, resolve, self.run.session_id,
                                          expected_run=self.run.run_id)
            authority.recheck(record.authority, caller)
            if getattr(resolve, 'attachment_id', None) != record.value.attachment_id:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        row, current = self.run.check_run(db, resolve=record.resolve)
        owner = db.get(RelayOwnership, self.run.session_id)
        if (not writers.public_ready(self.run.ctx, row) or owner is None or owner.mode != 'exclusive_writer'
                or owner.phase != 'owned' or owner.view != 'terminal'
                or owner.authority.get('writerAttachment') != record.value.attachment_id
                or row.controller != p.to_wire(record.value.actor) or row.control_revision != record.value.epoch
                or self.run.fence != record.value.fence):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        authority.recheck(record.authority, current)
        return record, row

    @write_intent
    def authorize(self, token, action, sequence=None):
        with self.run.ctx.db() as db, frame_identity(db):
            record, _ = self._current(db, token)
            if (action not in ('claim', 'input', 'held')
                    or (action == 'claim' and record.phase != 'claiming')
                    or (action == 'input' and (record.phase != 'active' or not record.pending
                        or type(sequence) is not int or sequence != record.sequence))):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            value = {**p.to_wire(record.value), 'grant': token}  # private broker callback only
            db.commit()
            return value

    @write_intent
    def prepare(self, operation_token):
        self.run.resolve_grant(operation_token)
        grant = self.run.grants[operation_token]
        op = grant.operation
        if op.method != 'control.claim' or op.actor.kind is not p.ActorKind.PERSON:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        with self.run.ctx.db() as db:
            row, current = self.run.check_run(db, resolve=grant.resolve)
            owner = db.get(RelayOwnership, self.run.session_id)
            if (not writers.public_ready(self.run.ctx, row) or not owner or owner.mode != 'exclusive_writer'
                    or owner.phase != 'owned' or owner.view != 'terminal' or row.controller != p.to_wire(current.actor)
                    or op.params.get('attachmentId') != getattr(grant.resolve, 'attachment_id', None)
                    or owner.authority.get('writerAttachment') != op.params.get('attachmentId')
                    or type(op.params.get('expectedRevision')) is not int or row.control_revision != op.params['expectedRevision']):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if self.record is not None or row.control_revision <= self.last_epoch:
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            writers.pending(db, row, op.operation_id)
            store.transition(db, row, db.get(RelayOperation, op.operation_id), p.ReceiptState.SUBMITTING)
            value = InputGrant(TerminalRun(self.run.session_id, str(self.run.run_id)), op.params['attachmentId'],
                               current.actor, secrets.token_hex(32), row.control_revision, self.run.fence)
            record = Stream(value, grant.resolve, current, op.operation_id, time.monotonic() + 3600)
            db.commit()
            self.record, self.last_epoch = record, value.epoch
            return value.grant

    @write_intent
    def finish(self, token, result):
        with self.run.ctx.db() as db:
            record, row = self._current(db, token)
            if record.phase != 'claiming' or result != {'claimed': True, 'epoch': record.value.epoch}:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            operation = db.get(RelayOperation, record.operation_id)
            if operation.deadline <= store.utcnow():
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            receipt = store.settle(db, row, operation, accepted=True,
                evidence=p.Evidence(p.EvidenceKind.HUB_COMMIT, operation.id, str(self.run.run_id)))
            Hub(self.run.ctx)._audit(db, record.value.actor, self.run.session_id, 'terminal.claim',
                                    attachment_id=record.value.attachment_id, input_epoch=record.value.epoch)
            db.commit()
            record.phase = 'active'
            return p.ControlResult(receipt, row.control_revision, record.value.actor, token, record.value.epoch)

    async def claim(self, resolve, params):
        async with self.run.submit_lock:
            operation_token = await write_in_thread(lambda: self.run.issue(resolve, params['operationId'],
                {key: value for key, value in params.items() if key != 'operationId'}))
            if not isinstance(operation_token, str):
                # Retained receipt lookup cannot mint or renew a lost stream.
                return p.ControlResult(operation_token, params['expectedRevision'])
            prepared = False
            try:
                token = await write_in_thread(lambda: self.prepare(operation_token))
                prepared = True
                result = await self.run.peer.call('terminal.claim', {'grant': token})
                return await write_in_thread(lambda: self.finish(token, result))
            except BaseException as error:
                try:
                    await write_in_thread(lambda failure=error: writers.refuse(self.run, operation_token, failure, prepared))
                finally:
                    if prepared or isinstance(error, asyncio.CancelledError):
                        await self.poison()
                if isinstance(error, (ConnectionError, OSError, TimeoutError)):
                    raise p.RelayError(p.ErrorCode.OFFLINE) from None
                raise
            finally:
                self.run.grants.pop(operation_token, None)

    @write_intent
    def frame(self, resolve, token, epoch, sequence, data):
        if type(data) is not bytes or not 0 < len(data) <= p.MAX_TERMINAL_INPUT_BYTES:
            raise p.RelayError(p.ErrorCode.LIMIT)
        with self.run.ctx.db() as db, frame_identity(db):
            record, row = self._current(db, token, resolve=resolve)
            if (record.phase != 'active' or type(epoch) is not int or epoch != record.value.epoch
                    or type(sequence) is not int or sequence != record.sequence + 1 or record.pending):
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            if record.entered_sequence is not None and record.processed < record.entered_sequence:
                raise p.RelayError(p.ErrorCode.BUSY)
            approval = self.approvals.offer is not None
            if approval:
                self.approvals.capture(db, record, sequence, data, resolve)
                candidate, submit = '', True
            else:
                # Refusal only: a pending unarmed review never supplies a question
                # identity, choice or actor for ordinary terminal bytes.
                # One materialized fact set in this locked synchronous frame.
                # Both existing refusals still run; no decision/authority result
                # is retained for the next frame or any post-await transaction.
                decisions = tuple(db.scalars(select(RelayDecision).where(
                    RelayDecision.session_id == self.run.session_id)))
                if self.run.provider == 'codex' and any(item.run_id == self.run.run_id
                        and item.decision['status'] == 'pending' for item in decisions):
                    raise p.RelayError(p.ErrorCode.UNSUPPORTED, details={'reason': 'terminal_review_required'})
                from acc.session_relay.claude_draft import project_text_frame
                candidate, submit = project_text_frame(record.private_draft, data)
                writers.pending(db, row, record.operation_id, decisions=decisions)
            db.commit()
            record.sequence, record.pending = sequence, True
            record.private_draft = candidate
            if submit:
                record.entered_sequence = sequence
        if approval:
            try:
                self.approvals.register()  # registration completes before the first broker byte
            except BaseException:
                self.invalidate()
                raise

    @write_intent
    def acknowledge(self, resolve, token, value):
        with self.run.ctx.db() as db, frame_identity(db):
            record, _ = self._current(db, token, resolve=resolve)
            if (not isinstance(value, dict) or value.keys() != {'watermark', 'partial'}
                    or not isinstance(value['watermark'], dict)
                    or value['watermark'].keys() != {'epoch', 'admitted', 'processed'}):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            watermark = value['watermark']
            if (not record.pending or value.get('partial') is not False or type(watermark.get('epoch')) is not int
                    or watermark['epoch'] != record.value.epoch or type(watermark.get('admitted')) is not int
                    or watermark['admitted'] != record.sequence or type(watermark.get('processed')) is not int
                    or not 0 <= watermark['processed'] <= record.processed <= record.sequence):
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            db.commit()
            record.pending = False
            return {'partial': False, 'watermark': {**watermark, 'processed': record.processed}}

    @write_intent
    def watermark(self, resolve, params):
        """Observe confirmed transport/decoder progress; never claim or flush input."""
        with self.run.ctx.db() as db:
            record, _ = self._current(db, params['inputGrant'], resolve=resolve)
            if record.phase != 'active' or record.pending:
                raise p.RelayError(p.ErrorCode.BUSY)
            if params['attachmentId'] != record.value.attachment_id:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            # sequence is only admitted once acknowledge() clears pending.
            # processed is updated by the causal native consumption callback.
            return {'epoch': record.value.epoch, 'admitted': record.sequence, 'processed': record.processed}

    @write_intent
    def native_register(self, token, sequence, text, revision):
        if not isinstance(text, str) or not text or len(text.encode()) > p.MAX_PROMPT_BYTES:
            raise p.RelayError(p.ErrorCode.LIMIT)
        with self.run.ctx.db() as db:
            record, row = self._current(db, token)
            if (record.phase != 'active' or type(sequence) is not int or sequence != record.sequence
                    or sequence <= record.processed or type(revision) is not int or revision != record.value.epoch):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            params = {'sessionId': self.run.session_id, 'expectedRunId': str(self.run.run_id), 'text': text,
                      'expectedControlRevision': revision, 'expectedConfigurationRevision': row.configuration_revision,
                      'ifBusy': 'reject'}
            previous = record.native
            if previous is not None and previous.sequence == sequence:
                if previous.params != params or previous.operation_id is None:
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                return NativeResolver(self, record, previous), True
            writers.pending(db, row, '')
            db.commit()
        submission = NativeSubmission(sequence, params)
        record.native = submission
        resolve = NativeResolver(self, record, submission)
        registered = Hub(self.run.ctx).register(resolve, self.run.session_id, str(self.run.run_id),
            f'native-{revision}-{sequence}', 'turn.submit', p.operation_digest('turn.submit', params))
        submission.operation_id = registered.operation_id
        return resolve, registered.existing

    async def native_submit(self, token, sequence, text, revision):
        # Independent of the frame lock: native ingress can arrive while the
        # framed write's delivery response is still awaiting its authority check.
        async with self.run.submit_lock:
            resolve, existing = await write_in_thread(lambda: self.native_register(token, sequence, text, revision))
            submission = resolve.submission
            if existing:
                def lookup():
                    with self.run.ctx.db() as db:
                        _, actor = resolve.in_transaction(db)
                        return store.receipt_from(store.operation(db, self.run.session_id, submission.operation_id, actor).receipt)
                return p.to_wire(await write_in_thread(lookup))  # observation only, never redispatch
            receipt = await self.run.manager._submit(resolve, self.run.session_id,
                {**submission.params, 'operationId': submission.operation_id})
            return p.to_wire(receipt)

    @write_intent
    def native_processed(self, token, sequence, operation_id):
        with self.run.ctx.db() as db:
            record, _ = self._current(db, token)
            submission = record.native
            if (record.phase != 'active' or submission is None or type(sequence) is not int
                    or submission.sequence != sequence or sequence != record.sequence
                    or submission.operation_id != operation_id):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            operation = store.operation(db, self.run.session_id, operation_id, record.value.actor)
            if operation.receipt['state'] not in ('accepted', 'failed'):
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            if operation.receipt['payloadDigest'] != p.operation_digest('turn.submit', submission.params):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            db.commit()
            record.processed = max(record.processed, sequence)
            record.private_draft, record.entered_sequence = '', None
            return True

    async def poison(self):
        self.invalidate()
        try:
            await write_in_thread(self.run.fence_lost)
        finally:
            self.run.peer.invalidate()

    async def input(self, resolve, params, data):
        async with self.frame_lock:
            token = params['inputGrant']
            reserved = False
            def reserve():
                nonlocal reserved
                self.frame(resolve, token, params['inputEpoch'], params['frameSeq'], data)
                reserved = True  # cancellation joins this worker before consulting the flag
            try:
                await write_in_thread(reserve)
                result = await self.run.peer.call('terminal.input', {'grant': token, 'sequence': params['frameSeq'],
                                                                   'data': params['data']})
                return await write_in_thread(lambda: self.acknowledge(resolve, token, result))
            except BaseException as error:
                if reserved:
                    await self.poison()
                if isinstance(error, (ConnectionError, OSError, TimeoutError)):
                    raise p.RelayError(p.ErrorCode.OFFLINE) from None
                raise
