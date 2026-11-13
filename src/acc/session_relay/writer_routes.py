"""Immutable mutation routes for the approved single-writer Codex mode.

Retired routes stay readable. A route can never be rebound to a later lease;
new terminal writer generations require a separately owned/verified native route.
There is no actor or input-watermark inference from a native request.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import protocol as p
from .writer_lease import ExclusiveWriterGate


@dataclass(frozen=True)
class WriterRoute:
    lease: object
    view: str
    actor: p.Actor


class WriterRoutes:
    def __init__(self, *, run_id, execution_identity, authorize, retire, admit, read):
        self.gate = ExclusiveWriterGate(run_id=run_id, execution_identity=execution_identity)
        self.authorize, self.retire, self.admit, self.read = authorize, retire, admit, read
        self.unsettled = set()
        self.closed = False

    def snapshot(self):
        state = self.gate.snapshot()
        lease = self.gate.lease
        return {**state, 'holder': ({'kind': lease.actor.kind.value, 'principal': lease.actor.principal,
                                   'channel': lease.actor.channel.value} if lease else None)}

    async def take(self, actor, view):
        if self.closed or actor.kind is not p.ActorKind.PERSON:
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        if self.unsettled:
            raise p.RelayError(p.ErrorCode.BUSY)
        lease = await self.gate.activate(actor=actor, view=view, authorize=self.authorize, retire=self.retire, renew=True)
        return WriterRoute(lease, view, actor)

    async def observe(self, method, params):
        # Caller supplies the existing scoped/read-authorized projection. Reads
        # never activate a writer, restart a provider or acquire the input lock.
        return await self.read(method, params)

    async def mutate(self, route, method, params):
        if self.closed or method not in ('turn.submit', 'turn.interrupt'):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        fields = {'text'} if method == 'turn.submit' else {'turnId'}
        if params.keys() != fields or not all(isinstance(value, str) and value for value in params.values()):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        if self.unsettled:
            raise p.RelayError(p.ErrorCode.BUSY)
        async with self.gate.lock:
            self.gate.admit(route.lease, actor=route.actor, view=route.view)
            if not await self.authorize(route.actor):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            self.gate.admit(route.lease, actor=route.actor, view=route.view)
            # Hub registers the immutable actor/run/full digest and rechecks at
            # its provider barrier. A lost receipt fences take-over, never retry.
            marker = route.lease.handle
            self.unsettled.add(marker)
            result = await self.admit(route.actor, method, {**params, 'expectedControlRevision': route.lease.epoch})
            if result.get('state') in ('accepted', 'failed'):
                self.unsettled.discard(marker)
            return result

    def close(self):
        self.closed = True
        self.gate.recovery_required = True
