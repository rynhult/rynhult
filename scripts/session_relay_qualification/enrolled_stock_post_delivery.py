"""Post-byte stock Chat/expiry races; pause the actual decoded reply callback."""
import asyncio
import json
from pathlib import Path

from enrolled_stock_public import Case as PublicCase
from enrolled_stock_public import prepare as prepare_public
from public_native_http import Public

from acc.broker import ops
from acc.server.db import write_in_thread, write_intent
from acc.server.models import RelayDecision, RelayOperation
from acc.session_relay import protocol as p


class Case(PublicCase):
    async def lifecycle(self,c,state,launch,manager,config,public,host,view,offer,answers,frame):
        sid=state.spec.spec['session_id'];run=str(launch.run_id)
        bound=state.native_socket.approval_aliases.pending[offer.alias]
        guard_refusals=[];view_checks=[];authority_checks=[]
        if self.mode=='cancel':
            bootstrapped=view.bootstrapped;verify=state.broker.verify;peer_call=state.peer.call
            def observed_view():
                value=bootstrapped();view_checks.append(value);return value
            async def observed_call(method,params):
                value=await peer_call(method,params)
                if method=='authority.verify':
                    fact={'accepted':value is True}
                    if value is not True:
                        try:
                            expected=await write_in_thread(lambda:launch.resolve_grant(params['grant']))
                            incoming=dict(params['operation']);incoming.pop('params')
                            fact['receiptState']=expected.pop('receiptState')
                            expected.pop('staging',None);expected.pop('deliveryWriter',None)
                            fact['differentFields']=sorted(key for key in expected.keys()|incoming.keys()
                                if expected.get(key)!=incoming.get(key))
                        except Exception as error:fact['inspectionError']=type(error).__name__
                    authority_checks.append(fact)
                return value
            async def observed_verify(operation):
                views,checks=len(view_checks),len(authority_checks)
                value=await verify(operation)
                if not value:
                    fact={'method':operation.method,'viewChecks':view_checks[views:],
                        'authorityChecks':authority_checks[checks:],'ready':state.ready,
                        'peerClosed':state.peer.closed,'linkMatches':state.broker.link==(operation.fence,state.link_id)}
                    guard_refusals.append(fact)
                    print('Post-key exact guard refusal:',json.dumps(fact),flush=True)
                return value
            view.bootstrapped=observed_view;state.peer.call=observed_call
            state.broker.verify=state.adapter.authorize=observed_verify
        main_refusals=[]
        main_reply=launch.terminal_grants.approvals.reply
        async def witnessed_reply(*args):
            assert args==(host.grant.grant,3,offer.alias,'accept')
            try:return await main_reply(*args)
            except p.RelayError as error:
                main_refusals.append({'decisionId':offer.decision_id,'frameSeq':3,'choice':'accept','code':error.code.value})
                raise
        launch.terminal_grants.approvals.reply=witnessed_reply
        entered=asyncio.Event();release=asyncio.Event();decoded_errors=[]
        decoded=host.approval_input.decoded
        async def paused(original_offer,choice):
            entered.set();await release.wait()
            try:return await decoded(original_offer,choice)
            except Exception as error:
                expected=(p.ErrorCode.STALE_AUTHORITY,p.ErrorCode.DECISION_RESOLVED,p.ErrorCode.DECISION_EXPIRED)
                if isinstance(error,p.RelayError):
                    assert error.code in expected,error
                    decoded_errors.append(error.code.value)
                else:
                    # RuntimePeer deliberately hides private refusal details.
                    # Require the matching typed main-side refusal; a generic
                    # transport failure alone cannot qualify this test.
                    assert (isinstance(error,ConnectionError) and str(error)=='Runtime authority refused'
                        and not state.peer.closed and main_refusals and main_refusals[-1]['code'] in expected),error
                    decoded_errors.append(main_refusals[-1]['code'])
                raise
        host.approval_input.decoded=paused
        delivered=await frame(3,b'\r');assert 'result' in delivered,delivered
        await asyncio.wait_for(entered.wait(),10)
        submission=launch.terminal_grants.approvals.submission
        assert host.admitted==3 and host.processed==2 and not answers
        chosen=None;cancelled_key=None
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
        elif self.mode=='cancel':
            reviewer=await Public.create(c,manager,sid,features=['queue'])
            try:
                current=await reviewer.call('operation.get',{'operationId':submission.operation_id})
                cancelled=await reviewer.call('operation.cancel',{'operationId':submission.operation_id,
                    'expectedRevision':current['result']['revision']})
                assert cancelled['result']['state']=='failed' and cancelled['result']['reason']=='cancelled',cancelled
                cancelled_key=cancelled['result']
                assert not answers
            finally:await reviewer.close()
        elif self.mode=='revoke':public.revoke()
        else:raise AssertionError(self.mode)
        release.set()
        async with asyncio.timeout(10):
            while not decoded_errors:await asyncio.sleep(.01)
        assert host.input_fenced
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
        if self.mode=='cancel':
            # Product sends nothing automatically: a fresh explicit public
            # review operation chooses Cancel only after the stale key lost.
            assert not answers
            reviewer=await Public.create(c,manager,sid,features=['decisions'])
            try:
                snapshot=(await reviewer.call('session.snapshot',{}))['result']
                card=next(v for v in snapshot['pendingDecisions'] if v['decisionId']==offer.decision_id)
                params={'sessionId':sid,'expectedRunId':run,'decisionId':offer.decision_id,'choiceId':'cancel',
                    'expectedRevision':card['revision'],'actionDigest':card['actionDigest']}
                registered=await reviewer.call('operation.register',{'expectedRunId':run,
                    'registrationKey':'fresh-post-key-review','method':'decision.reply',
                    'payloadDigest':p.operation_digest('decision.reply',params)})
                chosen=registered['result']['operationId']
                result=await reviewer.call('decision.reply',{**params,'operationId':chosen})
                assert 'result' in result,result
            finally:await reviewer.close()
        if self.mode!='revoke':assert 'error' in await frame(3,b'\r')
        else:
            response=await public.client.post(f'/api/sessions/{sid}/relay',json={'jsonrpc':'2.0','id':'revoked-key',
                'method':'ry/terminal.input','params':{'sessionId':sid,'attachmentId':public.view['attachmentId'],
                    'inputGrant':host.grant.grant,'inputEpoch':host.grant.epoch,'frameSeq':3,'data':'DQ=='}})
            assert response.status_code==401
        if self.mode not in ('chat','cancel'):
            entry=state.adapter.answer_evidence.entries[offer.decision_id]
            await state.owner.request('turn/interrupt',{'threadId':entry['thread'],'turnId':entry['turn']})
        try:
            async with asyncio.timeout(15):
                while True:
                    with c.db() as db:
                        decision=dict(db.get(RelayDecision,(sid,offer.decision_id)).decision)
                        receipt=dict(db.get(RelayOperation,chosen).receipt) if chosen else None
                    expected='denied' if self.mode in ('chat','cancel') else 'expired' if self.mode=='expiry' else 'cancelled'
                    if decision['status']==expected and (receipt is None or receipt['state']=='accepted'):break
                    await asyncio.sleep(.025)
        except TimeoutError:
            native_decision = state.adapter.decisions.get(offer.decision_id, {})
            evidence = state.adapter.answer_evidence.entries.get(offer.decision_id, {})
            print('Post-key lifecycle timeout:', json.dumps({'mode': self.mode, 'decisionStatus': decision['status'],
                'receiptState': receipt['state'] if receipt else None, 'answerAttempts': len(answers),
                'nativeProcessed': host.processed, 'mainProcessed': launch.terminal_grants.record.processed,
                'decodedRefusals': decoded_errors, 'mainRefusals': [v['code'] for v in main_refusals],
                'nativeDecisionSent': native_decision.get('sent'), 'evidenceResolved': evidence.get('resolved'),
                'evidenceFinished': evidence.get('finished'), 'evidenceSent': evidence.get('sent'),
                'viewBootstrapped': view.bootstrapped(), 'viewClosed': view.closed,
                'receiptReason': receipt.get('reason') if receipt else None,'guardRefusals':guard_refusals}), flush=True)
            raise
        assert host.admitted==3 and host.processed==launch.terminal_grants.record.processed==2
        assert len(answers)==(1 if self.mode in ('chat','cancel') else 0)
        assert all(value['value']=={'decision':'cancel'} for value in answers)
        Path('/evidence/result.json').write_text(json.dumps({'provider':'codex','mode':self.mode,
            'originalAlias':offer.alias,'aliasWithdrawn':True,'lateOwnedParserResponseRefused':True,
            'nativeModalRemoved':True,'publicCookieCsrf':True,'noApprovalKeyDelivered':False,'realKeyDecodedBeforeLifecycleChange':True,'keyOperation':submission.operation_id,'decodedReplyRefused':True,'decodedRefusalCodes':decoded_errors,'mainTypedRefusals':main_refusals,'inputRemainsFenced':True,
            'nativeAdmitted':host.admitted,'nativeProcessed':host.processed,'mainProcessed':launch.terminal_grants.record.processed,
            'providerResponseAttempts':answers,'decision':decision,'receipt':receipt,'guardRefusals':guard_refusals,
            'cancelledKeyReceipt':cancelled_key,'explicitFreshReviewAfterCancellation':self.mode=='cancel',
            'noReplay':True,'terminalMediated':False,'modelCalls':self.base.calls,
            'scope':'Post-delivery: genuine native Enter decoded before lifecycle change; callback paused. Actual enrolled pinned native UI with real main authority/CAS: Chat winner, expired review or revoked cookie withdraws only the original owned alias. Late direct owned parser response and old public key refuse. No automatic provider answer for expiry/revocation; fixture interrupts exact unchosen turn for cleanup. Fresh explicit Chat Cancel after cancelling a delivered/unreserved key intent is covered only in cancel mode; owner/transport failure plus new-session recovery remains outside this profile; no restart, files or Claude Allow claim.'}))


async def prepare_mode(root,work,c,mode):
    prepared=await prepare_public(root,work,c)
    case=Case(prepared.base);case.mode=mode
    return case
