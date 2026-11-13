"""Native command item lifecycle inventory, distinct from a qualified answer ACK."""
import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path

from installed_security import prepare as prepare_security
from public_native_http import Public

from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelaySession
from acc.server.session_relay import writers
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


class Attached:
    attachment_id='offline-codex-ack'
    def __init__(self,resolve):self.resolve=resolve
    def __call__(self):return self.resolve()


class Case:
    interrupted=False
    accepted=False
    loss=None
    identical=False
    expect_application=True
    request_method='item/commandExecution/requestApproval'
    item_type='commandExecution'
    selected_choice=None
    def __init__(self,base):self.base=base
    def __getattr__(self,key):return getattr(self.base,key)
    async def exercise(self,c,state,launch,manager,config):
        sid=state.spec.spec['session_id'];run=str(launch.run_id);native=state.binding.native_conversation
        events=[];original=state.owner.rpc.observe
        async def inventory(event):
            if 'id' in event or event.get('method') in ('item/completed','serverRequest/resolved','turn/completed'):
                events.append(event)
            await original(event)
        state.owner.rpc.observe=inventory
        state.adapter.record=replace(state.adapter.record,qualification_id='offline-ack-inventory',
            qualification_date='2026-11-12',operations={key:p.CapabilityEntry(p.Support.QUALIFIED,
                evidence=('isolated fixture only',)) for key in ('text','decision.tool_approval')})
        @write_intent
        def capabilities():
            with c.db() as db:
                db.get(RelaySession,sid).capabilities={'text':'qualified','decision.tool_approval':'qualified'}
                db.commit()
        capabilities();c.ctx.relay_runtime=manager
        with c.db() as db:revision=db.get(RelaySession,sid).control_revision
        params={'sessionId':sid,'expectedRunId':run,'text':'Offline native command acknowledgement inventory',
            'expectedConfigurationRevision':0,'expectedControlRevision':revision,'ifBusy':'reject'}
        # The initial Chat owner has an internal lifecycle resolver, not an
        # invented public writer attachment. Review below has its own attachment.
        registration=Hub(c.ctx).register(c.resolve,sid,run,'codex-ack-turn','turn.submit',p.operation_digest('turn.submit',params))
        await manager.submit(c.resolve,sid,{**params,'operationId':registration.operation_id})
        resolve=Attached(c.resolve)
        async with asyncio.timeout(20):
            while True:
                with c.db() as db:
                    cards=db.query(RelayDecision).filter_by(session_id=sid).all()
                    if len(cards)>=(2 if self.identical else 1):
                        decisions=[dict(value.decision) for value in cards];decision=decisions[0];break
                await asyncio.sleep(.02)
        request=next(v for v in events if v.get('method')==self.request_method)
        assert request['params']['threadId']==native
        answers=[];written=[];original_respond=state.owner.respond_guarded
        async def respond(identifier,value,before_write):
            answers.append({'id':identifier,'choice':value['decision']})
            if self.loss=='before':raise ConnectionError('Artificial loss before response write')
            await original_respond(identifier,value,before_write)
            written.append(identifier)
            if self.loss=='after':raise ConnectionError('Artificial loss after response write')
        state.owner.respond_guarded=respond
        if self.interrupted:
            await state.owner.request('turn/interrupt',{'threadId':native,'turnId':request['params']['turnId']})
            response=None
        else:
            previous=writers.PUBLIC_WRITER_QUALIFIED;writers.PUBLIC_WRITER_QUALIFIED=True
            c.ctx.settings=replace(c.ctx.settings,feature_overrides={'session_relay':True})
            try:
                chosen=[]
                for index,decision in enumerate(decisions):
                    denied=next(value['id'] for value in decision['choices'] if value['effect']=='deny')
                    answer={'sessionId':sid,'expectedRunId':run,'decisionId':decision['decisionId'],'choiceId':self.selected_choice or ('accept' if self.accepted else denied),
                        'expectedRevision':decision['revision'],'actionDigest':decision['actionDigest']}
                    operation=Hub(c.ctx).register(resolve,sid,run,'codex-ack-answer-'+str(index),'decision.reply',p.operation_digest('decision.reply',answer))
                    if getattr(self,'before_answer',None):await self.before_answer(c,state,launch,manager,resolve,decision)
                    response=p.to_wire(await manager.answer_decision(resolve,sid,{**answer,'operationId':operation.operation_id}))
                    assert response['state'] in ('uncertain','accepted')
                    chosen.append((decision,operation,answer))
                if self.loss=='before':
                    await state.owner.request('turn/interrupt',{'threadId':native,'turnId':request['params']['turnId']})
            finally:writers.PUBLIC_WRITER_QUALIFIED=previous
        async with asyncio.timeout(20):
            while not any(v.get('method')=='turn/completed' for v in events):await asyncio.sleep(.02)
        applied=not self.interrupted and self.loss!='before' and self.expect_application
        if self.interrupted:
            async with asyncio.timeout(10):
                while True:
                    with c.db() as db:status=db.get(RelayDecision,(sid,decision['decisionId'])).decision['status']
                    if status=='cancelled':break
                    await asyncio.sleep(.02)
        if not self.interrupted:
            from acc.server.models import RelayOperation
            async with asyncio.timeout(10):
                while True:
                    with c.db() as db:
                        settled=[(dict(db.get(RelayOperation,op.operation_id).receipt),
                            dict(db.get(RelayDecision,(sid,d['decisionId'])).decision)) for d,op,_ in chosen]
                    if not applied or all(receipt['state']=='accepted' for receipt,_ in settled):break
                    await asyncio.sleep(.02)
            for receipt,final in settled:
                assert receipt['state']==('accepted' if applied else 'uncertain'),(receipt,events)
                expected=('answered' if self.accepted else 'denied') if applied else 'outcome_unknown'
                assert final['status']==expected
            previous=writers.PUBLIC_WRITER_QUALIFIED;writers.PUBLIC_WRITER_QUALIFIED=True
            try:
                await manager.answer_decision(resolve,sid,{**answer,'operationId':operation.operation_id})
            finally:writers.PUBLIC_WRITER_QUALIFIED=previous
            assert len(answers)==len(chosen)  # receipt lookup never writes a response again
        relevant=[]
        for event in events:
            method=event.get('method');params=event.get('params',{})
            if method=='item/completed' and params.get('item',{}).get('type')==self.item_type:
                relevant.append({'method':method,'threadId':params.get('threadId'),'turnId':params.get('turnId'),
                    'itemId':params['item'].get('id'),'status':params['item'].get('status'),
                    'exitCode':params['item'].get('exitCode')})
            elif method=='turn/completed':
                relevant.append({'method':method,'threadId':params.get('threadId'),'turnId':params.get('turn',{}).get('id'),
                    'status':params.get('turn',{}).get('status')})
            elif method=='serverRequest/resolved':relevant.append({'method':method,**params})
        public=await Public.create(c,manager,sid,features=[])
        try:
            view=(await public.call('session.snapshot',{}))['result']
            expected_ids=({value['decisionId'] for value,_,_ in chosen} if applied else
                {decision['decisionId']} if self.interrupted else set())
            assert {value['decisionId'] for value in view['decisionOutcomes']}==expected_ids
            assert all(value['status']=='cancelled' and value.get('winner') is None or
                value['status'] in ('answered','denied') and value['winner']['principal']=='alex'
                for value in view['decisionOutcomes'])
        finally:await public.close()
        Path('/evidence/result.json').write_text(json.dumps({'provider':'codex','nativeId':native,'invalidCancelRefused':getattr(self,'invalid_cancel_refused',False),
            'nativeRequest':{k:request['params'].get(k) for k in ('threadId','turnId','itemId')},
            'requestId':request['id'],'lifecycle':relevant,'interrupted':self.interrupted,
            'choices':decision['choices'],
            'receipt':response,'inventoryOnly':False,'applicationConfirmed':applied,'lostResponse':self.loss,
            'selectedAllow':self.accepted,'unknownPreserved':self.loss=='before','unchosenQuestionCancelled':self.interrupted,
            'responseAttempts':len(answers),'responseWrites':len(written),
            'identicalConcurrentCommands':self.identical,'independentChosenReceipts':len(chosen) if not self.interrupted else 0,
            'finalReceipts':[receipt for receipt,_ in settled] if not self.interrupted else [],
            'finalDecisionStatuses':[value['status'] for _,value in settled] if not self.interrupted else [],
            'publicCookieApplicationOutcomeVisible':True,
            'modelCalls':self.base.calls,'terminalMediated':False,
            'scope':'Actual enrolled pinned native command approval and original main Hub winner/receipt. Choice-specific exact native request/item/turn lifecycle settles only correlated application; unanswered interrupt and pre-write loss remain unknown. Lost response after write and identical concurrent commands never replay. Synthetic model/principal/feature overrides only; no Claude or file-change qualification.'}))


