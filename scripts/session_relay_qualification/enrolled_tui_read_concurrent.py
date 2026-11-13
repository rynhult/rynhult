"""Identical actual native Read questions, late evidence and publication loss."""
import asyncio
import base64
import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from enrolled_public_decisions import Case as PublicCase
from enrolled_tui_expiry import QUESTION
from enrolled_tui_expiry import prepare as prepare_expiry
from public_native_http import Public

from acc.server.models import RelayDecision, RelayOperation
from acc.session_relay import protocol as p


class Case(PublicCase):
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        from acc.broker.claude_denial_evidence import NativeDenial
        sid,run=state.spec.spec['session_id'],str(launch.run_id)
        other=await Public.create(c,manager,sid,features=['decisions'])
        visible=False
        native_read=NativeDenial.denied
        native_answer=state.owners.hooks.answer
        publish=state.publish
        answers={};failures=set();attempts={};observed={}
        def delayed(reader,reference,prompt_id):
            if not visible:return None
            result=native_read(reader,reference,prompt_id)
            if result:observed[reference]=result
            return result
        async def once(identifier,*args,**kwargs):
            assert identifier not in answers
            answers[identifier]=kwargs['deny_reference']
            return await native_answer(identifier,*args,**kwargs)
        async def transient(event):
            if event.get('kind')=='decision.applied':
                op=event['operationId'];attempts[op]=attempts.get(op,0)+1
                if op not in failures:
                    failures.add(op)
                    raise ConnectionError('Fixture drops evidence publication before main receives it')
            return await publish(event)
        NativeDenial.denied=delayed
        state.owners.hooks.answer=once
        state.publish=transient
        try:
            for sequence,data in ((3,QUESTION.encode()),(4,b'\r')):
                value=await self.public.call('terminal.input',{'attachmentId':self.public.view['attachmentId'],
                    'inputGrant':claim.input_grant,'inputEpoch':claim.input_epoch,'frameSeq':sequence,
                    'data':base64.b64encode(data).decode()})
                assert 'result' in value,value
                if sequence==3:await asyncio.sleep(.7)
            async with asyncio.timeout(10):
                while len(state.owners.hooks.pending)!=2:await asyncio.sleep(.01)
            identifiers=set(state.owners.hooks.pending)
            assert all(v['nativeToolId'] is None for v in state.owners.hooks.pending.values())
            async with asyncio.timeout(3):
                while True:
                    snapshot=await self.public.call('session.snapshot',{})
                    cards={d['decisionId']:d for d in snapshot['result']['pendingDecisions'] if d['decisionId'] in identifiers}
                    if len(cards)==2:break
                    await asyncio.sleep(.01)
            assert len({d['actionDigest'] for d in cards.values()})==1  # identical inputs, no identity inference
            assert all('concurrent-read-target' in d['description'] for d in cards.values())
            selected=[];losers=[]
            for index,(identifier,card) in enumerate(cards.items()):
                chosen=other if index else self.public
                for public,choice,label,bucket in ((chosen,'decline','deny',selected),
                        (self.public if index else other,'accept','loser',losers)):
                    fields={'sessionId':sid,'expectedRunId':run,'decisionId':identifier,'choiceId':choice,
                        'expectedRevision':card['revision'],'actionDigest':card['actionDigest']}
                    result=await public.call('operation.register',{'expectedRunId':run,'registrationKey':label+'-'+identifier,
                        'method':'decision.reply','payloadDigest':p.operation_digest('decision.reply',fields)})
                    assert 'result' in result,result
                    bucket.append((public,{**fields,'operationId':result['result']['operationId']}))
            async def rejected(public,fields):
                await asyncio.sleep(.3)
                result=await public.call('decision.reply',fields)
                assert 'error' in result,result
            calls=[asyncio.create_task(public.call('decision.reply',fields)) for public,fields in selected]
            defeated=[asyncio.create_task(rejected(public,fields)) for public,fields in losers]
            initial=await asyncio.gather(*calls)
            await asyncio.gather(*defeated)
            assert all(r['result']['state']=='uncertain' for r in initial),initial
            assert len(answers)==2 and len(set(answers.values()))==2 and not observed and not attempts
            originals={}
            with c.db() as db:
                for _,fields in selected:
                    op=fields['operationId'];receipt=dict(db.get(RelayOperation,op).receipt)
                    assert receipt['state']=='uncertain'
                    assert db.get(RelayDecision,(sid,fields['decisionId'])).decision['status']=='outcome_unknown'
                    originals[op]=receipt
            for (public,fields),first in zip(selected,initial,strict=True):
                replay=await public.call('decision.reply',fields)
                assert replay==first
            assert len(answers)==2 and len(state.denial_observers)==2
            visible=True  # release only native reads; never manufacture an ACK or repeat a response
            try:
                async with asyncio.timeout(10):
                    while True:
                        with c.db() as db:
                            settled=all(db.get(RelayOperation,op).receipt['state']=='accepted' for op in originals)
                        if settled and not state.denial_observers:break
                        await asyncio.sleep(.025)
            except TimeoutError:
                print('Native late evidence diagnostic:',answers,observed,attempts,len(state.denial_observers),flush=True)
                for line in Path(state.native_transcript[1]).read_text().splitlines():
                    row=json.loads(line)
                    items=row.get('message',{}).get('content',[])
                    tools=[i.get('id') for i in items if isinstance(i,dict) and i.get('type')=='tool_use'] if isinstance(items,list) else []
                    if tools or row.get('attachment',{}).get('type')=='hook_permission_decision' or row.get('toolDenialKind'):
                        print('Native denial row:',{k:row.get(k) for k in ('uuid','parentUuid','sourceToolAssistantUUID','promptId')},
                            tools,row.get('attachment'),row.get('permissionDecision'),row.get('toolUseResult'),flush=True)
                raise
            final=[]
            with c.db() as db:
                for _,fields in selected:
                    receipt=dict(db.get(RelayOperation,fields['operationId']).receipt)
                    assert db.get(RelayDecision,(sid,fields['decisionId'])).decision['status']=='denied'
                    for key in ('operationId','actor','payloadDigest','runId','method'):
                        assert receipt[key]==originals[fields['operationId']][key]
                    final.append(receipt)
                assert all(db.get(RelayOperation,fields['operationId']).receipt['state']=='failed' for _,fields in losers)
            for (public,fields),receipt in zip(selected,final,strict=True):
                assert (await public.call('decision.reply',fields))['result']==receipt
            assert len(answers)==2 and len(observed)==2 and len(set(observed.values()))==2
            assert all(value==2 for value in attempts.values())
            path=Path(state.native_transcript[1]);assert path.is_relative_to(self.base.root/'home')
            rows=[json.loads(line) for line in path.read_text().splitlines()]
            native_ids={}
            for row in rows:
                for item in row.get('message',{}).get('content',[]) if isinstance(row.get('message',{}).get('content'),list) else []:
                    if isinstance(item,dict) and item.get('type')=='tool_result':
                        for identifier,reference in answers.items():
                            if reference in str(item.get('content')):native_ids[identifier]=item['tool_use_id']
            assert set(native_ids)==identifiers and len(set(native_ids.values()))==2
            async with asyncio.timeout(20):
                while owner.host.processed!=4 or launch.terminal_grants.record.processed!=4:await asyncio.sleep(.05)
            proof.update(nativeConcurrentIdenticalReadQuestions=True,exactNativeIds=native_ids,
                deniedOriginalReceipts=final,foregroundUnknownBeforeNativeEvidence=True,
                lateNativeEvidenceReconciled=True,publicationLostBeforeMainRetried=True,
                originalActorPayloadPreserved=True,oneAnswerPerQuestion=True,noReplay=True,
                isolatedHomeTranscript=True,observerResourcesReclaimed=True,
                scope='Two identical actual native Read questions with distinct private references; original public CAS, real two-second foreground unknown, delayed protected native evidence, pre-main publication loss/idempotent retry, original receipts and no native answer replay. Synthetic isolated generated HOME only; no TUI Allow/stock key/subscription qualification.')
        finally:
            NativeDenial.denied=native_read
            await other.close()
            await self.public.close()


