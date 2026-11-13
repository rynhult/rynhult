"""Generated-HOME native Allow hook metadata/stderr inventory; no promotion."""
import asyncio
import json
import sys
from pathlib import Path

from enrolled_public_decisions import Case as PublicCase
from enrolled_tui_expiry import prepare as prepare_expiry
from public_native_http import Public


class Case(PublicCase):
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        references=[];native_ids=[];payloads=[];observe=state.owners.hooks.observe;call=Public.call
        async def observed(payload):
            if payload.get('hook_event_name') in ('PreToolUse','PermissionRequest','PostToolUse','PostToolUseFailure'):
                payloads.append({key:payload[key] for key in ('hook_event_name','session_id','prompt_id','transcript_path','tool_name','tool_input','tool_use_id') if key in payload})
            if payload.get('hook_event_name')=='PermissionRequest':
                references.append(payload['_fixture_reference'])
                native_ids.append(payload.get('_fixture_native_tool_use_id'))
            return await observe(payload)
        async def allow_first(client,method,params):
            if method=='decision.reply' and params.get('choiceId')=='decline':await asyncio.sleep(.3)
            return await call(client,method,params)
        state.owners.hooks.observe=observed;Public.call=allow_first
        try:await super().after_granted(c,state,launch,manager,owner,attached,claim,proof)
        finally:Public.call=call
        path=Path(state.native_transcript[1]);assert path.is_relative_to(self.base.root/'home')
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        matched=[row for row in rows if any(reference in json.dumps(row) for reference in references)]
        typed=[row for row in rows if row.get('attachment',{}).get('type')=='hook_permission_decision']
        settings=json.loads(state.owners.files['settings.json'])
        registered=sorted(settings['hooks'])
        assert {'PreToolUse','PermissionRequest','PostToolUse','PostToolUseFailure'}.issubset(registered)
        proof.update(inventoryOnly=True,privateInvocationReferences=references,
            nativeHookPayloads=payloads,productionRegisteredHookEvents=registered,
            nativeToolTranscriptRows=[row for row in rows if row.get('type') in ('user','assistant')],
            hookNativeToolUseIds=native_ids,
            matchedNativeReferenceRows=matched,nativeTypedPermissionDecisions=typed,
            scope='Inventory only: generated private hook captures only the named CLAUDE_CODE_TOOL_USE_ID metadata and emits a reference on stderr after original response. No environment dump. Actual public Allow remains unknown. No input changes, native ACK promotion, real settings or subscription.')


async def prepare(root,work,c):
    from acc.server.session_relay import exclusive_runtime
    original=exclusive_runtime.module_command
    code="""import os,site,sys
r=sys.argv.pop(1);s=set(site.getsitepackages());sys.path.insert(next((i for i,p in enumerate(sys.path) if p in s),len(sys.path)),r)
from acc.session_relay import claude_hook as h
exchange=h.exchange
def observed(channel,payload):
    reference=os.urandom(16).hex()
    if payload.get('hook_event_name')=='PermissionRequest':payload={**payload,'_fixture_reference':reference,'_fixture_native_tool_use_id':os.environ.get('CLAUDE_CODE_TOOL_USE_ID')}
    result=exchange(channel,payload)
    if payload.get('hook_event_name')=='PermissionRequest':print('Native invocation reference '+reference,file=sys.stderr)
    return result
h.exchange=observed
h.main()
"""
    def command(module,*args):
        normal=original(module,*args)
        return [sys.executable,'-I','-c',code,normal[4],*args] if module=='acc.session_relay.claude_hook' else normal
    exclusive_runtime.module_command=command
    expiry=await prepare_expiry(root,work,c)
    return Case(expiry.base)
