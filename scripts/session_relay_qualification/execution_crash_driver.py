"""Build a native local-account execution crash fixture around pinned g56 code."""
import hashlib
from pathlib import Path

G56='141ece9734e7f743cf60b4bb932ef600425fdc15'
CONTROLLER='552fb9bdf8d2c124a1d4e0fad8953dfa7ea42cbb49aebe486fd7372901dd5877'


def build(original):
    suffix=original[original.index('async def controller():'):]
    assert hashlib.sha256(suffix.encode()).hexdigest()==CONTROLLER
    entry=suffix.index("if __name__ == '__main__':")
    function=suffix[:entry]
    config='''def config():
    import pwd
    account=pwd.getpwuid(os.getuid()).pw_name
    return BrokerConfig(socket_path=Path('/tmp/enrolled/sockets')/(account+'.sock'),
        run_dir=Path('/tmp/enrolled/runs'),allowed_roots=(Path('/tmp/enrolled/work'),),
        allowed_uids=frozenset({os.getuid()}),providers={'claude':'/usr/bin/claude'},
        tmux_socket='execution-crash-native',hook_path='/opt/relay-install/native-hook')


'''
    worker=Path(__file__).with_name('execution_crash_worker.py').read_text()
    readiness=Path(__file__).with_name('restart_readiness.py').read_text()
    survivor=Path(__file__).with_name('surviving_enclosure.py').read_text()
    wrapper='''
_g56_controller=controller
async def controller():
    task=asyncio.create_task(_g56_controller())
    survivor=None;survival_proof=None
    async def inspect(method):
        return await rpc('/evidence/inspect.sock',method)
    try:
        async with asyncio.timeout(15):
            while not Path('/evidence/ready.json').exists():
                if task.done():await task
                await asyncio.sleep(.02)
        child=task.get_coro().cr_frame.f_locals['process']
        await wait_for_worker_state(rpc,'/evidence/worker.sock',child.pid)
        async with asyncio.timeout(90):
            while True:
                state=await inspect('state')
                if state['barrier']:break
                await asyncio.sleep(.05)
        checkpoint=json.loads(Path('/evidence/execution-checkpoint.json').read_text())
        assert checkpoint['pid']==state['pid'] and state['active']==1 and not state['recoveryOnly']
        assert checkpoint['referenceDurable']==(checkpoint['stage']=='lost_ack')
        if (json.loads(Path('/evidence/security.json').read_text()).get('executionRecoveryNew')
                and checkpoint['stage'] in ('loss_before','loss_after')):
            survivor=SurvivingEnclosure(checkpoint['ownerPid'],checkpoint['ownerStarted'],'/usr/bin/claude',checkpoint['ownerEnclosure'])
            survival_proof=await survivor.pause()
        killed=await inspect('kill');assert killed['killed']==checkpoint['pid']
        restarted=await inspect('restart');assert restarted['started']!=killed['killed']
        # The pinned start removes stale names, but bind precedes listen.
        # The child publishes this marker atomically only after listening.
        readiness_checks=0
        async with asyncio.timeout(10):
            while True:
                readiness_checks+=1
                ready_path=Path('/evidence/inspection-ready.json')
                marker=json.loads(ready_path.read_text()) if ready_path.exists() else None
                if marker=={'pid':restarted['started']}:break
                frame=task.get_coro().cr_frame
                child=frame.f_locals.get('process') if frame else None
                assert child is not None and child.pid==restarted['started'] and child.returncode is None
                await asyncio.sleep(.02)
        await wait_for_worker_state(rpc,'/evidence/worker.sock',restarted['started'])
        after=await inspect('state')
        assert after['pid']==restarted['started'] and after['active']==0 and after['recoveryOnly']
        reconciled=await inspect('qualify')
        binding_loss=json.loads(Path('/evidence/security.json').read_text()).get('executionBindingLoss',False)
        if binding_loss:assert reconciled.get('refused')=='STALE_AUTHORITY',reconciled
        complete=not binding_loss and checkpoint['stage'] in ('completion','lost_ack','loss_after','revoke_after')
        assert reconciled['reconciled']==int(complete) and reconciled['requests']==0,reconciled
        repeated=await inspect('qualify');assert repeated['reconciled']==0,repeated
        final=await inspect('clean');receipt=final['receipt'];original=checkpoint['originalReceipt']
        assert receipt['state']==('accepted' if complete else 'uncertain'),final
        assert receipt['actor']==original['actor'] and receipt['payloadDigest']==original['payloadDigest']
        assert final['sealed'] and final['active']==0 and final['recoveryOnly'] and final['ownershipPhase']=='recovery_required'
        fresh=None
        if json.loads(Path('/evidence/security.json').read_text()).get('executionRecoveryNew'):
            assert checkpoint['stage'] in ('interrupt','loss_before','loss_after')
            if checkpoint['stage']=='interrupt':assert not final['old_execution']
            else:
                if not final['old_execution']:
                    old=Path('/proc/'+str(checkpoint['ownerPid'])+'/stat')
                    print('Old enclosure liveness refusal:',json.dumps({'trackedBeforeKill':state['runs'],
                        'ownerPid':checkpoint['ownerPid'],'exists':old.exists(),
                        'processState':old.read_text().rsplit(')',1)[1].split()[0] if old.exists() else None}),flush=True)
                assert final['old_execution']
            fresh=await inspect('debug')
            assert fresh['ordinaryPersonCookieCsrfLaunch'] and fresh['noOldPromptOrApprovalReplay']
            if survivor:
                assert survivor.check()==survival_proof
                survivor.resume();await survivor.exited()
                directory=Path(checkpoint['ownerEnclosure'])
                async with asyncio.timeout(3):
                    while not (directory/'exit.json').exists():await asyncio.sleep(.01)
                exited=json.loads((directory/'exit.json').read_text())
                started=json.loads((directory/'started.json').read_text())
                assert exited['empty'] and all(exited[key]==started[key] for key in ('runId','token','pid','started'))
                survival_proof={**survival_proof,'nativeChildAliveDuringFreshRecovery':True,
                    'brokerDeathFencePreserved':True,'resumedSupervisorEmptyTree':True}
        Path('/evidence/result.json').write_text(json.dumps({'provider':'claude','executionCrash':{
            'freshPidRpcReadiness':True,'postListenMarkerChecks':readiness_checks,'stage':checkpoint['stage'],'oldPid':killed['killed'],'newPid':restarted['started'],
            'executionBrokerAndMainReallyKilled':True,'freshMainOpenedExistingDB':True,
            'nativeCompletionOnlyBeforeCrash':checkpoint['stage']=='completion','verifiedReferenceAbsentBeforeCrash':not checkpoint['referenceDurable'],
            'reconciled':reconciled['reconciled'],'scopeRefusal':reconciled.get('refused'),'receipt':receipt,'originalOperation':checkpoint['originalOperation'],
            'sourceOwnerPid':checkpoint['ownerPid'],'oldExecutionStillAlive':final['old_execution'],
            'hungSupervisorFault':survival_proof,
            'explicitOwnerCleanupStillRequired':bool(final['old_execution']),
            'noNewWriterOrModelService':fresh is None,'readOnlyReconciliationCreatesNoWriterOrModelService':True,'freshSessionRecovery':fresh,'sessionStaysSealed':True,'productionRecoveryPhase':final['ownershipPhase'],'duplicateReconciliationEmpty':True,
            'checkpoint':checkpoint,'g56Revision':'141ece9734e7f743cf60b4bb932ef600425fdc15',
            'scope':'Actual pinned offline Claude local-account SDK-to-TUI/public cookie CAS, durable prepared/sent/completion barrier, real g56 SIGKILL/waitpid/restart of execution broker and co-located main, fresh DB/main and production original-binding read-only recovery. No automatic owner cleanup/recovery, no remote node/HA claim, no real settings/subscription.'}}))
    except BaseException:
        frame=task.get_coro().cr_frame
        child=frame.f_locals.get('process') if frame else None
        phase('controller_failure',workerPid=getattr(child,'pid',None),workerReturncode=getattr(child,'returncode',None))
        raise
    finally:
        if survivor:survivor.close()
        if not task.done() and Path('/evidence/inspect.sock').exists():await inspect('finish')
        await task

'''
    result=(original[:original.index('def config():')]+config+
        original[original.index('async def rpc('):original.index('async def worker():')]+readiness+'\n'+survivor+'\n'+worker+'\n'+function+wrapper+suffix[entry:])
    assert function in result
    return result,hashlib.sha256(function.encode()).hexdigest()
