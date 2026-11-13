"""Native exact-alias withdrawal on main Chat CAS, expiry or login revocation."""
import asyncio
import json
from pathlib import Path

from enrolled_stock_public import Case as PublicCase
from enrolled_stock_public import prepare as prepare_public
from public_native_http import Public

from acc.broker import ops
from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation
from acc.session_relay import protocol as p


class Case(PublicCase):
    async def lifecycle(self,c,state,launch,manager,config,public,host,view,offer,answers,frame):
        sid=state.spec.spec['session_id'];run=str(launch.run_id)
        bound=state.native_socket.approval_aliases.pending[offer.alias]
        chosen=None
        if self.mode=='chat':
            chat=await Public.create(c,manager,sid,features=['decisions'])
            try:
                params={'sessionId':sid,'expectedRunId':run,'decisionId':offer.decision_id,'choiceId':'cancel',
                    'expectedRevision':offer.revision,'actionDigest':offer.action_digest}
                registered=await chat.call('operation.register',{'expectedRunId':run,'registrationKey':'stock-chat-winner',
                    'method':'decision.reply','payloadDigest':p.operation_digest('decision.reply',params)})
                chosen=registered['result']['operationId']
                result=await chat.call('decision.reply',{**params,'operationId':chosen})
                assert 'result' in result,result
            finally:await chat.close()
        elif self.mode=='expiry':
            @write_intent
            def expire():
                with c.db() as db:
                    row=db.get(RelayDecision,(sid,offer.decision_id))
                    row.decision={**row.decision,'expiresAtMs':1};db.commit()
            expire()
        elif self.mode=='revoke':public.revoke()
        else:raise AssertionError(self.mode)
        async with asyncio.timeout(10):
            while host.approval_input.offer is not None or offer.alias in state.native_socket.approval_aliases.pending:
                await asyncio.sleep(.025)
        async with asyncio.timeout(10):
            while 'Press enter to confirm or esc to cancel' in ops.tmux(config,'capture-pane','-p','-J','-t','='+view.name+':',check=True).stdout:
                await asyncio.sleep(.025)
        try:
            await state.native_socket.approval_aliases.reply(bound.peer,host,{'id':offer.alias,'result':{'decision':'accept'}})
        except p.RelayError as error:assert error.code is p.ErrorCode.STALE_AUTHORITY
        else:raise AssertionError('Retired alias accepted late native choice')
        if self.mode!='revoke':assert 'error' in await frame(3,b'\r')
        else:
            response=await public.client.post(f'/api/sessions/{sid}/relay',json={'jsonrpc':'2.0','id':'revoked-key',
                'method':'ry/terminal.input','params':{'sessionId':sid,'attachmentId':public.view['attachmentId'],
                    'inputGrant':host.grant.grant,'inputEpoch':host.grant.epoch,'frameSeq':3,'data':'DQ=='}})
            assert response.status_code==401
        if self.mode!='chat':
            entry=state.adapter.answer_evidence.entries[offer.decision_id]
            await state.owner.request('turn/interrupt',{'threadId':entry['thread'],'turnId':entry['turn']})
        async with asyncio.timeout(15):
            while True:
                with c.db() as db:
                    decision=dict(db.get(RelayDecision,(sid,offer.decision_id)).decision)
                    receipt=dict(db.get(RelayOperation,chosen).receipt) if chosen else None
                expected='denied' if self.mode=='chat' else 'expired' if self.mode=='expiry' else 'cancelled'
                if decision['status']==expected and (receipt is None or receipt['state']=='accepted'):break
                await asyncio.sleep(.025)
        assert host.admitted==host.processed==launch.terminal_grants.record.processed==2
        assert len(answers)==(1 if self.mode=='chat' else 0)
        assert all(value['value']=={'decision':'cancel'} for value in answers)
        Path('/evidence/result.json').write_text(json.dumps({'provider':'codex','mode':self.mode,
            'originalAlias':offer.alias,'aliasWithdrawn':True,'lateOwnedParserResponseRefused':True,
            'nativeModalRemoved':True,'publicCookieCsrf':True,'noApprovalKeyDelivered':True,
            'nativeAdmitted':host.admitted,'nativeProcessed':host.processed,'mainProcessed':launch.terminal_grants.record.processed,
            'providerResponseAttempts':answers,'decision':decision,'receipt':receipt,
            'noReplay':True,'terminalMediated':False,'modelCalls':self.base.calls,
            'scope':'Actual enrolled pinned native UI with real main authority/CAS: Chat winner, expired review or revoked cookie withdraws only the original owned alias. Late direct owned parser response and old public key refuse. No automatic provider answer for expiry/revocation; fixture interrupts exact unchosen turn for cleanup. No post-byte failure recovery, restart, files or Claude Allow claim.'}))


async def prepare_mode(root,work,c,mode):
    prepared=await prepare_public(root,work,c)
    case=Case(prepared.base);case.mode=mode
    return case
