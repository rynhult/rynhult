"""Reviewed ordinary fresh Codex launch after isolated vendor/stdio loss."""
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
from acc.server.supervisor import Supervisor
from acc.session_relay import protocol as p


async def recover_codex(c,cfg,factory,manager,old_sid,*,source_old_owner_exited=None,require_message_output=True):
    assert not factory.active and old_sid not in manager.runs
    with c.db() as db:assert db.get(RelaySession,old_sid).sealed and db.get(Session,old_sid).provider=='codex'
    root=Path('/tmp/enrolled/fresh-review');root.mkdir()
    work=Path('/tmp/enrolled/work/fresh-review');work.mkdir()
    for args in (('init',),('config','user.name','Offline fixture'),('config','user.email','fixture@example.invalid'),('commit','--allow-empty','-m','Offline initial commit')):
        child=await asyncio.create_subprocess_exec('git','-C',str(work),*args,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
        _,error=await child.communicate();assert child.returncode==0,error.decode()
    model=Security(root,work,{'provider':'codex','owned_view':False,'tampered':False});payloads=[]
    def events(payload):
        payloads.append(payload)
        return Security.codex_events(SimpleNamespace(calls=2),payload)
    model.codex_events=events
    model.server=await asyncio.start_server(model.model,'127.0.0.1',0)
    port=model.server.sockets[0].getsockname()[1]
    home=Path(pwd.getpwuid(__import__('os').getuid()).pw_dir)
    assert home==Path('/home/probe'), 'Fresh Codex proof requires isolated generated fixture HOME'
    config=home/'.codex/config.toml'
    assert config.is_file()
    config.write_text('model_provider="fixture"\n[model_providers.fixture]\nname="Offline fresh Codex fixture"\n'
        +f'base_url="http://127.0.0.1:{port}/v1"\nwire_api="responses"\nrequires_openai_auth=false\n'
        +'request_max_retries=0\nstream_max_retries=0\n[features]\nhooks=true\n')
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
            for variable,value in {'OPENAI_API_KEY':'relay-fresh-codex-synthetic-only'}.items():
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
        body={'project_id':project,'name':'Explicit fresh recovery','provider':'codex',
            'session_relay':True,'worktree':False,'initial_prompt':'','model':'fixture-model'}
        response=await client.post('/api/sessions',json=body);assert response.status_code==201,response.text
        fresh_sid=response.json()['id'];assert fresh_sid!=old_sid
        assert len(factory.active)==1 and fresh_sid in manager.runs
        fresh,=factory.active.values();launch=manager.runs[fresh_sid]
        assert fresh.binding.native_conversation!=before['nativeId'] and fresh.owner.alive
        assert fresh.spec.spec['new_session'] is True
        assert model.calls==0 and not payloads
        with c.db() as db:
            session=db.get(Session,fresh_sid);row=db.get(RelaySession,fresh_sid)
            assert not row.sealed and not session.launch_spec.inputs.get('initial_prompt')
            assert not list(db.query(RelayOperation).filter_by(session_id=fresh_sid))
            assert not list(db.query(RelayDecision).filter_by(session_id=fresh_sid))
        @write_intent
        def qualification_only():
            with c.db() as db:
                db.get(RelaySession,fresh_sid).capabilities={'text':'qualified','terminal.input':'qualified'};db.commit()
            fresh.adapter.record=replace(fresh.adapter.record,qualification_id='offline-fresh-codex',qualification_date='2026-11-12',
                operations={name:p.CapabilityEntry(p.Support.QUALIFIED,evidence=('isolated generated fixture only',)) for name in ('text','terminal.input')})
        qualification_only()
        public=await Public.create(c,manager,fresh_sid,features=['queue','writer'])
        snapshot=(await public.call('session.snapshot',{}))['result']
        assert snapshot.get('l1') is None
        with c.db() as db:
            row=db.get(RelaySession,fresh_sid);configuration=row.configuration_revision;control=row.control_revision
        take={'sessionId':fresh_sid,'expectedRunId':str(launch.run_id),'attachmentId':public.view['attachmentId'],
            'expectedRevision':control,'view':'chat','reason':'Explicit fresh Codex Chat input'}
        registered_take=await public.call('operation.register',{'expectedRunId':str(launch.run_id),
            'registrationKey':'explicit-fresh-chat-writer','method':'writer.take','payloadDigest':p.operation_digest('writer.take',take)})
        taken=await public.call('writer.take',{**take,'operationId':registered_take['result']['operationId']})
        assert taken['result']['state']=='accepted',taken
        assert model.calls==0 and not payloads
        with c.db() as db:control=db.get(RelaySession,fresh_sid).control_revision
        text='Fresh reviewed recovery message'
        fields={'sessionId':fresh_sid,'expectedRunId':str(launch.run_id),'text':text,
            'expectedConfigurationRevision':configuration,'expectedControlRevision':control,'ifBusy':'reject'}
        registered=await public.call('operation.register',{'expectedRunId':str(launch.run_id),
            'registrationKey':'explicit-fresh-chat','method':'turn.submit','payloadDigest':p.operation_digest('turn.submit',fields)})
        result=await public.call('turn.submit',{**fields,'operationId':registered['result']['operationId']});assert 'result' in result,result
        async with asyncio.timeout(20):
            while True:
                with c.db() as db:
                    op=db.get(RelayOperation,registered['result']['operationId'])
                    completed=op.receipt['state']=='accepted' and db.get(RelaySession,fresh_sid).active_operation_id is None
                if completed and fresh.adapter.active_turn is None:break
                await asyncio.sleep(.02)
        assert model.calls==1 and len(payloads)==1
        messages=json.dumps(payloads[0].get('input',[]))
        assert text in messages and 'Native unanswered permission expiry' not in messages and 'Production same-id handover fixture' not in messages and 'Offline public stock approval request' not in messages and 'Offline installed guard response.' not in messages
        assert (await public.call('session.snapshot',{}))['result'].get('l1') is None
        after=await client.get(f'/api/sessions/{old_sid}/relay');assert after.status_code==200,after.text
        assert after.json()==old
        events_after=await client.get(f'/api/sessions/{old_sid}/relay/events');assert events_after.status_code==200
        assert events_after.json()['events']==old_events.json()['events']
        with c.db() as db:
            assert db.get(RelaySession,old_sid).sealed
            assert {v.id:dict(v.receipt) for v in db.query(RelayOperation).filter_by(session_id=old_sid)}==before['receipts']
            assert {v.id:dict(v.decision) for v in db.query(RelayDecision).filter_by(session_id=old_sid)}==before['decisions']
        await fresh.close();assert not fresh.owner.alive and fresh.owner.runtime.closed and not factory.active
        empty_tree=getattr(fresh.owner.runtime,'exit_proven',False)
        assert empty_tree, 'Fresh Codex qualification requires positive enclosure cleanup proof'
        return {'newProvider':'codex','oldSessionId':old_sid,'newSessionId':fresh_sid,'oldNativeId':before['nativeId'],
            'newNativeId':fresh.binding.native_conversation,'ordinaryPersonCookieCsrfLaunch':True,
            'realReviewedReservationEnrollmentAndNativeAppServer':True,'blankLaunchNoModelCall':True,
            'explicitNewChatOnly':True,'explicitNewChatWriterTake':True,'writerTakeNoModelCallOrTyping':True,'freshModelCalls':model.calls,'oldSnapshotEventsReceiptsDecisionsUnchanged':True,'oldMessageOutputPresent':old_message_output,
            'noOldPromptOrApprovalReplay':True,'freshNativeOwnerStopped':True,'freshNativeEmptyTreeProven':empty_tree,
            'sourceOldOwnerAlreadyExited':(json.loads(Path('/evidence/execution-checkpoint.json').read_text())['ordering'].get('ownerTreeExited',False) if source_old_owner_exited is None else source_old_owner_exited),'fixturePublicLaunchAndTextOverridesOnly':True,
            'scope':'Actual isolated fresh Codex public launch with generated synthetic-only endpoint/config/key after original owner/broker loss; old owner exit/liveness and restart are recorded separately. Explicit reviewed same-provider new Codex session and new Chat action, no live account or subscription or automatic same-session re-enrollment. UI click-to-blank-review is separately browser qualified.'}
    finally:
        if public:await public.close()
        await client.aclose()
        if fresh:await fresh.close()
        await model.close()
