"""Fixture worker owns the actual local main, broker, SDK and native TUI.

The pinned g56 controller SIGKILLs this interpreter, including the execution
broker. A restarted interpreter opens the existing DB and evidence-only channel;
no native owner is recreated. No real account/config or external model service.
"""
# asyncio/json/os/Path/Traceback/Broker/RuntimeTransportFactory/p are supplied by
# the pinned g56 module prefix, which remains in the generated fixture driver.
import asyncio
import base64
import faulthandler
import json
import os
import pwd
import signal
import time
import traceback
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from enrolled_public_decisions import Case as PublicCase
from enrolled_tui_expiry import QUESTION
from enrolled_tui_expiry import prepare as prepare_expiry
from relay_helpers import setup

from acc.broker.relay_runtime import RuntimeTransportFactory
from acc.broker.server import Broker
from acc.server.broker_client import Brokers
from acc.server.config import Settings
from acc.server.db import make_engine, make_sessionmaker, write_in_thread, write_intent
from acc.server.models import RelayOperation, RelayOwnership, RelayRun, RelaySession, Session, SessionRun
from acc.server.session_relay import reconciliation, writers
from acc.server.session_relay.hub import Hub
from acc.server.session_relay.runtime import RuntimeManager
from acc.session_relay import protocol as p

SID='abc123def456'


def phase(name, **facts):
    # Generated fixture metadata only; never hook input, settings or credentials.
    with Path('/evidence/execution-progress.jsonl').open('a') as stream:
        stream.write(json.dumps({'phase':name,'pid':os.getpid(),'monotonic':time.monotonic(),**facts})+'\n')
        stream.flush()



def save(path,value):
    with Path(path).open('w') as stream:
        stream.write(json.dumps(value));stream.flush();os.fsync(stream.fileno())


def hook_loss(stage):
    """Fault only the real private hook reply; production validation still runs."""
    if stage not in ('loss_before','loss_after'):return None
    from acc.broker.claude_hooks import ClaudeHookServer
    original=ClaudeHookServer.connection
    facts={'stage':stage,'allowWrites':0,'deliveredAllowWrites':0,'faults':0,'nativeCompletionBeforeFault':False}
    async def connection(server,reader,writer):
        raw=await asyncio.wait_for(reader.readline(),3)
        class Replay:
            async def readline(self):return raw
        class FaultWriter:
            allowed=False
            def __getattr__(self,name):return getattr(writer,name)
            def write(self,data):
                if json.loads(data).get('behavior')=='allow':
                    self.allowed=True;facts['allowWrites']+=1
                    assert facts['allowWrites']==1
                    if stage=='loss_before':writer.close();return
                    facts['deliveredAllowWrites']+=1
                writer.write(data)
            async def drain(self):
                if not self.allowed:return await writer.drain()
                if stage=='loss_after':
                    await writer.drain()
                    async with asyncio.timeout(15):
                        while True:
                            journal=server.evidence_journal
                            rows=journal.pending(vars(server.spec.binding)) if journal else []
                            if rows and rows[0][2]:break
                            await asyncio.sleep(.01)
                    facts['nativeCompletionBeforeFault']=True
                facts['faults']+=1
                phase('hook_reply_fault',**facts)
                raise ConnectionError('Fixture loses original hook transport response')
        await original(server,Replay(),FaultWriter())
    ClaudeHookServer.connection=connection
    return facts


