"""Actual public TUI Allow, unique hook digest and protected native tool ID."""
import asyncio
from pathlib import Path

from enrolled_public_decisions import Case as PublicCase
from enrolled_tui_expiry import prepare as prepare_expiry
from public_native_http import Public


class Case(PublicCase):
    allow_ack = True

    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        original=Public.call;bindings=[];errors=[];native_writes=[];native_evidence=[]
        from acc.broker.claude_allow_evidence import NativeAllow
        write=state.owners.hooks.answer
        async def once(identifier,*args,**kwargs):
            assert not native_writes
            native_writes.append(identifier)
            return await write(identifier,*args,**kwargs)
        state.owners.hooks.answer=once
        applied=NativeAllow.applied
        def captured(reader,binding,**kwargs):
            try:reference=applied(reader,binding,**kwargs)
            except Exception:
                import traceback
                errors.append(traceback.format_exc())
                raise
            if reference:native_evidence.append({'binding':dict(reader.evidence_binding),'nativeRef':reference})
            return reference
        NativeAllow.applied=captured
        native_answer=state.answer
        async def diagnosed(params):
            try:return await native_answer(params)
            except Exception:
                import traceback
                errors.append(traceback.format_exc())
                raise
        state.answer=diagnosed
        bind=NativeAllow.bind
        def diagnostic(reader,pending):
            try:return bind(reader,pending)
            except Exception as error:
                errors.append(('bind',type(error).__name__,str(error),{key:pending.get(key) for key in ('boundToolId','promptId','toolName','promptDigest')}))
                raise
        NativeAllow.bind=diagnostic
        async def prefer_allow(client,method,params):
            if method=='decision.reply':
                if params.get('choiceId')=='decline':await asyncio.sleep(.3)
                elif params.get('choiceId')=='accept' and params['decisionId'] in state.owners.hooks.pending:
                    pending=state.owners.hooks.pending[params['decisionId']]
                    bindings.append({key:pending[key] for key in ('nativeToolId','boundToolId','promptId','promptDigest','toolName')})
                    from acc.broker import claude_allow_evidence
                    errors.append(('qualified',claude_allow_evidence.CLAUDE_TUI_ALLOW_ACK_QUALIFIED,'permits',state.owners.hooks.tool_bindings.permits(pending,state.owners.hooks.pending)))
            return await original(client,method,params)
        Public.call=prefer_allow
        try:await super().after_granted(c,state,launch,manager,owner,attached,claim,proof)
        except Exception:
            await asyncio.sleep(3)
            print('Allow hook binding diagnostic:',bindings,'active',state.owners.hooks.tool_bindings.active,'ambiguous',state.owners.hooks.tool_bindings.ambiguous,flush=True)
            for line in Path(state.native_transcript[1]).read_text().splitlines():
                row=__import__('json').loads(line)
                message=row.get('message',{});content=message.get('content',[]) if isinstance(message,dict) else []
                items=[{key:item[key] for key in ('type','tool_use_id','is_error') if key in item}
                    for item in content if isinstance(item,dict) and item.get('type')=='tool_result'] if isinstance(content,list) else []
                if items or row.get('attachment',{}).get('type')=='hook_permission_decision':
                    print('NATIVE RESULT METADATA:',{key:row.get(key) for key in ('uuid','parentUuid','promptId','sourceToolAssistantUUID','permissionDecision','attachment')},items,flush=True)
            print('FINAL ALLOW ERROR:',errors,'writes',native_writes,'evidence',native_evidence,'pending',[(key,value['claimed'],value['future'].done(),value['future'].result() if value['future'].done() and not value['future'].cancelled() else None) for key,value in state.owners.hooks.pending.items()],'target',(self.base.work/'tui-expiry-target').read_text() if (self.base.work/'tui-expiry-target').exists() else None,flush=True)
            from acc.broker import ops
            screen=await asyncio.to_thread(ops.tmux,state.owners.config,'capture-pane','-p','-J','-S','-25','-t','='+owner.spec.spec['tmux_name']+':')
            print('FINAL ALLOW SCREEN:',screen.stdout[-4000:],flush=True)
            raise
        finally:
            Public.call=original
            NativeAllow.bind=bind
            NativeAllow.applied=applied
            state.owners.hooks.answer=write
            state.answer=native_answer
        assert len(native_writes)==len(native_evidence)==1
        assert native_evidence[0]['binding']['toolId']==bindings[0]['boundToolId']
        assert native_evidence[0]['binding']['promptDigest']==bindings[0]['promptDigest']
        assert len(bindings)==1  # completed receipt replay never reaches a pending hook
        assert bindings[0]['nativeToolId'] is None and bindings[0]['boundToolId']
        assert Path(state.native_transcript[1]).is_relative_to(self.base.root/'home')
        proof.update(allowBindings=bindings[:1],nativeAllowEvidence=native_evidence,oneNativeAnswer=True,isolatedHomeTranscript=True,
            scope='Pinned native TUI Bash Allow, actual production hook registrations and public cookie/CSRF CAS. PermissionRequest joins PreToolUse native ID by unique canonical session/prompt/tool/input digest, then protected pre-write cursor, original prompt sequence/tool source and native Allow attachment/result prove original application. Identical pending digests refuse. No real settings/subscription, stock keys or restart qualification.')


async def prepare(root,work,c):
    from acc.broker import claude_allow_evidence
    claude_allow_evidence.CLAUDE_TUI_ALLOW_ACK_QUALIFIED=True
    expiry=await prepare_expiry(root,work,c)
    return Case(expiry.base)
