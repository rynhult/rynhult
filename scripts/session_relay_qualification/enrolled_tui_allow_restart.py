"""Genuine native Allow journal across g56 reconciliation worker crashes."""
import asyncio
import base64
import time

from enrolled_public_decisions import Case as PublicCase
from enrolled_tui_expiry import QUESTION
from enrolled_tui_expiry import prepare as prepare_expiry

from acc.server.models import RelayDecision, RelayOperation, RelaySession
from acc.server.session_relay import reconciliation
from acc.session_relay import protocol as p


class Case(PublicCase):
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        sid,run=state.spec.spec['session_id'],str(launch.run_id)
        journal=None;writes=[];publications=[]
        answer=state.owners.hooks.answer;call=state.peer.call
        inspect_application=state.owners.hooks.inspect_application
        async def lose_direct_evidence(*args, **kwargs):
            return {'outcome': 'unknown'}
        state.owners.hooks.inspect_application=lose_direct_evidence
        async def once(*args,**kwargs):
            writes.append(args[0]);assert len(writes)==1
            return await answer(*args,**kwargs)
        async def lose(method,params):
            if method=='publication' and params.get('kind')=='decision.applied':
                publications.append(dict(params))
                raise ConnectionError('Fixture drops original evidence publication until explicit reconciliation')
            return await call(method,params)
        state.owners.hooks.answer=once;state.peer.call=lose
        previous=reconciliation.QUALIFIED;reconciliation.QUALIFIED=True
        try:
            for seq,data in ((3,QUESTION.encode()),(4,b'\r')):
                sent=await self.public.call('terminal.input',{'attachmentId':self.public.view['attachmentId'],
                    'inputGrant':claim.input_grant,'inputEpoch':claim.input_epoch,'frameSeq':seq,'data':base64.b64encode(data).decode()})
                assert 'result' in sent,sent
                if seq==3:await asyncio.sleep(.7)
            async with asyncio.timeout(10):
                while not state.owners.hooks.pending:await asyncio.sleep(.01)
            identifier,pending=next(iter(state.owners.hooks.pending.items()))
            async with asyncio.timeout(3):
                while True:
                    view=await self.public.call('session.snapshot',{})
                    card=next((v for v in view['result']['pendingDecisions'] if v['decisionId']==identifier),None)
                    if card:break
                    await asyncio.sleep(.01)
            payload={'sessionId':sid,'expectedRunId':run,'decisionId':identifier,'choiceId':'accept',
                'expectedRevision':card['revision'],'actionDigest':card['actionDigest']}
            registered=await self.public.call('operation.register',{'expectedRunId':run,'registrationKey':'durable-original-Allow',
                'method':'decision.reply','payloadDigest':p.operation_digest('decision.reply',payload)})
            fields={**payload,'operationId':registered['result']['operationId']}
            start=time.monotonic();result=await self.public.call('decision.reply',fields)
            assert result['result']['state']=='uncertain',result
            async with asyncio.timeout(40):
                while (not publications or owner.host.processed!=4
                        or launch.terminal_grants.record.processed!=4):await asyncio.sleep(.05)
            elapsed=time.monotonic()-start;assert elapsed>0,elapsed
            journal=state.decision_evidence
            records=journal.pending(vars(state.spec.binding));assert len(records)==1
            key,record,completion,reference=records[0]
            assert key==fields['operationId'] and completion and reference
            assert record['operation']['actor']==result['result']['actor']
            with c.db() as db:
                before=dict(db.get(RelayOperation,key).receipt)
                decision=dict(db.get(RelayDecision,(sid,identifier)).decision)
                current=db.get(RelaySession,sid).current_run_id
            assert before['state']=='uncertain' and decision['status']=='outcome_unknown'
            count=self.base.calls;pid=owner.process.pid
            replay=await self.public.call('decision.reply',fields)
            assert replay['result']==before and len(writes)==1
            from native_restart_controller import exercise
            restart_proof=await exercise(state,manager,sid,launch.run_id,state.factory.config)
            with c.db() as db:
                final=dict(db.get(RelayOperation,key).receipt)
                final_decision=dict(db.get(RelayDecision,(sid,identifier)).decision)
                assert db.get(RelaySession,sid).current_run_id==current
            assert final['state']=='accepted' and final_decision['status']=='answered'
            assert final['actor']==before['actor'] and final['payloadDigest']==before['payloadDigest']
            assert owner.process.pid==pid and not owner.process.current() and self.base.calls==count and len(writes)==1
            assert state.owners.close_complete
            proof.update(durableRestart={'preRetirementElapsedSeconds':elapsed,'observerReclaimed':True,
                'originalActorDigestPreserved':True,'oneNativeAnswer':True,'readOnlyVersion4Channel':True,
                'originalBindingMainPublication':True,'duplicateReconciliationEmpty':True,'noWriterCreated':True,
                'sameOwnerPid':True,'restart':restart_proof,'nativeCompletionDurable':True,'operationId':key,'nativeRef':reference,
                'scope':'Actual pinned offline Claude TUI Allow and public cookie/CSRF receipt; retire the execution owner after durable completion, then g56 SIGKILL/restart two read-only brokers before publication and after main commit with ACK lost. No execution-broker crash during native answer delivery; no real subscription/settings.'})
        finally:
            state.peer.call=call;state.owners.hooks.answer=answer;state.owners.hooks.inspect_application=inspect_application;reconciliation.QUALIFIED=previous
            await self.public.close()


async def prepare(root,work,c):
    from acc.broker import claude_allow_evidence
    claude_allow_evidence.CLAUDE_TUI_ALLOW_ACK_QUALIFIED=True
    expiry=await prepare_expiry(root,work,c)
    return Case(expiry.base)