class Case(PublicCase):
    async def bootstrap(self,c,state,launch,manager):
        # Only the prerequisite real Chat turn/handover/claim. The inherited
        # exercise repeats unrelated refusal matrices and a second native turn,
        # exhausting the controller's aggregate deadline before this case starts.
        phase('sdk_chat_start')
        source=state.owners.current
        run=str(launch.run_id)
        fields={'sessionId':SID,'expectedRunId':run,'text':'Production same-id handover fixture',
            'expectedConfigurationRevision':0,'expectedControlRevision':0,'ifBusy':'reject'}
        op=Hub(c.ctx).register(c.resolve,SID,run,'execution-bootstrap','turn.submit',p.operation_digest('turn.submit',fields))
        await manager.submit(c.resolve,SID,{**fields,'operationId':op.operation_id})
        async with asyncio.timeout(15):
            while True:
                with c.db() as db:
                    accepted=db.get(RelayOperation,op.operation_id).receipt['state']=='accepted'
                    completed=db.get(RelaySession,SID).active_operation_id is None
                if accepted and completed and not source.active:break
                await asyncio.sleep(.02)
        assert (await source.prepare_switch()).idle
        phase('sdk_chat_complete')
        with c.db() as db:revision=db.get(RelaySession,SID).revision
        result=await manager.select_owner(c.resolve,SID,target='terminal',revision=revision,request_key='execution-tui')
        assert result['changed'] and source.exit_verified and source.process.returncode is not None
        owner=state.owners.current
        assert owner.spec.binding.native_conversation==source.spec.binding.native_conversation
        assert state.owners.exit_qualified and owner.process.current()
        phase('tui_handover_complete')
        @write_intent
        def qualify():
            with c.db() as db:
                row=db.get(RelaySession,SID)
                row.capabilities={'text':'qualified','terminal.input':'qualified','decision.tool_approval':'qualified'}
                db.commit()
        qualify();writers.PUBLIC_WRITER_QUALIFIED=True
        attached,claim=await self.claim(c,SID,str(launch.run_id),launch,manager)
        assert claim.receipt.state is p.ReceiptState.ACCEPTED
        assert owner.host.admitted==owner.host.processed==0
        assert owner.host.grant.grant==claim.input_grant
        phase('input_claim_complete')
        await self.after_granted(c,state,launch,manager,owner,attached,claim,{})

    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        phase('after_granted')
        security=json.loads(Path('/evidence/security.json').read_text());stage=security['executionStage']
        failure=security.get('executionFailure',False)
        sid,run=SID,str(launch.run_id)
        answer=state.owners.hooks.answer
        async def lose_direct_evidence(*_args, **_kwargs):
            # The combined runtime also has primary direct application evidence.
            # The crash fixture loses both return/publication paths deliberately.
            return {'outcome': 'unknown'}
        state.owners.hooks.inspect_application = lose_direct_evidence
        answers=[]
        ordering={'revoked':False,'interruptSent':False,'nativeInterruptHooks':0}
        from acc.broker.claude_decision_journal import DecisionEvidenceJournal
        original_observe=DecisionEvidenceJournal.observe
        def observed(journal,scope,payload):
            if payload.get('hook_event_name')=='PostToolUseFailure' and payload.get('is_interrupt') is True:
                ordering['nativeInterruptHooks']+=1
            phase('authenticated_completion_hook',event=payload.get('hook_event_name'),interrupt=payload.get('is_interrupt'))
            return original_observe(journal,scope,payload)
        DecisionEvidenceJournal.observe=observed
        revoked=asyncio.Event()
        async def revoke_original():
            kind=security.get('executionRevocation','cookie')
            if kind=='cookie':
                await write_in_thread(self.public.revoke)
                return
            if kind in ('ownership','account','workspace'):
                @write_intent
                def lose_scope():
                    with c.db() as db:
                        if kind=='ownership':db.get(RelayOwnership,sid).phase='recovery_required'
                        elif kind=='account':db.get(Session,sid).run_as='other-fixture-account'
                        else:db.get(Session,sid).workdir=str(self.base.work/'moved-fixture')
                        db.commit()
                await write_in_thread(lose_scope);ordering['lostScope']=kind;return
            @write_intent
            def reviewed_policy():
                from acc.server import security_settings
                from acc.server.models import SecurityRevision
                with c.db() as db:
                    before=security_settings.revision(db)
                    if db.get(SecurityRevision,1) is None:
                        db.add(SecurityRevision(id=1,revision=before));db.flush()
                    if kind=='strict':
                        from acc.server import guardrails
                        current=guardrails.load(db)
                        guardrails.save(db,{**current,'custom':[*current['custom'],{'pattern':'reviewed-stricter-policy-only','reason':'Fixture stricter rule'}]},'alex')
                    after=security_settings.save(db,{'kind':'instance'},{'custom_rules':True} if kind=='strict' else {'risk_labels':False},
                        actor='alex',expected_revision=before)
                    db.commit();return before,after
            before,after=await write_in_thread(reviewed_policy)
            assert after==before+1
            ordering.update(securityRevisionBefore=before,securityRevisionAfter=after,policyTightened=kind=='strict')
        if stage=='revoke_before':
            authorize=state.owners.hooks.authorize
            async def final_authority():
                if any(v['claimed'] for v in state.owners.hooks.pending.values()):await revoked.wait()
                return await authorize()
            state.owners.hooks.authorize=final_authority
        def checkpoint(kind):
            phase('checkpoint',kind=kind)
            journal=state.decision_evidence
            values=journal.pending(vars(state.spec.binding),prepared=True)
            assert len(values)==1
            key,record,completion,reference=values[0]
            assert record['binding']['toolName']==self.tool
            sent=journal.db.execute('SELECT sent FROM decision_evidence WHERE operation=?',(key,)).fetchone()[0]
            if kind!='interrupt_before':assert bool(sent)==(kind not in ('prepared','revoke_before'))
            with c.db() as db:receipt=dict(db.get(RelayOperation,key).receipt)
            save('/evidence/execution-checkpoint.json',{'stage':kind,'toolName':self.tool,'targetContent':self.target.read_text() if self.target and self.target.exists() else None,'pid':os.getpid(),'runId':run,
                'operationId':key,'originalReceipt':receipt,'originalOperation':record['operation'],
                'revocationKind':security.get('executionRevocation','cookie'),'fixtureToolFailure':failure,'completionEvent':completion.get('event') if completion else None,'completionDurable':completion is not None,'referenceDurable':reference is not None,
                'answerCalls':len(answers),'allowSendIntentDurable':bool(sent),
                'toolStarted':(self.base.work/'execution-started').exists(),'targetExists':(self.base.work/'tui-expiry-target').exists(),
                'ownerPid':owner.process.pid,'ownerStarted':owner.process.started,
                'ownerEnclosure':str(owner.process.enclosure.path),
                'modelCalls':self.base.calls,'hookFault':getattr(self,'hook_fault',None),'ordering':ordering})
        async def gated(*args,**kwargs):
            phase('hook_answer_enter')
            if stage=='prepared':
                checkpoint('prepared')
                await asyncio.Event().wait()
            if stage=='interrupt_before':
                answers.append(args[0])
                signal.pidfd_send_signal(owner.process.fd,signal.SIGINT);ordering['interruptSent']=True
                await owner.process.wait()
                ordering['ownerTreeExited']=await owner.process.tree_exited(owner.process)
                assert ordering['ownerTreeExited']
            result=await answer(*args,**kwargs)
            phase('hook_answer_return')
            if stage!='interrupt_before':answers.append(args[0])
            assert len(answers)==1
            if stage=='revoke_before':
                await revoke_original();ordering['revoked']=True;revoked.set()
            elif stage in ('revoke_after','interrupt'):
                async def after_started():
                    try:
                        async with asyncio.timeout(15):
                            while not (self.base.work/'execution-started').exists():await asyncio.sleep(.01)
                    except TimeoutError:
                        from acc.broker import ops
                        screen=ops.tmux(state.owners.config,'capture-pane','-p','-J','-S','-100',
                            '-t','='+owner.spec.spec['tmux_name']+':').stdout
                        phase('start_marker_missing',screen=screen[-5000:],work=str(self.base.work),
                            pending=list(state.owners.hooks.pending),allowed=state.owners.hooks.tool_bindings.allowed)
                        raise
                    phase('native_tool_started',stage=stage)
                    if stage=='revoke_after':
                        await revoke_original();ordering['revoked']=True;revoked.set()
                        (self.base.work/'execution-release').write_text('explicit fixture release')
                    else:
                        signal.pidfd_send_signal(owner.process.fd,signal.SIGINT)
                        ordering['interruptSent']=True
                        await owner.process.wait()
                        ordering['ownerTreeExited']=await owner.process.tree_exited(owner.process)
                task=asyncio.create_task(after_started())
                def finished(t):
                    if not t.cancelled() and t.exception():phase('ordering_error',error=repr(t.exception()))
                task.add_done_callback(finished)
            return result
        state.owners.hooks.answer=gated
        # Force completion-only recovery: no live verified reference may exist.
        from acc.broker.claude_allow_evidence import NativeAllow
        if stage!='lost_ack':NativeAllow.applied=lambda *args,**kwargs:None
        if stage=='lost_ack':
            call=state.peer.call
            async def accepted_without_ack(method,params):
                result=await call(method,params)
                if method=='publication' and params.get('kind')=='decision.applied':
                    assert result is True
                    checkpoint('lost_ack')
                    await asyncio.Event().wait()
                return result
            state.peer.call=accepted_without_ack
        phase('question_frames_start')
        for seq,data in ((1,QUESTION.encode()),(2,b'\r')):
            value=await self.public.call('terminal.input',{'attachmentId':self.public.view['attachmentId'],
                'inputGrant':claim.input_grant,'inputEpoch':claim.input_epoch,'frameSeq':seq,'data':base64.b64encode(data).decode()})
            assert 'result' in value,value
            if seq==1:await asyncio.sleep(.7)
        phase('question_frames_admitted')
        # Frame admission precedes UserPromptSubmit, the model response and
        # PreToolUse. Start the existing permission wait at the native tool
        # phase, not at transport delivery. This observes no approval authority.
        async with asyncio.timeout(20):
            while 'installed_security_tool' not in state.owners.hooks.tool_bindings.active:
                await asyncio.sleep(.01)
        phase('native_tool_bound')
        async with asyncio.timeout(10):
            while not state.owners.hooks.pending:await asyncio.sleep(.01)
        phase('permission_pending')
        identifier=next(iter(state.owners.hooks.pending))
        async with asyncio.timeout(3):
            while True:
                view=await self.public.call('session.snapshot',{})
                card=next((v for v in view['result']['pendingDecisions'] if v['decisionId']==identifier),None)
                if card:break
                await asyncio.sleep(.01)
        if failure:
            if self.tool=='Read':self.target.unlink()
            elif self.tool=='Edit':self.target.write_text('external fixture change\n')
            else:raise AssertionError(self.tool)
        payload={'sessionId':sid,'expectedRunId':run,'decisionId':identifier,'choiceId':'accept',
            'expectedRevision':card['revision'],'actionDigest':card['actionDigest']}
        registered=await self.public.call('operation.register',{'expectedRunId':run,'registrationKey':'execution-crash-Allow',
            'method':'decision.reply','payloadDigest':p.operation_digest('decision.reply',payload)})
        phase('public_answer_registered')
        async def request_answer():
            fields={**payload,'operationId':registered['result']['operationId']}
            if stage.startswith('revoke_'):
                response=await self.public.client.post(f'/api/sessions/{sid}/relay',json={
                    'jsonrpc':'2.0','id':'revoked-original-answer','method':'ry/decision.reply','params':fields})
                ordering['publicResponseStatus']=response.status_code
                assert response.status_code in (200,401),response.text
                value=response.json() if response.status_code==200 else {'revokedResponse':True}
                if security.get('executionRevocation','cookie')!='cookie' and 'error' in value:
                    ordering['publicResponseError']=value['error'];return {'revokedResponse':True}
                return value
            return await self.public.call('decision.reply',fields)
        task=asyncio.create_task(request_answer())
        if stage=='prepared':await asyncio.Event().wait()
        result=await task;phase('public_answer_returned');assert result.get('revokedResponse') is True or result['result']['state']=='uncertain',result
        try:
            async with asyncio.timeout(20):
                while True:
                    rows=state.decision_evidence.pending(vars(state.spec.binding),prepared=True)
                    if stage=='revoke_before':
                        if ordering['revoked'] and self.base.native_denied_at is not None:break
                    elif stage=='revoke_after':
                        if ordering['revoked'] and rows and rows[0][2]:break
                    elif stage in ('interrupt','interrupt_before'):
                        if ordering.get('ownerTreeExited'):
                            ordering['ownerTreeExited']=True
                            break
                    elif stage=='loss_before':
                        if self.hook_fault['faults'] and self.base.native_denied_at is not None and owner.host.processed==launch.terminal_grants.record.processed==2:break
                    elif stage=='sent':
                        if (self.base.work/'execution-started').exists():break
                    elif rows and rows[0][2] and owner.host.processed==launch.terminal_grants.record.processed==2:break
                    await asyncio.sleep(.02)
        except TimeoutError:
            from acc.broker import ops
            screen=ops.tmux(state.owners.config,'capture-pane','-p','-J','-S','-100',
                '-t','='+owner.spec.spec['tmux_name']+':').stdout
            phase('completion_wait_failed',ordering=ordering,screen=screen[-6000:],
                journal=[{'completion':v[2],'ref':v[3]} for v in rows],
                started=(self.base.work/'execution-started').exists(),target=(self.base.work/'tui-expiry-target').exists(),
                ownerCurrent=owner.process.current())
            raise
        if stage in ('sent','loss_before','revoke_before','interrupt','interrupt_before'):assert rows[0][2] is None and rows[0][3] is None
        else:assert rows[0][2] and rows[0][3] is None
        if stage.startswith('loss_'):
            assert self.hook_fault['allowWrites']==self.hook_fault['faults']==1
            assert self.hook_fault['deliveredAllowWrites']==int(stage=='loss_after')
            assert self.hook_fault['nativeCompletionBeforeFault'] is (stage=='loss_after')
            if self.tool=='Bash':
                assert (self.base.work/'tui-expiry-target').exists() is (stage=='loss_after')
            else:
                expected='changed fixture text\n' if self.tool=='Edit' and stage=='loss_after' else 'original fixture text\n'
                if failure:
                    assert rows[0][2]['event']=='PostToolUseFailure'
                    assert (self.target.read_text() if self.target.exists() else None)==(None if self.tool=='Read' else 'external fixture change\n')
                else:assert self.target.read_text()==expected
        checkpoint(stage)
        await asyncio.Event().wait()  # external g56 controller must kill this broker


