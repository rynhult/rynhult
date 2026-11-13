"""Conservative text-only native input over the existing framed TerminalHost.

Every native mutation consumes one ticket captured from an authenticated input
stream. Neither the native connection nor the current holder supplies its actor.
No socket drain, screen heuristic or timer is a processing acknowledgement.
This component does not attest a production placement or enable Terminal.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from . import protocol as p
from .terminal_host import TmuxTerminalHost
from .writer_lease import WriterRetirement
from .writer_routes import WriterRoute


@dataclass(repr=False)
class Ticket:
    grant: object
    route: WriterRoute
    sequence: int
    text: str
    inflight: bool = False


class NativeInputHost(TmuxTerminalHost):
    def __init__(self, run, opener, validate_input, validate_geometry, *, validate_read,
                 routes, idle, processed, capacity=512, consume_output=True, approval_bridge=None, approval_ready=None):
        async def held(grant):
            if not await validate_input(grant):
                return False
            component = self.approval_input
            return await component.authorized(grant) if component.writing else True
        # Production input-only bridges drain internally; an explicitly owned
        # read consumer may instead own the duplex stream's single reader.
        super().__init__(run, opener, held, validate_geometry, validate_read=validate_read, consume_output=consume_output)
        self.routes, self.idle, self.on_processed = routes, idle, processed
        from .stock_approval_input import StockApprovalInput
        self.approval_input = StockApprovalInput(self, approval_bridge)
        self.approval_ready = approval_ready
        self.capacity = capacity
        self.seen = set()
        self.input_lock = asyncio.Lock()
        self.route = self.ticket = None
        self.draft = ''
        self.input_fenced = True
        self.retired = False
        self.attested = False  # placement, main ingress and all R1 gates still required

    async def claim_input(self, grant):
        async with self.input_lock:
            if self.retired:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if not await self.validate_input(grant):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            lease = self.routes.gate.lease
            self.routes.gate.admit(lease, actor=grant.actor, view='terminal')
            if lease.epoch != grant.epoch or self.partial:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if self.grant == grant:
                if self.input_fenced or self.route is None or self.route.lease != lease:
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                return
            if self.draft or self.ticket:
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            await super().claim_input(grant)
            self.routes.gate.admit(lease, actor=grant.actor, view='terminal')
            self.route = WriterRoute(lease, 'terminal', grant.actor)
            self.input_fenced = False

    def _draft(self, data):
        from .claude_draft import project_text_frame
        return project_text_frame(self.draft, data)

    async def write_frames(self, grant, sequence, data):
        if type(data) is not bytes or not 0 < len(data) <= p.MAX_TERMINAL_INPUT_BYTES:
            raise p.RelayError(p.ErrorCode.LIMIT)
        async with self.input_lock:
            if self.input_fenced or self.grant != grant or self.route is None:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            self.routes.gate.admit(self.route.lease, actor=grant.actor, view='terminal')
            if self.ticket:
                raise p.RelayError(p.ErrorCode.BUSY)
            if self.approval_input.offer is not None:
                return await self.approval_input.write(grant, sequence, data)
            candidate, submit = self._draft(data)
            # Reserve before transport awaits. A late native request retains this
            # exact grant/actor/lease/prefix even if another person tries to take.
            ticket = Ticket(grant, self.route, sequence, candidate) if submit else None
            old_draft = self.draft
            self.draft, self.ticket = candidate, ticket
            try:
                result = await super().write_frames(grant, sequence, data)
            except BaseException:
                self.input_fenced = True
                raise
            if result.partial:
                self.input_fenced = True
            elif result.watermark.admitted != sequence:
                self.draft, self.ticket = old_draft, None
                self.input_fenced = True
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            return result

    async def offer_native_approval(self, offer):
        return await self.approval_input.offered(offer)

    async def native_approval_response(self, offer, choice):
        return await self.approval_input.decoded(offer, choice)

    async def native_request(self, method, params, message_id):
        async with self.input_lock:
            ticket = self.ticket
            if not p.valid_opaque(message_id) or message_id in self.seen:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if self.input_fenced or ticket is None or ticket.inflight:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if method != 'turn.submit' or params != {'text': ticket.text}:
                self.input_fenced = True
                self.routes.close()
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            if len(self.seen) >= self.capacity:
                self.input_fenced = True
                raise p.RelayError(p.ErrorCode.LIMIT)
            if not await self.validate_input(ticket.grant):
                self.input_fenced = True
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            ticket.inflight = True
            self.seen.add(message_id)  # bounded; exhaustion refuses rather than forgetting old IDs
        try:
            result = await self.routes.mutate(ticket.route, method, params)
            if result.get('state') not in ('accepted', 'failed'):
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            if await self.on_processed(ticket.grant, ticket.sequence, result) is not True:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            async with self.input_lock:
                if (self.ticket is not ticket or not await self.validate_input(ticket.grant)
                        or self.input_fenced or self.partial):
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                self.processed = ticket.sequence
                self.draft, self.ticket = '', None
            if self.approval_ready is not None:
                await self.approval_ready()
            return result
        except BaseException:
            self.input_fenced = True
            self.routes.close()
            raise

    async def retire(self, lease):
        async with self.input_lock:
            self.input_fenced = True
            if self.partial or self.ticket or self.approval_input.offer or self.approval_input.ticket or (not self.draft and self.admitted != self.processed):
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            if lease.view == 'terminal' and (self.route is None or self.route.lease != lease):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            idle = await self.idle()
            if idle is not True:
                raise p.RelayError(p.ErrorCode.BUSY)
            self.retired = True  # irreversible, including future grants for this Host
            return WriterRetirement(lease.handle, True, not self.draft, not self.routes.unsettled,
                                    self.input_fenced, self.routes.gate.execution_identity)
