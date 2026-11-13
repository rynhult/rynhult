"""Actual cookie HTTP + main grant + owned native codec unsupported matrix."""
import asyncio
import base64
from types import SimpleNamespace

from enrolled_frozen_native import Case as FrozenCase
from enrolled_frozen_native import prepare as prepare_frozen
from public_native_http import Public

from acc.server.models import RelayOperation, RelaySession
from acc.server.session_relay import store
from acc.session_relay import protocol as p


class Case(FrozenCase):
    async def check_revocation(self,c,owner,grant):
        return False  # actual cookie deletion is tested after the supported HTTP turn

    async def claim(self,c,sid,run,launch,manager):
        self.public=await Public.create(c,manager,sid)
        with c.db() as db:revision=db.get(RelaySession,sid).control_revision
        fields={'sessionId':sid,'expectedRunId':run,'attachmentId':self.public.view['attachmentId'],'expectedRevision':revision}
        registration=await self.public.call('operation.register',{'expectedRunId':run,'registrationKey':'public-native-claim',
            'method':'control.claim','payloadDigest':p.operation_digest('control.claim',fields)})
        assert 'error' not in registration,registration
        value=await self.public.call('control.claim',{**fields,'operationId':registration['result']['operationId']})
        assert 'error' not in value,value
        value=value['result']
        return launch.terminal_grants.record.resolve,SimpleNamespace(receipt=store.receipt_from(value['receipt']),
            input_grant=value['inputGrant'],input_epoch=value['inputEpoch'])

    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        sid,run=state.spec.spec['session_id'],str(launch.run_id)
        before=self.base.calls
        def frame(data,sequence=3):return {'attachmentId':self.public.view['attachmentId'],
            'inputGrant':claim.input_grant,'inputEpoch':claim.input_epoch,
            'frameSeq':sequence,'data':base64.b64encode(data).decode()}
        bad_csrf=await self.public.client.post(f'/api/sessions/{sid}/relay',headers={'x-acc-csrf':'wrong'},json={
            'jsonrpc':'2.0','id':'csrf','method':'ry/terminal.input','params':{'sessionId':sid,**frame(b'forbidden')}})
        assert bad_csrf.status_code==403 and owner.host.admitted==owner.host.processed==2
        refused=[]
        controls=[b'/model other\r',b'/config\r',b'/permissions\r',b'/login\r',b'/logout\r',b'!shell\r',
            b'\x03',b'\x1b[200~paste\x1b[201~',b'\xc3\xa9',b'a\rb']
        for data in controls:
            result=await self.public.call('terminal.input',frame(data))
            assert result.get('error',{}).get('data',{}).get('code')=='UNSUPPORTED',result
            stream=launch.terminal_grants.record
            assert stream.phase=='active' and not stream.pending and stream.sequence==stream.processed==2
            assert owner.host.admitted==owner.host.processed==2 and not launch.peer.closed
            refused.append(data.hex())
        # Public schema/feature checks must refuse extra model/effort/rich and
        # unsupported operations before reserving a native frame or operation.
        for method,fields in [('terminal.input',{**frame(b'valid'),'model':'other'}),
            ('terminal.input',{**frame(b'valid'),'effort':'high'}),
            ('terminal.input',{**frame(b'valid'),'attachments':['unsupported']}),
            ('turn.steer',{'expectedRunId':run,'operationId':'not-registered','turnId':'native-turn','text':'steer'}),
            ('configuration.request',{'expectedRunId':run,'operationId':'not-registered','expectedRevision':0,'model':'other'}),
            ('turn.submit',{'expectedRunId':run,'operationId':'not-registered','text':'rich',
                'expectedConfigurationRevision':0,'expectedControlRevision':claim.input_epoch,'stagedIds':['unsupported']}),
            ('control.take',{'expectedRunId':run,'operationId':'not-registered','attachmentId':attached.attachment_id,
                'expectedRevision':claim.input_epoch,'reason':'Unsupported L1 take'}),
            ('writer.take',{'expectedRunId':run,'operationId':'not-registered','attachmentId':attached.attachment_id,
                'expectedRevision':claim.input_epoch,'reason':'Unsupported L1 writer','view':'terminal'})]:
            result=await self.public.call(method,fields)
            assert 'error' in result,result
            assert owner.host.admitted==owner.host.processed==2
        assert self.base.calls==before
        text='Public native text after unsupported controls'
        for seq,data in ((3,text.encode()),(4,b'\r')):
            result=await self.public.call('terminal.input',frame(data,seq))
            assert 'error' not in result,result
            if seq==3:await asyncio.sleep(.7)
        async with asyncio.timeout(20):
            while owner.host.processed!=4 or launch.terminal_grants.record.processed!=4:await asyncio.sleep(.05)
        submission=launch.terminal_grants.record.native
        with c.db() as db:
            receipt=db.get(RelayOperation,submission.operation_id).receipt
            assert receipt['state']=='accepted' and receipt['actor']==p.to_wire(owner.host.grant.actor)
        self.public.revoke()
        response=await self.public.client.post(f'/api/sessions/{sid}/relay',json={
            'jsonrpc':'2.0','id':'revoked','method':'ry/terminal.input','params':{'sessionId':sid,**frame(b'revoked',5)}})
        assert response.status_code==401
        assert owner.host.admitted==owner.host.processed==4
        proof.update(realCookieCsrfAuthentication=True,publicGrantClaim=True,publicNativeControlRefusals=refused,
            publicUnsupportedSchemaRefusals=8,publicNativeTextProcessed=True,revokedCookieRefused=True,revokedNativePersonRefused=True,
            csrfRefusedBeforeInput=True,
            noSideEffectsBeforeSupportedText=True,modelCalls=self.base.calls,
            scope='Real production HTTP router/cookie/CSRF/main grants and owned pinned Claude/native receipt in isolated ASGI requests. No browser/new-session opt-in or Codex public matrix claim; explicit fixture capability enablement only.')
        await self.public.close()


async def prepare(root,work,c):
    frozen=await prepare_frozen(root,work,c)
    return Case(frozen.base)
