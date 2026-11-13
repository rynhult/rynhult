"""Actual native source exit and failed replacement; no automatic relaunch."""
import copy
import os

from enrolled_frozen_native import Case as FrozenCase
from enrolled_frozen_native import prepare as prepare_frozen

from acc.broker import claude_owner, ops
from acc.server.models import RelayOwnership, RelaySession
from acc.session_relay import protocol as p


class Case(FrozenCase):
    after_fork=False
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        sid=state.spec.spec['session_id']
        async def select(target,key):
            with c.db() as db:revision=db.get(RelaySession,sid).revision
            return await manager.select_owner(c.resolve,sid,target=target,revision=revision,request_key=key)
        assert (await select('chat','failed-target-source-sdk'))['view']=='chat'
        source=state.owners.current
        identity=source.attestation.process_identity
        witness=copy.copy(state.owners.enclosure);witness.fd=os.dup(witness.fd)
        original_launch,original_observer=ops.launch,claude_owner.OwnedObserver
        calls=[]
        def refused(*args,**kwargs):
            calls.append('postfork' if self.after_fork else 'prefork')
            raise ops.OpError('Artificial failure after native start' if self.after_fork else 'Artificial refusal before launch')
        if self.after_fork:claude_owner.OwnedObserver=refused
        else:ops.launch=refused
        try:
            try:await select('terminal','failed-target-native')
            except (p.RelayError,ConnectionError):pass
            else:raise AssertionError('Failed target acquired ownership')
        finally:
            ops.launch,claude_owner.OwnedObserver=original_launch,original_observer
        assert calls==['postfork' if self.after_fork else 'prefork']
        try:assert source.exit_verified and witness.proof('exit.json',identity)
        finally:witness.close()
        assert state.owners.current is None and state.controller.closed and state.controller.fenced
        assert state.controller.switch.recovery_required and state.controller.switch.owner is None
        with c.db() as db:
            ownership=db.get(RelayOwnership,sid)
            assert ownership.phase=='recovery_required',ownership.phase
        attempt=state.owners.launch_attempt
        if self.after_fork:
            assert attempt is not None and attempt.started and attempt.process is not None
            target=copy.copy(state.owners.enclosure);target.fd=os.dup(target.fd)
            target_identity=f'{attempt.process.pid}:{attempt.process.started}'
            assert target.proof('started.json',target_identity)
        else:
            assert attempt is None and await state.owners.exited()
            assert not (state.owners.enclosure.path/'started.json').exists()
        try:
            await state.controller.close()
            assert state.owners.close_complete and await state.owners.exited()
            if self.after_fork:assert target.proof('exit.json',target_identity)
        finally:
            if self.after_fork:target.close()
        try:await state.controller.select('must-not-retry')
        except p.RelayError:pass
        else:raise AssertionError('Failed target was retried automatically')
        assert self.base.calls==2
        proof.update(failedTargetSourceWholeTreeExited=True,failedTargetNeverOwned=True,
            failedTargetMainRecoveryRequired=True,failedTargetFactoryReclaimed=True,
            failedTargetOldOwnerNotRevived=True,failedTargetNoReplay=True,modelCalls=self.base.calls,
            failurePhase='after-authenticated-native-start' if self.after_fork else 'before-fork',
            failedTargetOwnEmptyTreeProof=self.after_fork,
            scope='Actual enrolled offline API-key SDK/TUI source transition and failed replacement. Pre-fork refusal or failure after authenticated native start; original source and known partial target require separate whole-tree receipts. Main recovery remains fenced; no blind retry. Arbitrary fork outcome without a pinned target remains unknown, not clean.')


async def prepare(root,work,c):
    frozen=await prepare_frozen(root,work,c)
    return Case(frozen.base)
