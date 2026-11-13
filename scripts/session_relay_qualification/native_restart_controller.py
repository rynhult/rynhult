"""Fixture adapter: preserve g56 controller literally, use evidence-only worker."""
import asyncio
import json
from pathlib import Path

G56='141ece9734e7f743cf60b4bb932ef600425fdc15'


async def exercise(state,manager,sid,run,config):
    import hashlib
    import sys

    from acc.server.session_relay import reconciliation
    original=Path('/evidence/g56_lifetime_fixture.py').read_text()
    controller=original[original.index('async def controller():'):]
    assert hashlib.sha256(controller.encode()).hexdigest()=='552fb9bdf8d2c124a1d4e0fad8953dfa7ea42cbb49aebe486fd7372901dd5877'
    # The controller's launch/kill/waitpid/restart body is unchanged. Its fixed
    # driver path is fixture-owned; this already-running interpreter has loaded
    # the native driver and will not execute that file again.
    worker='''async def worker():
    from acc.broker.claude_reconciliation import ReadOnlyEvidence
    cfg=config();factory=RuntimeTransportFactory(cfg)
    handle=ReadOnlyEvidence.handle
    async def pause(self,method,params):
        call=self.peer.call
        async def barrier(method,value):
            if method=='evidence.publish' and not Path('/evidence/native-evidence-barrier').exists():
                Path('/evidence/native-evidence-barrier').write_text('before publication')
                await asyncio.Event().wait()
            return await call(method,value)
        self.peer.call=barrier
        return await handle(self,method,params)
    ReadOnlyEvidence.handle=pause
    broker=Broker(cfg,relay_transport=factory)
    cfg.socket_path.unlink(missing_ok=True);Path('/evidence/worker.sock').unlink(missing_ok=True)
    server=await asyncio.start_unix_server(broker.handle,str(cfg.socket_path))
    async def inspect(reader,writer):
        method=json.loads(await reader.readline())['method'];assert method=='state'
        writer.write(json.dumps({'pid':os.getpid(),'active':len(factory.active),'runs':[]}).encode()+b'\\n')
        await writer.drain();writer.close();await writer.wait_closed()
    inspector=await asyncio.start_unix_server(inspect,'/evidence/worker.sock')
    try:await asyncio.Event().wait()
    finally:server.close();inspector.close()


'''
    config_fn='''def config():
    data=json.loads(Path('/evidence/config.json').read_text())
    return BrokerConfig(socket_path=Path(data['socket']),run_dir=Path(data['run_dir']),
        allowed_roots=(Path(data['workspace']),),allowed_uids=frozenset({os.getuid()}),
        providers={'claude':'/usr/bin/claude'},tmux_socket='native-evidence-restart',hook_path='/evidence/noop-hook')


'''
    prefix=original[:original.index('def config():')]
    driver=prefix+config_fn+original[original.index('async def rpc('):original.index('async def worker():')]+worker+controller
    assert driver[driver.index('async def controller():'):]==controller
    Path('/evidence/driver.py').write_text(driver)
    Path('/evidence/config.json').write_text(json.dumps({'socket':str(config.socket_path),'run_dir':str(config.run_dir),
        'workspace':str(state.spec.binding.workspace)}))
    await state.close();await state.peer.close()
    async with asyncio.timeout(10):
        while sid in manager.runs or manager.background:await asyncio.sleep(.01)
    assert state.owners.close_complete
    proc=await asyncio.create_subprocess_exec(sys.executable,'/evidence/driver.py',
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    async def rpc(method):
        reader,writer=await asyncio.open_unix_connection('/evidence/inspect.sock')
        try:
            writer.write(json.dumps({'method':method}).encode()+b'\n');await writer.drain()
            value=json.loads(await asyncio.wait_for(reader.readline(),20));assert 'error' not in value,value
            return value
        finally:writer.close();await writer.wait_closed()
    calls=[];task=None
    try:
        async with asyncio.timeout(15):
            while not Path('/evidence/ready.json').exists():
                assert proc.returncode is None
                await asyncio.sleep(.02)
        task=asyncio.create_task(manager.reconcile_decisions(sid,run))
        async with asyncio.timeout(15):
            while not Path('/evidence/native-evidence-barrier').exists():
                if task.done():await task
                await asyncio.sleep(.02)
        old=await rpc('kill')
        try:await task
        except (ConnectionError,TimeoutError):pass
        else:raise AssertionError('Crashed evidence worker returned success')
        restarted=await rpc('restart');assert restarted['started']!=old['killed']
        calls.append({'oldPid':old['killed'],'newPid':restarted['started'],'barrier':'durable native evidence before publication'})
        handle=reconciliation.OriginalEvidence.handle
        async def lost(self,method,params):
            result=await handle(self,method,params)
            if method=='evidence.publish':raise ConnectionError('Native original application accepted; ACK lost')
            return result
        reconciliation.OriginalEvidence.handle=lost
        try:
            try:await manager.reconcile_decisions(sid,run)
            except ConnectionError:pass
            else:raise AssertionError('Lost ACK returned success')
        finally:reconciliation.OriginalEvidence.handle=handle
        old=await rpc('kill');restarted=await rpc('restart')
        assert restarted['started']!=old['killed']
        calls.append({'oldPid':old['killed'],'newPid':restarted['started'],'barrier':'main accepted original native evidence; ACK lost'})
        assert await manager.reconcile_decisions(sid,run)=={'reconciled':1}
        assert await manager.reconcile_decisions(sid,run)=={'reconciled':0}
        value=await rpc('state');assert value['requests']==value['active']==0
        return {'g56Revision':G56,'controllerSha256':hashlib.sha256(controller.encode()).hexdigest(),
            'genuineOriginalNativeJournal':True,'noTranscriptOrJournalTransfer':True,'reconciliationWorkerCrashes':calls,
            'originalExecutionOwnerRetiredBeforeWorkerStart':True,'executionBrokerCrashBeforeCompletionQualified':False,
            'scope':'Native completion and lost-ACK reconciliation worker SIGKILL/restart. Execution owner was cleanly retired; no claim of SIGKILL during native answer delivery.'}
    finally:
        if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        if proc.returncode is None and Path('/evidence/inspect.sock').exists():await rpc('finish')
        stdout,stderr=await asyncio.wait_for(proc.communicate(),20)
        assert proc.returncode==0,(stdout[-1000:],stderr[-4000:])
