"""Explicit normal public fresh API-key launch after actual isolated g56 restart."""
import asyncio
import json
import pwd
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from installed_security import Security
from public_native_http import Public

from acc.server import auth, vault
from acc.server.api.context import Context, Principal
from acc.server.api.session_relay import router as relay_router
from acc.server.api.sessions import router as sessions_router
from acc.server.db import write_intent
from acc.server.models import Project, RelayDecision, RelayOperation, RelaySession, Session, User, VaultGrant
from acc.server.session_relay import claude_public, exclusive_public
from acc.server.supervisor import Supervisor
from acc.session_relay import protocol as p


async def recover_new(c,cfg,factory,manager,old_sid,*,source_old_owner_exited=None,require_message_output=True):
    root=Path('/tmp/enrolled/fresh-review');root.mkdir()
    work=Path('/tmp/enrolled/work/fresh-review');work.mkdir()
    for args in (('init',),('config','user.name','Offline fixture'),('config','user.email','fixture@example.invalid'),('commit','--allow-empty','-m','Offline initial commit')):
        child=await asyncio.create_subprocess_exec('git','-C',str(work),*args,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
        _,error=await child.communicate();assert child.returncode==0,error.decode()
    model=Security(root,work,{'provider':'claude','owned_view':False,'tampered':False});payloads=[]
    def events(payload):
        payloads.append(payload)
        return Security.claude_events(SimpleNamespace(calls=2),payload)
    model.claude_events=events
    model.server=await asyncio.start_server(model.model,'127.0.0.1',0)
    port=model.server.sockets[0].getsockname()[1]
    settings=replace(c.ctx.settings,hook_path=cfg.hook_path,statusline_path=cfg.statusline_path,run_dir=cfg.run_dir,root_run_dir=root/'root-runs',
        workspaces_root=Path('/tmp/enrolled/work'),git_dir=root/'git-storage',catalog_dir=root/'catalog',catalog_local_dir=root/'catalog-local',
        broker_socket=cfg.socket_path,cookie_secure=False,allowed_origins=('http://example.lan',))
    settings.catalog_dir.mkdir();(settings.catalog_dir/'manifest.toml').write_text('')
    supervisor=Supervisor(settings,c.db,c.ctx.broker)
    ctx=Context(settings,c.db,supervisor,c.ctx.broker,None,'offline-service-token')
    ctx.relay_runtime=manager;supervisor.relay_manager=manager;manager.ctx=ctx;c.ctx=ctx
    account=pwd.getpwuid(__import__('os').getuid()).pw_name
    @write_intent
    def review():
        with c.db() as db:
            user=db.get(User,1);user.account=account
            project=Project(slug='fresh-review',name='Fresh reviewed recovery',workspace_path=str(work),host=db.get(Session,old_sid).host or 'local',
                account=account,owner=user.username,trusted=True,git_backup=False)
            db.add(project);db.flush()
            for variable,value in {'ANTHROPIC_API_KEY':'relay-fresh-recovery-synthetic-only',
                    'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{port}',
                    'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','DISABLE_AUTOUPDATER':'1'}.items():
                key=vault.create_key(db,supervisor.vault,name='Offline '+variable,variable=variable,value=value,owner=user.username,remote=project.host!='local')
                db.add(VaultGrant(key_id=key.id,project_id=project.id,given_by=user.username))
            token,csrf=auth.create_session(db,user,'offline-explicit-fresh-review')
            before={'nativeId':db.get(Session,old_sid).native_id,
                'receipts':{v.id:dict(v.receipt) for v in db.query(RelayOperation).filter_by(session_id=old_sid)},
                'decisions':{v.id:dict(v.decision) for v in db.query(RelayDecision).filter_by(session_id=old_sid)}}
            db.commit();return project.id,token,csrf,before
    project,cookie,csrf,before=review()
    c.resolve=lambda:(Principal('alex','owner',account,1),p.Actor(p.ActorKind.PERSON,'alex',p.Channel.CHAT))
    app=FastAPI();app.include_router(sessions_router(ctx));app.include_router(relay_router(ctx))
    qualification = (exclusive_public.CLAUDE_LAUNCH_QUALIFIED, claude_public.PUBLIC_CLAUDE_QUALIFIED)
    exclusive_public.CLAUDE_LAUNCH_QUALIFIED=claude_public.PUBLIC_CLAUDE_QUALIFIED=True
    client=httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://example.lan',
        cookies={ctx.session_cookie:cookie},headers={'origin':'http://example.lan','x-acc-csrf':csrf})
    fresh=None;public=None
    try:
        old=await client.get(f'/api/sessions/{old_sid}/relay');assert old.status_code==200,old.text
        old=old.json();assert old['l1']['recoveryMode']=='new_session' and old['l1']['recoveryAvailable']
        old_events=await client.get(f'/api/sessions/{old_sid}/relay/events');assert old_events.status_code==200,old_events.text
        old_message_output=any(v['kind']=='message.upsert' and v['payload'].get('text') for v in old_events.json()['events'])
        if require_message_output:assert old_message_output
        else:assert any(v['kind']=='decision.changed' for v in old_events.json()['events'])
        assert before['decisions'] and before['receipts']
        # Ordinary reviewed blank Launch, not old switch.recover or replay.
        body={'project_id':project,'name':'Explicit fresh recovery','provider':'claude','claude_auth':'api_key',
            'session_relay':True,'worktree':False,'initial_prompt':'','model':'fixture-model'}
        response=await client.post('/api/sessions',json=body);assert response.status_code==201,response.text
        fresh_sid=response.json()['id'];assert fresh_sid!=old_sid
        assert len(factory.active)==1 and fresh_sid in manager.runs
        fresh,=factory.active.values();launch=manager.runs[fresh_sid]
        assert fresh.binding.native_conversation!=before['nativeId'] and fresh.owners.current.process.returncode is None and fresh.owners.exit_qualified
        assert fresh.owners.current.spec.spec['new_session'] is True
        assert model.calls==0 and not payloads
        with c.db() as db:
            session=db.get(Session,fresh_sid);row=db.get(RelaySession,fresh_sid)
            assert not row.sealed and not session.launch_spec.inputs.get('initial_prompt')
            assert not list(db.query(RelayOperation).filter_by(session_id=fresh_sid))
            assert not list(db.query(RelayDecision).filter_by(session_id=fresh_sid))
        @write_intent
        def qualification_only():
            with c.db() as db:
                db.get(RelaySession,fresh_sid).capabilities={'text':'qualified'};db.commit()
        qualification_only()
        public=await Public.create(c,manager,fresh_sid,features=['queue'])
        snapshot=(await public.call('session.snapshot',{}))['result']
        assert snapshot['l1']['owner']=='chat' and snapshot['l1']['terminalWaitingReason']
        text='Fresh reviewed recovery message'
        fields={'sessionId':fresh_sid,'expectedRunId':str(launch.run_id),'text':text,
            'expectedConfigurationRevision':0,'expectedControlRevision':0,'ifBusy':'reject'}
        registered=await public.call('operation.register',{'expectedRunId':str(launch.run_id),
            'registrationKey':'explicit-fresh-chat','method':'turn.submit','payloadDigest':p.operation_digest('turn.submit',fields)})
        result=await public.call('turn.submit',{**fields,'operationId':registered['result']['operationId']});assert 'result' in result,result
        async with asyncio.timeout(20):
            while True:
                with c.db() as db:
                    op=db.get(RelayOperation,registered['result']['operationId'])
                    completed=op.receipt['state']=='accepted' and db.get(RelaySession,fresh_sid).active_operation_id is None
                if completed and not fresh.owners.current.active:break
                await asyncio.sleep(.02)
        assert model.calls==1 and len(payloads)==1
        messages=json.dumps(payloads[0].get('messages',[]))
        assert text in messages and 'Native unanswered permission expiry' not in messages and 'Production same-id handover fixture' not in messages
        assert 'terminalWaitingReason' not in (await public.call('session.snapshot',{}))['result']['l1']
        after=await client.get(f'/api/sessions/{old_sid}/relay');assert after.status_code==200,after.text
        assert after.json()==old
        events_after=await client.get(f'/api/sessions/{old_sid}/relay/events');assert events_after.status_code==200
        assert events_after.json()['events']==old_events.json()['events']
        with c.db() as db:
            assert db.get(RelaySession,old_sid).sealed
            assert {v.id:dict(v.receipt) for v in db.query(RelayOperation).filter_by(session_id=old_sid)}==before['receipts']
            assert {v.id:dict(v.decision) for v in db.query(RelayDecision).filter_by(session_id=old_sid)}==before['decisions']
        await fresh.close();assert fresh.owners.close_complete and not factory.active
        return {'oldSessionId':old_sid,'newSessionId':fresh_sid,'oldNativeId':before['nativeId'],
            'newNativeId':fresh.binding.native_conversation,'ordinaryPersonCookieCsrfLaunch':True,
            'realExclusiveReservationEnrollmentAndNativeSdk':True,'blankLaunchNoModelCall':True,
            'explicitNewChatOnly':True,'freshModelCalls':model.calls,'oldSnapshotEventsReceiptsDecisionsUnchanged':True,'oldMessageOutputPresent':old_message_output,
            'noOldPromptOrApprovalReplay':True,'freshNativeOwnerWholeTreeStopped':True,
            'sourceOldOwnerAlreadyExited':(json.loads(Path('/evidence/execution-checkpoint.json').read_text())['ordering'].get('ownerTreeExited',False) if source_old_owner_exited is None else source_old_owner_exited),'fixturePublicLaunchAndTextOverridesOnly':True,
            'scope':'Actual isolated native API-key fresh public launch after original owner/broker loss; old owner exit/liveness and restart are recorded separately. Explicit reviewed new session and new Chat action, no subscription or automatic same-session re-enrollment. UI click-to-blank-review is separately browser qualified.'}
    finally:
        if public:await public.close()
        await client.aclose()
        if fresh and not fresh.owners.close_complete:await fresh.close()
        await model.close()
        exclusive_public.CLAUDE_LAUNCH_QUALIFIED, claude_public.PUBLIC_CLAUDE_QUALIFIED = qualification
