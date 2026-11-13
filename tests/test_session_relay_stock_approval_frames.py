"""Public HTTP frames and real main CAS; physical decoder/provider are explicit fakes."""
import asyncio
import base64
import time
from dataclasses import replace

import httpx
import pytest
from fastapi import HTTPException
from test_session_relay_public_writer import SID, attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_terminal_grants import claim, frame, stream_case  # noqa: F401

from acc.server.db import write_in_thread, write_intent
from acc.server.models import RelayDecision, RelayOperation, RelaySession
from acc.session_relay import protocol as p


@pytest.fixture
async def approval_case(stream_case, monkeypatch):  # noqa: F811
    c=stream_case
    c.key_grant=(await claim(c))['result']
    monkeypatch.setattr('acc.server.session_relay.stock_approval_frames.PUBLIC_STOCK_KEYS_QUALIFIED',True)
    @write_intent
    def qualify():
        with c.db() as db:
            row=db.get(RelaySession,SID)
            row.capabilities={**row.capabilities,'decision.tool_approval':'qualified'}
            db.commit()
    qualify()
    state=c.broker_state
    state.adapter.record=replace(state.adapter.record,qualification_id='synthetic-key-frames',
        qualification_date='2026-11-12',operations={**state.adapter.record.operations,
            'decision.tool_approval':p.CapabilityEntry(p.Support.QUALIFIED,evidence=('fake native only',))})
    state.adapter.active_turn='fixture-operation';state.adapter.turns['fixture-operation']='fixture-turn'
    c.digest=p.sha256_digest(b'artificial reviewed key question')
    state.adapter.register_decision('key-question',native_request={'id':7,'method':'item/commandExecution/requestApproval',
        'params':{'threadId':state.binding.native_conversation,'turnId':'fixture-turn','itemId':'fixture-item',
                  'availableDecisions':['accept','cancel']}},action_digest=c.digest,revision=0,
        expires_ms=int(time.time()*1000)+60_000)
    c.launch.publication({'kind':'decision','decisionId':'key-question','actionDigest':c.digest,
        'expiresAtMs':int(time.time()*1000)+60_000,'choices':['accept','cancel'],'description':'Artificial command review'})
    c.replies=[]
    async def respond(identifier, value, *, before_write=None):
        if before_write is not None:
            await before_write()
        c.replies.append((identifier, value))
    state.owner.respond=respond
    c.offer={'alias':'opaque-owned-alias','decisionId':'key-question','actionDigest':c.digest,'revision':0,'choices':['accept','cancel']}
    c.approvals=c.launch.terminal_grants.approvals
    assert await write_in_thread(lambda:c.approvals.arm(c.key_grant['inputGrant'],c.offer))
    return c


def key(c,data=b'\r',**changes):
    return {**frame(c,c.key_grant),'data':base64.b64encode(data).decode(),**changes}


@pytest.mark.parametrize('data,choice',[(b'\r','accept'),(b'\x1b','cancel')])
async def test_public_frame_registers_original_actor_before_bytes_then_decoded_reply_uses_main_cas(approval_case,data,choice):
    c=approval_case;original=c.host.write_frames
    async def inspect(grant,sequence,data):
        submission=c.approvals.submission
        with c.db() as db:
            op=db.get(RelayOperation,submission.operation_id)
            assert op.receipt['state']=='queued' and op.receipt['actor']==p.to_wire(grant.actor)
            assert op.receipt['payloadDigest']==p.operation_digest('decision.reply',c.approvals.params(submission))
        proof=await c.broker_state.peer.call('native.approval_frame',{'grant':grant.grant,'sequence':sequence,
            'alias':c.offer['alias'],'choice':choice})
        assert proof['operationId']==submission.operation_id
        return await original(grant,sequence,data)
    c.host.write_frames=inspect
    reply=await call(c.client,'terminal.input',key(c,data))
    assert 'result' in reply,reply
    ticket=c.approvals.submission
    assert ticket.frame.grant==c.launch.terminal_grants.record.value and ticket.frame.choice==choice
    result=await c.broker_state.peer.call('native.approval_reply',{'grant':ticket.frame.grant.grant,
        'sequence':1,'alias':c.offer['alias'],'choice':choice})
    assert result['state']=='uncertain'  # fake provider response write is not application
    with c.db() as db:
        decision=db.get(RelayDecision,(SID,'key-question')).decision
        assert decision['winner']==p.to_wire(ticket.frame.grant.actor) and decision['choiceId']==choice
        assert db.get(RelayOperation,ticket.operation_id).receipt['state']=='uncertain'
    assert c.replies==[(7,{'decision':choice})]
    with pytest.raises((p.RelayError,RuntimeError)):
        await c.approvals.reply(ticket.frame.grant.grant,1,c.offer['alias'],choice)
    assert len(c.replies)==1


@pytest.mark.parametrize('data',[b'y',b'Y',b'1',b'\n',b'\r\r',b'\x1b[A',b'\x1b[13u',b'hello',b'\x03'])
async def test_unsupported_keys_never_register_or_reach_transport(approval_case,data):
    c=approval_case
    reply=await call(c.client,'terminal.input',key(c,data))
    assert 'error' in reply and not c.host.writes and c.approvals.submission is None
    assert reply['error']['data']['details']=={'reason':'unsupported_terminal_approval_key'}
    assert 'This key was not sent' in reply['error']['message']
    assert not c.replies