async def initial(c,cfg,factory,status):
    phase('initial_start')
    root=Path('/tmp/enrolled');work=root/'work';work.mkdir()
    security=json.loads(Path('/evidence/security.json').read_text())
    if security.get('executionRecoveryNew') and security['executionStage'] in ('loss_before','loss_after'):
        from acc.broker.relay_enclosure import Enclosure
        original_enclosure_init = Enclosure.__init__
        def delayed_cleanup(enclosure, *args, **kwargs):
            original_enclosure_init(enclosure, *args, **kwargs)
            if kwargs.get('lifetime') is not None:
                globals()['inject_cleanup_delay'](enclosure)
        Enclosure.__init__ = delayed_cleanup
    tool=security.get('executionTool','Bash')
    if tool=='Bash':expiry=await prepare_expiry(root,work,c)
    else:
        from enrolled_tui_allow_tools import prepare_mode
        expiry=await prepare_mode(root,work,c,tool)
    case=Case(expiry.base);case.tool=tool
    case.target=Path(expiry.target) if tool != 'Bash' else None
    assert str(case.hook) == cfg.hook_path
    c.ctx.settings = replace(c.ctx.settings, hook_path=str(case.hook))
    case.hook_fault=status.get('hook_fault')
    native=case.base.base
    stage=json.loads(Path('/evidence/security.json').read_text())['executionStage']
    events=native.claude_events
    def controlled(payload):
        values=events(payload)
        if stage in ('sent','revoke_after','interrupt') and any(name=='content_block_start' and value['content_block'].get('type')=='tool_use' for name,value in values):
            import shlex
            code='import time\nfrom pathlib import Path\nPath("execution-started").write_text("started")\nwhile not Path("execution-release").exists(): time.sleep(.05)\nPath("tui-expiry-target").write_text("completed")'
            for name,value in values:
                if name=='content_block_delta':value['delta']['partial_json']=json.dumps({'command':'python3 -c '+shlex.quote(code)})
        return values
    native.claude_events=controlled
    from acc.broker import claude_allow_evidence
    claude_allow_evidence.CLAUDE_TUI_ALLOW_ACK_QUALIFIED=True
    delivery=SimpleNamespace(env=case.env,catalog=None,check_env=lambda _:None)
    c.ctx.supervisor=SimpleNamespace(catalog=None,vault=SimpleNamespace(deliver=lambda *_a,**_k:delivery,
        redactor=lambda *_a:SimpleNamespace(text=lambda value:value)))
    @write_intent
    def configure():
        with c.db() as db:
            db.delete(db.get(RelayRun,1));db.delete(db.get(RelaySession,'relay'));db.delete(db.get(SessionRun,1));db.flush()
            session=db.get(Session,'relay');session.id=SID;session.run_as=pwd.getpwuid(os.getuid()).pw_name
            session.run_dir=str(cfg.run_dir/SID);session.workdir=str(work)
            session.provider=session.launch_spec.provider='claude';session.claude_auth='api_key'
            session.native_id='11111111-1111-4111-8111-111111111111';session.tmux_name='acc-execution-native'
            session.launch_spec.files=case.compiled.files;session.launch_spec.digest=case.compiled.digest
            session.launch_spec.inputs=case.inputs.as_dict();db.commit()
    configure()
    manager=RuntimeManager(c.ctx);status['manager']=manager
    launch=manager.new_exclusive_launch(c.resolve)
    original_main_handle = launch.handle
    async def main_diagnostic(method, params):
        try:
            return await original_main_handle(method, params)
        except Exception:
            # Offline fixture diagnostics only; never serialize grant payloads.
            print('EXECUTION MAIN ERROR', method, traceback.format_exc(), file=__import__('sys').stderr, flush=True)
            raise
    launch.handle = main_diagnostic
    @write_intent
    def reserve():
        with c.db() as db:launch.reserve(db,db.get(Session,SID));db.commit()
    reserve();phase('launch_start');await launch.launch(SID);phase('launch_complete')
    state,=factory.active.values();status['runtime']=state
    phase('exercise_start')
    await case.bootstrap(c,state,launch,manager)
    raise AssertionError('Execution fixture returned without an external crash')