async def prepare(root,work,c):
    from acc.broker import claude_denial_evidence
    claude_denial_evidence.CLAUDE_TUI_DENY_ACK_QUALIFIED=True
    expiry=await prepare_expiry(root,work,c)
    native=expiry.base.base
    target=work/'concurrent-read-target';target.write_text('fixture read content\n')
    original=native.claude_events
    def events(payload):
        values=original(payload)
        if native.native_tool_offered and any(name=='content_block_start' and value['content_block'].get('type')=='tool_use' for name,value in values):
            extra=[]
            for name,value in values:
                if name=='content_block_start':value['content_block']['name']='Read'
                if name=='content_block_delta':value['delta']['partial_json']=json.dumps({'file_path':str(target)})
                if name.startswith('content_block_'):
                    twin=copy.deepcopy(value);twin['index']=1
                    if name=='content_block_start':twin['content_block']['id']='installed_security_twin'
                    extra.append((name,twin))
            index=next(i for i,(name,_) in enumerate(values) if name=='message_delta')
            values[index:index]=extra
        return values
    native.claude_events=events
    files=dict(native.compiled.files);settings=json.loads(files['settings.json']);settings.setdefault('permissions',{})['ask']=['Read']
    files['settings.json']=json.dumps(settings)
    digest=hashlib.sha256(json.dumps({'inputs':native.inputs.as_dict(),'files':files},sort_keys=True).encode()).hexdigest()
    native.compiled=replace(native.compiled,files=files,digest=digest)
    return Case(expiry.base)
