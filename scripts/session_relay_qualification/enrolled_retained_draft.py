"""Nonempty old native input remains read-only across same-id replacement."""
import asyncio
import base64
from dataclasses import FrozenInstanceError

from enrolled_frozen_native import Attached
from enrolled_frozen_native import Case as FrozenCase
from enrolled_frozen_native import prepare as prepare_frozen

from acc.broker import ops
from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelaySession
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


class Case(FrozenCase):
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        sid=state.spec.spec['session_id'];old_run=str(launch.run_id)
        native=state.binding.native_conversation
        prefix='Keep this old native draft unsent'
        await manager.input_terminal(attached,sid,{'inputGrant':claim.input_grant,'inputEpoch':claim.input_epoch,
            'frameSeq':3,'data':base64.b64encode(prefix.encode()).decode()})
        # Display evidence only: never an idle/drain/processing shortcut.
        async with asyncio.timeout(10):
            while prefix not in ops.tmux(state.owners.config,'capture-pane','-p','-J','-S','-200','-t','='+owner.spec.spec['tmux_name']+':').stdout:
                await asyncio.sleep(.05)
        assert owner.host.admitted==3 and owner.host.processed==2 and owner.idle
        async def select(target,key):
            with c.db() as db:revision=db.get(RelaySession,sid).revision
            return await manager.select_owner(c.resolve,sid,target=target,revision=revision,request_key=key)
        @write_intent
        def capabilities():
            with c.db() as db:
                db.get(RelaySession,sid).capabilities={'text':'qualified','terminal.input':'qualified'}
                db.commit()
        assert (await select('chat','retained-draft-sdk'))['view']=='chat'
        old_client=owner.host.retained_client
        assert old_client.draft==prefix and old_client.read_only
        assert owner.host.closed and owner.host.boundary.frozen and not owner.host.boundary.verified_empty
        assert owner.host.admitted==3 and owner.host.processed==2
        assert state.owners.retained_clients[old_run]==old_client
        assert launch.terminal_grants.retained_clients[(old_run,attached.attachment_id)]==old_client
        try:old_client.draft='must never mutate'
        except FrozenInstanceError:pass
        else:raise AssertionError('Old retained client became writable')
        sdk=state.owners.current
        assert sdk.client.options.resume==native and not sdk.active and not sdk.pending
        assert self.base.calls==2 and sdk is not owner
        assert (await select('terminal','retained-draft-fresh-tui'))['view']=='terminal'
        fresh=state.owners.current
        assert fresh is not owner and fresh.spec.binding.native_conversation==native
        capabilities()
        resolver=Attached(c.resolve)
        with c.db() as db:revision=db.get(RelaySession,sid).control_revision
        params={'sessionId':sid,'expectedRunId':str(launch.run_id),'attachmentId':resolver.attachment_id,'expectedRevision':revision}
        op=Hub(c.ctx).register(resolver,sid,str(launch.run_id),'retained-draft-new-claim','control.claim',p.operation_digest('control.claim',params))
        current=await manager.claim_terminal(resolver,sid,{**params,'operationId':op.operation_id})
        assert current.input_grant!=claim.input_grant and current.input_epoch>claim.input_epoch
        assert fresh.host.boundary.draft=='' and fresh.host.admitted==fresh.host.processed==0
        assert self.base.calls==2
        try:await owner.host.write_frames(owner.host.grant,4,b'\r')
        except (p.RelayError,ConnectionError):pass
        else:raise AssertionError('Retired input client was revived')
        text='Fresh input must contain only this new text'
        for sequence,data in ((1,text.encode()),(2,b'\r')):
            await manager.input_terminal(resolver,sid,{'inputGrant':current.input_grant,'inputEpoch':current.input_epoch,
                'frameSeq':sequence,'data':base64.b64encode(data).decode()})
            if sequence==1:await asyncio.sleep(.7)
        async with asyncio.timeout(20):
            while fresh.host.processed!=2 or launch.terminal_grants.record.processed!=2:await asyncio.sleep(.05)
        submission=launch.terminal_grants.record.native
        assert submission.params['text']==text and prefix not in submission.params['text']
        with c.db() as db:assert db.get(RelayOperation,submission.operation_id).receipt['state']=='accepted'
        assert self.base.calls==3 and old_client.draft==prefix and old_client.read_only
        assert owner.host.processed==2 and owner.host.admitted==3
        proof.update(nonemptyNativeDraftRetainedReadonly=True,oldDraftDisplayedNatively=True,
            oldWatermarksNeverAdvanced=True,newWriterFreshInput=True,oldClientNeverRevived=True,
            nativeSameIdAcrossRetainedDraft=True,exactFreshNativePromptAccepted=True,
            noReplay=True,modelCalls=self.base.calls,
            scope='Actual enrolled offline API-key SDK/TUI/native text-only unsent draft, immutable retained old client data in main and broker, SDK and fresh TUI resume same ID. Exact fresh native prompt/receipt proves no draft replay. Public retained-client rendering and public L1 mutation composition are separate criteria; no subscription automation.')


async def prepare(root,work,c):
    frozen=await prepare_frozen(root,work,c)
    return Case(frozen.base)
