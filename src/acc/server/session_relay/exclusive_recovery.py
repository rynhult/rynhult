"""Fresh explicit recovery of a sealed failed exclusive owner, default off."""
import secrets
import time

from acc.session_relay import protocol as p

from ..admin_features import enabled
from ..db import write_in_thread, write_intent
from ..models import RelayOperation, RelayOwnership, RelayRun, Session, SessionRun, utcnow
from . import authority, store

RECOVERY_QUALIFIED=False


def ready(ctx,row):
    from .exclusive_runtime import ExclusiveLaunch
    manager=getattr(ctx,'relay_runtime',None)
    run=manager.runs.get(row.session_id) if manager else None
    return (RECOVERY_QUALIFIED is True and enabled(ctx.settings,'session_relay') and row.sealed
        and isinstance(run,ExclusiveLaunch) and run.run_id==row.current_run_id
        and run.binding_id==row.binding_id and run.peer is not None and not run.peer.closed)


def current(run,db,resolve,expected_run):
    row,grant=authority.current(run.ctx,db,resolve,run.session_id,action='recover',expected_run=expected_run)
    owner=db.get(RelayOwnership,run.session_id)
    if (not ready(run.ctx,row) or not owner or owner.mode!='exclusive_idle'
            or owner.phase not in ('recovery_required','recovery_cleaning','recovery_starting')
            or grant.actor.kind is not p.ActorKind.PERSON or not p.valid_opaque(getattr(resolve,'attachment_id',None))):
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    run.owner_hub._policy(db,db.get(Session,run.session_id),grant,db.get(RelayRun,grant.run_id))
    return row,grant,owner


@write_intent
def claim(run,resolve,params,token):
    with run.ctx.db() as db:
        row,grant,owner=current(run,db,resolve,params['expectedRunId'])
        operation=store.operation(db,run.session_id,params['operationId'],grant.actor)
        fields={k:v for k,v in params.items() if k!='operationId'}
        p.check_registered(store.receipt_from(operation.receipt),actor=grant.actor,session_id=run.session_id,
            run_id=str(grant.run_id),method='switch.recover',payload_digest=p.operation_digest('switch.recover',fields))
        if (owner.phase!='recovery_required' or row.revision!=params['expectedRevision']
                or operation.authority!=grant.stamp() or operation.receipt['state']!='queued'
                or operation.queue_seq is not None or operation.deadline<=utcnow()):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        source=db.get(RelayRun,grant.run_id)
        if source.frozen_policy.get('ownershipMode')!='exclusive_idle':raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        failed_run=owner.target_run_id;failed_operation=owner.commit_operation_id
        target=SessionRun(session_id=run.session_id,reason='relay_explicit_recovery');db.add(target);db.flush()
        plan={'resolve':resolve,'stamp':grant.stamp(),'sourceRunId':str(grant.run_id),'targetRunId':str(target.id),
            'bindingId':row.binding_id,'target':params['target'],'operationId':operation.id,
            'failedRunId':failed_run,'failedOperationId':failed_operation,'expires':time.monotonic()+60,'clean':False}
        store.transition(db,row,operation,p.ReceiptState.SUBMITTING)
        owner.phase='recovery_cleaning';owner.target_run_id=target.id;owner.commit_operation_id=operation.id
        row.revision+=1
        store.append(db,row,'switch.changed',{'state':owner.phase,'target':params['target']})
        run.owner_hub._audit(db,grant.actor,run.session_id,'owner.recover',target=params['target'],
            source_run_id=grant.run_id,target_run_id=target.id)
        db.commit();run.recovery_plans[token]=plan
        return plan


def verify(run,db,token):
    plan=run.recovery_plans.get(token)
    if plan is None or plan['expires']<=time.monotonic():raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    row,grant,owner=current(run,db,plan['resolve'],plan['sourceRunId'])
    op=store.operation(db,run.session_id,plan['operationId'],grant.actor)
    if (grant.stamp()!=plan['stamp'] or owner.commit_operation_id!=op.id
            or str(owner.target_run_id)!=plan['targetRunId'] or op.receipt['state'] not in ('submitting','uncertain')
            or op.deadline<=utcnow()):raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    return plan,row,owner,op


