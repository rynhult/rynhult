"""Observe an actual file approval then interrupt; never answer it or apply it."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path

from installed_security import prepare as prepare_security

from acc.server.db import write_intent
from acc.server.models import RelaySession
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


class Case:
    def __init__(self,base):self.base=base
    def __getattr__(self,key):return getattr(self.base,key)
    async def exercise(self,c,state,launch,manager,config):
        events=[];requests=[];original=state.owner.rpc.observe
        async def inventory(event):
            if event.get('method') in ('item/started','item/completed','turn/completed','serverRequest/resolved'):
                events.append(event)
            if 'id' in event:
                requests.append(event)
                return  # inventory only: no native response, no Hub choice, no effect
            await original(event)
        state.owner.rpc.observe=inventory
        state.adapter.record=replace(state.adapter.record,qualification_id='offline-file-inventory',
            qualification_date='2026-11-12',operations={'text':p.CapabilityEntry(p.Support.QUALIFIED,evidence=('isolated fixture',))})
        sid=launch.session_id;run=str(launch.run_id)
        @write_intent
        def qualify():
            with c.db() as db:db.get(RelaySession,sid).capabilities={'text':'qualified'};db.commit()
        qualify()
        with c.db() as db:revision=db.get(RelaySession,sid).control_revision
        fields={'sessionId':sid,'expectedRunId':run,'text':'Offline native file approval inventory',
            'expectedConfigurationRevision':0,'expectedControlRevision':revision,'ifBusy':'reject'}
        op=Hub(c.ctx).register(c.resolve,sid,run,'file-inventory','turn.submit',p.operation_digest('turn.submit',fields))
        await manager.submit(c.resolve,sid,{**fields,'operationId':op.operation_id})
        try:
            async with asyncio.timeout(20):
                while not requests:await asyncio.sleep(.02)
        except TimeoutError:
            print('Synthetic file inventory:',{'events':events,'calls':self.base.calls,
                'targetExists':self.base.file_target.exists()},flush=True)
            raise
        request,=requests
        await state.owner.request('turn/interrupt',{'threadId':state.binding.native_conversation,
            'turnId':request['params']['turnId']})
        async with asyncio.timeout(10):
            while not any(v.get('method')=='turn/completed' for v in events):await asyncio.sleep(.02)
        assert not self.base.file_target.exists()
        Path('/evidence/result.json').write_text(json.dumps({'provider':'codex','nativeApprovalRequest':request,'nativeFileApprovalObserved':request['method']=='item/fileChange/requestApproval',
            'lifecycle':events,'noAnswerOrApplication':True,'terminalMediated':False,'scope':'Native file request inventory and deliberate interruption only; no chosen answer, application or actor qualification.'}))


async def prepare(root,work,c):
    base=await prepare_security(root,work,c)
    from acc.server.compiler import compile_files
    base.inputs=replace(base.inputs,model='gpt-5.3-codex',sandbox='read-only',approval='on-request')
    base.compiled=compile_files(base.inputs,str(base.hook),'/usr/bin/true',agent_home='/home/probe',
        guardrails=json.loads(base.compiled.files['guardrails.json']))
    base.file_target=root/'file-approval-target.txt'
    original=base.codex_events
    def events(payload):
        if base.calls!=1:return original(payload)
        patch='*** Begin Patch\n*** Add File: '+str(base.file_target)+'\n+fixture file approval\n*** End Patch'
        if not any(v.get('name')=='apply_patch' for v in payload.get('tools',[])):
            print('Synthetic unnamed tool schemas:',[v for v in payload.get('tools',[]) if v.get('name') is None],flush=True)
            base.command="apply_patch <<'PATCH'\n"+patch+"\nPATCH"
            return original(payload)
        item={'id':'fc_file_inventory','type':'custom_tool_call','call_id':'file_inventory_tool','name':'apply_patch','input':patch,'status':'completed'}
        response={'id':'resp_file_inventory','object':'response','created_at':1,'status':'completed','model':'fixture-model',
            'output':[item],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}
        values=[('response.created',{'response':{**response,'status':'in_progress','output':[]}}),
            ('response.output_item.added',{'output_index':0,'item':{**item,'input':'','status':'in_progress'}}),
            ('response.custom_tool_call_input.delta',{'output_index':0,'item_id':item['id'],'delta':patch}),
            ('response.custom_tool_call_input.done',{'output_index':0,'item_id':item['id'],'input':patch}),
            ('response.output_item.done',{'output_index':0,'item':item}),('response.completed',{'response':response})]
        return [(name,{'type':name,**value,'sequence_number':n}) for n,(name,value) in enumerate(values)]
    base.codex_events=events
    return Case(base)
