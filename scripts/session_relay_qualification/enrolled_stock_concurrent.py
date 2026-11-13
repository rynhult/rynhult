"""Identical concurrent commands: original native ticket and independent Chat CAS."""
import asyncio
import json
from pathlib import Path

from enrolled_codex_ack import prepare as prepare_ack
from enrolled_stock_public import Case as PublicCase
from public_native_http import Public

from acc.server.models import RelayDecision, RelayOperation
from acc.session_relay import protocol as p


class Case(PublicCase):
    async def exercise(self,*args):
        self.native_events=[]
        state=args[1];observe=state.owner.rpc.observe
        async def capture(event):
            if event.get('method')=='item/commandExecution/requestApproval':self.native_events.append(event)
            await observe(event)
        state.owner.rpc.observe=capture
        return await super().exercise(*args)

    async def lifecycle(self,c,state,launch,manager,config,public,host,view,offer,answers,frame):
        sid=state.spec.spec['session_id'];run=str(launch.run_id)
        async with asyncio.timeout(15):
            while True:
                with c.db() as db:
                    decisions=[dict(row.decision) for row in db.query(RelayDecision).filter_by(session_id=sid).all()]
                if len(decisions)==2 and len(self.native_events)==2:break
                await asyncio.sleep(.025)
        assert len({event['id'] for event in self.native_events})==2
        commands=[event['params']['command'] for event in self.native_events]
        assert commands[0]==commands[1] and self.base.command in commands[0],commands
        other=next(value for value in decisions if value['decisionId']!=offer.decision_id)
        chat=await Public.create(c,manager,sid,features=['decisions','terminal'])
        try:
            params={'sessionId':sid,'expectedRunId':run,'decisionId':other['decisionId'],'choiceId':'accept',
                'expectedRevision':other['revision'],'actionDigest':other['actionDigest']}
            registration=await chat.call('operation.register',{'expectedRunId':run,'registrationKey':'concurrent-chat',
                'method':'decision.reply','payloadDigest':p.operation_digest('decision.reply',params)})
            chat_operation=registration['result']['operationId']
            async def answer_other():
                value=await chat.call('decision.reply',{**params,'operationId':chat_operation})
                assert 'result' in value,value
            if self.mode=='chat_first':await answer_other()
            key=await frame(3,b'\r');assert 'result' in key,key
            submission=launch.terminal_grants.approvals.submission
            assert submission.frame.offer.decision_id==offer.decision_id
            async with asyncio.timeout(10):
                while True:
                    with c.db() as db:first=dict(db.get(RelayOperation,submission.operation_id).receipt)
                    if first['state']=='uncertain' or self.mode=='chat_first' and first['state']=='accepted':break
                    await asyncio.sleep(.025)
            if self.mode!='chat_first':
                assert first['state']=='uncertain' and host.processed==2
                if self.mode=='revoked_native':public.revoke()
                await answer_other()
            async with asyncio.timeout(20):
                while True:
                    with c.db() as db:
                        receipts=[dict(db.get(RelayOperation,identifier).receipt) for identifier in (submission.operation_id,chat_operation)]
                        final=[dict(db.get(RelayDecision,(sid,identifier)).decision) for identifier in (offer.decision_id,other['decisionId'])]
                    expected_mark=2 if self.mode=='revoked_native' else 3
                    if all(value['state']=='accepted' for value in receipts) and host.processed==expected_mark:break
                    await asyncio.sleep(.025)
            assert len(answers)==2 and len({answer['id'] for answer in answers})==2
            assert all(answer['value']=={'decision':'accept'} for answer in answers)
            assert all(value['status']=='answered' for value in final)
            assert receipts[0]['actor']==p.to_wire(submission.frame.grant.actor)==final[0]['winner']
            assert receipts[0]['payloadDigest']==p.operation_digest('decision.reply',launch.terminal_grants.approvals.params(submission))
            assert receipts[1]['payloadDigest']==p.operation_digest('decision.reply',params)
            assert submission.operation_id!=chat_operation and offer.decision_id!=other['decisionId']
            if self.mode=='revoked_native':
                assert host.input_fenced and host.processed==launch.terminal_grants.record.processed==2
                stolen=await chat.call('terminal.input',{'attachmentId':public.view['attachmentId'],
                    'inputGrant':host.grant.grant,'inputEpoch':host.grant.epoch,'frameSeq':4,'data':'DQ=='})
                assert 'error' in stolen
            else:
                assert host.processed==launch.terminal_grants.record.processed==3 and not host.input_fenced
                assert 'error' in await frame(3,b'\r')
            assert len(answers)==2
            Path('/evidence/result.json').write_text(json.dumps({'provider':'codex','mode':self.mode,
                'identicalCommands':True,'nativeCommands':commands,'nativeRequestIds':[event['id'] for event in self.native_events],
                'nativeDecisionId':offer.decision_id,'chatDecisionId':other['decisionId'],'receipts':receipts,
                'decisions':final,'providerAnswers':answers,'nativeProcessed':host.processed,
                'mainProcessed':launch.terminal_grants.record.processed,'originalCookieRevoked':self.mode=='revoked_native',
                'originalFrameActorPreserved':True,'noBorrowedGrant':True,'noReplay':True,'terminalMediated':False,
                'scope':'Actual pinned identical concurrent command requests, one explicit owned native alias/immutable public key ticket and independent cookie/CSRF Chat CAS for the other question. Both review orders and original-key cookie revocation are covered. Late application can settle original receipts without reopening revoked input. Not multiple simultaneously armed public native aliases, files, restart or explicit recovery.'}))
        finally:await chat.close()


async def prepare_mode(root,work,c,mode):
    ack=await prepare_ack(root,work,c);ack.identical=True
    case=Case(ack.base);case.mode=mode
    return case
