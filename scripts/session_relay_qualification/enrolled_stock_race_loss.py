"""Owned stock response held at the actual stdio write lock, after main CAS."""
import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path

from enrolled_stock_owner_loss import terminate_native
from enrolled_stock_public import Case as PublicCase
from enrolled_stock_public import prepare as prepare_public
from public_native_http import Public

from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation, RelayOwnership, RelaySession, utcnow
from acc.session_relay import protocol as p


class Case(PublicCase):
    async def lifecycle(self,c,state,launch,manager,config,public,host,view,offer,answers,frame):
        sid=state.spec.spec['session_id'];rpc=state.owner.rpc
        bound=state.native_socket.approval_aliases.pending[offer.alias]
        original_record=launch.terminal_grants.record
        native_id=state.adapter.decisions[offer.decision_id]['native_id']
        post_write=self.mode.endswith('_after')
        after_write_ready=asyncio.Event();after_write_release=asyncio.Event()
        entered=asyncio.Event();finished=asyncio.Event();writes=[];original_write=rpc._write;writer=rpc.writer
        class Witness:
            def __getattr__(self,name):return getattr(writer,name)
            def write(self,data):
                message=json.loads(data)
                if type(message.get('id')) is type(native_id) and message.get('id')==native_id and 'result' in message:
                    writes.append(message)
                return writer.write(data)
        rpc.writer=Witness()
        async def observed(message,**options):
            if type(message.get('id')) is type(native_id) and message.get('id')==native_id and 'result' in message:entered.set()
            try:
                result=await original_write(message,**options)
                if post_write and message.get('id')==native_id and 'result' in message:
                    after_write_ready.set();await after_write_release.wait()
                return result
            finally:
                if message.get('id')==native_id and 'result' in message:finished.set()
        rpc._write=observed
        stale_mode=self.mode in ('revoke','expiry','deadline','retired')
        expiry_proof={}
        if self.mode in ('expiry','deadline'):
            register=launch.terminal_grants.approvals.register
            @write_intent
            def short_lifetime():
                register()
                with c.db() as db:
                    expires=utcnow()+timedelta(seconds=5)
                    if self.mode=='deadline':db.get(RelayOperation,launch.terminal_grants.approvals.submission.operation_id).deadline=expires
                    else:
                        record=db.get(RelayDecision,(sid,offer.decision_id))
                        record.decision={**record.decision,'expiresAtMs':int(expires.timestamp()*1000)}
                    db.commit();expiry_proof.update(expiresMs=int(expires.timestamp()*1000),createdMs=int(time.time()*1000),lifetimeSeconds=5)
            launch.terminal_grants.approvals.register=short_lifetime
        await rpc.write_lock.acquire();lock_held=True
        native_loss=None;pre_write_refusal=None;cancellation_proof={}
        expected_receipt='uncertain';expected_decision='outcome_unknown'
        def release_barriers():
            nonlocal lock_held
            if lock_held:rpc.write_lock.release();lock_held=False
            after_write_release.set()
        try:
            delivered=await frame(3,b'\r');assert 'result' in delivered,delivered
            await asyncio.wait_for(entered.wait(),10)
            submission=launch.terminal_grants.approvals.submission
            with c.db() as db:
                before=dict(db.get(RelayOperation,submission.operation_id).receipt)
                reserved=dict(db.get(RelayDecision,(sid,offer.decision_id)).decision)
            assert before['state']=='submitting' and reserved['status']=='reserved', (before,reserved)
            assert host.admitted==3 and host.processed==2 and len(answers)==1 and not writes
            process=state.owner.runtime.process
            if self.mode=='cancel':
                reviewer=await Public.create(c,manager,sid,features=['queue'])
                try:
                    current=await reviewer.call('operation.get',{'operationId':submission.operation_id})
                    assert current['result']['state']=='submitting'
                    assert current['result']['revision']==before['revision']
                    started=time.monotonic()
                    refused=await asyncio.wait_for(reviewer.call('operation.cancel',{'operationId':submission.operation_id,
                        'expectedRevision':current['result']['revision']}),5)
                    elapsed=time.monotonic()-started
                    assert refused['error']['data']['code']=='REVISION_CONFLICT',refused
                    with c.db() as db:
                        assert dict(db.get(RelayOperation,submission.operation_id).receipt)==before
                        assert dict(db.get(RelayDecision,(sid,offer.decision_id)).decision)==reserved
                        phase=db.get(RelayOwnership,sid).phase
                    assert phase=='owned' and state.ready and not launch.peer.closed and not rpc.closed
                    assert not writes and len(answers)==1
                    cancellation_proof={'code':refused['error']['data']['code'],'seconds':elapsed,
                        'refusedBeforeDispatchTimeoutOrLoss':True,'ownerPhase':phase,
                        'originalReceiptUnchanged':True,'reservedDecisionUnchanged':True,
                        'providerResponseWritesAtRefusal':len(writes),'responseAttemptsAtRefusal':len(answers)}
                finally:await reviewer.close()
            if post_write:
                rpc.write_lock.release();lock_held=False
                await asyncio.wait_for(after_write_ready.wait(),10)
                assert len(writes)==1 and host.processed==original_record.processed==2
                assert not state.adapter.answer_evidence.entries[offer.decision_id]['finished']
                async with asyncio.timeout(10):
                    while not (self.base.work/'.stock-loss-start').exists():await asyncio.sleep(.01)
            if self.mode in ('expiry','deadline'):
                async with asyncio.timeout(8):
                    while int(time.time()*1000)<=expiry_proof['expiresMs']:await asyncio.sleep(.02)
                expiry_proof['releasedMs']=int(time.time()*1000)
            elif self.mode=='retired':
                launch.terminal_grants.invalidate()
                assert launch.terminal_grants.record is None and original_record.phase=='revoked'
            elif self.mode=='revoke':
                public.revoke()
            elif self.mode in ('owner','owner_after','cancel'):
                # Only the exact fixture-owned process, pinned before signalling.
                native_loss=await terminate_native(state,release_barriers)
            elif self.mode in ('transport','transport_after'):
                writer.close();await asyncio.wait_for(writer.wait_closed(),10)
            else:raise AssertionError(self.mode)
            if not stale_mode:
                async with asyncio.timeout(10):
                    while not rpc.closed or not launch.peer.closed:await asyncio.sleep(.01)
        finally:
            release_barriers()
        if stale_mode:
            await asyncio.wait_for(finished.wait(),10)
            assert not writes, ('Provider response wrote after public cookie revocation while blocked at real write lock',writes)
            async with asyncio.timeout(10):
                while True:
                    with c.db() as db:receipt=dict(db.get(RelayOperation,submission.operation_id).receipt)
                    if receipt['state']=='failed':
                        journal=state.journal.lookup(submission.operation_id)
                        assert receipt.get('reason')=='rejected' and journal is not None and journal[0]=='rejected', (receipt,journal)
                        result=journal[1];evidence=result['evidence']
                        assert result['outcome']=='rejected' and evidence['kind']=='broker_refusal'
                        assert evidence['operation_id']==submission.operation_id and evidence['run_id']==before['runId']
                        assert not writes and receipt['actor']==before['actor'] and receipt['payloadDigest']==before['payloadDigest']
                        pre_write_refusal=result;expected_receipt='failed';expected_decision='invalidated'
                    if receipt['state']==expected_receipt:
                        with c.db() as db:status=db.get(RelayDecision,(sid,offer.decision_id)).decision['status']
                        if status==expected_decision:break
                    await asyncio.sleep(.01)
            assert host.processed==original_record.processed==2
            # Additional defined fixture failure, not a product auto-stop claim:
            # dispose this exact old owner, then ordinary person fresh recovery.
            native_loss=await terminate_native(state,release_barriers)
            async with asyncio.timeout(10):
                while not rpc.closed or not launch.peer.closed:await asyncio.sleep(.01)
        await asyncio.wait_for(state.close(),10)
        async with asyncio.timeout(10):
            while True:
                with c.db() as db:
                    row=db.get(RelaySession,sid);sealed=row.sealed
                    receipt=dict(db.get(RelayOperation,submission.operation_id).receipt)
                    decision=dict(db.get(RelayDecision,(sid,offer.decision_id)).decision)
                    phase=db.get(RelayOwnership,sid).phase
                if sealed and receipt['state']==expected_receipt:break
                await asyncio.sleep(.01)
        assert receipt['state']==expected_receipt and len(writes)==int(post_write) and len(answers)==1 and decision['status']==expected_decision
        assert host.processed==original_record.processed==2
        assert sid not in manager.runs and not state.factory.active and not state.owner.alive
        viewer=await Public.create(c,manager,sid,features=[])
        try:
            snapshot=(await viewer.call('session.snapshot',{}))['result']
            l1=snapshot.get('l1')
            assert l1 and l1['recoveryAvailable'] and l1['recoveryMode']=='new_session', (phase,l1)
            assert any(v['decisionId']==offer.decision_id and v['status']==expected_decision
                for v in [*snapshot['pendingDecisions'],*snapshot['decisionOutcomes']])
            if self.mode!='revoke':assert 'error' in await frame(3,b'\r')
            else:
                response=await public.client.post(f'/api/sessions/{sid}/relay',json={'jsonrpc':'2.0','id':'revoked-key',
                    'method':'ry/terminal.input','params':{'sessionId':sid,'attachmentId':public.view['attachmentId'],
                    'inputGrant':submission.frame.grant.grant,'inputEpoch':submission.frame.grant.epoch,'frameSeq':3,'data':'DQ=='}})
                assert response.status_code==401
            try:
                await state.native_socket.approval_aliases.reply(bound.peer,host,{'id':offer.alias,'result':{'decision':'accept'}})
            except p.RelayError as error:assert error.code is p.ErrorCode.STALE_AUTHORITY
            else:raise AssertionError('Old alias replayed after owner loss')
        finally:await viewer.close()
        from enrolled_fresh_recovery import recover_new
        if getattr(self,'fresh_codex',False):
            from enrolled_fresh_codex_recovery import recover_codex
            fresh=await recover_codex(c,config,state.factory,manager,sid,source_old_owner_exited=True,require_message_output=False)
        else:fresh=await recover_new(c,config,state.factory,manager,sid,source_old_owner_exited=True,require_message_output=False)
        assert len(writes)==int(post_write) and len(answers)==1
        Path('/evidence/result.json').write_text(json.dumps({'provider':'codex','mode':self.mode,
            'actualWriteLockHeldAfterMainCas':True,'queuedExpiryProof':expiry_proof,'mainInputGrantRetired':self.mode=='retired','postCasCancellationRefused':self.mode=='cancel','postCasCancellationProof':cancellation_proof,'actualProviderWriteDrainedBeforeLoss':post_write,'nativeToolStartedBeforeLoss':post_write,'nativeApplicationAcknowledged':False,'additionalFixtureOwnerStopAfterRevocation':self.mode=='revoke','before':before,'reserved':reserved,
            'receipt':receipt,'decision':decision,'providerResponseWrites':writes,'responseAttempts':answers,
            'nativeProcessed':host.processed,'mainProcessed':original_record.processed,
            'actualOwnedPid':process.pid,'nativeLossProof':native_loss,'preWriteRefusalProof':pre_write_refusal,'actualOwnedReturnCode':process.returncode,
            'oldOwnerStopped':not state.owner.alive,'publicFreshSessionRecovery':fresh,'freshRecoveryProvider':'codex' if getattr(self,'fresh_codex',False) else 'claude',
            'retiredAliasRefused':True,'noReplay':True,'terminalMediated':False,
            'scope':'Actual isolated pinned stock Enter decoded and reserved by main, genuine stdio write blocked, exact owned app-server stopped or real owned stdin closed before response bytes. Unknown original receipt, or failed/rejected only with exact original broker-refusal journal proof of zero bytes and invalidated decision; no processing ACK, explicit ordinary public fresh selected-provider session/new Chat (provider recorded) without replay. Revoke mode changes real public cookie while blocked before bytes, then injects separate exact old owner loss for explicit fresh recovery; no claim revocation auto-stops. After profiles wait for a fresh native tool-start marker from a bounded sleeping command, then lose real owner/stdin after response write+drain and before command completion, while adapter reply callback still awaits release and exact native application has not been published; original outcome remains unknown even if provider privately completed. Expiry/deadline modes register a valid five-second lifetime before grant capture then wait for real wall-clock expiry under the queued write lock. Retired mode invokes actual main TerminalGrants.invalidate at the defined barrier; no public release capability is claimed. Cancel mode issues real public operation.cancel after native reservation, requires prompt REVISION_CONFLICT while still owned/open with unchanged receipt/winner and zero provider bytes before the dispatch timeout, then separately injects exact old owner loss for fresh recovery. No subscription, broker restart, conclusive post-write application or other stock key claim.'}))


async def prepare_mode(root,work,c,mode):
    prepared=await prepare_public(root,work,c)
    case=Case(prepared.base);case.mode=mode
    if mode.endswith('_after'):
        assert not (case.base.work/'.stock-loss-start').exists()
        case.base.command='printf started > .stock-loss-start; sleep 60'
    return case
