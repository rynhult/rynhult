"""Real stock same-question CAS loser and cancellation after accepted application."""
import asyncio
import json
from pathlib import Path

from enrolled_stock_public import Case as PublicCase
from enrolled_stock_public import prepare as prepare_public
from public_native_http import Public

from acc.server.models import RelayDecision, RelayOperation
from acc.session_relay import protocol as p


class Case(PublicCase):
    async def lifecycle(self,c,state,launch,manager,config,public,host,view,offer,answers,frame):
        sid=state.spec.spec['session_id'];run=str(launch.run_id);rpc=state.owner.rpc
        native_id=state.adapter.decisions[offer.decision_id]['native_id']
        arrived=asyncio.Event();writes=[];original=rpc._write;writer=rpc.writer
        class Witness:
            def __getattr__(self,name):return getattr(writer,name)
            def write(self,data):
                message=json.loads(data)
                if type(message.get('id')) is type(native_id) and message.get('id')==native_id and 'result' in message:writes.append(message)
                return writer.write(data)
        rpc.writer=Witness()
        async def observed(message,**options):
            if message.get('id')==native_id and 'result' in message:arrived.set()
            return await original(message,**options)
        rpc._write=observed
        reviewer=await Public.create(c,manager,sid,features=['decisions','queue'])
        refusals=[];loser_id=None;loser_task=None
        def records(operation):
            with c.db() as db:return (dict(db.get(RelayOperation,operation).receipt),dict(db.get(RelayDecision,(sid,offer.decision_id)).decision))
        try:
            await rpc.write_lock.acquire()
            try:
                assert 'result' in await frame(3,b'\r')
                await asyncio.wait_for(arrived.wait(),10)
                submission=launch.terminal_grants.approvals.submission
                before,reserved=records(submission.operation_id)
                assert before['state']=='submitting' and reserved['status']=='reserved'
                assert len(answers)==1 and not writes and host.processed==2
                if self.mode=='chat_loser':
                    params={'sessionId':sid,'expectedRunId':run,'decisionId':offer.decision_id,
                        'choiceId':'cancel','expectedRevision':offer.revision,'actionDigest':offer.action_digest}
                    registered=await reviewer.call('operation.register',{'expectedRunId':run,'registrationKey':'same-question-loser',
                        'method':'decision.reply','payloadDigest':p.operation_digest('decision.reply',params)})
                    loser_id=registered['result']['operationId']
                    loser_task=asyncio.create_task(reviewer.call('decision.reply',{**params,'operationId':loser_id}))
                    async with asyncio.timeout(5):
                        while not launch.submit_lock._waiters:await asyncio.sleep(.01)
                    assert not loser_task.done()
                    assert records(submission.operation_id)==(before,reserved)
                    assert not writes and len(answers)==1
            finally:rpc.write_lock.release()
            if loser_task is not None:
                lost=await asyncio.wait_for(loser_task,20)
                assert lost['error']['data']['code']=='DECISION_RESOLVED',lost
                refusals.append(lost)
            async with asyncio.timeout(20):
                while True:
                    receipt,decision=records(submission.operation_id)
                    if receipt['state']=='accepted' and host.processed==3:break
                    await asyncio.sleep(.02)
            assert decision['status']=='answered' and decision['choiceId']=='accept'
            assert receipt['actor']==before['actor']==decision['winner'] and receipt['payloadDigest']==before['payloadDigest']
            evidence=state.adapter.answer_evidence.entries[offer.decision_id]
            assert evidence['finished'] and evidence['resolved'] and evidence['item_status']=='completed'
            assert len(writes)==len(answers)==1 and writes[0]['result']=={'decision':'accept'}
            if self.mode=='cancel_applied':
                current=await reviewer.call('operation.get',{'operationId':submission.operation_id})
                refused=await reviewer.call('operation.cancel',{'operationId':submission.operation_id,'expectedRevision':current['result']['revision']})
                assert refused['error']['data']['code']=='REVISION_CONFLICT',refused
                refusals.append(refused)
            elif self.mode not in ('chat_loser','owner_applied','transport_applied'):raise AssertionError(self.mode)
            assert records(submission.operation_id)==(receipt,decision)
            assert 'error' in await frame(3,b'\r')
            assert len(writes)==len(answers)==1 and host.processed==launch.terminal_grants.record.processed==3
            snapshot=(await reviewer.call('session.snapshot',{}))['result']
            assert any(v['decisionId']==offer.decision_id and v['status']=='answered' for v in snapshot['decisionOutcomes'])
            with c.db() as db:loser_receipt=dict(db.get(RelayOperation,loser_id).receipt) if loser_id else None
            proof={'provider':'codex','mode':self.mode,'nativeId':state.binding.native_conversation,
                'originalDecisionId':offer.decision_id,'originalOperationId':submission.operation_id,'originalNativeRequestId':native_id,
                'before':before,'reserved':reserved,'receipt':receipt,'decision':decision,'refusals':refusals,
                'loserOperationId':loser_id,'loserReceipt':loser_receipt,'providerResponseWrites':writes,'responseAttempts':answers,
                'nativeProcessed':host.processed,'mainProcessed':launch.terminal_grants.record.processed,
                'exactNativeApplicationPublished':True,'originalWinnerUnchanged':True,'publicOutcomeReadable':True,'noReplay':True,'terminalMediated':False,
                'scope':'Actual pinned isolated stock Enter and main reserved CAS. Same exact question Chat Cancel loses while original response waits on actual stdio lock, then original native Allow conclusively applies once; or public cancellation after durable accepted native application refuses undo. Actual exact owned lifecycle, public cookie/CSRF routes, no repeated response or stale frame ACK. No failure recovery, restart, real account or subscription.'}
            if hasattr(self,'after_application'):
                await self.after_application(c,state,launch,manager,host,offer,frame,proof,records,config)
            else:Path('/evidence/result.json').write_text(json.dumps(proof))
        finally:
            if loser_task is not None and not loser_task.done():
                loser_task.cancel();await asyncio.gather(loser_task,return_exceptions=True)
            await reviewer.close()


async def prepare_mode(root,work,c,mode):
    prepared=await prepare_public(root,work,c)
    case=Case(prepared.base);case.mode=mode
    return case
