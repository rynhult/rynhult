"""Private stock-key input. The main frame ticket, never the native peer, supplies authority."""
from __future__ import annotations

from dataclasses import dataclass

from . import protocol as p
from .terminal_host import TmuxTerminalHost

STOCK_APPROVAL_INPUT_QUALIFIED = False


@dataclass
class KeyTicket:
    offer: object
    grant: object
    route: object
    sequence: int
    choice: str
    operation_id: str
    inflight: bool = False
    invalidated: bool = False


class StockApprovalInput:
    def __init__(self, host, bridge):
        self.host, self.bridge = host, bridge
        self.offer = self.ticket = None
        self.writing = False

    def enabled(self):
        if STOCK_APPROVAL_INPUT_QUALIFIED is not True or self.bridge is None:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)

    async def offered(self, offer):
        self.enabled()
        h = self.host
        async with h.input_lock:
            if (self.offer is not None or self.ticket is not None or h.input_fenced or h.retired or h.partial
                    or h.grant is None or h.route is None or h.draft or h.ticket
                    or h.processed == 0 or h.admitted != h.processed or offer.choices != frozenset({'accept', 'cancel'})):
                raise p.RelayError(p.ErrorCode.BUSY)
            grant, route = h.grant, h.route
            h.routes.gate.admit(route.lease, actor=grant.actor, view='terminal')
            value = {'alias': offer.alias, 'decisionId': offer.decision_id, 'actionDigest': offer.action_digest,
                'revision': offer.revision, 'choices': sorted(offer.choices)}
            if await self.bridge('native.approval_offer', {'grant': grant.grant, 'offer': value}) is not True:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if not await h.validate_input(grant) or h.grant != grant or h.route is not route:
                h.input_fenced = True
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            h.routes.gate.admit(route.lease, actor=grant.actor, view='terminal')
            self.offer = offer
            return True

    async def write(self, grant, sequence, data):
        # Caller holds input_lock; main has already captured/registered this frame.
        self.enabled()
        h = self.host
        choice = {b'\r': 'accept', b'\x1b': 'cancel'}.get(data)
        if choice is None or self.ticket is not None or h.draft or h.ticket:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        offer, route = self.offer, h.route
        value = await self.bridge('native.approval_frame', {'grant': grant.grant, 'sequence': sequence,
            'alias': offer.alias, 'choice': choice})
        if (not isinstance(value, dict) or not p.valid_opaque(value.get('operationId'))
                or value != {'operationId': value['operationId'], 'sessionId': h.run.session_id,
                    'expectedRunId': h.run.run_id, 'decisionId': offer.decision_id, 'choiceId': choice,
                    'expectedRevision': offer.revision, 'actionDigest': offer.action_digest}
                or not await h.validate_input(grant) or h.grant != grant or h.route is not route):
            h.input_fenced = True
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        h.routes.gate.admit(route.lease, actor=grant.actor, view='terminal')
        self.ticket = KeyTicket(offer, grant, route, sequence, choice, value['operationId'])
        self.writing = True
        try:
            result = await TmuxTerminalHost.write_frames(h, grant, sequence, data)
        except BaseException:
            h.input_fenced = True
            raise
        finally:
            self.writing = False
        if result.partial:
            h.input_fenced = True
        return result

    async def authorized(self, grant):
        # TmuxTerminalHost invokes this after connect/held awaits, immediately
        # before its write. Cancellation or a Chat winner cannot borrow an old proof.
        ticket = self.ticket
        if ticket is None or ticket.grant != grant or ticket.invalidated:
            return False
        value = await self.bridge('native.approval_frame', {'grant': grant.grant,
            'sequence': ticket.sequence, 'alias': ticket.offer.alias, 'choice': ticket.choice})
        return value == {'operationId': ticket.operation_id, 'sessionId': self.host.run.session_id,
            'expectedRunId': self.host.run.run_id, 'decisionId': ticket.offer.decision_id,
            'choiceId': ticket.choice, 'expectedRevision': ticket.offer.revision,
            'actionDigest': ticket.offer.action_digest}

    async def decoded(self, offer, choice):
        self.enabled()
        h = self.host
        async with h.input_lock:
            ticket = self.ticket
            if (ticket is None or ticket.inflight or ticket.invalidated or ticket.offer is not offer or ticket.choice != choice
                    or h.input_fenced or h.grant != ticket.grant or h.route is not ticket.route):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            try:
                h.routes.gate.admit(ticket.route.lease, actor=ticket.grant.actor, view='terminal')
                if not await h.validate_input(ticket.grant):
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            except BaseException:
                # This exact ticket has already crossed the byte boundary.
                # Refusal/cancellation must fence locally before the slower
                # lifecycle watch withdraws its offer; never imply processing.
                h.input_fenced = True
                raise
            ticket.inflight = True
        try:
            value = await self.bridge('native.approval_reply', {'grant': ticket.grant.grant,
                'sequence': ticket.sequence, 'alias': offer.alias, 'choice': choice})
            if value.get('operationId') != ticket.operation_id:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            if value.get('state') == 'accepted':
                await self.completed(ticket.grant.grant, ticket.sequence, ticket.operation_id)
            return value
        finally:
            async with h.input_lock:
                if self.ticket is ticket:
                    h.input_fenced = True  # uncertain/CAS loser never advances processing or permits replay

    async def completed(self, token, sequence, operation_id):
        h = self.host
        async with h.input_lock:
            ticket = self.ticket
            if (ticket is None or ticket.invalidated or not ticket.inflight or ticket.grant.grant != token
                    or ticket.sequence != sequence or ticket.operation_id != operation_id):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if await self.bridge('native.approval_processed', {'grant': token, 'sequence': sequence,
                    'operationId': operation_id}) is not True:
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            if h.grant != ticket.grant or h.route is not ticket.route or not await h.validate_input(ticket.grant) or h.partial or h.retired:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            h.routes.gate.admit(ticket.route.lease, actor=ticket.grant.actor, view='terminal')
            h.processed = sequence
            h.input_fenced = False
            self.offer = self.ticket = None
            return True
