"""Private read-only evidence channel; never enrolls or constructs a producer."""
import asyncio

from acc.session_relay import protocol as p

from . import application_proofs


class ApplicationReader:
    def __init__(self, factory, token):
        self.factory, self.token = factory, token
        self.peer = None

    def lost(self):
        pass

    async def close(self):
        pass

    async def handle(self, method, params):
        if method != 'application.read' or params != {}:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        before = await self.peer.call('authority.application_read', {'grant': self.token})
        result = await asyncio.to_thread(application_proofs.read, self.factory.config, before)
        after = await self.peer.call('authority.application_read', {'grant': self.token})
        if p.canonical_json(before) != p.canonical_json(after) or self.peer.closed:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return result
