"""Default-off stock approval frames bound before delivery to their original login.

The broker supplies an explicit immutable offer, never the identity of a person.
Only this stream's authenticated frame can create its decision operation.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from fastapi import HTTPException

from acc.session_relay import protocol as p

from ..db import write_in_thread, write_intent
from ..models import RelayDecision, RelayOperation
from . import authority, native_decisions, store
from .hub import Hub

PUBLIC_STOCK_KEYS_QUALIFIED = False


@dataclass(frozen=True)
class Offer:
    alias: str
    decision_id: str
    digest: str
    revision: int
    choices: tuple[str, ...]


@dataclass(frozen=True)
class KeyFrame:
    offer: Offer
    grant: object
    sequence: int
    choice: str
    resolve: object


@dataclass
class Submission:
    frame: KeyFrame
    operation_id: str | None = None
    dispatched: bool = False


class Resolver:
    def __init__(self, owner, record, submission):
        self.owner, self.record, self.submission = owner, record, submission
        self.attachment_id = record.value.attachment_id

    def __call__(self):
        with self.owner.run.ctx.db() as db:
            return self.in_transaction(db)

    def in_transaction(self, db):
        record, _ = self.owner.control._current(db, self.record.value.grant, resolve=self.submission.frame.resolve)
        if (record is not self.record or self.owner.submission is not self.submission
                or record.sequence != self.submission.frame.sequence
                or record.value != self.submission.frame.grant
                or self.submission.operation_id is None and self.owner.offer is not self.submission.frame.offer):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        resolve = self.submission.frame.resolve
        return resolve.in_transaction(db) if hasattr(resolve, 'in_transaction') else resolve()


class ApprovalFrames:
    def __init__(self, control):
        self.control, self.run = control, control.run
        self.offer = self.record = self.submission = None
        self.watch_task = None
        self.lifecycle_lock = asyncio.Lock()

    def reset(self):
        self.offer = self.record = self.submission = None
        task, self.watch_task = self.watch_task, None
        if task is not None and not task.done():
            loop = task.get_loop()
            if not loop.is_closed():
                loop.call_soon_threadsafe(task.cancel)

    def enabled(self):
        if PUBLIC_STOCK_KEYS_QUALIFIED is not True or self.run.provider != 'codex':
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)

    def decision(self, db, record, offer, resolve=None):
        row = db.get(RelayDecision, (self.run.session_id, offer.decision_id))
        if row is None:
            raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
        decision = store.decision_from(row.decision)
        if (not native_decisions.public_ready(self.run.ctx, store.get(db, self.run.session_id))
                or decision.run_id != str(self.run.run_id) or decision.policy_revision != self.run.policy_revision
                or decision.kind is not p.DecisionKind.TOOL_APPROVAL or decision.action_digest != offer.digest
                or decision.revision != offer.revision or decision.status is not p.DecisionStatus.PENDING
                or decision.expires_at_ms <= int(time.time() * 1000)
                or not set(offer.choices) <= {choice.id for choice in decision.choices}):
            raise p.RelayError(p.ErrorCode.DECISION_RESOLVED)
        _, current = authority.current(self.run.ctx, db, record.resolve if resolve is None else resolve, self.run.session_id,
            action='decide', expected_run=self.run.run_id, attached=True, decision=decision)
        authority.recheck(record.authority, current)
        return decision

    @write_intent
    def arm(self, token, value):
        self.enabled()
        if (not isinstance(value, dict) or value.keys() != {'alias', 'decisionId', 'actionDigest', 'revision', 'choices'}
                or not p.valid_opaque(value['alias']) or not p.valid_opaque(value['decisionId'])
                or not p.valid_digest(value['actionDigest']) or type(value['revision']) is not int or value['revision'] < 0
                or value['choices'] != ['accept', 'cancel']):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        offer = Offer(value['alias'], value['decisionId'], value['actionDigest'], value['revision'], tuple(value['choices']))
        with self.run.ctx.db() as db:
            record, _ = self.control._current(db, token)
            if (record.phase != 'active' or record.pending and record.processed < record.sequence
                    or record.private_draft or self.offer is not None
                    or record.entered_sequence is not None and record.processed < record.entered_sequence
                    or self.submission is not None and record.processed < self.submission.frame.sequence):
                raise p.RelayError(p.ErrorCode.BUSY)
            self.decision(db, record, offer)
            db.commit()
        self.record, self.offer, self.submission = record, offer, None
        return True

    def capture(self, db, record, sequence, data, resolve):
        self.enabled()
        if self.record is not record or self.offer is None or self.submission is not None or record.private_draft:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        choice = {b'\r': 'accept', b'\x1b': 'cancel'}.get(data)
        if choice not in self.offer.choices:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED, details={'reason': 'unsupported_terminal_approval_key'})
        self.decision(db, record, self.offer, resolve)
        self.submission = Submission(KeyFrame(self.offer, record.value, sequence, choice, resolve))

    def params(self, submission):
        frame = submission.frame
        return {'sessionId': self.run.session_id, 'expectedRunId': str(self.run.run_id),
            'decisionId': frame.offer.decision_id, 'choiceId': frame.choice,
            'expectedRevision': frame.offer.revision, 'actionDigest': frame.offer.digest}

    def register(self):
        submission = self.submission
        resolve = Resolver(self, self.record, submission)
        params = self.params(submission)
        if self.offer is not submission.frame.offer:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        registered = Hub(self.run.ctx).register(resolve, self.run.session_id, str(self.run.run_id),
            'stock-frame-' + submission.frame.offer.alias + '-' + str(submission.frame.sequence),
            'decision.reply', p.operation_digest('decision.reply', params))
        if registered.existing:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        submission.operation_id = registered.operation_id
        if self.offer is not submission.frame.offer or self.submission is not submission:
            with self.run.ctx.db() as db:
                row = store.lock(db, self.run.session_id)
                operation = store.operation(db, self.run.session_id, registered.operation_id, submission.frame.grant.actor)
                if operation.receipt['state'] == 'queued':
                    store.transition(db, row, operation, p.ReceiptState.FAILED, reason=p.FailureReason.CONFLICT)
                db.commit()
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)

    def current(self, db, token, sequence, alias, choice):
        self.enabled()
        record, _ = self.control._current(db, token)
        submission = self.submission
        if (submission is None or self.record is not record or submission.operation_id is None
                or type(sequence) is not int or record.sequence != sequence or submission.frame.sequence != sequence
                or submission.frame.offer.alias != alias or submission.frame.choice != choice
                or submission.frame.grant != record.value):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        Resolver(self, record, submission).in_transaction(db)
        return record, submission

    @write_intent
    def authorize(self, token, sequence, alias, choice):
        with self.run.ctx.db() as db:
            record, submission = self.current(db, token, sequence, alias, choice)
            operation = store.operation(db, self.run.session_id, submission.operation_id, submission.frame.grant.actor)
            if (operation.receipt['state'] != 'queued' or operation.deadline <= store.utcnow()
                    or not record.pending or submission.dispatched):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            self.decision(db, record, submission.frame.offer)
            db.commit()
            return {'operationId': submission.operation_id, **self.params(submission)}

    @write_intent
    def begin_reply(self, token, sequence, alias, choice):
        with self.run.ctx.db() as db:
            record, submission = self.current(db, token, sequence, alias, choice)
            operation = store.operation(db, self.run.session_id, submission.operation_id, submission.frame.grant.actor)
            if submission.dispatched or operation.receipt['state'] != 'queued':
                raise p.RelayError(p.ErrorCode.DECISION_RESOLVED)
            self.decision(db, record, submission.frame.offer)
            db.commit()
            submission.dispatched = True  # consume before any awaited CAS/provider dispatch
        return Resolver(self, record, submission), {**self.params(submission), 'operationId': submission.operation_id}

    async def reply(self, token, sequence, alias, choice):
        resolve, params = await write_in_thread(lambda: self.begin_reply(token, sequence, alias, choice))
        receipt = await self.run.manager.answer_decision(resolve, self.run.session_id, params)
        return p.to_wire(receipt)

    @write_intent
    def processed(self, token, sequence, operation_id):
        with self.run.ctx.db() as db:
            record, _ = self.control._current(db, token)
            submission = self.submission
            if (submission is None or self.record is not record or type(sequence) is not int
                    or sequence != record.sequence or submission.frame.sequence != sequence
                    or submission.operation_id != operation_id or not submission.dispatched):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            operation = store.operation(db, self.run.session_id, operation_id, submission.frame.grant.actor)
            if (operation.receipt['state'] != 'accepted'
                    or operation.receipt['payloadDigest'] != p.operation_digest('decision.reply', self.params(submission))):
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            db.commit()
            record.processed = sequence
            record.entered_sequence = None
            self.offer = None
            return True

    async def application_published(self, params):
        submission = self.submission
        if submission is None or params.get('operationId') != submission.operation_id:
            return
        frame = submission.frame
        try:
            await write_in_thread(lambda: self.processed(frame.grant.grant, frame.sequence, submission.operation_id))
            await self.run.peer.call('terminal.approval_processed', {'grant': frame.grant.grant,
                'sequence': frame.sequence, 'operationId': submission.operation_id})
        except (p.RelayError, HTTPException, ConnectionError, TimeoutError):
            pass  # durable application survives lost input authority; never restore a grant

    @write_intent
    def still_valid(self, offer):
        if self.offer is not offer or self.record is None:
            return True
        try:
            self.enabled()
            with self.run.ctx.db() as db:
                record, _ = self.control._current(db, self.record.value.grant)
                submission = self.submission
                if submission is not None and submission.operation_id is not None:
                    op = db.get(RelayOperation, submission.operation_id)
                    if op is None or op.receipt['state'] == 'failed':
                        return False
                    if submission.dispatched and op.receipt['state'] in ('submitting', 'uncertain', 'accepted'):
                        return True  # already reserved original winner; expiry cannot undo it
                self.decision(db, record, offer)
                return True
        except (p.RelayError, HTTPException):
            return False

    def watch(self):
        offer = self.offer
        if offer is None:
            return
        async def observe():
            while self.offer is offer:
                await asyncio.sleep(.25)
                if not await write_in_thread(lambda: self.still_valid(offer)):
                    await self.resolved(offer.decision_id, None)
                    return
        self.watch_task = asyncio.create_task(observe())
        def finished(task):
            if not task.cancelled():
                task.exception()
        self.watch_task.add_done_callback(finished)

    @write_intent
    def remove_offer(self, decision_id, winning_operation):
        offer = self.offer
        if offer is None or offer.decision_id != decision_id:
            return None
        submission = self.submission
        with self.run.ctx.db() as db:
            row = store.lock(db, self.run.session_id)
            if submission is not None and submission.operation_id is not None and submission.operation_id != winning_operation:
                operation = store.operation(db, self.run.session_id, submission.operation_id, submission.frame.grant.actor)
                if operation.receipt['state'] == 'queued':
                    # Main CAS has not reserved this operation, so no provider dispatch
                    # is possible. Never apply this to submitting/uncertain receipts.
                    store.transition(db, row, operation, p.ReceiptState.FAILED, reason=p.FailureReason.CONFLICT)
            decision_row = db.get(RelayDecision, (self.run.session_id, decision_id))
            if decision_row is not None:
                decision = store.decision_from(decision_row.decision)
                if (decision.status is p.DecisionStatus.PENDING
                        and decision.expires_at_ms <= int(time.time() * 1000)):
                    decision_row.decision = p.to_wire(p.advance_decision(decision, p.DecisionStatus.EXPIRED))
                    store.append(db, row, 'decision.changed', {'decision': decision_row.decision}, decision_id=decision_id)
            db.commit()
        self.offer = None
        return {'alias': offer.alias, 'decisionId': decision_id, 'winningOperationId': winning_operation}

    async def resolved(self, decision_id, winning_operation):
        async with self.lifecycle_lock:
            value = await write_in_thread(lambda: self.remove_offer(decision_id, winning_operation))
            if value is None:
                return
            try:
                await self.run.peer.call('terminal.approval_withdraw', value)
            except (p.RelayError, ConnectionError, TimeoutError):
                pass  # local tombstone survives lost notification; never resend a key