async def prepare(root,work,c):
    base=await prepare_security(root,work,c)
    base.command='printf fixture > /tmp/relay-codex-ack-target'
    original=base.codex_events
    case=Case(base)
    def events(payload):
        values=original(payload)
        if base.calls==1:
            for _,value in values:
                item=value.get('item')
                if isinstance(item,dict) and item.get('type')=='function_call' and item.get('arguments'):
                    args={'cmd':base.command,'sandbox_permissions':'require_escalated','justification':'Offline person decision fixture'}
                    item['arguments']=json.dumps(args)
                if value.get('type') in ('response.function_call_arguments.delta','response.function_call_arguments.done'):
                    value['delta' if 'delta' in value else 'arguments']=json.dumps({'cmd':base.command,
                        'sandbox_permissions':'require_escalated','justification':'Offline person decision fixture'})
                for item in value.get('response',{}).get('output',[]):
                    if item.get('type')=='function_call':item['arguments']=json.dumps({'cmd':base.command,
                        'sandbox_permissions':'require_escalated','justification':'Offline person decision fixture'})
        if case.identical and base.calls==1:
            result=[values[0]];items=[]
            for index in range(2):
                for name,value in values[1:-1]:
                    value=copy.deepcopy(value)
                    if 'output_index' in value:value['output_index']=index
                    if 'item_id' in value:value['item_id']='fc_identical_'+str(index)
                    if 'item' in value:
                        value['item']['id']='fc_identical_'+str(index)
                        value['item']['call_id']='identical_tool_'+str(index)
                        if name=='response.output_item.done':items.append(value['item'])
                    result.append((name,value))
            name,last=copy.deepcopy(values[-1]);last['response']['output']=items
            result.append((name,last));return result
        return values
    base.codex_events=events
    return case
