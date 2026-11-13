"""Supervisor kernel death while the real pinned vendor retains inherited pipes."""
import asyncio
import copy
import hashlib
import json
import os
import select
import signal
import time
from pathlib import Path
from types import SimpleNamespace

from enrolled_fresh_codex_recovery import recover_codex
from enrolled_stock_public import Case as PublicCase
from enrolled_stock_public import prepare as prepare_public

from acc.server.models import RelayDecision, RelayOperation, RelayOwnership, RelaySession, SessionRun
from acc.session_relay import protocol as p


def proc_identity(pid):
    fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    return int(fields[1]),fields[19]


class Case(PublicCase):
    async def lifecycle(self,c,state,launch,manager,config,public,host,view,offer,answers,frame):
        from acc.broker.relay_runtime import CLI_DIGEST
        runtime=state.owner.runtime;process=runtime.process;rpc=state.owner.rpc
        sid=state.spec.spec['session_id'];native_id=state.adapter.decisions[offer.decision_id]['native_id']
        ready=asyncio.Event();release=asyncio.Event();writes=[];original=rpc._write;writer=rpc.writer
        class Witness:
            def __getattr__(self,name):return getattr(writer,name)
            def write(self,data):
                value=json.loads(data)
                if 'result' in value or value.get('method') in ('turn/start','turn/interrupt'):writes.append(value)
                return writer.write(data)
        rpc.writer=Witness()
        async def held(value,**options):
            result=await original(value,**options)
            if type(value.get('id')) is type(native_id) and value.get('id')==native_id and 'result' in value:
                ready.set();await release.wait()
            return result
        rpc._write=held
        enclosure=runtime.enclosure;reader=copy.copy(enclosure);reader.fd=os.dup(enclosure.fd)
        root_fd=vendor_fd=None
        try:
            assert reader.proof('started.json',runtime.token)
            assert 'result' in await frame(3,b'\r')
            await asyncio.wait_for(ready.wait(),10)
            async with asyncio.timeout(10):
                while not (self.base.work/'.supervisor-tool-start').exists():await asyncio.sleep(.01)
            submission=launch.terminal_grants.approvals.submission
            with c.db() as db:
                before=dict(db.get(RelayOperation,submission.operation_id).receipt)
                reserved=dict(db.get(RelayDecision,(sid,offer.decision_id)).decision)
            assert before['state']=='submitting' and reserved['status']=='reserved' and host.processed==2
            assert len(writes)==len(answers)==1
            candidates=[]
            for value in Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split():
                pid=int(value)
                try:
                    parent,started=proc_identity(pid)
                    digest='sha256:'+hashlib.sha256(Path(f'/proc/{pid}/exe').read_bytes()).hexdigest()
                except OSError:continue
                if parent==process.pid and digest==CLI_DIGEST:candidates.append((pid,started))
            assert len(candidates)==1,candidates
            vendor,started=candidates[0];vendor_fd=os.pidfd_open(vendor);root_fd=os.pidfd_open(process.pid)
            assert proc_identity(vendor)==(process.pid,started)
            assert proc_identity(process.pid)[1]==runtime.token.split(':')[1]
            signal.pidfd_send_signal(root_fd,signal.SIGKILL)
            async with asyncio.timeout(2):
                while not select.select([root_fd],[],[],0)[0]:await asyncio.sleep(.005)
            kernel_exit=time.monotonic()
            vendor_alive=not select.select([vendor_fd],[],[],0)[0]
            assert vendor_alive and not rpc.closed, 'Fixture must retain live vendor and real pipes at root death'
            try:runtime.validate()
            except p.RelayError as error:assert error.code is p.ErrorCode.STALE_AUTHORITY
            else:raise AssertionError('Dead supervisor still validates while vendor retains inherited pipes')
            async with asyncio.timeout(1):
                while state.ready or state.broker.link is not None:await asyncio.sleep(.005)
            fenced_seconds=time.monotonic()-kernel_exit
            assert fenced_seconds<1 and not reader.proof('exit.json',runtime.token)
            try:await state.owner.notify('turn/interrupt',{'threadId':state.binding.native_conversation,'turnId':'never-delivered'})
            except p.RelayError as error:assert error.code is p.ErrorCode.STALE_AUTHORITY
            else:raise AssertionError('Dead-root native mutation was delivered')
            assert len(writes)==1
        finally:release.set()
        try:
            assert 'error' in await frame(4,b'Never deliver after supervisor death')
            async with asyncio.timeout(8):
                while not launch.peer.closed:await asyncio.sleep(.01)
            await asyncio.wait_for(state.close(),10)
            async with asyncio.timeout(10):
                while True:
                    with c.db() as db:
                        row=db.get(RelaySession,sid);owner=db.get(RelayOwnership,sid)
                        receipt=dict(db.get(RelayOperation,submission.operation_id).receipt)
                        decision=dict(db.get(RelayDecision,(sid,offer.decision_id)).decision)
                        run=db.get(SessionRun,launch.run_id)
                        ended=(run.end_reason,run.exit_status,run.exit_signal)
                        sealed=row.sealed and owner.phase=='recovery_required'
                    if sealed and receipt['state']=='uncertain' and decision['status']=='outcome_unknown':break
                    await asyncio.sleep(.01)
            assert ended==('native_exit_unknown',None,None),ended
            assert not runtime.exit_proven and not reader.proof('exit.json',runtime.token)
            assert len(writes)==len(answers)==1 and host.processed==2
            # No trusted native-tree exit: fresh review must not adopt this tree.
            fresh=await recover_codex(c,config,state.factory,manager,sid,source_old_owner_exited=False)
            assert not fresh['sourceOldOwnerAlreadyExited'] and fresh['oldMessageOutputPresent']
            assert len(writes)==1 and not runtime.exit_proven
            Path('/evidence/result.json').write_text(json.dumps({'provider':'codex','supervisorKernelKilled':True,
                'supervisorPid':process.pid,'vendorPid':vendor,'vendorStarted':started,'vendorAliveAtKernelExit':vendor_alive,
                'fencedSeconds':fenced_seconds,'mutationValidationRefusedBeforePipeEof':True,'oldFrameRefused':True,
                'nativeMutationsAfterFence':0,'providerResponseWrites':writes,'responseAttempts':answers,
                'receipt':receipt,'decision':decision,'originalReceipt':before,'originalDecision':reserved,
                'nativeProcessed':host.processed,'oldCleanupUnknown':True,'emptyTreeReceiptAbsent':True,
                'nativeExitOutcome':ended,'publicFreshSessionRecovery':fresh,'noAdoptionOrReplay':True,
                'scope':'Actual isolated integration supervisor SIGKILL, real pinned vendor alive with inherited pipes at kernel death, prompt mutation fencing independent of EOF, unknown native exit/cleanup retained. Cached old records/output readable; explicit ordinary reviewed new Codex session/writer take/new Chat, no old-tree adoption or prompt/answer replay. Fixture PID namespace teardown is not product cleanup. No live account, subscription or automatic same-session recovery.'}))
        finally:
            for fd in (root_fd,vendor_fd):
                if fd is not None:os.close(fd)
            reader.close()


async def prepare(root,work,c):
    prepared=await prepare_public(root,work,c);case=Case(prepared.base)
    case.base.command='printf started > .supervisor-tool-start; sleep 60'
    assert not (work/'.supervisor-tool-start').exists()
    original=case.base.codex_events
    def events(payload):
        values=original(payload)
        if case.base.calls!=1:return values
        from installed_security import Security
        text=Security.codex_events(SimpleNamespace(calls=2),payload)
        combined=[values[0]]+text[1:-1]
        for name,value in values[1:-1]:
            value=copy.deepcopy(value)
            if 'output_index' in value:value['output_index']=1
            combined.append((name,value))
        name,last=copy.deepcopy(values[-1]);last['response']['output']=[text[-1][1]['response']['output'][0],*last['response']['output']]
        combined.append((name,last))
        return [(name,{**value,'sequence_number':index}) for index,(name,value) in enumerate(combined)]
    case.base.codex_events=events
    return case
