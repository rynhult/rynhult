"""L1 terminal grants use the exact owned process, never Codex writer routes."""
import asyncio

from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import InputGrant, TerminalRun


async def terminal_value(runtime, token, action, sequence=None):
    value = await runtime.peer.call('authority.terminal', {'grant': token, 'action': action, 'sequence': sequence})
    actor, fence = value['actor'], value['fence']
    grant = InputGrant(TerminalRun(value['run']['sessionId'], value['run']['runId']), value['attachmentId'],
        p.Actor(p.ActorKind(actor['kind']), actor['principal'], p.Channel(actor['channel']), actor.get('device')),
        value['grant'], value['epoch'], p.Fence(fence['authorityEpoch'], fence['brokerIncarnation'], fence['hubIncarnation']))
    owner = runtime.controller.writer('terminal')
    if (grant.grant != token or grant.actor.kind is not p.ActorKind.PERSON
            or grant.run != TerminalRun(runtime.spec.spec['session_id'], runtime.spec.run_id)
            or runtime.broker.link is None or grant.fence != runtime.broker.link[0]
            or runtime.owners.current is not owner or runtime.terminal_host is not owner.host
            or not await asyncio.to_thread(owner.process.current)
            or not await runtime.owners.authorize(runtime.spec, lifecycle=True)):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    # Process/auth checks awaited IO. Revalidate this exact opaque grant, not a
    # cached owner actor or the person who happens to hold the view afterwards.
    current = await runtime.peer.call('authority.terminal', {'grant': token, 'action': action, 'sequence': sequence})
    if current != value or runtime.controller.writer('terminal') is not owner:
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    return grant
