"""Public Claude review uses the existing exact Hub CAS; no native key tunnel."""
import asyncio

from acc.session_relay import protocol as p

from ..db import write_in_thread, write_intent
from ..models import RelayDecision
from . import authority, store
from .native_decisions import public_ready


@write_intent
def receipt(run,resolve,params,*,starting=False,refused=False):
    with run.ctx.db() as db:
        row=store.get(db,run.session_id)
        record=db.get(RelayDecision,(run.session_id,params['decisionId']))
        if record is None:raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
        decision=store.decision_from(record.decision)
        _,current=authority.current(run.ctx,db,resolve,run.session_id,action='decide',
            expected_run=params['expectedRunId'],attached=True,decision=decision)
        if getattr(resolve,'attachment_id',None) is None:
            raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
        op=store.operation(db,run.session_id,params['operationId'],current.actor)
        digest=p.operation_digest('decision.reply',{k:v for k,v in params.items() if k!='operationId'})
        value=store.receipt_from(op.receipt)
        p.check_registered(value,actor=current.actor,session_id=run.session_id,run_id=str(current.run_id),
            method='decision.reply',payload_digest=digest)
        if op.authority!=current.stamp():raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        if refused and value.state is p.ReceiptState.QUEUED:
            value=store.transition(db,row,op,p.ReceiptState.FAILED,reason=p.FailureReason.CONFLICT)
            db.commit()
        if starting and value.state is p.ReceiptState.QUEUED:
            if not public_ready(run.ctx,row):raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            return None
        return value


async def answer(run,resolve,params):
    existing=await write_in_thread(lambda:receipt(run,resolve,params,starting=True))
    if existing is not None:return existing  # observation only, never resend
    async def dispatch():
        try:
            await run.answer(resolve,params)
        except p.RelayError:
            # Only a still-queued, exact authenticated intent may fail here.
            # A CAS winner/submitting/unknown answer is never called unsent.
            await write_in_thread(lambda:receipt(run,resolve,params,refused=True))
            raise
        return await write_in_thread(lambda:receipt(run,resolve,params))
    task=asyncio.create_task(dispatch())
    run.manager.background.add(task)
    def finished(done):
        run.manager.background.discard(done)
        if not done.cancelled():done.exception()
    task.add_done_callback(finished)
    # A browser leaving cannot cancel a committed CAS or cause another send.
    return await asyncio.shield(task)
