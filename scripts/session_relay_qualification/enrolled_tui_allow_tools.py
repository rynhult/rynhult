"""Native Read/Write/Edit Allow using the same production registrations."""
import hashlib
import json
from dataclasses import replace

from enrolled_tui_allow_ack import Case as AllowCase
from enrolled_tui_expiry import prepare as prepare_expiry
from enrolled_tui_file_deny import prepare_tool


class Case(AllowCase):
    async def after_granted(self,*args):
        await super().after_granted(*args)
        proof=args[-1]
        expected='original fixture text\n' if self.tool=='Read' else 'changed fixture text\n'
        assert self.target.read_text()==expected
        assert proof['allowBindings'][0]['toolName']==self.tool
        proof.update(allowedTool=self.tool,expectedFileContentVerified=True,
            scope=proof['scope'].replace('TUI Bash Allow','TUI '+self.tool+' Allow'))


async def prepare_mode(root,work,c,tool):
    from acc.broker import claude_allow_evidence
    claude_allow_evidence.CLAUDE_TUI_ALLOW_ACK_QUALIFIED=True
    if tool!='Read':
        result=await prepare_tool(root,work,c,tool,case_type=Case)
        native=result.base.base
        native.inputs=replace(native.inputs,permission_mode='default')
        files=dict(native.compiled.files);settings=json.loads(files['settings.json']);settings['permissions'].pop('ask',None)
        files['settings.json']=json.dumps(settings)
        digest=hashlib.sha256(json.dumps({'inputs':native.inputs.as_dict(),'files':files},sort_keys=True).encode()).hexdigest()
        native.compiled=replace(native.compiled,files=files,digest=digest)
        result.target=work/'tui-expiry-target'
        return result
    expiry=await prepare_expiry(root,work,c);native=expiry.base.base
    target=root/'tui-expiry-target';target.write_text('original fixture text\n')
    (work/'tui-expiry-target').write_text('original fixture text\n')
    native.inputs=replace(native.inputs,permission_mode='default')
    original=native.claude_events
    def events(payload):
        values=original(payload)
        if native.native_tool_offered and any(name=='content_block_start' and value['content_block'].get('type')=='tool_use' for name,value in values):
            for name,value in values:
                if name=='content_block_start':value['content_block']['name']='Read'
                elif name=='content_block_delta':value['delta']['partial_json']=json.dumps({'file_path':str(target)})
        return values
    native.claude_events=events
    files=dict(native.compiled.files);settings=json.loads(files['settings.json']);settings.setdefault('permissions',{}).pop('ask',None)
    files['settings.json']=json.dumps(settings)
    digest=hashlib.sha256(json.dumps({'inputs':native.inputs.as_dict(),'files':files},sort_keys=True).encode()).hexdigest()
    native.compiled=replace(native.compiled,files=files,digest=digest)
    result=Case(expiry.base);result.tool=tool;result.target=target
    return result
