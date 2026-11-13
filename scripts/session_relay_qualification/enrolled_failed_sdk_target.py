"""Actual SDK replacement loses authority after connect; its own tree is reaped."""
import copy
import os

from enrolled_frozen_native import Case as FrozenCase
from enrolled_frozen_native import prepare as prepare_frozen

from acc.server.models import RelayOwnership, RelaySession
from acc.session_relay import protocol as p
from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter


class Case(FrozenCase):
    connect_failure=False
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        factory=state.owners;sid=state.spec.spec['session_id']
        original=factory.authorize;refusals=[]
        from claude_agent_sdk import ClaudeSDKClient
        original_connect=ClaudeSDKClient.connect
        async def failed_connect(client,*args,**kwargs):
            await original_connect(client,*args,**kwargs)
            refusals.append('native-connect-error')
            raise ConnectionError('Artificial connect failure after native fork')
        source=copy.copy(factory.enclosure);source.fd=os.dup(source.fd)
        source_identity=f'{owner.process.pid}:{owner.process.started}'
        async def revoke_after_connect(*args,**kwargs):
            target=factory.current
            if isinstance(target,ClaudeAgentAdapter) and target.client is not None:
                assert target.process is not None and ':' in target.attestation.process_identity
                refusals.append(target.attestation.process_identity)
                return False
            return await original(*args,**kwargs)
        if self.connect_failure:ClaudeSDKClient.connect=failed_connect
        else:factory.authorize=revoke_after_connect
        with c.db() as db:revision=db.get(RelaySession,sid).revision
        try:
            try:await manager.select_owner(c.resolve,sid,target='chat',revision=revision,request_key='failed-sdk-connected')
            except (p.RelayError,ConnectionError):pass
            else:raise AssertionError('Revoked SDK target acquired ownership')
        finally:
            factory.authorize=original
            ClaudeSDKClient.connect=original_connect
        try:assert source.proof('exit.json',source_identity)
        finally:source.close()
        target=factory.current
        assert isinstance(target,ClaudeAgentAdapter) and refusals and factory.exit_qualified
        assert target.spec.binding.native_conversation==state.binding.native_conversation
        witness=copy.copy(factory.enclosure);witness.fd=os.dup(witness.fd)
        identity=target.attestation.process_identity
        assert witness.proof('started.json',identity)
        assert state.controller.closed and state.controller.fenced and state.controller.switch.recovery_required
        assert state.controller.switch.owner is None
        with c.db() as db:assert db.get(RelayOwnership,sid).phase=='recovery_required'
        try:
            await state.controller.close()
            assert target.exit_verified and target.process.returncode is not None
            assert factory.close_complete and await factory.exited() and witness.proof('exit.json',identity)
        finally:witness.close()
        try:await state.controller.select('no-automatic-retry')
        except p.RelayError:pass
        else:raise AssertionError('Failed SDK replacement was retried')
        assert self.base.calls==2
        proof.update(nativeSDKPostConnectAuthorityLoss=not self.connect_failure,failedSDKIdentityPinnedBeforeRefusal=True,
            sourceOwnWholeTreeExited=True,failedSDKOwnWholeTreeExited=True,failedSDKNeverOwned=True,
            failedSDKMainRecoveryRequired=True,failedSDKNoReplay=True,modelCalls=self.base.calls,
            nativeSDKConnectError=self.connect_failure,
            scope='Actual enrolled offline API-key native TUI to SDK replacement; synthetic authority refusal after real SDK connect, pinned target identity and independent authenticated source/target ECHILD receipts. Original main recovery_required; no new model call or automatic retry. Explicit public recovery remains separate.')


async def prepare(root,work,c):
    frozen=await prepare_frozen(root,work,c)
    return Case(frozen.base)
