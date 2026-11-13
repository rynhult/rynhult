"""Owned-client approval aliases. This boundary grants no input or actor authority.

The route must independently bind a pre-byte authenticated frame and submit its
original Hub operation. Aliases never contain or forward a provider request ID.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

from . import protocol as p

NATIVE_APPROVAL_DECODER_QUALIFIED = False


@dataclass(frozen=True)
class ApprovalOffer:
    alias: str
    decision_id: str
    action_digest: str
    revision: int
    choices: frozenset[str]
    params_json: str


@dataclass(frozen=True)
class BoundOffer:
    peer: object
    route: object
    offer: ApprovalOffer


class ApprovalAliases:
    def __init__(self, native_id, *, capacity=16, lifetime_capacity=512):
        self.native_id = native_id
        self.capacity, self.lifetime_capacity = capacity, lifetime_capacity
        self.pending = {}
        self.used = set()
        self.reserved = {}
        # Strong references prevent peer/route identity reuse. Bounded by used.
        self.history = {}

    async def offer(self, peer, route, *, decision_id, action_digest, revision, params, choices):
        if (route is None or not hasattr(route, 'offer_native_approval')
                or not hasattr(route, 'native_approval_response') or not p.valid_opaque(decision_id)
                or not p.valid_digest(action_digest) or type(revision) is not int or revision < 0
                or not isinstance(params, dict) or params.get('threadId') != self.native_id
                or params.keys() - {'threadId', 'turnId', 'itemId', 'command', 'cwd', 'reason', 'startedAtMs'}
                or not p.valid_opaque(params.get('turnId')) or not p.valid_opaque(params.get('itemId'))
                or type(params.get('startedAtMs')) is not int or params['startedAtMs'] < 0
                or not isinstance(params.get('command'), str) or not params['command']
                or not isinstance(choices, frozenset) or not choices or choices - {'accept', 'cancel'}):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        if any(bound.peer is peer and bound.route is route and bound.offer.decision_id == decision_id
               for bound in self.history.values()):
            raise p.RelayError(p.ErrorCode.BUSY)
        if len(self.pending) + len(self.reserved) >= self.capacity or len(self.used) >= self.lifetime_capacity:
            raise p.RelayError(p.ErrorCode.LIMIT)
        alias = secrets.token_hex(16)
        if alias in self.used:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        # Preserve the reviewed basic command context. Scoped/native amendment
        # responses are refused by the response parser, regardless of UI options.
        projection = json.loads(p.canonical_json(params))
        encoded = p.canonical_json(projection)
        if len(encoded) > p.MAX_FRAME_BYTES - 1024:
            raise p.RelayError(p.ErrorCode.LIMIT)
        offer = ApprovalOffer(alias, decision_id, action_digest, revision, choices, encoded.decode())
        bound = BoundOffer(peer, route, offer)
        self.used.add(alias)  # never reuse even a refused/retired alias
        self.history[alias] = bound
        self.reserved[alias] = bound  # reserve identity/capacity before consent awaits
        try:
            if await route.offer_native_approval(offer) is not True:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if self.reserved.get(alias) is not bound:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            self.pending[alias] = bound
        finally:
            self.reserved.pop(alias, None)
        return {'id': alias, 'method': 'item/commandExecution/requestApproval', 'params': projection}

    async def reply(self, peer, route, request):
        if (not isinstance(request, dict) or request.keys() != {'id', 'result'}
                or not p.valid_opaque(request.get('id')) or not isinstance(request.get('result'), dict)
                or request['result'].keys() != {'decision'}):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        bound = self.pending.get(request['id'])
        if bound is None or bound.peer is not peer or bound.route is not route:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        choice = request['result']['decision']
        if not isinstance(choice, str) or choice not in bound.offer.choices:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        # Consume once before an awaited route callback. A lost response cannot
        # authorize another provider write, even through this same connection.
        del self.pending[bound.offer.alias]
        return await route.native_approval_response(bound.offer, choice)

    def withdraw(self, route, offer):
        """Retire this exact offered question; never turn removal into an answer."""
        for collection in (self.pending, self.reserved):
            bound = collection.get(offer.alias)
            if bound is not None and bound.route is route and bound.offer is offer:
                del collection[offer.alias]
                return bound if collection is self.pending else None
        return None

    def retire(self, peer):
        for collection in (self.pending, self.reserved):
            for alias, bound in tuple(collection.items()):
                if bound.peer is peer:
                    del collection[alias]
