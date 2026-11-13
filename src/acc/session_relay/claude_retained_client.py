"""Immutable old Claude input model. It has no input or replay authority.

The previous native execution owner must still be stopped and reaped by L1.
This is the retained read-only client data, not a live provider or SDK owner.
Draft text is ephemeral and must never enter durable events or a new input.
"""
from dataclasses import dataclass, field

from . import protocol as p


@dataclass(frozen=True,repr=False)
class RetainedClaudeClient:
    session_id: str
    run_id: str
    native_id: str
    attachment_id: str
    actor: p.Actor
    epoch: int
    admitted: int
    processed: int
    draft: str
    read_only: bool = field(default=True,init=False)

    def __post_init__(self):
        if (not all(p.valid_opaque(v) for v in (self.session_id,self.run_id,self.native_id,self.attachment_id))
                or self.actor.kind is not p.ActorKind.PERSON
                or any(type(v) is not int for v in (self.epoch,self.admitted,self.processed))
                or not 0 < self.epoch or not 0 < self.processed < self.admitted
                or type(self.draft) is not str or len(self.draft.encode())>p.MAX_PROMPT_BYTES
                or any(not 32<=ord(c)<=126 for c in self.draft)):
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)

    @classmethod
    def capture(cls,grant,native_id,admitted,processed,draft):
        return cls(grant.run.session_id,grant.run.run_id,native_id,grant.attachment_id,
            grant.actor,grant.epoch,admitted,processed,draft)

    def proof(self):
        # Bind exact old client/run/person/text without sending its unsent text
        # through ownership checkpoints or durable event/audit records.
        return {'epoch':self.epoch,'admitted':self.admitted,'processed':self.processed,
            'draftDigest':p.sha256_digest(p.canonical_json({'sessionId':self.session_id,'runId':self.run_id,
                'nativeId':self.native_id,'attachmentId':self.attachment_id,'actor':p.to_wire(self.actor),
                'epoch':self.epoch,'admitted':self.admitted,'processed':self.processed,'draft':self.draft}))}
