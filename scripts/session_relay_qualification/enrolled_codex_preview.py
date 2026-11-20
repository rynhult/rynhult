"""Pinned Codex owner preview: real grant, cookie/CSRF, same owner and round trip.

Enrollment is the existing isolated-node reservation; its pin is obtained through
actual relay_probe. No capability, declaration, retirement or release-flag override.
"""
import asyncio
import base64
import json
from pathlib import Path

from installed_security import prepare as prepare_security
from public_native_http import Public

from acc.server.models import RelayOperation, RelayRun, RelaySession
from acc.session_relay import protocol as p


class Case:
    def __init__(self,base):self.base=base
    def __getattr__(self,key):return getattr(self.base,key)
    async def exercise(self,c,state,launch,manager,config):
        from acc.server.session_relay import (
            delivery,
            exclusive_recovery,
            native_decisions,
            stock_approval_frames,
            writers,
        )
        from acc.session_relay import native_approval_aliases, stock_approval_input
        assert writers.CODEX_PUBLIC_WRITER_QUALIFIED and writers.PUBLIC_WRITER_QUALIFIED
        assert not any([stock_approval_frames.PUBLIC_STOCK_KEYS_QUALIFIED,stock_approval_input.STOCK_APPROVAL_INPUT_QUALIFIED,
            native_approval_aliases.NATIVE_APPROVAL_DECODER_QUALIFIED,native_decisions.CODEX_ENROLLED_DECISIONS_QUALIFIED,
            delivery.DELIVERY_QUALIFIED,exclusive_recovery.RECOVERY_QUALIFIED])
        sid=state.spec.spec['session_id'];native=state.binding.native_conversation
        identity=state.owner.attestation.process_identity
        assert self.base.calls==0
        assert {key for key in p.CAPABILITIES if state.adapter.record.support(key) is p.Support.QUALIFIED}=={'text','terminal.input'}
        with c.db() as db:
            row=db.get(RelaySession,sid);run=db.get(RelayRun,row.current_run_id)
            assert row.capabilities=={'text':'qualified','terminal.input':'qualified'}
            assert run.identity['cliDigest']==self.probed['cliDigest']
        public=await Public.create(c,manager,sid,features=['writer','terminal','queue'])
        c.ctx.relay_runtime=manager
        readers=[];receipts=[]
        async def operation(method,key,params):
            params={'sessionId':sid,**params}
            registered=await public.call('operation.register',{'expectedRunId':str(launch.run_id),
                'registrationKey':key,'method':method,'payloadDigest':p.operation_digest(method,params)})
            assert 'result' in registered,registered
            reply=await public.call(method,{**params,'operationId':registered['result']['operationId']})
            assert 'result' in reply,reply
            return reply['result']
        def revision():
            with c.db() as db:return db.get(RelaySession,sid).control_revision
        async def take(view,key):
            receipt=await operation('writer.take',key,{'expectedRunId':str(launch.run_id),
                'attachmentId':public.view['attachmentId'],'expectedRevision':revision(),'view':view,'reason':'Explicit offline owner preview take'})
            assert receipt['state']=='accepted';receipts.append(receipt)
            assert state.owner.attestation.process_identity==identity and state.binding.native_conversation==native
            return receipt
        async def idle(calls):
            async with asyncio.timeout(25):
                while True:
                    with c.db() as db:active=db.get(RelaySession,sid).active_operation_id
                    if self.base.calls==calls and active is None and state.adapter.active_turn is None:break
                    await asyncio.sleep(.025)
        async def chat(text,key,calls):
            result=await operation('turn.submit',key,{'expectedRunId':str(launch.run_id),'text':text,
                'expectedConfigurationRevision':0,'expectedControlRevision':revision(),'ifBusy':'reject'})
            assert result['state']=='accepted',result
            receipts.append(result);await idle(calls)
        async def frame(sequence,data,grant):
            return await public.call('terminal.input',{'attachmentId':public.view['attachmentId'],
                'inputGrant':grant['inputGrant'],'inputEpoch':grant['inputEpoch'],'frameSeq':sequence,
                'data':base64.b64encode(data).decode()})
        try:
            await take('chat','initial-chat-take')
            assert self.base.calls==0,'Taking a writer must not type or query'
            await chat('First explicit offline Chat message','chat-first',1)
            await take('terminal','take-terminal')
            assert self.base.calls==1,'Terminal take-over must not replay Chat'
            grant=await operation('control.claim','claim-terminal',{'expectedRunId':str(launch.run_id),
                'attachmentId':public.view['attachmentId'],'expectedRevision':revision()})
            assert grant['receipt']['state']=='accepted' and grant['inputGrant']
            host=state.terminal_host;old_view=state.owned_view
            async def read():
                async for _ in host.attach_read(host.run):pass
            reader=asyncio.create_task(read());readers.append(reader)
            assert 'result' in await frame(1,b'Explicit offline Terminal message',grant)
            assert 'result' in await frame(2,b'\r',grant)
            await idle(2)
            async with asyncio.timeout(10):
                while host.processed!=2 or launch.terminal_grants.record.processed!=2:await asyncio.sleep(.025)
            assert host.draft=='' and host.ticket is None
            draft='Retain this unsent Codex terminal draft'
            assert 'result' in await frame(3,draft.encode(),grant)
            assert host.draft==draft and self.base.calls==2
            old_record=launch.terminal_grants.record
            await take('chat','return-to-chat')
            fresh=state.terminal_host
            assert host.retired and host.draft==draft and fresh is not host
            assert fresh.draft=='' and fresh.grant is None and fresh.ticket is None
            assert state.owned_view.name!=old_view.name and state.owned_view.peer!=old_view.peer
            assert not old_view.closed and old_record.phase=='revoked'
            assert self.base.calls==2,'Return to Chat must neither type nor replay the old draft'
            refused=await frame(4,b'Never replay this retired input',grant)
            assert 'error' in refused and host.draft==draft and self.base.calls==2
            await chat('Second separate explicit Chat message','chat-final',3)
            assert host.draft==draft and host.retired and self.base.calls==3
            snapshot=(await public.call('session.snapshot',{}))['result']
            assert snapshot['terminalMediated'] is False
            with c.db() as db:
                for receipt in receipts:assert db.get(RelayOperation,receipt['operationId']).receipt==receipt
                assert db.get(RelaySession,sid).capabilities=={'text':'qualified','terminal.input':'qualified'}
            Path('/evidence/result.json').write_text(json.dumps({'provider':'codex','probedPin':self.probed,
                'productionGrant':True,'noQualificationOverrides':True,'publicCookieCsrf':True,
                'chatTerminalChat':True,'sameNativeId':native,'sameExecutionIdentity':identity,
                'oneExecutionOwner':len(state.factory.active)==1,'modelCalls':self.base.calls,
                'terminalProcessed':host.processed,'retainedDraft':host.draft,'oldClientReadOnly':host.retired,
                'freshInputEmpty':fresh.draft=='' and fresh.grant is None,'retiredInputRefused':refused['error']['data']['code'],
                'receiptsUnchanged':True,'noAutomaticTypingOrReplay':True,'excludedFlagsOff':True,
                'terminalMediated':False,'receipts':receipts,
                'scope':'Actual pinned offline Codex, enrolled node/main/broker and owned native views. Real broker probe, production owner grant/declaration, cookie/CSRF writer.take, control.claim, input and three explicit messages. Same native conversation/process, old unsent terminal draft read-only, new empty input, no replay. Enrollment reservation and generated model/account are fixture setup; no browser, live account, approvals, delivery or recovery qualification.'}))
        finally:
            for reader in readers:reader.cancel()
            await asyncio.gather(*readers,return_exceptions=True)
            await public.close()


async def prepare(root,work,c):
    base=await prepare_security(root,work,c)
    original=base.codex_events
    def text_only(payload):
        count=base.calls;base.calls=count+1
        try:return original(payload)
        finally:base.calls=count
    base.codex_events=text_only
    return Case(base)
