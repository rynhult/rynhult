"""Strict text-only Claude native draft/processing boundary; no transport authority.

Only the owned Host can reserve frames and supply authenticated native decoder
observations plus main's conclusive receipt. Stop/quiet output/socket drain has
no method here. This component alone cannot enable native input or attestation.
"""
from __future__ import annotations

from . import protocol as p
from .adapters.base import Watermark
from .text_policy import REFUSED_PREFIXES


def project_text_frame(prefix, data):
    if type(data) is not bytes or not 0 < len(data) <= p.MAX_TERMINAL_INPUT_BYTES:
        raise p.RelayError(p.ErrorCode.LIMIT)
    candidate, submit = prefix, False
    for index, byte in enumerate(data):
        if byte in (8, 127):
            if not candidate:
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            candidate = candidate[:-1]
        elif 32 <= byte <= 126:
            candidate += chr(byte)
        elif byte == 13 and index == len(data) - 1 and candidate.strip():
            submit = True
        else:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        if candidate.lstrip().startswith(REFUSED_PREFIXES) or len(candidate.encode()) > p.MAX_PROMPT_BYTES:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    return candidate, submit


class ClaudeDraft:
    def __init__(self, grant, native_id):
        if grant.actor.kind is not p.ActorKind.PERSON or not p.valid_opaque(native_id):
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        self.grant, self.native_id = grant, native_id
        self.admitted = self.processed = 0
        self.draft = ''
        self.ticket = None
        self.decoded = False
        self.operation = None
        self.completed = set()
        self.verified_empty = False
        self.frozen = self.partial = False
        self.retained_client = None

    def reserve(self, grant, sequence, data):
        if (grant != self.grant or self.frozen or self.partial or type(sequence) is not int
                or sequence != self.admitted + 1 or self.ticket is not None):
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        candidate, submit = project_text_frame(self.draft, data)
        # Reserve before transport IO. A partial/cancelled write must poison this
        # object, never roll back the sequence and invite a resend.
        self.draft, self.admitted, self.verified_empty = candidate, sequence, False
        if submit:
            self.ticket = sequence, candidate
            self.decoded, self.operation = False, None
        return Watermark(grant.epoch, self.admitted, self.processed)

    def poison(self):
        self.partial = self.frozen = True

    def observe_prompt(self, native_id, prompt_digest):
        if (self.partial or self.frozen or self.ticket is None or self.decoded
                or native_id != self.native_id or prompt_digest != p.sha256_digest(self.ticket[1].encode())):
            self.poison()
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        self.decoded = True
        # Do not clear a draft or advance processing on a pre-submit hook alone.

    def bind_operation(self, operation):
        if (self.ticket is None or not self.decoded or self.operation is not None
                or operation.operation_id in self.completed or len(self.completed) >= 512
                or operation.method != 'turn.submit'
                or operation.session_id != self.grant.run.session_id or operation.run_id != self.grant.run.run_id
                or operation.actor != self.grant.actor or operation.fence != self.grant.fence
                or operation.params.get('text') != self.ticket[1]
                or operation.params.get('expectedControlRevision') != self.grant.epoch
                or operation.payload_digest != p.operation_digest('turn.submit', operation.params)):
            self.poison()
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        self.operation = operation

    def confirm(self, receipt):
        operation = self.operation
        if (self.partial or self.frozen or not self.decoded or operation is None
                or receipt.operation_id != operation.operation_id or receipt.actor != operation.actor
                or receipt.state is not p.ReceiptState.ACCEPTED):
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        p.check_registered(receipt, actor=operation.actor, session_id=operation.session_id,
            run_id=operation.run_id, method=operation.method, payload_digest=operation.payload_digest)
        self.completed.add(operation.operation_id)
        self.processed = self.ticket[0]
        self.draft, self.ticket, self.operation = '', None, None
        self.verified_empty = True

    def drain(self, grant):
        if (grant != self.grant or self.partial or not self.verified_empty or self.draft or self.ticket is not None
                or self.admitted != self.processed):
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        self.frozen = True
        return Watermark(grant.epoch, self.admitted, self.processed)

    def freeze_for_handover(self, grant):
        if grant != self.grant or self.partial or self.ticket is not None or self.operation is not None:
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        if self.retained_client is not None:
            return Watermark(grant.epoch,self.admitted,self.processed)
        if self.admitted==self.processed and not self.draft:
            return self.drain(grant)
        from .claude_retained_client import RetainedClaudeClient
        # A positive processed boundary must precede these unsent text frames.
        # No Enter, partial write or first-use uncertainty can become a draft.
        self.retained_client=RetainedClaudeClient.capture(grant,self.native_id,
            self.admitted,self.processed,self.draft)
        self.frozen=True
        return Watermark(grant.epoch,self.admitted,self.processed)
