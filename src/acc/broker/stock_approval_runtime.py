"""Explicit immutable stock command offers; no native request ID or person inference."""
from __future__ import annotations

import asyncio
import json

from acc.session_relay import protocol as p


class StockApprovals:
    def __init__(self, runtime):
        self.runtime = runtime
        self.pending = {}
        self.lock = asyncio.Lock()

    async def queued(self, identifier, event, digest):
        from acc.session_relay.stock_approval_input import STOCK_APPROVAL_INPUT_QUALIFIED
        if not STOCK_APPROVAL_INPUT_QUALIFIED or event.get('method') != 'item/commandExecution/requestApproval':
            return
        decision = self.runtime.adapter.decisions[identifier]
        if not {'accept', 'cancel'} <= decision['choices']:
            return
        fields = {'threadId', 'turnId', 'itemId', 'command', 'cwd', 'reason', 'startedAtMs'}
        params = {key: value for key, value in event['params'].items() if key in fields}
        if not isinstance(params.get('command'), str) or 'startedAtMs' not in params:
            return  # unqualified profiles remain Chat-only
        if len(self.pending) >= p.MAX_PENDING_DECISIONS:
            raise p.RelayError(p.ErrorCode.LIMIT)
        self.pending[identifier] = (p.canonical_json(params), digest)
        await self.ready()

    async def ready(self):
        from acc.session_relay.stock_approval_input import STOCK_APPROVAL_INPUT_QUALIFIED
        if not STOCK_APPROVAL_INPUT_QUALIFIED:
            return
        r = self.runtime
        async with self.lock:
            host = r.terminal_host
            if (host is None or host.input_fenced or host.retired or host.partial or host.grant is None
                    or host.ticket or host.draft or host.processed == 0 or host.admitted != host.processed
                    or host.approval_input.offer is not None or host.approval_input.ticket is not None):
                return
            # Arrival order chooses one explicit immutable offer to display.
            # It never chooses the identity of a decoded response or its actor.
            for identifier in tuple(self.pending):
                encoded, digest = self.pending.pop(identifier)
                decision = r.adapter.decisions[identifier]
                if decision['sent'] or decision['expires'] <= r.adapter.clock():
                    continue
                try:
                    await r.native_socket.offer_approval(host, decision_id=identifier, action_digest=digest,
                        revision=decision['revision'], params=json.loads(encoded), choices=frozenset({'accept', 'cancel'}))
                except (p.RelayError, ConnectionError, TimeoutError):
                    pass  # one offer attempt only; never retry a key or permission answer
                return

    async def withdraw(self, params):
        host = self.runtime.terminal_host
        if host is None or not hasattr(host, 'approval_input'):
            return False
        async with host.input_lock:
            component = host.approval_input
            offer = component.offer
            if offer is None or (offer.alias, offer.decision_id) != (params['alias'], params['decisionId']):
                return False
            ticket = component.ticket
            if ticket is not None and ticket.operation_id != params['winningOperationId']:
                ticket.invalidated = True
                host.input_fenced = True
            component.offer = None
        return await self.runtime.native_socket.withdraw_approval(host, offer)


def service(runtime):
    if not hasattr(runtime, 'stock_approvals'):
        runtime.stock_approvals = StockApprovals(runtime)
    return runtime.stock_approvals
