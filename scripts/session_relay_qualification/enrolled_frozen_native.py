"""Frozen production native security with actual grants/decoder/provider receipt."""
import asyncio
import base64
import json
from pathlib import Path

from enrolled_handover import prepare as prepare_handover

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelaySession
from acc.server.session_relay import writers
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


class Attached:
    attachment_id='offline-frozen-tab'
    def __init__(self,resolve):self.resolve=resolve
    def __call__(self):return self.resolve()


class Case:
    def __init__(self,base):self.base=base
    def __getattr__(self,name):return getattr(self.base,name)

    async def claim(self,c,sid,run,launch,manager):
        attached=Attached(c.resolve)
        with c.db() as db:revision=db.get(RelaySession,sid).control_revision
        params={'sessionId':sid,'expectedRunId':run,'attachmentId':attached.attachment_id,'expectedRevision':revision}
        op=Hub(c.ctx).register(attached,sid,run,'frozen-native-claim','control.claim',p.operation_digest('control.claim',params))
        claim=await manager.claim_terminal(attached,sid,{**params,'operationId':op.operation_id})
        return attached,claim

    async def check_revocation(self,c,owner,grant):
        c.state['revoked']=True
        try:
            try:await owner.host.write_frames(grant,1,b'Revoked native input')
            except (p.RelayError,ConnectionError):pass
            else:raise AssertionError('Revoked current person reached native decoder')
        finally:c.state['revoked']=False
        return True

    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        pass

    async def exercise(self,c,state,launch,manager,config):
        await self.base.exercise(c,state,launch,manager,config)
        proof=json.loads(Path('/evidence/result.json').read_text())
        sid,run=state.spec.spec['session_id'],str(launch.run_id)
        owner=state.owners.current
        @write_intent
        def capabilities(value):
            with c.db() as db:
                db.get(RelaySession,sid).capabilities=value
                db.commit()
        # Explicit isolated qualification gate, never a production attestation.
        capabilities({'text':'qualified','terminal.input':'qualified'})
        previous=writers.PUBLIC_WRITER_QUALIFIED
        writers.PUBLIC_WRITER_QUALIFIED=True
        try:
            attached,claim=await self.claim(c,sid,run,launch,manager)
            assert claim.receipt.state is p.ReceiptState.ACCEPTED
            grant=owner.host.grant
            assert grant.grant==claim.input_grant and owner.host.attested is False
            pid=owner.process.pid
            refused=[]
            for data in (b'/config\r',b'/login\r',b'/logout\r',b'/permissions\r',b'/model other\r'):
                try:await owner.host.write_frames(grant,1,data)
                except p.RelayError as error:assert error.code is p.ErrorCode.UNSUPPORTED
                else:raise AssertionError('Granted config/auth bytes reached native decoder')
                assert owner.host.admitted==owner.host.processed==0
                refused.append(data.decode().strip())
            revoked=await self.check_revocation(c,owner,grant)
            assert owner.host.admitted==owner.host.processed==0
            assert self.base.calls==1
            text='Frozen granted native continuation'
            for sequence,data in ((1,text.encode()),(2,b'\r')):
                await manager.input_terminal(attached,sid,{'inputGrant':claim.input_grant,'inputEpoch':claim.input_epoch,
                    'frameSeq':sequence,'data':base64.b64encode(data).decode()})
                if sequence==1:await asyncio.sleep(.7)  # actual stock paste-burst boundary, no synthetic Enter
            async with asyncio.timeout(20):
                while True:
                    with c.db() as db:
                        completed=db.get(RelaySession,sid).active_operation_id is None
                    if owner.host.processed==2 and launch.terminal_grants.record.processed==2 and completed:break
                    await asyncio.sleep(.05)
            boundary=owner.host.boundary
            assert boundary.operation is None and owner.process.pid==pid and owner.process.current()
            submission=launch.terminal_grants.record.native
            assert submission.params['text']==text
            with c.db() as db:
                record=db.get(RelayOperation,submission.operation_id)
                assert record.receipt['state']=='accepted'
                assert record.receipt['payloadDigest']==p.operation_digest('turn.submit',submission.params)
                assert record.receipt['actor']==p.to_wire(grant.actor)
                assert db.get(RelaySession,sid).active_operation_id is None
            assert owner.host.admitted==owner.host.processed==2
            entries=[entry for entry in launch.terminal_grants.native_operations.values()
                if entry['operation'].operation_id==submission.operation_id]
            assert len(entries)==1 and entries[0]['native_ref'].startswith(state.binding.native_conversation+':')
            assert state.native_transcript[0]==run
            state.owners.guard_check()
            proof.update(grantedNativeConfigAuthRefused=refused,revokedNativePersonRefused=revoked,
                supportedNativeTextProcessed=True,finalMainAdmission=True,realNativeReader=True,
                sameOwnerAfterRefusal=True,fixtureCapabilityOverrides=True,modelCalls=self.base.calls,
                nativeAdmissionRef=entries[0]['native_ref'],mainReceiptOperation=submission.operation_id,
                scope='Real enrolled production native Host/grants/final main/native transcript with frozen policy. Principal and capability enablement are isolated fixtures; no browser/new-session opt-in or public capability promotion.')
            await self.after_granted(c,state,launch,manager,owner,attached,claim,proof)
        finally:
            writers.PUBLIC_WRITER_QUALIFIED=previous
            capabilities({})
        Path('/evidence/result.json').write_text(json.dumps(proof))


async def prepare(root,work,c):return Case(await prepare_handover(root,work,c))
