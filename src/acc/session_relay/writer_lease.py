"""Protocol-independent single-writer gate; views can remain readable.

A prospective Codex mode retires the old mutation route/lease, retaining
read subscriptions in both views and one private app-server/thread. It does not need native per-keystroke correlation:
late messages keep the retired lease and are refused. No capability is enabled.
This is distinct from Claude's stop/resume of its execution-owner process.
"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass

from .protocol import Actor, ErrorCode, RelayError


@dataclass(frozen=True)
class WriterLease:
    handle: str
    run_id: str
    view: str
    actor: Actor
    epoch: int


@dataclass(frozen=True)
class WriterRetirement:
    handle: str
    idle: bool
    draft_empty: bool
    requests_settled: bool
    channel_closed: bool  # Mutation route closed; the read subscription stays live.
    # This identifies the retained execution owner. Killing/replacing it is not
    # evidence for a Codex writer-only handover.
    execution_identity: str
    retired_client: str | None = None
    fresh_client: str | None = None
    draft_retained: bool = False
    draft_retained_readonly: bool = False
    new_input_empty: bool = False


class ExclusiveWriterGate:
    def __init__(self, *, run_id, execution_identity):
        self.run_id, self.execution_identity = run_id, execution_identity
        self.lease = None
        self.epoch = 0
        self.transition = False
        self.recovery_required = False
        self.lock = asyncio.Lock()

    def snapshot(self):
        # Reads/attach/preferences cannot acquire control or start a provider.
        return {'writer': self.lease.view if self.lease else None, 'epoch': self.epoch,
                'transition': self.transition, 'recoveryRequired': self.recovery_required}

    def admit(self, lease, *, actor, view):
        if (self.transition or self.recovery_required or self.lease != lease or
                lease is None or lease.actor != actor or lease.view != view or lease.run_id != self.run_id):
            raise RelayError(ErrorCode.STALE_AUTHORITY)

    async def activate(self, *, actor, view, authorize, retire, renew=False):
        if view not in ('chat', 'terminal'):
            raise RelayError(ErrorCode.UNSUPPORTED)
        if self.lock.locked():
            raise RelayError(ErrorCode.BUSY)
        async with self.lock:
            if self.recovery_required or await authorize(actor) is not True:
                raise RelayError(ErrorCode.POLICY_REFUSED)
            if not renew and self.lease and self.lease.actor == actor and self.lease.view == view:
                return self.lease
            self.transition = True
            try:
                if self.lease:
                    # Nonempty drafts require permanent client retirement and a
                    # distinct fresh client, never erasure or a processing guess.
                    # No socket-drain or current-holder inference is accepted.
                    proof = await retire(self.lease)
                    retained = ((proof.draft_retained is True) and bool(proof.retired_client)
                                and bool(proof.fresh_client) and proof.retired_client != proof.fresh_client)
                    retained = retained or (proof.draft_retained_readonly is True and proof.new_input_empty is True)
                    if (proof.handle != self.lease.handle or not proof.idle or not (proof.draft_empty or retained) or
                            not proof.requests_settled or not proof.channel_closed or
                            proof.execution_identity != self.execution_identity):
                        self.recovery_required = True
                        raise RelayError(ErrorCode.INPUT_CONFLICT)
                if await authorize(actor) is not True:
                    self.recovery_required = True
                    raise RelayError(ErrorCode.STALE_AUTHORITY)
                self.epoch += 1
                self.lease = WriterLease(secrets.token_hex(32), self.run_id, view, actor, self.epoch)
                return self.lease
            except BaseException:
                # Once retirement was attempted, an exception cannot restore a
                # possibly closed old channel or automatically grant a new one.
                if self.lease:
                    self.recovery_required = True
                raise
            finally:
                self.transition = False