async def test_other_attachment_and_forged_decoded_context_have_no_actor_fallback(approval_case):
    c=approval_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app),base_url='http://fixture') as other:
        view=await attach(other,'other-key-tab',['writer','terminal'])
        assert 'error' in await call(other,'terminal.input',key(c,attachmentId=view['attachmentId']))
    for args in [('wrong',1,c.offer['alias'],'accept'),(c.key_grant['inputGrant'],1,'wrong','accept')]:
        with pytest.raises(p.RelayError):await write_in_thread(lambda:c.approvals.authorize(*args))
    assert not c.host.writes and not c.replies


async def test_revoke_after_frame_before_native_reply_cannot_release_allow(approval_case):
    c=approval_case
    assert 'result' in await call(c.client,'terminal.input',key(c))
    c.state['revoked']=True
    with pytest.raises(HTTPException):await c.approvals.reply(c.key_grant['inputGrant'],1,c.offer['alias'],'accept')
    assert not c.replies


async def test_chat_winner_after_frame_makes_native_ticket_lose_without_second_provider_answer(approval_case):
    c=approval_case
    assert 'result' in await call(c.client,'terminal.input',key(c))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app),base_url='http://fixture') as chat:
        await attach(chat,'chat-reviewer',['decisions'])
        params={**c.approvals.params(c.approvals.submission),'choiceId':'cancel'}
        registration=await call(chat,'operation.register',{'expectedRunId':str(c.launch.run_id),'registrationKey':'chat-wins',
            'method':'decision.reply','payloadDigest':p.operation_digest('decision.reply',params)})
        reply=await call(chat,'decision.reply',{**params,'operationId':registration['result']['operationId']})
        assert reply['result']['state']=='uncertain'
    with pytest.raises(p.RelayError):await c.approvals.reply(c.key_grant['inputGrant'],1,c.offer['alias'],'accept')
    assert c.replies==[(7,{'decision':'cancel'})]


@pytest.mark.parametrize('fault',['default','capability','expired','changed_revision','draft'])
async def test_no_arm_or_frame_when_review_or_stream_is_not_current(approval_case,monkeypatch,fault):
    c=approval_case
    if fault=='default':monkeypatch.setattr('acc.server.session_relay.stock_approval_frames.PUBLIC_STOCK_KEYS_QUALIFIED',False)
    elif fault=='draft':c.launch.terminal_grants.record.private_draft='unsent'
    else:
        @write_intent
        def change():
            with c.db() as db:
                if fault=='capability':
                    row=db.get(RelaySession,SID);row.capabilities={**row.capabilities,'decision.tool_approval':'unsupported'}
                else:
                    row=db.get(RelayDecision,(SID,'key-question'))
                    row.decision={**row.decision,**({'expiresAtMs':1} if fault=='expired' else {'revision':9})}
                db.commit()
        change()
    assert 'error' in await call(c.client,'terminal.input',key(c))
    assert not c.host.writes and not c.replies and c.approvals.submission is None


async def test_duplicate_decoded_reply_during_main_staging_never_dispatches_twice(approval_case):
    c=approval_case
    assert 'result' in await call(c.client,'terminal.input',key(c))
    entered,release=asyncio.Event(),asyncio.Event();original=c.launch.peer.call
    async def paused(method,params):
        if method=='stage':entered.set();await release.wait()
        return await original(method,params)
    c.launch.peer.call=paused
    task=asyncio.create_task(c.approvals.reply(c.key_grant['inputGrant'],1,c.offer['alias'],'accept'))
    await asyncio.wait_for(entered.wait(),3)
    try:
        with pytest.raises(p.RelayError):await c.approvals.reply(c.key_grant['inputGrant'],1,c.offer['alias'],'accept')
    finally:release.set()
    assert (await asyncio.wait_for(task,5))['state']=='uncertain'
    assert c.replies==[(7,{'decision':'accept'})]


async def test_processing_requires_exact_accepted_original_operation_not_transport_or_unknown(approval_case):
    c=approval_case
    assert 'result' in await call(c.client,'terminal.input',key(c))
    ticket=c.approvals.submission
    args=(c.key_grant['inputGrant'],1,ticket.operation_id)
    with pytest.raises(p.RelayError):await write_in_thread(lambda:c.approvals.processed(*args))
    assert (await c.approvals.reply(c.key_grant['inputGrant'],1,c.offer['alias'],'accept'))['state']=='uncertain'
    with pytest.raises(p.RelayError):await write_in_thread(lambda:c.approvals.processed(*args))
    assert c.launch.terminal_grants.record.processed==0
    applied={'kind':'decision.applied','decisionId':'key-question','operationId':ticket.operation_id,
        'choiceId':'accept','actionDigest':c.digest,'nativeRef':p.sha256_digest(b'artificial exact application evidence')}
    assert c.launch.publication(applied)
    assert await write_in_thread(lambda:c.approvals.processed(*args))
    assert c.launch.terminal_grants.record.processed==1 and c.approvals.offer is None
    with c.db() as db:assert db.get(RelayOperation,ticket.operation_id).receipt['state']=='accepted'


@pytest.mark.parametrize('data',[b'\r',b'\x1b',b'y',b'\x1b[A'])
async def test_unarmed_review_refuses_keys_with_clear_safe_message_before_any_byte(approval_case,data):
    c=approval_case;c.approvals.reset()
    reply=await call(c.client,'terminal.input',key(c,data))
    error=reply['error']['data']
    assert error['code']=='UNSUPPORTED' and error['details']=={'reason':'terminal_review_required'}
    assert 'review buttons' in error['message'] and 'No key was sent' in error['message']
    assert not c.host.writes and not c.replies and c.approvals.submission is None
