"""Nonmutating native idle facts, never a draft freeze, input claim or switch."""
import asyncio

from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import ExitState


async def inspect_owner(runtime, token):
    async def authorize():
        if await runtime.peer.call('authority.owner_observation', {'observation': token}) is not True:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    await authorize()
    controller = runtime.controller
    if not controller or controller.closed or controller.fenced or runtime.operation_lock.locked():
        return {'runId': runtime.spec.run_id, 'view': controller.switch.view if controller else None,
            'idle': False, 'blockers': ['uncertain_operation']}
    owner = controller.switch.owner
    stamp = sample(owner)
    blockers = []
    if getattr(owner, 'active', False):
        blockers.append('active_turn')
    if getattr(owner, 'background', ()) or getattr(owner, 'native_tools', ()) or getattr(owner, 'tools', ()):
        blockers.append('background_tool')
    if getattr(owner, 'pending', ()) or getattr(owner, 'native_permissions', ()) or getattr(owner, 'decisions', ()):
        blockers.append('pending_decision')
    if controller.switch.view == 'chat':
        proof = await owner.prepare_switch()  # SDK method only observes its ledgers.
        idle = proof.idle and proof.exit is ExitState.RUNNING
    else:
        process = getattr(owner, 'process', None)
        live = process is not None and hasattr(process, 'current') and await asyncio.to_thread(process.current)
        boundary = getattr(getattr(owner, 'host', None), 'boundary', None)
        if boundary and (boundary.draft or boundary.ticket is not None or boundary.admitted != boundary.processed):
            blockers.append('terminal_draft')
        idle = bool(live and owner.idle and not owner.failed and boundary and boundary.verified_empty
            and not boundary.partial and not boundary.frozen and not blockers)
    await authorize()
    if stamp != sample(owner):
        idle = False
    if runtime.controller is not controller or controller.closed or controller.fenced:
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    # Native registration can take this lock during the read's awaits without
    # replacing/revoking the owner. Discard idle, not the reader/input authority.
    if runtime.operation_lock.locked():
        idle = False
        if 'uncertain_operation' not in blockers:
            blockers.append('uncertain_operation')
    return {'runId': runtime.spec.run_id, 'view': controller.switch.view, 'idle': bool(idle and not blockers), 'blockers': blockers}


def sample(owner):
    boundary = getattr(getattr(owner, 'host', None), 'boundary', None)
    return (getattr(owner, 'active', False), getattr(owner, 'failure', False), getattr(owner, 'revision', None),
        *(tuple(sorted(map(repr, getattr(owner, key, ())))) for key in
          ('background', 'native_tools', 'tools', 'pending', 'native_permissions', 'decisions')),
        (boundary.admitted, boundary.processed, boundary.draft, boundary.ticket, boundary.partial, boundary.frozen)
        if boundary else None)
