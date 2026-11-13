"""Real cookie/CSRF frame -> owned stock decoder -> main CAS -> native application."""
import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path

from enrolled_codex_ack import prepare as prepare_ack
from public_native_http import Public

from acc.broker import ops
from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation, RelaySession
from acc.server.session_relay import stock_approval_frames, writers
from acc.session_relay import native_approval_aliases, stock_approval_input
from acc.session_relay import protocol as p


class Case:
    def __init__(self,base):self.base=base;self.accepted=True
    def __getattr__(self,key):return getattr(self.base,key)
    async def exercise(self,c,state,launch,manager,config):
        sid=state.spec.spec['session_id'];run=str(launch.run_id)
        assert writers.PUBLIC_WRITER_QUALIFIED is False
        assert stock_approval_frames.PUBLIC_STOCK_KEYS_QUALIFIED is False
        assert stock_approval_input.STOCK_APPROVAL_INPUT_QUALIFIED is False
        assert native_approval_aliases.NATIVE_APPROVAL_DECODER_QUALIFIED is False
        writers.PUBLIC_WRITER_QUALIFIED=True
        stock_approval_frames.PUBLIC_STOCK_KEYS_QUALIFIED=True
        stock_approval_input.STOCK_APPROVAL_INPUT_QUALIFIED=True
        native_approval_aliases.NATIVE_APPROVAL_DECODER_QUALIFIED=True
        state.adapter.record=replace(state.adapter.record,qualification_id='offline-public-stock-keys',
            qualification_date='2026-11-12',operations={key:p.CapabilityEntry(p.Support.QUALIFIED,
                evidence=('offline pinned fixture only',)) for key in ('text','terminal.input','decision.tool_approval')})
        @write_intent
        def qualify():
            with c.db() as db:
                row=db.get(RelaySession,sid)
                row.capabilities={key:'qualified' for key in ('text','terminal.input','decision.tool_approval')}
                db.commit();return row.control_revision
        revision=qualify();c.ctx.relay_runtime=manager
        c.ctx.settings=replace(c.ctx.settings,feature_overrides={'session_relay':True})
        public=await Public.create(c,manager,sid,features=['writer','terminal','decisions'])
        async def operation(method,key,params):
            registered=await public.call('operation.register',{'expectedRunId':run,'registrationKey':key,
                'method':method,'payloadDigest':p.operation_digest(method,params)})
            assert 'result' in registered,registered
            result=await public.call(method,{**params,'operationId':registered['result']['operationId']})
            assert 'result' in result,result
            return result['result']
        params={'sessionId':sid,'expectedRunId':run,'attachmentId':public.view['attachmentId'],
            'expectedRevision':revision,'view':'terminal','reason':'Offline explicit stock writer'}
        assert (await operation('writer.take','stock-public-take',params))['state']=='accepted'
        grant=await operation('control.claim','stock-public-claim',{'sessionId':sid,'expectedRunId':run,
            'attachmentId':public.view['attachmentId'],'expectedRevision':revision+1})
        host=state.terminal_host;view=state.owned_view;output=[]
        assert host.grant is not None and host.processed==0 and not host.draft
        assert host.writer is None and host.output_task is None
        # This fixture owns the sole output reader before the first connection.
        # Production retains its default internal output drainage.
        host.consume_output=False
        async def read():
            async for data in host.attach_read(host.run):output.append(data)
        reader=asyncio.create_task(read())
        answers=[];written=[];events=[];original=state.owner.respond_guarded
        observed=state.owner.rpc.observe
        async def observe(event):
            if event.get('method') in ('item/completed','turn/completed','serverRequest/resolved'):events.append(event)
            await observed(event)
        state.owner.rpc.observe=observe
        async def respond(identifier,value,before_write):
            answers.append({'id':identifier,'value':value})
            if getattr(self,'loss',None)=='before':raise ConnectionError('Fixture loss before provider response write')
            result=await original(identifier,value,before_write);written.append(identifier)
            if getattr(self,'loss',None)=='after':raise ConnectionError('Fixture loss after provider response write')
            return result
        state.owner.respond_guarded=respond
        async def frame(sequence,data):
            return await public.call('terminal.input',{'attachmentId':public.view['attachmentId'],
                'inputGrant':grant['inputGrant'],'inputEpoch':grant['inputEpoch'],'frameSeq':sequence,
                'data':base64.b64encode(data).decode()})
        try:
            async with asyncio.timeout(10):
                while not output:await asyncio.sleep(.01)
            assert 'result' in await frame(1,b'Offline public stock approval request')
            assert 'result' in await frame(2,b'\r')
            async with asyncio.timeout(20):
                while host.approval_input.offer is None:await asyncio.sleep(.025)
            offer=host.approval_input.offer
            assert host.processed==2 and host.draft=='' and host.ticket is None and not answers
            async with asyncio.timeout(10):
                while True:
                    pane=ops.tmux(config,'capture-pane','-p','-J','-t','='+view.name+':',check=True).stdout
                    if self.base.command in pane and 'Press enter to confirm or esc to cancel' in pane:break
                    await asyncio.sleep(.025)
            if hasattr(self,'lifecycle'):
                await self.lifecycle(c,state,launch,manager,config,public,host,view,offer,answers,frame)
                return
            choice='accept' if self.accepted else 'cancel';raw=b'\r' if self.accepted else b'\x1b'
            result=await frame(3,raw)
            assert 'result' in result,result
            submission=launch.terminal_grants.approvals.submission
            assert submission.frame.offer.alias==offer.alias and submission.frame.choice==choice
            if getattr(self,'loss',None)=='before':
                async with asyncio.timeout(15):
                    while True:
                        with c.db() as db:
                            receipt=dict(db.get(RelayOperation,submission.operation_id).receipt)
                            decision=dict(db.get(RelayDecision,(sid,offer.decision_id)).decision)
                        if receipt['state']=='uncertain' and host.input_fenced:break
                        await asyncio.sleep(.025)
                entry=state.adapter.answer_evidence.entries[offer.decision_id]
                await state.owner.request('turn/interrupt',{'threadId':entry['thread'],'turnId':entry['turn']})
                async with asyncio.timeout(10):
                    while True:
                        with c.db() as db:active=db.get(RelaySession,sid).active_operation_id
                        if any(event.get('method')=='turn/completed' for event in events) and active is None:break
                        await asyncio.sleep(.025)
                with c.db() as db:
                    receipt=dict(db.get(RelayOperation,submission.operation_id).receipt)
                    decision=dict(db.get(RelayDecision,(sid,offer.decision_id)).decision)
                assert receipt['state']=='uncertain' and decision['status']=='outcome_unknown'
                assert host.processed==launch.terminal_grants.record.processed==2 and host.input_fenced
                assert len(answers)==1 and not written
                assert 'error' in await frame(3,raw) and len(answers)==1
                snapshot=(await public.call('session.snapshot',{}))['result']
                assert any(value['decisionId']==offer.decision_id and value['status']=='outcome_unknown'
                    for value in snapshot['pendingDecisions'])
                Path('/evidence/result.json').write_text(json.dumps({'provider':'codex','choice':choice,'loss':'before',
                    'receipt':receipt,'decision':decision,'alias':offer.alias,'nativeResponseAttempts':answers,
                    'responseWrites':0,'publicCookieCsrf':True,'originalFrameActor':True,
                    'preByteRegisteredOperation':submission.operation_id,'nativeProcessedFrame':host.processed,
                    'mainProcessedFrame':launch.terminal_grants.record.processed,'conclusiveProviderApplication':False,
                    'unknownPreserved':True,'noReplay':True,'publicUnknownVisible':True,'terminalMediated':False,
                    'scope':'Chosen public Allow with simulated response-write loss before provider input. Exact unchosen-turn cleanup cannot prove Allow application. Original receipt stays unknown and old frame/input cannot replay. No recovery or restart qualification.'}))
                return
            async with asyncio.timeout(20):
                while True:
                    with c.db() as db:
                        receipt=dict(db.get(RelayOperation,submission.operation_id).receipt)
                        decision=dict(db.get(RelayDecision,(sid,offer.decision_id)).decision)
                    if receipt['state']=='accepted' and host.processed==3:break
                    await asyncio.sleep(.025)
            assert decision['status']==('answered' if self.accepted else 'denied')
            assert receipt['actor']==p.to_wire(submission.frame.grant.actor)
            assert receipt['payloadDigest']==p.operation_digest('decision.reply',launch.terminal_grants.approvals.params(submission))
            assert len(answers)==1 and answers[0]['value']=={'decision':choice}
            assert not host.input_fenced and host.approval_input.ticket is None
            assert launch.terminal_grants.record.processed==3
            assert 'error' in await frame(3,raw) and len(answers)==1
            entry=state.adapter.answer_evidence.entries[offer.decision_id]
            completed=[event['params']['item'] for event in events if event.get('method')=='item/completed'
                and event.get('params',{}).get('turnId')==entry['turn']
                and event.get('params',{}).get('item',{}).get('id')==entry['item']]
            assert len(completed)==1
            command_evidence={key:completed[0].get(key) for key in ('id','type','status','exitCode')}
            if getattr(self,'expected_exit',None) is not None:
                assert command_evidence['status']=='failed' and command_evidence['exitCode']==self.expected_exit
            snapshot=(await public.call('session.snapshot',{}))['result']
            assert any(value['decisionId']==offer.decision_id for value in snapshot['decisionOutcomes'])
            Path('/evidence/result.json').write_text(json.dumps({'provider':'codex','choice':choice,
                'receipt':receipt,'decision':decision,'alias':offer.alias,'nativeResponseAttempts':answers,
                'responseWrites':len(written),'loss':getattr(self,'loss',None),'commandEvidence':command_evidence,
                'expectedExit':getattr(self,'expected_exit',None),'publicCookieCsrf':True,'originalFrameActor':True,'preByteRegisteredOperation':submission.operation_id,
                'publicInputFrames':3,'nativeProcessedFrame':host.processed,'mainProcessedFrame':launch.terminal_grants.record.processed,
                'conclusiveProviderApplication':True,'noReplay':True,'noSyntheticStartupTyping':True,
                'modelCalls':self.base.calls,'terminalMediated':False,
                'scope':'Actual enrolled pinned Codex command approval: cookie/CSRF public key frame, immutable original input grant/actor/lease/epoch/frame/alias, real owned decoder, original main CAS and exact native command outcome. Fixture-only capability flags and generated offline model; no real account, other keys/files, restart or Claude Allow qualification.'}))
        finally:
            await public.close();reader.cancel();await asyncio.gather(reader,return_exceptions=True)


async def prepare(root,work,c):
    ack=await prepare_ack(root,work,c)
    return Case(ack.base)
