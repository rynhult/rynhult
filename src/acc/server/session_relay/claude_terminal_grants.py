"""Person/attachment-bound input authority for the current Claude L1 owner.

The public qualification gate remains closed. Neither an ownership switch nor a
read attachment creates this grant. Native submit/settlement needs its separate
qualified hook boundary; it must never fall back to a second SDK dispatch.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import replace

from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import InputGrant, TerminalRun

from ..db import write_in_thread, write_intent
from ..models import RelayOperation, RelayOwnership
from . import authority, store, writers
from .hub import Hub
from .terminal_grants import Stream, TerminalGrants


class ClaudeTerminalGrants(TerminalGrants):
    def __init__(self, run):
        super().__init__(run)
        self.native_operations = {}
        self.entered_sequence = None
        self.private_draft = ''
        self.retained_clients = {}  # bounded ephemeral old-client data; never native submission input

    def handover_client(self):
        record=self.record
        if record is None:return None
        if record.pending or self.entered_sequence is not None or record.entered_sequence is not None:
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        if record.sequence==record.processed:return None
        if (record.phase!='active' or self.private_draft!=record.private_draft
                or len(self.retained_clients)>=16):
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        from acc.session_relay.claude_retained_client import RetainedClaudeClient
        return RetainedClaudeClient.capture(record.value,self.run.binding.native_conversation,
            record.sequence,record.processed,self.private_draft)

    def queued_dispatch_allowed(self, token):
        return token not in self.native_operations

    def invalidate(self):
        super().invalidate()
        self.entered_sequence = None
        self.private_draft = ''

    @write_intent
    def frame(self, resolve, token, epoch, sequence, data):
        with self.run.ctx.db() as db:
            record, _ = self._current(db, token, resolve=resolve)
            from .native_prompt import current
            if current(db, self.run.session_id):
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            if self.entered_sequence is not None and record.processed < self.entered_sequence:
                raise p.RelayError(p.ErrorCode.BUSY)
        from acc.session_relay.claude_draft import project_text_frame
        candidate, submit = project_text_frame(self.private_draft, data)
        super().frame(resolve, token, epoch, sequence, data)
        self.private_draft = candidate
        if submit:
            self.entered_sequence = sequence

    def _current(self, db, token, *, resolve=None):
        record = self.record
        if (record is None or not p.valid_opaque(token) or not secrets.compare_digest(token, record.value.grant)
                or record.phase not in ('claiming', 'active') or record.expires <= time.monotonic()):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        row, current = self.run.check_run(db, resolve=record.resolve)
        owner = db.get(RelayOwnership, self.run.session_id)
        if (not writers.public_ready(self.run.ctx, row) or not owner or owner.mode != 'exclusive_idle'
                or owner.phase != 'owned' or owner.view != 'terminal'
                or owner.source_run_id != self.run.run_id
                or owner.authority.get('actor') != p.to_wire(record.value.actor)
                or owner.authority.get('writerAttachment') != record.value.attachment_id
                or owner.authority.get('inputEpoch') != record.value.epoch
                or record.value.run != TerminalRun(self.run.session_id, str(self.run.run_id))
                or row.control_revision != record.value.epoch or self.run.fence != record.value.fence):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        authority.recheck(record.authority, current)
        if resolve is not None:
            _, caller = self.run.check_run(db, resolve=resolve)
            authority.recheck(record.authority, caller)
            if getattr(resolve, 'attachment_id', None) != record.value.attachment_id:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return record, row

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
            attachment = getattr(grant.resolve, 'attachment_id', None)
            if (not writers.public_ready(self.run.ctx, row) or not owner or owner.mode != 'exclusive_idle'
                    or owner.phase != 'owned' or owner.view != 'terminal' or owner.source_run_id != self.run.run_id
                    or owner.authority.get('actor') != p.to_wire(current.actor)
                    or not p.valid_opaque(attachment) or op.params.get('attachmentId') != attachment
                    or type(op.params.get('expectedRevision')) is not int
                    or row.control_revision != op.params['expectedRevision']):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if self.record is not None:
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            writers.pending(db, row, op.operation_id)
            # L1 owner replacement does not use the Codex writer revision. An
            # explicit claim advances its own never-renewed stream generation.
            epoch = max(row.control_revision, self.last_epoch) + 1
            value = InputGrant(TerminalRun(self.run.session_id, str(self.run.run_id)), attachment,
                               current.actor, secrets.token_hex(32), epoch, self.run.fence)
            store.transition(db, row, db.get(RelayOperation, op.operation_id), p.ReceiptState.SUBMITTING)
            row.control_revision = epoch
            owner.authority = {**owner.authority, 'writerAttachment': attachment, 'inputEpoch': epoch}
            record = Stream(value, grant.resolve, current, op.operation_id, time.monotonic() + 3600)
            db.commit()
            self.record, self.last_epoch = record, epoch
            return value.grant

    def native_register(self, *args):
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)

    async def native_submit(self, *args):
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)

    async def prepare_native(self, token, sequence, text, revision):
        async with self.run.submit_lock:
            return await write_in_thread(lambda: self._prepare_native(token, sequence, text, revision))

    @write_intent
    def _prepare_native(self, token, sequence, text, revision):
        if sequence != self.entered_sequence or len(self.native_operations) >= 512:
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        resolve, existing = TerminalGrants.native_register(self, token, sequence, text, revision)
        if existing:
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)  # no renewed native hook permission
        submission = resolve.submission
        handle = self.run.issue(resolve, submission.operation_id, submission.params)
        if not isinstance(handle, str):
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        self.native_operations[handle] = {'operation': self.run.grants[handle].operation,
            'resolve': resolve, 'claimed': False, 'native_ref': None}
        return {'grant': handle}

    @write_intent
    def claim_native(self, handle):
        entry = self.native_operations.get(handle)
        if entry is None or entry['claimed']:
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        operation, resolve = entry['operation'], entry['resolve']
        value = self.run.resolve_grant(handle)
        if 'staging' not in value or value['receiptState'] != 'queued':
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        Hub(self.run.ctx).queue(resolve, self.run.session_id, operation.operation_id,
            expected_run=operation.run_id, digest=operation.payload_digest, if_busy='reject')
        receipt = Hub(self.run.ctx).claim(resolve, self.run.session_id, operation.operation_id)
        if receipt.state is not p.ReceiptState.SUBMITTING:
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        entry['claimed'] = True
        return self.run.resolve_grant(handle)

    @write_intent
    def complete_native(self, handle, digest, native_ref):
        entry = self.native_operations.get(handle)
        if (entry is None or not entry['claimed'] or digest != entry['operation'].payload_digest
                or not isinstance(native_ref, str) or len(native_ref) > 512
                or not native_ref.startswith(self.run.binding.native_conversation + ':')
                or (entry['native_ref'] is not None and entry['native_ref'] != native_ref)):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        operation = entry['operation']
        # Conclusive evidence settles its original receipt even after revocation;
        # processing/turn completion below still requires the current input grant.
        with self.run.ctx.db() as db:
            row = store.lock(db, self.run.session_id)
            op = store.operation(db, self.run.session_id, operation.operation_id, operation.actor)
            if op.receipt['payloadDigest'] != digest:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            if op.receipt['state'] not in ('submitting', 'uncertain', 'accepted'):
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            if op.receipt['state'] != 'accepted':
                store.settle(db, row, op, accepted=True, evidence=p.Evidence(
                    p.EvidenceKind.PROVIDER_ADMISSION, operation.operation_id, operation.run_id, native_ref))
            elif entry['native_ref'] is None:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            receipt = store.receipt_from(op.receipt)
            db.commit()
        entry['native_ref'], entry['resolve'] = native_ref, None
        entry['operation'] = replace(operation, params={})
        self.run.grants.pop(handle, None)
        return p.to_wire(receipt)

    @write_intent
    def native_processed(self, token, sequence, operation_id):
        if not any(e['operation'].operation_id == operation_id and e['native_ref'] for e in self.native_operations.values()):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        TerminalGrants.native_processed(self, token, sequence, operation_id)
        with self.run.ctx.db() as db:
            record, row = self._current(db, token)
            store.complete_turn(db, row, operation_id)
            db.commit()
        self.run.turn_origins.pop(operation_id, None)
        self.entered_sequence = None
        self.private_draft = ''
        return True
