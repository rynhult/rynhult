"""Actual TUI question; two authenticated public Hub reviewers, no raw keys."""
import asyncio
import base64
from types import SimpleNamespace

from enrolled_public_controls import Case as PublicCase
from enrolled_tui_expiry import QUESTION
from enrolled_tui_expiry import prepare as prepare_expiry
from public_native_http import Public

from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation, RelaySession
from acc.server.session_relay import store
from acc.session_relay import protocol as p


class Case(PublicCase):
    async def claim(self,c,sid,run,launch,manager):
        c.ctx.relay_runtime=manager  # production manager(ctx) binding, same enrolled runtime
        @write_intent
        def qualify():
            with c.db() as db:
                row=db.get(RelaySession,sid)
                row.capabilities={**row.capabilities,'decision.tool_approval':'qualified'}
                db.commit()
        qualify()
        self.public=await Public.create(c,manager,sid,features=['writer','terminal','decisions'])
        with c.db() as db:revision=db.get(RelaySession,sid).control_revision
        fields={'sessionId':sid,'expectedRunId':run,'attachmentId':self.public.view['attachmentId'],'expectedRevision':revision}
        registration=await self.public.call('operation.register',{'expectedRunId':run,'registrationKey':'public-decision-claim',
            'method':'control.claim','payloadDigest':p.operation_digest('control.claim',fields)})
        value=await self.public.call('control.claim',{**fields,'operationId':registration['result']['operationId']})
        assert 'result' in value,value
        value=value['result']
        return launch.terminal_grants.record.resolve,SimpleNamespace(receipt=store.receipt_from(value['receipt']),
            input_grant=value['inputGrant'],input_epoch=value['inputEpoch'])

    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        sid,run=state.spec.spec['session_id'],str(launch.run_id)
        if not getattr(self,'denial_ack',False):
            # Explicit original unknown-outcome scenario: withhold the separate
            # primary application reader while retaining real native permission
            # delivery, guard checks, exact Hub CAS and both cookie reviewers.
            state.owners.hooks.prepare_application=lambda *_:None
            proof['primaryApplicationEvidenceWithheld']=True
        other=await Public.create(c,manager,sid,features=['decisions'])
        try:
            for sequence,data in ((3,QUESTION.encode()),(4,b'\r')):
                value=await self.public.call('terminal.input',{'attachmentId':self.public.view['attachmentId'],
                    'inputGrant':claim.input_grant,'inputEpoch':claim.input_epoch,'frameSeq':sequence,
                    'data':base64.b64encode(data).decode()})
                assert 'result' in value,value
                if sequence==3:await asyncio.sleep(.7)
            async with asyncio.timeout(10):
                while not state.owners.hooks.pending:await asyncio.sleep(.01)
            identifier,pending=next(iter(state.owners.hooks.pending.items()))
            async with asyncio.timeout(3):
                while True:
                    snapshot=await self.public.call('session.snapshot',{})
                    decisions=snapshot['result']['pendingDecisions']
                    card=next((d for d in decisions if d['decisionId']==identifier),None)
                    if card:break
                    await asyncio.sleep(.01)
            assert 'Exact input:' in card['description'] and 'tui-expiry-target' in card['description']
            contenders=[]
            for public,label,choice in ((self.public,'terminal-review','accept'),(other,'chat-review','decline')):
                fields={'sessionId':sid,'expectedRunId':run,'decisionId':identifier,'choiceId':choice,
                    'expectedRevision':card['revision'],'actionDigest':card['actionDigest']}
                registered=await public.call('operation.register',{'expectedRunId':run,'registrationKey':label,
                    'method':'decision.reply','payloadDigest':p.operation_digest('decision.reply',fields)})
                assert 'result' in registered,registered
                contenders.append((public,{**fields,'operationId':registered['result']['operationId']}))
            results=await asyncio.gather(*(public.call('decision.reply',fields) for public,fields in contenders))
            assert sum('result' in value for value in results)==1,results
            winner=next(i for i,value in enumerate(results) if 'result' in value)
            deny_ack=getattr(self,'denial_ack',False)
            allow_ack=getattr(self,'allow_ack',False)
            ack=deny_ack or allow_ack
            initial_state=results[winner]['result']['state']
            winner_receipt=results[winner]['result']
            if ack:
                assert initial_state in ('accepted','uncertain'),(winner,results)
                async with asyncio.timeout(20):
                    while winner_receipt['state']!='accepted':
                        await asyncio.sleep(.025)
                        observed=await contenders[winner][0].call('operation.get',{'operationId':contenders[winner][1]['operationId']})
                        assert 'result' in observed,observed
                        winner_receipt=observed['result']
            else:assert initial_state=='uncertain'
            async with asyncio.timeout(20):
                while owner.host.processed!=4 or launch.terminal_grants.record.processed!=4:await asyncio.sleep(.05)
            with c.db() as db:
                decision=db.get(RelayDecision,(sid,identifier)).decision
                assert decision['status']==('denied' if deny_ack else 'answered' if allow_ack else 'outcome_unknown') and decision['choiceId']==contenders[winner][1]['choiceId']
                receipts=[db.get(RelayOperation,fields['operationId']).receipt for _,fields in contenders]
                assert receipts[winner]['state']==('accepted' if ack else 'uncertain') and receipts[1-winner]['state']=='failed'
            before=self.base.calls
            replay=await contenders[winner][0].call('decision.reply',contenders[winner][1])
            assert replay['result']==winner_receipt and self.base.calls==before
            assert (self.base.work/'tui-expiry-target').exists()==getattr(self,'expected_target_exists',winner==0)
            proof.update(realCookieCsrfAuthentication=True,nativeTuiQuestion=True,publicReviewers=2,
                publicFirstValidCAS=True,loserFailedBeforeDispatch=True,exactReviewContext=True,
                initialAnswerState=initial_state,conclusiveReceiptRead=ack,unknownPreserved=not ack,denialApplicationAcknowledged=deny_ack,allowApplicationAcknowledged=allow_ack,noReplay=True,nativeToolId=pending['nativeToolId'],modelCalls=self.base.calls,
                scope='Actual enrolled native TUI question plus two cookie/CSRF-authenticated public Hub review connections, one owns Terminal input. Shared review UI is separately browser-tested; stock native approval keys and conclusive chosen-answer acknowledgement remain unqualified. Synthetic model/capability overrides only.')
        finally:
            await other.close()
            await self.public.close()


async def prepare(root,work,c):
    expiry=await prepare_expiry(root,work,c)
    return Case(expiry.base)
