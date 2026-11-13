"""Native retained client composed with real cookie-authenticated read projection."""
from enrolled_retained_draft import Case as RetainedCase
from enrolled_retained_draft import prepare as prepare_retained
from public_native_http import Public


class Case(RetainedCase):
    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        await super().after_granted(c,state,launch,manager,owner,attached,claim,proof)
        record=launch.terminal_grants.record;grant=state.owners.current.host.grant
        public=await Public.create(c,manager,state.spec.spec['session_id'],features=[])
        try:
            value=(await public.call('session.snapshot',{}))['result']
            assert value['retainedInputs']==[{'sourceRunId':owner.spec.run_id,'view':'terminal',
                'text':'Keep this old native draft unsent','readOnly':True}],value
            assert value['terminalMediated'] is False
            assert 'draftDigest' not in str(value) and claim.input_grant not in str(value)
            assert launch.terminal_grants.record is record and state.owners.current.host.grant==grant
            public.revoke()
            response=await public.client.get(f"/api/sessions/{state.spec.spec['session_id']}/relay")
            assert response.status_code in (401,403) and 'Keep this old native draft' not in response.text
            assert self.base.calls==3
        finally:await public.close()
        proof.update(nativeRetainedClientPublicCookieRead=True,retainedReadNeverClaimsInput=True,
            revokedCookieCannotReadRetained=True,retainedReadExportsNoGrantOrProof=True,
            scope='Actual enrolled offline API-key native retained old client and fresh same-id input, composed with real cookie/CSRF authenticated public snapshot and revocation. Read-only UI is separately browser-tested. L1 selection in this fixture still uses the internal original ownership manager; public L1 mutation/recovery remains separate.')


async def prepare(root,work,c):
    retained=await prepare_retained(root,work,c)
    return Case(retained.base)
