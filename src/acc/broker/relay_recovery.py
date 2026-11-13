"""Explicit one-use recovery; retired factories and controllers never reopen."""
from dataclasses import asdict, replace

from acc.session_relay import protocol as p


async def recover(runtime,token):
    from .claude_owner import ClaudeOwnerFactory
    from .relay_ownership import OwnershipController
    old=runtime.owners;controller=runtime.controller
    if (runtime.recovery_plan is not None or not controller.closed or not controller.fenced
            or not controller.switch.recovery_required):
        raise p.RelayError(p.ErrorCode.BUSY)
    plan=await runtime.peer.call('authority.recovery_resolve',{'plan':token})
    if (plan['sourceRunId'] not in (controller.spec.run_id,runtime.spec.run_id) or plan['bindingId']!=old.base.binding.binding_id
            or plan['target'] not in ('chat','terminal')):
        raise p.RelayError(p.ErrorCode.STALE_RUN)
    runtime.recovery_plan=token
    try:
        # close refuses unknown forks and verifies every known target's own
        # whole-tree receipt. Previous owners were reclaimed before target prepare.
        await old.close()
        if not old.close_complete or not await old.exited():raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        await runtime.peer.call('authority.recovery_clean',{'plan':token,'factory':old.incarnation})
        target=replace(runtime.spec,run_id=plan['targetRunId'])
        runtime.spec=target
        factory=ClaudeOwnerFactory(runtime.factory,target,{'files':old.files,'env':old.env},runtime,recovered=True)
        runtime.owners=factory
        launched=await (factory.chat(target) if plan['target']=='chat' else factory.terminal(target))
        observed=launched.attestation
        if (observed.run_id!=target.run_id or observed.binding_id!=target.binding.binding_id
                or observed.native_conversation!=target.binding.native_conversation or not observed.process_identity):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        result=await runtime.peer.call('authority.recovery_owned',{'plan':token,'evidence':p.to_wire(asdict(observed))})
        runtime.controller=OwnershipController(spec=target,view=plan['target'],owner=launched.owner,
            factory=factory,peer=runtime.peer)
        runtime.adapter=launched.owner
        runtime.terminal_host=launched.owner.host if plan['target']=='terminal' else None
        runtime.recovery_plan=None
        return result
    except BaseException:
        # Retain a possibly forked replacement for authenticated cleanup. Do not
        # blindly resume it or report a pre-fork refusal after the start gate.
        runtime.controller=controller
        if runtime.owners is not old:
            try:await runtime.owners.close()
            except Exception:pass
        raise
    finally:runtime.recovery_plan=None