@write_intent
def observe(run,method,params):
    with run.ctx.db() as db:
        plan,row,owner,op=verify(run,db,params.get('plan'))
        if method=='authority.recovery_resolve':
            return {key:plan[key] for key in ('sourceRunId','targetRunId','bindingId','target')}
        if method=='authority.recovery_verify':return True
        if method=='authority.recovery_clean':
            if params.keys()!={'plan','factory'} or not p.valid_opaque(params['factory']) or owner.phase!='recovery_cleaning':
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            # Positive cleanup plus main's never-owned target record can settle
            # only the original failed switch/recovery, never another unknown IO.
            previous=db.get(RelayOperation,plan['failedOperationId']) if plan['failedOperationId'] else None
            partial=db.get(SessionRun,plan['failedRunId']) if plan['failedRunId'] else None
            if (previous is not None and partial is not None and partial.session_id==run.session_id
                    and partial.verified_at is None and str(previous.run_id)==plan['sourceRunId']
                    and previous.receipt['method'] in ('switch.commit','switch.recover')
                    and previous.receipt['state'] in ('submitting','uncertain')):
                ref=p.sha256_digest(p.canonical_json([params['factory'],partial.id,previous.id,'whole-tree-clean']))
                store.settle(db,row,previous,accepted=False,evidence=p.Evidence(
                    p.EvidenceKind.BROKER_REFUSAL,previous.id,str(previous.run_id),ref))
                partial.ended_at=utcnow()
            plan['clean']=True;owner.phase='recovery_starting';db.commit();return True
        if method!='authority.recovery_owned' or not plan['clean'] or owner.phase!='recovery_starting':
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        evidence=params.get('evidence',{});source=db.get(RelayRun,int(plan['sourceRunId']))
        binding=source.identity['binding']
        if (evidence.get('binding_id')!=row.binding_id or evidence.get('run_id')!=plan['targetRunId']
                or evidence.get('native_conversation')!=binding['native_conversation']
                or not p.valid_opaque(evidence.get('process_identity'))):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        old=db.get(SessionRun,int(plan['sourceRunId']));target=db.get(SessionRun,int(plan['targetRunId']))
        if old.ended_at or target.ended_at or target.verified_at:raise p.RelayError(p.ErrorCode.STALE_RUN)
        old.ended_at=target.verified_at=utcnow()
        if plan['failedRunId']:
            failed=db.get(SessionRun,plan['failedRunId'])
            if failed and not failed.verified_at:failed.ended_at=utcnow()
        db.add(RelayRun(run_id=target.id,session_id=run.session_id,binding_id=row.binding_id,
            identity={**source.identity,'processIdentity':evidence['process_identity'],'writerView':plan['target']},
            frozen_policy=source.frozen_policy,policy_revision=source.policy_revision))
        store.settle(db,row,op,accepted=True,evidence=p.Evidence(p.EvidenceKind.HUB_COMMIT,op.id,str(op.run_id)))
        row.current_run_id=target.id;row.sealed=False;row.capabilities={};row.authority_epoch+=1
        owner.view=plan['target'];owner.target=plan['target'];owner.phase='owned';owner.source_run_id=target.id
        owner.authority={**plan['stamp'],'runId':target.id,'epoch':row.authority_epoch}
        row.revision+=1;store.append(db,row,'switch.changed',{'state':'owned','view':owner.view})
        db.commit()
        run.run_id=target.id
        run.fence=p.Fence(row.authority_epoch,run.fence.broker_incarnation,run.manager.incarnation)
        from .runtime import RequestResolver
        original=plan['resolve']
        run.resolve=RequestResolver(original.ctx,original.request) if isinstance(original,RequestResolver) else original
        run.terminal_grants.invalidate()
        return {**run.owner_hub.snapshot(row,owner),'changed':True}


async def broker(run,method,params):return await write_in_thread(lambda:observe(run,method,params))


@write_intent
def failed(run,token):
    plan=run.recovery_plans.get(token)
    if plan is None:return
    with run.ctx.db() as db:
        row=store.lock(db,run.session_id);owner=db.get(RelayOwnership,run.session_id)
        if owner.commit_operation_id!=plan['operationId']:return
        owner.phase='recovery_required';store.seal(db,row)
        op=db.get(RelayOperation,plan['operationId'])
        if op.receipt['state']=='submitting':store.transition(db,row,op,p.ReceiptState.UNCERTAIN)
        db.commit()


async def recover(run,resolve,params):
    from .exclusive_runtime import ExclusiveLaunch
    if not isinstance(run,ExclusiveLaunch):raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    async with run.terminal_grants.frame_lock,run.submit_lock:
        token=secrets.token_hex(32)
        try:
            await write_in_thread(lambda:claim(run,resolve,params,token))
            return await run.peer.call('owner.recover',{'plan':token})
        except BaseException as error:
            await write_in_thread(lambda:failed(run,token))
            if isinstance(error,(ConnectionError,TimeoutError)):
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT) from None
            raise
        finally:run.recovery_plans.pop(token,None)
