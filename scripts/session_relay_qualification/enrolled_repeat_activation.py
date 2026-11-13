"""Genuine input/receipt/idle Claude L1 round trip; repeated native activation."""
import asyncio
import base64
import copy
import os

from enrolled_frozen_native import Attached
from enrolled_frozen_native import Case as FrozenCase
from enrolled_frozen_native import prepare as prepare_frozen

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelaySession
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


class Case(FrozenCase):
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        original_select=state.controller.select
        async def diagnosed_select(token):
            try:return await original_select(token)
            except BaseException:
                import traceback
                traceback.print_exc()
                raise
        state.controller.select=diagnosed_select
        sid=state.spec.spec['session_id']
        native=state.binding.native_conversation
        old_run=str(launch.run_id)
        old_host,old_grant=owner.host,owner.host.grant
        old_enclosure=owner.process.enclosure
        old_identity=f'{owner.process.pid}:{owner.process.started}'
        assert old_enclosure.proof('started.json',old_identity)
        # Keep a read-only witness across normal factory cleanup of its fd.
        witness=copy.copy(old_enclosure)
        witness.fd=os.dup(old_enclosure.fd)
        assert old_host.boundary.verified_empty and old_host.admitted==old_host.processed==2
        async def select(target,key):
            with c.db() as db:revision=db.get(RelaySession,sid).revision
            return await manager.select_owner(c.resolve,sid,target=target,revision=revision,request_key=key)
        @write_intent
        def capabilities():
            with c.db() as db:
                db.get(RelaySession,sid).capabilities={'text':'qualified','terminal.input':'qualified'}
                db.commit()
        try:
            result=await select('chat','roundtrip-back-to-sdk')
            exited=witness.proof('exit.json',old_identity)
        finally:
            witness.close()
        assert result['changed'] and result['view']=='chat' and str(launch.run_id)!=old_run
        assert old_host.closed and old_host.boundary.frozen
        assert not owner.process.current() and exited
        sdk=state.owners.current
        assert sdk is not owner and sdk.spec.binding.native_conversation==native
        assert sdk.client.options.resume==native
        assert launch.terminal_grants.record is None
        capabilities()
        # The retired opaque token cannot acquire authority on the replacement run.
        old_frame={'inputGrant':claim.input_grant,'inputEpoch':claim.input_epoch,'frameSeq':3,
            'data':base64.b64encode(b'Must never replay').decode()}
        try:await manager.input_terminal(attached,sid,old_frame)
        except (p.RelayError,ConnectionError):pass
        else:raise AssertionError('Retired Claude grant reached SDK replacement')
        run=str(launch.run_id)
        with c.db() as db:revision=db.get(RelaySession,sid).control_revision
        fields={'sessionId':sid,'expectedRunId':run,'text':'SDK after genuine native handover',
            'expectedControlRevision':revision,'expectedConfigurationRevision':0,'ifBusy':'reject'}
        registration=Hub(c.ctx).register(c.resolve,sid,run,'roundtrip-sdk-turn','turn.submit',p.operation_digest('turn.submit',fields))
        await manager.submit(c.resolve,sid,{**fields,'operationId':registration.operation_id})
        async with asyncio.timeout(20):
            while True:
                with c.db() as db:
                    accepted=db.get(RelayOperation,registration.operation_id).receipt['state']=='accepted'
                    idle=db.get(RelaySession,sid).active_operation_id is None
                if accepted and idle and not sdk.active:break
                await asyncio.sleep(.02)
        result=await select('terminal','roundtrip-new-native')
        assert result['changed'] and result['view']=='terminal' and str(launch.run_id)!=run
        fresh=state.owners.current
        assert fresh is not owner and fresh.spec.binding.native_conversation==native
        assert sdk.exit_verified and sdk.process.returncode is not None
        capabilities()
        resolver=Attached(c.resolve)
        run=str(launch.run_id)
        with c.db() as db:revision=db.get(RelaySession,sid).control_revision
        fields={'sessionId':sid,'expectedRunId':run,'attachmentId':resolver.attachment_id,'expectedRevision':revision}
        registration=Hub(c.ctx).register(resolver,sid,run,'roundtrip-fresh-claim','control.claim',p.operation_digest('control.claim',fields))
        current=await manager.claim_terminal(resolver,sid,{**fields,'operationId':registration.operation_id})
        assert current.input_grant!=claim.input_grant and current.input_epoch>claim.input_epoch
        assert fresh.host.boundary.draft=='' and fresh.host.admitted==fresh.host.processed==0
        for target,token in ((old_host,old_grant),):
            try:await target.write_frames(token,3,b'\r')
            except (p.RelayError,ConnectionError):pass
            else:raise AssertionError('Closed old Claude Host was revived')
        for sequence,data in ((1,b'Fresh native after SDK roundtrip'),(2,b'\r')):
            await manager.input_terminal(resolver,sid,{'inputGrant':current.input_grant,'inputEpoch':current.input_epoch,
                'frameSeq':sequence,'data':base64.b64encode(data).decode()})
            if sequence==1:await asyncio.sleep(.7)
        async with asyncio.timeout(20):
            while fresh.host.processed!=2 or launch.terminal_grants.record.processed!=2:await asyncio.sleep(.05)
        operation=launch.terminal_grants.record.native.operation_id
        with c.db() as db:
            receipt=db.get(RelayOperation,operation).receipt
            assert receipt['state']=='accepted' and receipt['actor']==p.to_wire(fresh.host.grant.actor)
        assert self.base.calls==4 and state.binding.native_conversation==native
        proof.update(actualPostInputTuiToSDK=True,genuineEmptyDrain=True,oldTuiWholeTreeExited=True,
            sameNativeIdAcrossRoundTrip=True,sdkResumeUsesExactId=True,postHandoverSDKTurnAccepted=True,
            secondNativeGrantFresh=True,oldGrantAndHostNeverRevived=True,freshNativeInputProcessed=True,
            noReplay=True,modelCalls=self.base.calls,
            scope='Actual enrolled main/broker/native API-key SDK->TUI->SDK->TUI, real input and protected native receipts; synthetic principal/attachment and fixture capability overrides. Empty drained boundary only; nonempty retained Claude client, approved subscription and public/browser integration remain unqualified.')


async def prepare(root,work,c):
    frozen=await prepare_frozen(root,work,c)
    return Case(frozen.base)
