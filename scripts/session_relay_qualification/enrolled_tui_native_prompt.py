"""Missing-hook native overwrite modal: notice only, separate fixture person answers."""
import asyncio
import base64
import json
import sys

from enrolled_public_decisions import Case as PublicCase
from enrolled_tui_allow_tools import prepare_mode
from enrolled_tui_expiry import QUESTION

from acc.server.models import AttentionItem, RelayDecision, Session


class Case(PublicCase):
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        from acc.broker import ops
        sid=state.spec.spec['session_id'];run=str(launch.run_id)
        product_writes=[];product_keys=[];write=owner.host.write_frames;tmux=ops.tmux
        def observed(config,*args,**kwargs):
            if args and args[0] in ('send-keys','paste-buffer','load-buffer'):
                product_keys.append(args[0])
            return tmux(config,*args,**kwargs)
        ops.tmux=observed
        async def tracked(*args):
            product_writes.append(args[-1].hex())
            return await write(*args)
        owner.host.write_frames=tracked
        try:
            for sequence,data in ((3,QUESTION.encode()),(4,b'\r')):
                result=await self.public.call('terminal.input',{'attachmentId':self.public.view['attachmentId'],
                    'inputGrant':claim.input_grant,'inputEpoch':claim.input_epoch,'frameSeq':sequence,
                    'data':base64.b64encode(data).decode()})
                assert 'result' in result,result
                if sequence==3:await asyncio.sleep(.7)
            async with asyncio.timeout(30):
                while True:
                    snapshot=await self.public.call('session.snapshot',{})
                    if snapshot['result'].get('nativePrompt'):break
                    await asyncio.sleep(.1)
            notice=snapshot['result']['nativePrompt']
            assert notice['message']=='Claude is asking in the terminal; answer it there'
            assert notice['remoteAllow'] is False and notice['terminalUrl']==f'/sessions/{sid}?tab=terminal'
            assert not state.owners.hooks.pending and len(product_writes)==2 and not product_keys
            assert product_writes==[QUESTION.encode().hex(),'0d']
            with c.db() as db:
                assert db.get(Session,sid).observed_state=='waiting_input'
                assert db.query(RelayDecision).filter_by(session_id=sid,run_id=launch.run_id).count()==0
                item=db.query(AttentionItem).filter_by(session_id=sid,kind='needs_input').one()
                assert item.source=='claude_terminal' and item.detail==notice['message']
                assert not item.verified
            remote=await self.public.call('decision.reply',{'expectedRunId':run,'decisionId':'native-modal',
                'choiceId':'accept','expectedRevision':0,'actionDigest':'sha256:'+'a'*64,'operationId':'0'*32})
            assert 'error' in remote and len(product_writes)==2
            key=await self.public.call('terminal.input',{'attachmentId':self.public.view['attachmentId'],
                'inputGrant':claim.input_grant,'inputEpoch':claim.input_epoch,'frameSeq':5,'data':'DQ=='})
            assert 'error' in key and len(product_writes)==2 and not product_keys
            assert (self.base.work/'tui-expiry-target').read_text()=='original fixture text\n'
            # ONLY the offline fixture's separate person action types into their
            # native terminal. No product route, observer or callback sends keys.
            person=await asyncio.to_thread(tmux,state.owners.config,'send-keys','-t',
                '='+owner.spec.spec['tmux_name']+':','Enter')
            assert person.returncode==0
            async with asyncio.timeout(25):
                while (self.base.work/'tui-expiry-target').read_text()!='changed fixture text\n' or owner.host.processed!=4 or launch.terminal_grants.record.processed!=4:
                    await asyncio.sleep(.1)
            async with asyncio.timeout(15):
                while (await self.public.call('session.snapshot',{}))['result'].get('nativePrompt'):
                    await asyncio.sleep(.1)
            with c.db() as db:
                assert db.query(AttentionItem).filter_by(session_id=sid,kind='needs_input').one().resolved_at is not None
                assert db.query(RelayDecision).filter_by(session_id=sid,run_id=launch.run_id).count()==0
            assert len(product_writes)==2 and not product_keys and not state.owners.hooks.pending
            proof.update(nativeOverwriteDetected=True,noPermissionRequestCard=True,chatSnapshotNotice=notice,
                inboxNeedsInput=True,remoteAllowRefused=True,productModalKeysSent=len(product_keys),fixturePersonTerminalAnswers=1,
                personAnswerContinued=True,originalInputProcessed=owner.host.processed,
                noteResolved=True,productionRegistrationEvents=sorted(json.loads(state.owners.files['settings.json'])['hooks']),
                scope='Actual installed native Write overwrite modal with generated tmp HOME and default policy. Fault injection makes registered PermissionRequest command exit before delivering a hook question, reproducing native no-card fallback. Product watcher only captures screen; Chat snapshot and Inbox note plus refusal; zero product modal keys. Separate offline fixture person presses Enter in own native terminal; original turn completes and note resolves. Browser presentation tested separately. No real account/subscription or writable browser Terminal attestation.')
        finally:
            owner.host.write_frames=write
            ops.tmux=tmux
            await self.public.close()


async def prepare(root,work,c):
    from acc.server.session_relay import exclusive_runtime
    original=exclusive_runtime.module_command
    code="""import json,site,sys
r=sys.argv.pop(1);s=set(site.getsitepackages());sys.path.insert(next((i for i,p in enumerate(sys.path) if p in s),len(sys.path)),r)
from acc.session_relay import claude_hook as h
exchange=h.exchange
def observed(channel,payload):
    if payload.get('hook_event_name')=='PermissionRequest':raise SystemExit(1)
    return exchange(channel,payload)
h.exchange=observed
h.main()
"""
    def command(module,*args):
        normal=original(module,*args)
        return [sys.executable,'-I','-c',code,normal[4],*args] if module=='acc.session_relay.claude_hook' else normal
    exclusive_runtime.module_command=command
    write=await prepare_mode(root,work,c,'Write')
    return Case(write.base)
