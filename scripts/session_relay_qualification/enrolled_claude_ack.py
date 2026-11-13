"""Exact native SDK chosen-answer lifecycle and loss countercases."""
import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from enrolled_decisions import prepare as prepare_decisions
from public_native_http import Public

from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation, RelaySession
from acc.server.session_relay import claude_public, interrupts
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


class Case:
    choice='decline'
    interrupt=False
    loss=None
    def __init__(self,base):self.base=base
    def __getattr__(self,name):return getattr(self.base,name)
    async def exercise(self,c,state,launch,manager,config):
        self.interrupt_client = None
        self.interrupt_proof = None
        if not self.interrupt and self.loss != 'before':
            return await self.chosen_exercise(c,state,launch,manager,config)
        qualification = (claude_public.PUBLIC_CLAUDE_QUALIFIED, interrupts.PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED)
        claude_public.PUBLIC_CLAUDE_QUALIFIED = interrupts.PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED = True
        @write_intent
        def qualify():
            with c.db() as db:
                row = db.get(RelaySession, state.spec.spec['session_id'])
                row.capabilities = {**row.capabilities, 'text': 'qualified', 'interrupt': 'qualified',
                    'switch.to_chat': 'qualified', 'switch.to_terminal': 'qualified'}
                db.commit()
        try:
            qualify()
            c.ctx.relay_runtime = manager
            self.interrupt_client = await Public.create(c,manager,state.spec.spec['session_id'],features=['queue'])
            return await self.chosen_exercise(c,state,launch,manager,config)
        finally:
            if self.interrupt_client:await self.interrupt_client.close()
            claude_public.PUBLIC_CLAUDE_QUALIFIED, interrupts.PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED = qualification

    async def chosen_exercise(self,c,state,launch,manager,config):
        from acc.session_relay import claude_answer_evidence
        claude_answer_evidence.CLAUDE_ACK_QUALIFIED=True
        sid=state.spec.spec['session_id'];run=str(launch.run_id)
        environment=state.owners.environment(state.spec)
        assert Path(environment['HOME'])==self.base.root/'home'
        assert Path(environment['CLAUDE_CONFIG_DIR'])==self.base.root/'home'/'.claude'
        adapter=state.owners.current;wire=adapter.client._transport
        messages=[];hooks=[];attempts=[];writes=[]
        original_publish=adapter.publish;original_observe=adapter.observe_hook;original_write=wire.base.write
        original_call=launch.peer.call
        async def publish(event):
            if event.get('kind')=='provider_message':
                value=event['message']
                if event['sdk_type']=='UserMessage':
                    for block in value.get('content',[]):
                        if isinstance(block,dict) and 'tool_use_id' in block:
                            messages.append({**{k:block.get(k) for k in ('tool_use_id','is_error')},
                                'diagnostic':str(block.get('content',''))[:400]})
            return await original_publish(event)
        def observe(payload):
            if payload.get('tool_use_id'):
                hooks.append({'event':payload.get('hook_event_name'),'toolId':payload['tool_use_id']})
            return original_observe(payload)
        async def call(method,params):
            value=await original_call(method,params)
            if self.loss=='after' and method=='owner.answer':
                raise ConnectionError('Artificial lost caller acknowledgement after native answer dispatch')
            return value
        async def write(data):
            value=json.loads(data);body=value.get('response',{})
            if value.get('type')=='control_response' and body.get('request_id') in wire.requests:
                attempts.append(body.get('response',{}).get('behavior'))
                if self.loss=='before':return  # acknowledged local write whose bytes never reach native CLI
                await original_write(data);writes.append(attempts[-1])
                return
            return await original_write(data)
        adapter.publish=publish;adapter.observe_hook=observe;wire.base.write=write;launch.peer.call=call
        fields={'sessionId':sid,'expectedRunId':run,'text':'Offline exact SDK chosen answer',
            'expectedConfigurationRevision':0,'expectedControlRevision':0,'ifBusy':'reject'}
        if self.interrupt_client:
            registered=await self.interrupt_client.call('operation.register',{'expectedRunId':run,
                'registrationKey':'claude-ack-turn','method':'turn.submit','payloadDigest':p.operation_digest('turn.submit',fields)})
            assert 'result' in registered,registered
            turn_id=registered['result']['operationId']
            submitted=await self.interrupt_client.call('turn.submit',{**fields,'operationId':turn_id})
            assert 'result' in submitted,submitted
        else:
            op=Hub(c.ctx).register(c.resolve,sid,run,'claude-ack-turn','turn.submit',p.operation_digest('turn.submit',fields))
            await manager.submit(c.resolve,sid,{**fields,'operationId':op.operation_id})
        async def interrupt():
            with c.db() as db:revision=db.get(RelaySession,sid).control_revision
            fields={'sessionId':sid,'expectedRunId':run,'turnId':turn_id,'expectedControlRevision':revision}
            registered=await self.interrupt_client.call('operation.register',{'expectedRunId':run,
                'registrationKey':'claude-ack-interrupt','method':'turn.interrupt',
                'payloadDigest':p.operation_digest('turn.interrupt',fields)})
            assert 'result' in registered,registered
            reply=await self.interrupt_client.call('turn.interrupt',{**fields,'operationId':registered['result']['operationId']})
            assert reply.get('result',{}).get('state') in ('accepted','uncertain'),reply
            receipt=reply['result']
            assert receipt['method']=='turn.interrupt' and receipt['sessionId']==sid and receipt['runId']==run
            assert receipt['payloadDigest']==p.operation_digest('turn.interrupt',fields)
            self.interrupt_proof={'cookieCsrfRouter':True,'exactTurn':True,'sameSubmittingAttachment':True,
                'receiptDigestBound':True,'state':receipt['state']}
        async with asyncio.timeout(15):
            while not adapter.pending:await asyncio.sleep(.02)
        identifier=next(iter(adapter.pending))
        async with asyncio.timeout(5):
            while True:
                with c.db() as db:
                    row=db.get(RelayDecision,(sid,identifier))
                    if row:decision=dict(row.decision);break
                await asyncio.sleep(.02)
        identity=adapter.native_decisions[identifier]
        answer_id=None
        if self.interrupt:
            await interrupt()
        else:
            fields={'sessionId':sid,'expectedRunId':run,'decisionId':identifier,'choiceId':self.choice,
                'expectedRevision':decision['revision'],'actionDigest':decision['actionDigest']}
            operation=Hub(c.ctx).register(c.resolve,sid,run,'claude-ack-answer','decision.reply',p.operation_digest('decision.reply',fields))
            answer_id=operation.operation_id
            if getattr(self,'before_answer',None):await self.before_answer()
            await launch.answer(c.resolve,{**fields,'operationId':answer_id})
            if self.loss=='before':
                await asyncio.sleep(.1)
                await interrupt()
        async with asyncio.timeout(20):
            while adapter.active:await asyncio.sleep(.02)
        expected=not self.interrupt and self.loss!='before'
        if answer_id and expected:
            async with asyncio.timeout(10):
                while True:
                    with c.db() as db:status=db.get(RelayOperation,answer_id).receipt['state']
                    if status=='accepted':break
                    if not getattr(self,'diagnosed_ack',False):
                        self.diagnosed_ack=True
                        print('Synthetic SDK result inventory:',{'tool':identity.tool,'results':wire.results,
                            'typed':[{k:v for k,v in value.items() if k!='diagnostic'} for value in messages],
                            'hooks':hooks},flush=True)
                    await asyncio.sleep(.02)
        with c.db() as db:
            final=dict(db.get(RelayDecision,(sid,identifier)).decision)
            receipt=dict(db.get(RelayOperation,answer_id).receipt) if answer_id else None
        if answer_id:
            assert receipt['state']==('accepted' if expected else 'uncertain'),receipt
            assert final['status']==(('answered' if self.choice=='accept' else 'denied') if expected else 'outcome_unknown')
        proof={'provider':'claude','nativeId':state.binding.native_conversation,'choice':self.choice,
            'publicInterrupt':self.interrupt_proof,
            'interrupted':self.interrupt,'loss':self.loss,'requestId':identity.request_id,'toolId':identity.tool_use_id,'tool':identity.tool,
            'wireCurrent':wire.current(identity),'response':wire.responses.get(identity.request_id),
            'nativeResults':wire.results,'nativeToolResults':messages,'hooks':hooks,
            'attempts':attempts,'writes':writes,'finalDecision':final,'receipt':receipt,'modelCalls':self.base.calls,
            'inventoryOnly':False,'applicationConfirmed':expected,'terminalMediated':False,
            'scope':'Actual enrolled pinned API-key native SDK with exact native IDs, original Hub chosen answer or native interrupt. Exact original receipt settles only after qualified native lifecycle; interruption and loss before native consumption stay unknown.'}
        Path('/evidence/result.json').write_text(json.dumps(proof))


async def prepare(root,work,c):
    (root/'home').mkdir(mode=0o700)
    original=await prepare_decisions(root,work,c)
    original.base.command='printf fixture > '+str(work/'claude-ack-target')
    # Stricter generated fixture policy asks even for an otherwise safe sandbox
    # command. No account settings, guard or sandbox exception is changed.
    files=dict(original.base.compiled.files)
    settings=json.loads(files['settings.json']);settings.setdefault('permissions',{})['ask']=['Bash']
    files['settings.json']=json.dumps(settings)
    digest=hashlib.sha256(json.dumps({'inputs':original.base.inputs.as_dict(),'files':files},sort_keys=True).encode()).hexdigest()
    original.base.compiled=replace(original.base.compiled,files=files,digest=digest)
    return Case(original.base)