async def worker():
    faults=Path('/evidence/execution-faults.log').open('a')
    faulthandler.enable(file=faults)
    async def sample_tasks():
        # Sample on the interpreter/event loop, never the C watchdog thread.
        while True:
            await asyncio.sleep(25)
            faults.write('Task sample '+str(time.monotonic())+'\n')
            for task in asyncio.all_tasks():
                faults.write(task.get_name()+'\n')
                for frame in task.get_stack(limit=3):
                    faults.write(f'{frame.f_code.co_filename}:{frame.f_lineno}:{frame.f_code.co_name}\n')
            faults.flush()
    asyncio.create_task(sample_tasks(),name='execution-fixture-diagnostics')
    phase('worker_start')
    cfg=globals()['config']();factory=RuntimeTransportFactory(cfg,owned_view_factory=None)
    from acc.broker.relay_ownership import ExclusiveRuntime
    original_handle=ExclusiveRuntime.handle
    async def diagnostic(runtime,method,params):
        try:return await original_handle(runtime,method,params)
        except Exception:
            text=traceback.format_exc()
            Path('/evidence/broker-error.txt').write_text(text)
            print('EXECUTION BROKER ERROR',method,text,flush=True,file=__import__('sys').stderr)
            raise
    ExclusiveRuntime.handle=diagnostic
    broker=Broker(cfg,relay_transport=factory)
    cfg.socket_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    cfg.run_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    cfg.socket_path.unlink(missing_ok=True);Path('/evidence/worker.sock').unlink(missing_ok=True)
    server=await asyncio.start_unix_server(broker.handle,str(cfg.socket_path))
    root=Path('/tmp/enrolled');status={'hook_fault':hook_loss(json.loads(Path('/evidence/security.json').read_text())['executionStage'])};recovery=Path('/evidence/execution-checkpoint.json').exists()
    if recovery:
        engine=make_engine(f'sqlite:///{root}/relay.db');db=make_sessionmaker(engine)
        c=SimpleNamespace(db=db,engine=engine,ctx=SimpleNamespace(db=db,settings=Settings(data_dir=root/'data',hook_path=cfg.hook_path,feature_overrides={'session_relay':True})))
    else:
        root.mkdir(mode=0o700,exist_ok=True);c=setup(root)
    c.ctx.broker=Brokers(cfg.socket_path,None,pwd.getpwuid(os.getuid()).pw_name,'fixture-services')
    if recovery:
        manager=RuntimeManager(c.ctx);status['manager']=manager;reconciliation.QUALIFIED=True
        from acc.server.session_relay.maintenance import recover
        recover(c.ctx)  # production coordinator-start fencing, not a test receipt

    else:
        async def run():
            try:await initial(c,cfg,factory,status)
            except Exception:
                status['error']=traceback.format_exc();save('/evidence/execution-error.json',{'error':status['error']})
        status['task']=asyncio.create_task(run())
    async def inspect(reader,writer):
        try:
            method=json.loads(await reader.readline())['method']
            if 'error' in status:raise AssertionError(status['error'])
            if method=='state':
                runs=[]
                for state in factory.active.values():
                    owner=getattr(getattr(state,'owners',None),'current',None)
                    if owner is not None and hasattr(owner,'process'):
                        process=owner.process
                        if hasattr(process,'pid') and process.pid:runs.append({'owner':process.pid,'view':process.pid})
                value={'pid':os.getpid(),'active':len(factory.active),'runs':runs,'recoveryOnly':recovery,
                    'barrier':Path('/evidence/execution-checkpoint.json').exists()}
            elif method=='qualify':
                assert recovery and not factory.active
                checkpoint=json.loads(Path('/evidence/execution-checkpoint.json').read_text())
                try:value=await status['manager'].reconcile_decisions(SID,int(checkpoint['runId']))
                except p.RelayError as error:
                    if not json.loads(Path('/evidence/security.json').read_text()).get('executionBindingLoss'):raise
                    value={'reconciled':0,'refused':error.code.value}
            elif method=='debug':
                assert recovery and not factory.active
                from enrolled_fresh_recovery import recover_new
                value=await recover_new(c,cfg,factory,status['manager'],SID)
            elif method=='clean':
                checkpoint=json.loads(Path('/evidence/execution-checkpoint.json').read_text())
                with c.db() as db:
                    row=db.get(RelaySession,SID);receipt=dict(db.get(RelayOperation,checkpoint['operationId']).receipt)
                    phase=db.get(RelayOwnership,SID).phase
                value={'sealed':row.sealed,'receipt':receipt,'active':len(factory.active),'recoveryOnly':recovery,
                    'ownershipPhase':phase}
            else:raise ValueError(method)
        except Exception as error:
            traceback.print_exc()
            value={'error':repr(error)}
        writer.write(json.dumps(value).encode()+b'\n');await writer.drain();writer.close();await writer.wait_closed()
    if recovery and json.loads(Path('/evidence/security.json').read_text()).get('executionReadyWindow'):
        import socket
        bound=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        bound.bind('/evidence/worker.sock')
        phase('inspection_bound_before_listen')
        await asyncio.sleep(.2)  # deterministic reproduction of the observed OS boundary
        inspector=await asyncio.start_unix_server(inspect,sock=bound)
    else:inspector=await asyncio.start_unix_server(inspect,'/evidence/worker.sock')
    ready=Path('/evidence/inspection-ready-'+str(os.getpid())+'.tmp')
    save(ready,{'pid':os.getpid()});os.replace(ready,'/evidence/inspection-ready.json')
    phase('inspection_listening')
    try:await asyncio.Event().wait()
    finally:server.close();inspector.close()
