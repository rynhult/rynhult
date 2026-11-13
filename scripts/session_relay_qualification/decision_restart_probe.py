"""g56 real process controller plus production original-binding evidence channel.

Synthetic provider completion only. Does not qualify native application across
restart; native >30s proof is a separate profile.
"""
import asyncio
import hashlib
import importlib.util
import json
import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from relay_helpers import ACTOR, setup

from acc.server.broker_client import Brokers
from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation, RelayRun, RelaySession, Session, SessionRun, utcnow
from acc.server.session_relay import reconciliation, store
from acc.server.session_relay.runtime import RuntimeManager
from acc.server.vault import Cipher
from acc.session_relay import protocol as p

G56='141ece9734e7f743cf60b4bb932ef600425fdc15'
SID='abc123def456'


async def capture(repeats=20):
    root=Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='acc-pytest-decision-restart-') as temp:
        home=Path(temp);helpers=home/'helpers';helpers.mkdir()
        def original(name):return subprocess.check_output(['git','show',G56+':scripts/session_relay_qualification/'+name],cwd=root).decode()
        fixture=original('lifetime_fixture.py')
        controller=fixture[fixture.index('async def controller():'):]
        worker=Path(__file__).with_name('decision_restart_worker.py').read_text()
        fixture=fixture[:fixture.index('async def worker():')]+worker+'\n\n'+controller
        assert fixture[fixture.index('async def controller():'):]==controller
        (helpers/'lifetime_fixture.py').write_text(fixture)
        (helpers/'main_restart_worker.py').write_text(original('main_restart_worker.py'))
        probe=original('lifetime_probe.py').replace('root = Path(__file__).resolve().parents[2]','root = Path('+repr(str(root))+')')
        (helpers/'lifetime_probe.py').write_text(probe)
        shutil.copyfile(Path(__file__).with_name('probe.py'),helpers/'probe.py')
        spec=importlib.util.spec_from_file_location('decision_g56_probe',helpers/'lifetime_probe.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        work=home/'workspace';work.mkdir(mode=0o700);runs=home/'runs';runs.mkdir(mode=0o700)
        case=setup(home);account=pwd.getpwuid(os.getuid()).pw_name
        case.ctx.broker=Brokers(home/'unused.sock',None,account,'fixture-services')
        case.ctx.supervisor=SimpleNamespace()
        manager=RuntimeManager(case.ctx)
        guards={'guardrails.json':p.sha256_digest(b'{}')}
        directory=runs/SID;directory.mkdir(mode=0o700)
        (directory/'guardrails.json').write_text('{}');(directory/'guardrails.json').chmod(0o400)
        installation=p.sha256_digest(Cipher(case.ctx.settings.data_dir/'secrets/session_relay_cursor.key').key(create=True))
        scope=vars(p.Binding('binding',installation,'local',account,str(work),'claude','native',p.sha256_digest(p.canonical_json(guards))))
        @write_intent
        def configure():
            with case.db() as db:
                db.delete(db.get(RelayRun,1));db.delete(db.get(RelaySession,'relay'));db.delete(db.get(SessionRun,1));db.flush()
                session=db.get(Session,'relay');session.id=SID;session.run_as=account;session.workdir=str(work)
                session.run_dir=str(directory);session.native_id='native';db.flush()
                db.add(SessionRun(id=1,session_id=SID,reason='launch',verified_at=utcnow()));db.flush()
                store.enroll(db,SID,1,'binding',identity={},frozen_policy={},policy_revision=0)
                session.provider='claude'
                session.claude_auth='api_key'
                placement=manager.current_placement(db,session,provider='claude')
                run=db.get(RelayRun,1);run.identity={**run.identity,'binding':scope,'placement':placement}
                run.frozen_policy={'launchDigest':session.launch_spec.digest,'guardDigest':scope['launch_policy_digest'],
                    'authMode':'api_key','ownershipMode':'exclusive_idle'}
                db.commit()
        configure()
        output=None;config={'workspace':str(work),'run_dir':str(runs),'scope':scope,'session_id':SID}
        def prepare(folder):
            nonlocal output
            output=folder;(folder/'config.json').write_text(json.dumps(config))
            case.ctx.broker.default_socket=folder/'broker.sock'
        task=asyncio.create_task(asyncio.to_thread(module.capture,prepare,timeout=300))
        async def rpc(method):
            reader,writer=await asyncio.open_unix_connection(str(output/'inspect.sock'))
            try:
                writer.write(json.dumps({'method':method}).encode()+b'\n');await writer.drain()
                result=json.loads(await asyncio.wait_for(reader.readline(),20));assert 'error' not in result,result
                return result
            finally:writer.close();await writer.wait_closed()
        previous=reconciliation.QUALIFIED;reconciliation.QUALIFIED=True
        results=[]
        try:
            for _ in range(1000):
                if task.done():await task
                if output and (output/'ready.json').exists():break
                await asyncio.sleep(.02)
            assert output and (output/'ready.json').exists()
            # Brokers.fresh uses its configured socket selector; this is an
            # actual account Broker stream, never a mocked RuntimePeer.
            case.ctx.broker.default_socket=output/'broker.sock'
            for index in range(repeats):
                barrier=('prepared','sent','completion','lost_ack')[index%4]
                decision_id='decision-'+str(index)
                digest=p.sha256_digest(p.canonical_json(['original-action',index]))
                payload={'sessionId':SID,'expectedRunId':'1','decisionId':decision_id,'choiceId':'accept','expectedRevision':0,'actionDigest':digest}
                @write_intent
                def chosen():
                    with case.db() as db:
                        row=store.lock(db,SID)
                        op=store.register(db,row,ACTOR,'restart-'+str(index),'decision.reply',p.operation_digest('decision.reply',payload),{})
                        operation=db.get(RelayOperation,op.operation_id)
                        store.transition(db,row,operation,p.ReceiptState.SUBMITTING)
                        store.transition(db,row,operation,p.ReceiptState.UNCERTAIN)
                        decision=p.Decision(decision_id,SID,'1',p.DecisionKind.TOOL_APPROVAL,digest,'Synthetic original action',
                            (p.DecisionChoice('accept','Allow',p.ChoiceEffect.ALLOW_ONCE),),2**52,0)
                        decision=p.reserve_answer(decision,actor=ACTOR,run_id='1',choice_id='accept',expected_revision=0,action_digest=digest,policy_revision=0,now_ms=0)
                        decision=p.advance_decision(decision,p.DecisionStatus.OUTCOME_UNKNOWN)
                        db.add(RelayDecision(id=decision_id,session_id=SID,run_id=1,decision=p.to_wire(decision)))
                        db.commit();return operation.receipt
                receipt=chosen();key=receipt['operationId']
                config.update(barrier=barrier,record={'event':{'kind':'decision.applied','decisionId':decision_id,'operationId':key,'choiceId':'accept','actionDigest':digest},
                    'runId':'1','operation':{k:receipt[k] for k in ('operationId','runId','method','actor','payloadDigest')}})
                (output/'config.json').write_text(json.dumps(config))
                prepared=await rpc('qualify')
                if barrier=='lost_ack':
                    handle=reconciliation.OriginalEvidence.handle
                    async def lost(self,method,params):
                        result=await handle(self,method,params)
                        if method=='evidence.publish':raise ConnectionError('Publication ACK lost after main commit')
                        return result
                    reconciliation.OriginalEvidence.handle=lost
                    try:
                        try:await manager.reconcile_decisions(SID,1)
                        except (p.RelayError,ConnectionError):pass
                        else:raise AssertionError('Lost ACK appeared accepted at broker')
                    finally:reconciliation.OriginalEvidence.handle=handle
                killed=await rpc('kill');assert killed['killed']==prepared['pid']
                restarted=await rpc('restart');assert restarted['started']!=killed['killed']
                result=await manager.reconcile_decisions(SID,1)
                assert result=={'reconciled':int(barrier in ('completion','lost_ack'))},result
                assert await manager.reconcile_decisions(SID,1)=={'reconciled':0}
                with case.db() as db:
                    final=db.get(RelayOperation,key).receipt
                    assert final['state']==('accepted' if barrier in ('completion','lost_ack') else 'uncertain')
                    assert final['actor']==receipt['actor'] and final['payloadDigest']==receipt['payloadDigest']
                state=await rpc('state');assert state['active']==state['requests']==0
                results.append({'barrier':barrier,'oldPid':killed['killed'],'newPid':restarted['started'],'receiptState':final['state']})
            return {'g56Revision':G56,'controllerSha256':hashlib.sha256(controller.encode()).hexdigest(),
                'unchangedG56Controller':True,'productionReadOnlyChannel':True,'productionMainOriginalBinding':True,
                'syntheticCompletionOnly':True,'nativeRestartQualified':False,'cycles':results}
        finally:
            reconciliation.QUALIFIED=previous
            if output and (output/'inspect.sock').exists() and not task.done():await rpc('finish')
            await task
            await manager.close();await case.ctx.broker.close();case.engine.dispose()
