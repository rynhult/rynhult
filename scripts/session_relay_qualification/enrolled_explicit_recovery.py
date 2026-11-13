"""Explicit public recovery only after authenticated old/partial tree cleanup."""
import asyncio
import base64

from enrolled_failed_target import Case as FailedCase
from enrolled_frozen_native import prepare as prepare_frozen
from public_native_http import Public

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelayOwnership, RelaySession
from acc.session_relay import protocol as p


class Case(FailedCase):
    after_fork=True
    failure_family='tui'
    connect_failure=False
    recovery_target='chat'
    lose_owned_ack=False
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        if self.failure_family=='sdk':
            from enrolled_failed_sdk_target import Case as SDKFailedCase
            await SDKFailedCase.after_granted(self,c,state,launch,manager,owner,attached,claim,proof)
        else:await super().after_granted(c,state,launch,manager,owner,attached,claim,proof)
        for peer in (state.peer,launch.peer):
            original=peer.handle
            async def diagnosed(method,params,_original=original):
                try:return await _original(method,params)
                except Exception:
                    import traceback
                    traceback.print_exc()
                    raise
            peer.handle=diagnosed
        from acc.server.session_relay import exclusive_recovery
        exclusive_recovery.RECOVERY_QUALIFIED=True
        c.ctx.relay_runtime=manager
        retired_factory=state.owners;retired_controller=state.controller
        old_run=str(launch.run_id);native=state.binding.native_conversation
        with c.db() as db:failed_switch=db.get(RelayOwnership,launch.session_id).commit_operation_id
        assert retired_factory.close_complete and await retired_factory.exited()
        expected_owned_loss=self.lose_owned_ack
        original_call=state.peer.call
        async def lose_owned(method,params):
            value=await original_call(method,params)
            if self.lose_owned_ack and method=='authority.recovery_owned':
                self.lose_owned_ack=False
                raise ConnectionError('Artificial loss after durable owned recovery commit')
            return value
        state.peer.call=lose_owned
        public=await Public.create(c,manager,launch.session_id,features=['switch'])
        try:
            snapshot=(await public.call('session.snapshot',{}))['result']
            assert snapshot['l1']['recoveryAvailable']
            fields={'sessionId':launch.session_id,'expectedRunId':old_run,'target':self.recovery_target,
                'expectedRevision':snapshot['l1']['revision'],'reason':'Reviewed stopped owner and same conversation recovery'}
            registered=await public.call('operation.register',{'expectedRunId':old_run,'registrationKey':'explicit-recovery',
                'method':'switch.recover','payloadDigest':p.operation_digest('switch.recover',fields)})
            assert 'result' in registered,registered
            operation=registered['result']['operationId']
            response=await public.call('switch.recover',{**fields,'operationId':operation})
            lost_commit=False
            if 'error' in response:
                assert expected_owned_loss,response  # only the planned lost-ACK scenario may request a new review
                assert response['error']['data']['code']=='INPUT_CONFLICT',response
                with c.db() as db:
                    assert db.get(RelayOperation,operation).receipt['state']=='accepted'
                    assert db.get(RelaySession,launch.session_id).sealed
                    assert db.get(RelayOwnership,launch.session_id).phase=='recovery_required'
                assert self.base.calls==2 and state.owners.close_complete
                lost_commit=True
                await public.close()
                public=await Public.create(c,manager,launch.session_id,features=['switch'])
                snapshot=(await public.call('session.snapshot',{}))['result']
                fields={**fields,'expectedRunId':str(launch.run_id),'expectedRevision':snapshot['l1']['revision'],
                    'reason':'New explicit review after lost owned acknowledgement'}
                registered=await public.call('operation.register',{'expectedRunId':str(launch.run_id),
                    'registrationKey':'second-explicit-recovery','method':'switch.recover',
                    'payloadDigest':p.operation_digest('switch.recover',fields)})
                assert 'result' in registered,registered
                operation=registered['result']['operationId']
                response=await public.call('switch.recover',{**fields,'operationId':operation})
            assert lost_commit==expected_owned_loss
            assert 'result' in response,response
            result=response['result']
            assert result['phase']=='owned' and result['view']==self.recovery_target and result['changed']
            assert str(launch.run_id)!=old_run and result['operationId']==operation
            assert state.owners is not retired_factory and state.controller is not retired_controller
            assert retired_factory.closed and retired_controller.closed and retired_controller.fenced
            assert state.owners.lease.root==retired_factory.lease.root
            assert state.owners.lease.identity.st_ino==retired_factory.lease.identity.st_ino
            from acc.broker.session_relay import PrivateRuntime
            try:PrivateRuntime(state.owners.directory/'exclusive',state.owners.base)
            except p.RelayError as error:assert error.code is p.ErrorCode.BUSY
            else:raise AssertionError('Recovery lost the conversation-wide owner mutex')
            adapter=state.owners.current
            assert adapter.spec.run_id==str(launch.run_id)
            if self.recovery_target=='chat':assert adapter.client.options.resume==native
            else:assert adapter.spec.spec['terminal_resume_args'][adapter.spec.spec['terminal_resume_args'].index('--resume')+1]==native
            assert self.base.calls==2  # connect/resume never queries or replays old text
            with c.db() as db:
                assert db.get(RelayOperation,operation).receipt['state']=='accepted'
                assert db.get(RelayOwnership,launch.session_id).phase=='owned'
                assert not db.get(RelaySession,launch.session_id).sealed
            stale=await public.call('session.snapshot',{})
            assert 'error' in stale  # never renew the old connection/input scope
        finally:await public.close()
        @write_intent
        def qualify():
            with c.db() as db:
                row=db.get(RelaySession,launch.session_id)
                row.capabilities={'text':'qualified','terminal.input':'qualified'};db.commit()
        qualify()
        fresh=await Public.create(c,manager,launch.session_id,
            features=['writer','terminal'] if self.recovery_target=='terminal' else [])
        try:
            snapshot=(await fresh.call('session.snapshot',{}))['result']
            if self.recovery_target=='terminal':
                fields={'sessionId':launch.session_id,'expectedRunId':str(launch.run_id),
                    'attachmentId':fresh.view['attachmentId'],'expectedRevision':snapshot['controlRevision']}
                op=await fresh.call('operation.register',{'expectedRunId':str(launch.run_id),'registrationKey':'fresh-recovery-claim',
                    'method':'control.claim','payloadDigest':p.operation_digest('control.claim',fields)})
                assert 'result' in op,op
                claimed=await fresh.call('control.claim',{**fields,'operationId':op['result']['operationId']})
                assert 'result' in claimed,claimed
                granted=claimed['result']
                for seq,data in ((1,b'Fresh explicit turn after recovery'),(2,b'\r')):
                    sent=await fresh.call('terminal.input',{'attachmentId':fresh.view['attachmentId'],
                        'inputGrant':granted['inputGrant'],'inputEpoch':granted['inputEpoch'],'frameSeq':seq,
                        'data':base64.b64encode(data).decode()})
                    assert 'result' in sent,sent
                    if seq==1:await asyncio.sleep(.7)
                async with asyncio.timeout(20):
                    while adapter.host.processed!=2 or self.base.calls!=3:await asyncio.sleep(.02)
            else:
                fields={'sessionId':launch.session_id,'expectedRunId':str(launch.run_id),'text':'Fresh explicit turn after recovery',
                    'expectedConfigurationRevision':0,'expectedControlRevision':snapshot['controlRevision'],'ifBusy':'reject'}
                op=await fresh.call('operation.register',{'expectedRunId':str(launch.run_id),'registrationKey':'fresh-after-recovery',
                    'method':'turn.submit','payloadDigest':p.operation_digest('turn.submit',fields)})
                assert 'result' in op,op
                reply=await fresh.call('turn.submit',{**fields,'operationId':op['result']['operationId']})
                assert 'result' in reply,reply
                async with asyncio.timeout(15):
                    while adapter.active or self.base.calls!=3:await asyncio.sleep(.02)
            with c.db() as db:
                assert db.get(RelayOperation,failed_switch).receipt['state']=='failed'
            receipt=await fresh.call('operation.get',{'operationId':operation})
            assert receipt['result']['state']=='accepted' and receipt['result']['method']=='switch.recover'
        finally:await fresh.close()
        proof.update(publicExplicitRecovery=True,oldAndPartialTreesVerifiedBeforeRecovery=True,
            recoveryNewFactoryAndController=True,recoveryStableConversationLease=True,recoverySameIdResume=True,recoveryNoAutomaticQuery=True,
            recoveryOriginalReceiptAccepted=True,failedSwitchConclusiveRefusalAfterCleanup=True,recoveryOldConnectionStale=True,recoveryFreshPublicTurn=True,
            recoveredView=self.recovery_target,failureFamily=self.failure_family,
            connectFailure=self.connect_failure,lostOwnedAckRequiresNewReview=lost_commit,
            explicitRecoveryAttempts=2 if lost_commit else 1,modelCalls=self.base.calls,scope='Public cookie explicit reviewed recovery after actual native post-fork TUI failure; authenticated source/partial-target tree cleanup, new factory/controller/run, exact same-id SDK resume, no replay and fresh public turn. API-key offline fixture only.')


async def prepare(root,work,c):
    frozen=await prepare_frozen(root,work,c)
    return Case(frozen.base)
