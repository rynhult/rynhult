"""Actual production Hub/SDK-to-TUI handover; unknown reverse draft stays fenced."""
import asyncio
import json
from pathlib import Path

from fastapi import HTTPException
from installed_security import prepare as prepare_security

from acc.broker import ops
from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelayOwnership, RelayRun, RelaySession
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


class Case:
    def __init__(self, base):
        self.base = base

    def __getattr__(self, name):
        return getattr(self.base, name)

    async def exercise(self, c, state, launch, manager, config):
        sid, original_run = state.spec.spec['session_id'], str(launch.run_id)
        native = state.binding.native_conversation
        source = state.owners.current
        original_publish=source.publish
        async def diagnosed_publish(event):
            if event.get('sdk_type')=='ResultMessage' and event.get('message',{}).get('subtype')!='success':
                print('Artificial fixture native result:',json.dumps(event['message']),flush=True)
            return await original_publish(event)
        source.publish=diagnosed_publish
        original_receive=source.client.receive_response
        async def diagnosed_receive():
            try:
                async for message in original_receive():
                    yield message
            except Exception:
                import traceback
                traceback.print_exc()
                raise
        source.client.receive_response=diagnosed_receive
        params = {'sessionId':sid,'expectedRunId':original_run,'text':'Production same-id handover fixture',
            'expectedConfigurationRevision':0,'expectedControlRevision':0,'ifBusy':'reject'}
        registered = Hub(c.ctx).register(c.resolve,sid,original_run,'handover-turn','turn.submit',p.operation_digest('turn.submit',params))
        await manager.submit(c.resolve,sid,{**params,'operationId':registered.operation_id})
        async with asyncio.timeout(15):
            while True:
                with c.db() as db:
                    receipt = db.get(RelayOperation,registered.operation_id).receipt
                    complete = db.get(RelaySession,sid).active_operation_id is None
                if receipt['state']=='accepted' and complete and not source.active:
                    break
                await asyncio.sleep(.02)
        assert (await source.prepare_switch()).idle
        transcript_paths = list(self.base.root.rglob('*'+native+'*.jsonl'))
        assert transcript_paths and all(path.is_relative_to(self.base.root/'home') for path in transcript_paths), [str(path) for path in transcript_paths]
        async def select(target, key):
            with c.db() as db:
                revision = db.get(RelaySession,sid).revision
            return await manager.select_owner(c.resolve,sid,target=target,revision=revision,request_key=key)
        c.state['revoked'] = True
        try:
            await select('terminal','revoked-handover')
        except (p.RelayError,HTTPException):
            pass
        else:
            raise AssertionError('Revoked person replaced native owner')
        finally:
            c.state['revoked'] = False
        assert source.process.returncode is None and state.owners.current is source
        # Unapproved subscription policy cannot start another native owner or
        # reuse the fixture API key as subscription authority.
        @write_intent
        def auth_policy(mode):
            with c.db() as db:
                row = db.get(RelayRun,int(original_run))
                row.frozen_policy = {**row.frozen_policy,'authMode':mode}
                db.commit()
        auth_policy('subscription')
        try:
            await select('terminal','unapproved-subscription')
        except p.RelayError:
            pass
        else:
            raise AssertionError('Unapproved subscription replaced native owner')
        finally:
            auth_policy('api_key')
        assert source.process.returncode is None and self.base.calls==1
        result = await select('terminal','native-to-terminal')
        assert result['changed'] and result['view']=='terminal' and str(launch.run_id)!=original_run
        assert source.exit_verified and source.process.returncode is not None
        owner = state.owners.current
        assert owner.spec.binding.native_conversation==native and owner.process.current()
        assert state.owners.exit_qualified and await state.owners.authorize()
        state.owners.guard_check()
        from acc.session_relay.claude_metadata_policy import validate
        validate(json.loads(state.owners.files['settings.json']),state.spec.spec['run_dir'])
        assert source.client.options.setting_sources==[]
        assert source.client.options.extra_args['strict-mcp-config'] is None
        assert 'mcp.json' in state.owners.guards
        mcp=Path(state.spec.spec['run_dir'])/'mcp.json'
        original_mcp=mcp.read_text()
        assert json.loads(original_mcp)=={'mcpServers':{}}
        try:
            mcp.chmod(0o640)
            mcp.write_text('{"mcpServers":{"forged":{}}}')
            mcp.chmod(0o440)
            try:
                state.owners.guard_check()
            except p.RelayError as error:
                assert error.code is p.ErrorCode.INTEGRITY_FAILURE
            else:
                raise AssertionError('Changed MCP writer escaped frozen integrity check')
        finally:
            mcp.chmod(0o640)
            mcp.write_text(original_mcp)
            mcp.chmod(0o440)
        state.owners.guard_check()
        # Inspect only this owned fixture process's fixed argv, never host auth.
        args = Path(f'/proc/{owner.process.pid}/cmdline').read_bytes().split(b'\0')
        resume = args.index(b'--resume')
        assert args[resume+1].decode()==native and args.count(b'--resume')==1
        assert b'--settings' in args
        assert args[args.index(b'--setting-sources')+1]==b'' and b'--strict-mcp-config' in args
        try:
            async with asyncio.timeout(10):
                while True:
                    screen = ops.tmux(config,'capture-pane','-p','-J','-S','-200','-t','='+owner.spec.spec['tmux_name']+':')
                    if 'Production same-id handover fixture' in screen.stdout:
                        break
                    await asyncio.sleep(.1)
        except TimeoutError:
            print('Artificial native fixture startup screen:',screen.stdout[-1800:],flush=True)
            raise
        refused = []
        for frame in (b'/config\r',b'/login\r',b'/logout\r',b'/permissions\r',b'/model other\r'):
            try:
                await owner.host.write_frames(owner.grant,1,frame)
            except p.RelayError as error:
                assert error.code in (p.ErrorCode.UNSUPPORTED,p.ErrorCode.STALE_AUTHORITY)
                refused.append(frame.decode().strip())
            else:
                raise AssertionError('Unqualified production Claude Host wrote input')
        before = owner.process.pid, str(launch.run_id)
        try:
            await select('chat','unknown-native-draft')
        except (p.RelayError,ConnectionError):
            pass
        else:
            raise AssertionError('Unknown native draft/idle permitted reverse handover')
        assert state.owners.current is owner and owner.process.current()
        assert (owner.process.pid,str(launch.run_id))==before and self.base.calls==1
        with c.db() as db:
            assert db.get(RelaySession,sid).capabilities=={}
            assert db.get(RelayOwnership,sid).view=='terminal'
        Path('/evidence/result.json').write_text(json.dumps({'provider':'claude','nativeId':native,
            'productionHubOwnershipPlan':True,'actualSDKToTUI':True,'sdkWholeTreeExitedBeforeTUI':True,
            'exactResumeArgument':True,'nativeHistoryVisible':True,'currentPersonApiAuth':True,
            'productionFrozenMetadataPolicy':True,'mutableSettingsScopesExcluded':True,
            'strictEmptyMcpConfig':True,'mcpTamperingRefused':True,
            'revokedPersonRefusedBeforeStop':True,'unapprovedSubscriptionRefusedBeforeStop':True,
            'unqualifiedConfigAuthControlsRefused':refused,'unknownNativeDraftRefusedWithoutReplacement':True,
            'modelCalls':1,'terminalMediated':False,'declaredCapabilities':{},
            'remaining':['Production TUI-to-SDK handover with genuine idle/draft/drain after input.',
                'Approved-subscription native path with current-person consent.',
                'Production native TUI decision races and conclusive reconciliation.',
                'Native transcript metadata protection when integrating the primary worker admission reader.']}))


async def prepare(root, work, c):
    (root/'home').mkdir(mode=0o700)
    base = await prepare_security(root,work,c)
    # Only the synthetic model response changes: a completed ordinary text turn
    # establishes genuine SDK idle before the production ownership plan.
    def text_events(payload):
        return [('message_start',{'type':'message_start','message':{'id':'msg_handover','type':'message',
            'role':'assistant','model':payload.get('model','fixture'),'content':[],'stop_reason':None,
            'stop_sequence':None,'usage':{'input_tokens':10,'output_tokens':2}}}),
            ('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),
            ('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'Native same-id fixture reply.'}}),
            ('content_block_stop',{'type':'content_block_stop','index':0}),
            ('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':2}}),
            ('message_stop',{'type':'message_stop'})]
    base.claude_events = text_events
    return Case(base)
