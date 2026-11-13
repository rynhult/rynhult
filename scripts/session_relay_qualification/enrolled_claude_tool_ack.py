"""Exact native SDK non-Bash tools under stricter generated ask policy."""
import hashlib
import json
from dataclasses import replace

from enrolled_claude_ack import prepare as prepare_ack


async def prepare_tool(root,work,c,tool,choice='accept',loss=None,interrupt=False):
    case=await prepare_ack(root,work,c)
    case.choice=choice;case.loss=loss;case.interrupt=interrupt
    target=work/'sdk-tool-target.txt';target.write_text('original fixture text\n')
    inputs={
        'Read':{'file_path':str(target)},
        'Write':{'file_path':str(target),'content':'updated fixture text\n'},
        'Edit':{'file_path':str(target),'old_string':'original fixture text','new_string':'updated fixture text'},
    }[tool]
    original=case.base.claude_events
    def events(payload):
        count=case.base.calls
        prerequisite=tool in ('Edit','Write')
        tool_call=(count==1 or prerequisite and count==2)
        if tool_call:
            case.base.calls=1
            try:values=original(payload)
            finally:case.base.calls=count
            selected='Read' if prerequisite and count==1 else tool
            offered={value.get('name') for value in payload.get('tools',[])}
            assert selected in offered,('Native tool not offered',selected,sorted(offered))
            selected_inputs={'file_path':str(target)} if selected=='Read' else inputs
            for name,value in values:
                if name=='content_block_start':
                    value['content_block']['name']=selected
                    if prerequisite and count==1:value['content_block']['id']='sdk_tool_preread'
                elif name=='content_block_delta':value['delta']['partial_json']=json.dumps(selected_inputs)
            return values
        return original(payload)
    case.base.claude_events=events
    files=dict(case.base.compiled.files);settings=json.loads(files['settings.json'])
    settings['permissions']['ask']=[tool]
    files['settings.json']=json.dumps(settings)
    digest=hashlib.sha256(json.dumps({'inputs':case.base.inputs.as_dict(),'files':files},sort_keys=True).encode()).hexdigest()
    case.base.compiled=replace(case.base.compiled,files=files,digest=digest)
    return case
