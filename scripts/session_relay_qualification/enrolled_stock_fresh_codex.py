"""Same-provider fresh Codex review after unknown or durably accepted old work."""
import asyncio
import json
from pathlib import Path

from enrolled_fresh_codex_recovery import recover_codex
from enrolled_stock_owner_loss import terminate_native
from enrolled_stock_race_loss import prepare_mode as prepare_unknown
from enrolled_stock_settled import Case as SettledCase
from enrolled_stock_settled import prepare_mode as prepare_settled

from acc.server.models import RelayOwnership, RelaySession


class AppliedCase(SettledCase):
    async def after_application(self,c,state,launch,manager,host,offer,frame,proof,records,config):
        sid=state.spec.spec['session_id'];rpc=state.owner.rpc
        async with asyncio.timeout(10):
            while True:
                with c.db() as db:active=db.get(RelaySession,sid).active_operation_id
                if active is None:break
                await asyncio.sleep(.02)
        assert proof['receipt']['state']=='accepted' and proof['decision']['status']=='answered'
        if self.mode=='owner_applied':loss=await terminate_native(state,lambda:None)
        else:
            assert self.mode=='transport_applied'
            rpc.writer.close();await asyncio.wait_for(rpc.writer.wait_closed(),10);loss=None
        async with asyncio.timeout(10):
            while not rpc.closed or not launch.peer.closed:await asyncio.sleep(.01)
        await asyncio.wait_for(state.close(),10)
        async with asyncio.timeout(10):
            while True:
                with c.db() as db:
                    sealed=db.get(RelaySession,sid).sealed
                    phase=db.get(RelayOwnership,sid).phase
                if sealed and phase=='recovery_required':break
                await asyncio.sleep(.01)
        before=(proof['receipt'],proof['decision'])
        assert records(proof['originalOperationId'])==before
        assert not state.owner.alive and state.owner.runtime.exit_proven
        assert 'error' in await frame(3,b'\r')
        fresh=await recover_codex(c,config,state.factory,manager,sid,source_old_owner_exited=True)
        assert records(proof['originalOperationId'])==before and host.processed==3
        proof.update(publicFreshSessionRecovery=fresh,durableApplicationBeforeLoss=True,
            freshRecoveryProvider='codex',nativeLossProof=loss,oldOwnerEmptyTreeProven=True,
            scope='Actual accepted stock native application and ACK3, then real pinned vendor loss or real owned stdin close; accepted original receipt/winner retained. Explicit ordinary public fresh Codex Launch/new Chat, old output/receipts/decisions readable and unchanged, no replay; old/new enclosures positively stopped. No supervisor SIGKILL, broker restart, live account or subscription.')
        Path('/evidence/result.json').write_text(json.dumps(proof))


async def prepare_mode(root,work,c,mode):
    if mode=='owner':
        case=await prepare_unknown(root,work,c,'owner');case.fresh_codex=True;return case
    prepared=await prepare_settled(root,work,c,mode)
    case=AppliedCase(prepared.base);case.mode=mode;return case
