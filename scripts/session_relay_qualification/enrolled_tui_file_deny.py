"""Native Write/Edit permission denial after actual automatic prerequisite Read."""
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

from enrolled_tui_deny_ack import Case as DenyCase
from enrolled_tui_expiry import prepare as prepare_expiry
from installed_security import Security


class Case(DenyCase):
    expected_target_exists=True

    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        answer=state.owners.hooks.answer;writes=[]
        async def exact(identifier,*args,**kwargs):
            assert state.owners.hooks.pending[identifier]['toolName']==self.tool
            writes.append(identifier)
            return await answer(identifier,*args,**kwargs)
        state.owners.hooks.answer=exact
        await super().after_granted(c,state,launch,manager,owner,attached,claim,proof)
        assert len(writes)==1
        assert (self.base.work/'tui-expiry-target').read_text()=='original fixture text\n'
        proof.update(nativeTuiDeniedTool=self.tool,oneNativeAnswer=True,originalFileRetained=True,
            scope='Pinned actual native TUI '+self.tool+' Deny after actual automatic prerequisite Read. Exact original public CAS and private reference/native tool source/rejection pair; one answer, original file retained. No positive chosen Allow, stock-key or subscription qualification.')


async def prepare_tool(root,work,c,tool,*,case_type=Case):
    from acc.broker import claude_denial_evidence
    claude_denial_evidence.CLAUDE_TUI_DENY_ACK_QUALIFIED=True
    expiry=await prepare_expiry(root,work,c);native=expiry.base.base
    target=work/'tui-expiry-target';target.write_text('original fixture text\n')
    inputs={'Write':{'file_path':str(target),'content':'changed fixture text\n'},
        'Edit':{'file_path':str(target),'old_string':'original fixture text','new_string':'changed fixture text'}}[tool]
    original=native.claude_events;phase=0
    def convert(values,name,arguments,identifier):
        for event,value in values:
            if event=='content_block_start':value['content_block'].update(name=name,id=identifier)
            elif event=='content_block_delta':value['delta']['partial_json']=json.dumps(arguments)
        return values
    def events(payload):
        nonlocal phase
        if phase==1 and any(item.get('type')=='tool_result' and item.get('tool_use_id')=='tui_file_preread'
                for message in payload.get('messages',[]) for item in message.get('content',[]) if isinstance(message.get('content'),list)):
            phase=2
            assert any(t.get('name')==tool for t in payload.get('tools',[]))
            return convert(Security.claude_events(SimpleNamespace(calls=1,command='unused'),payload),tool,inputs,'installed_security_tool')
        values=original(payload)
        if phase==0 and native.native_tool_offered and any(event=='content_block_start' and value['content_block'].get('type')=='tool_use' for event,value in values):
            phase=1
            return convert(values,'Read',{'file_path':str(target)},'tui_file_preread')
        return values
    native.claude_events=events
    files=dict(native.compiled.files);settings=json.loads(files['settings.json']);settings.setdefault('permissions',{})['ask']=[tool]
    files['settings.json']=json.dumps(settings)
    digest=hashlib.sha256(json.dumps({'inputs':native.inputs.as_dict(),'files':files},sort_keys=True).encode()).hexdigest()
    native.compiled=replace(native.compiled,files=files,digest=digest)
    result=case_type(expiry.base);result.tool=tool
    return result
