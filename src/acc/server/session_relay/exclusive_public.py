"""Gated public L1 operations over the existing opaque ownership plan contract."""
from sqlalchemy import select

from acc.session_relay import protocol as p

from ..admin_features import enabled
from ..db import write_in_thread, write_intent
from ..models import RelayOperation, RelayOwnership, Session, utcnow
from . import authority, store

PUBLIC_L1_QUALIFIED=False
CLAUDE_LAUNCH_QUALIFIED=False


def public_ready(ctx,row):
    from .exclusive_runtime import ExclusiveLaunch
    runtime=getattr(ctx,'relay_runtime',None)
    run=runtime.runs.get(row.session_id) if runtime else None
    return (PUBLIC_L1_QUALIFIED is True and enabled(ctx.settings,'session_relay') and not row.sealed
        and isinstance(run,ExclusiveLaunch) and run.binding_id==row.binding_id and run.run_id==row.current_run_id
        and all(row.capabilities.get('switch.to_'+view)=='qualified' for view in ('chat','terminal')))


def presentation(ctx,db,row,actor):
    owner=db.get(RelayOwnership,row.session_id)
    if not owner or (owner.mode!='exclusive_idle' and owner.phase!='recovery_required'):return None
    session = db.get(Session, row.session_id)
    accepted = db.scalar(select(RelayOperation.id).where(
        RelayOperation.session_id == row.session_id,
        RelayOperation.receipt['method'].as_string() == 'turn.submit',
        RelayOperation.receipt['state'].as_string() == 'accepted').limit(1))
    waiting = bool(session is not None and session.provider == 'claude' and owner.view == 'chat'
        and owner.phase == 'owned' and not row.sealed
        and not accepted)
    return {'owner': owner.view, 'phase': owner.phase, 'revision': row.revision,
        'available': owner.mode == 'exclusive_idle' and actor.kind is p.ActorKind.PERSON and public_ready(ctx, row),
        'recoveryAvailable': actor.kind is p.ActorKind.PERSON and owner.phase == 'recovery_required',
        'recoveryMode': 'new_session', 'terminalWaitingForChat': waiting,
        **({'terminalWaitingReason': 'Terminal is waiting for your first real Chat message to finish. Your Chat session keeps running.'} if waiting else {})}


@write_intent
def intent(run,resolve,params,method):
    with run.ctx.db() as db:
        row,grant=authority.current(run.ctx,db,resolve,run.session_id,action='switch',expected_run=params['expectedRunId'])
        if (grant.actor.kind is not p.ActorKind.PERSON or row.binding_id!=run.binding_id
                or not p.valid_opaque(getattr(resolve,'attachment_id',None))):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        operation=store.operation(db,run.session_id,params['operationId'],grant.actor)
        receipt=store.receipt_from(operation.receipt)
        p.check_registered(receipt,actor=grant.actor,session_id=run.session_id,run_id=str(grant.run_id),
            method=method,payload_digest=p.operation_digest(method,{key:value for key,value in params.items() if key!='operationId'}))
        if (operation.authority!=grant.stamp() or operation.receipt['state']!='queued'
                or operation.queue_seq is not None or operation.deadline<=utcnow()):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return receipt


@write_intent
def check(run,resolve,params,method):
    with run.ctx.db() as db:
        row,grant=run.check_run(db,resolve=resolve,action='switch')
        if not public_ready(run.ctx,row) or grant.actor.kind is not p.ActorKind.PERSON or not p.valid_opaque(getattr(resolve,'attachment_id',None)):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        fields={key:value for key,value in params.items() if key!='operationId'}
        run.owner_hub.supplied_operation(db,row,grant,params['operationId'],method,fields)
        return store.receipt_from(store.operation(db,run.session_id,params['operationId'],grant.actor).receipt)


@write_intent
def refuse(run,receipt):
    # A cancelled worker may already have committed. Observe its durable state;
    # never roll an accepted/submitting operation back to a retryable intent.
    with run.ctx.db() as db:
        row=store.lock(db,run.session_id)
        operation=store.operation(db,run.session_id,receipt.operation_id,receipt.actor)
        if operation.receipt['state']=='queued':
            store.transition(db,row,operation,p.ReceiptState.FAILED,reason=p.FailureReason.CONFLICT)
            db.commit()


async def prepare(run,resolve,params):
    async with run.terminal_grants.frame_lock,run.submit_lock:
        receipt=await write_in_thread(lambda:intent(run,resolve,params,'switch.prepare'))
        try:
            await write_in_thread(lambda:check(run,resolve,params,'switch.prepare'))
            return await write_in_thread(lambda:run.owner_hub.prepare(resolve,run.session_id,
                target=params['target'],revision=params['expectedRevision'],request_key=params['operationId'],
                operation_id=params['operationId']))
        except BaseException:
            await write_in_thread(lambda:refuse(run,receipt))
            raise


async def commit(run,resolve,params):
    receipt=await write_in_thread(lambda:intent(run,resolve,params,'switch.commit'))
    try:
        await write_in_thread(lambda:check(run,resolve,params,'switch.commit'))
        with run.ctx.db() as db:
            row=store.get(db,run.session_id);owner=db.get(RelayOwnership,run.session_id)
            if (owner is None or owner.plan_id!=params['planId'] or owner.digest!=params['planDigest']
                    or owner.authority.get('_requestAttachment')!=resolve.attachment_id):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            plan=run.owner_hub.snapshot(row,owner)
        if params['expectedRevision']!=plan['revision']:raise p.RelayError(p.ErrorCode.REVISION_CONFLICT)
        return await run.select_owner(resolve,target=plan['target'],revision=plan['revision'],
            request_key=params['operationId'],prepared=plan,operation_id=params['operationId'])
    except BaseException:
        await write_in_thread(lambda:refuse(run,receipt))
        raise


async def cancel(run,resolve,params):
    receipt=await write_in_thread(lambda:intent(run,resolve,params,'switch.cancel'))
    try:
        await write_in_thread(lambda:check(run,resolve,params,'switch.cancel'))
        @write_intent
        def cancel_plan():
            with run.ctx.db() as db:
                row,grant=run.check_run(db,resolve=resolve,action='switch')
                run.owner_hub.supplied_operation(db,row,grant,params['operationId'],'switch.cancel',
                    {key:value for key,value in params.items() if key!='operationId'})
                owner=db.get(RelayOwnership,run.session_id)
                if (owner is None or owner.plan_id!=params['planId'] or owner.phase!='prepared'
                        or owner.authority.get('_requestAttachment')!=resolve.attachment_id):
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                op=store.operation(db,run.session_id,receipt.operation_id,receipt.actor)
                source=dict(owner.authority['_sourceAuthority'])
                owner.phase,owner.authority='owned',source
                store.transition(db,row,op,p.ReceiptState.SUBMITTING)
                store.settle(db,row,op,accepted=True,evidence=p.Evidence(p.EvidenceKind.HUB_COMMIT,op.id,str(op.run_id)))
                row.revision+=1
                store.append(db,row,'switch.changed',{'state':'owned','view':owner.view})
                db.commit()
                return run.owner_hub.snapshot(row,owner)
        async with run.terminal_grants.frame_lock,run.submit_lock:
            return await write_in_thread(cancel_plan)
    except BaseException:
        await write_in_thread(lambda:refuse(run,receipt))
        raise
