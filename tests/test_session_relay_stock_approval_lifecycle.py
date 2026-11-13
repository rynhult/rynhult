"""Public CAS/lifecycle races; main/broker/auth are real, physical owner is artificial."""
# ruff: noqa: F811
import asyncio

import httpx
import pytest
from test_session_relay_public_writer import SID, attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_stock_approval_frames import approval_case, key  # noqa: F401
from test_session_relay_terminal_grants import stream_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation
from acc.session_relay import protocol as p


async def chat_wins(c):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app),base_url='http://fixture') as chat:
        await attach(chat,'lifecycle-chat',['decisions'])
        params={'sessionId':SID,'expectedRunId':str(c.launch.run_id),'decisionId':'key-question','choiceId':'cancel',
            'expectedRevision':0,'actionDigest':c.digest}
        registered=await call(chat,'operation.register',{'expectedRunId':str(c.launch.run_id),'registrationKey':'lifecycle-chat',
            'method':'decision.reply','payloadDigest':p.operation_digest('decision.reply',params)})
        return await call(chat,'decision.reply',{**params,'operationId':registered['result']['operationId']})


async def test_chat_winner_retires_armed_offer_and_fails_unsubmitted_key_operation(approval_case):
    c=approval_case
    assert 'result' in await call(c.client,'terminal.input',key(c))
    submission=c.approvals.submission
    assert (await chat_wins(c))['result']['state']=='uncertain'
    assert c.approvals.offer is None
    with c.db() as db:
        assert db.get(RelayOperation,submission.operation_id).receipt['state']=='failed'
        assert db.get(RelayDecision,(SID,'key-question')).decision['choiceId']=='cancel'
    with pytest.raises(p.RelayError):await c.approvals.reply(c.key_grant['inputGrant'],1,c.offer['alias'],'accept')
    assert c.replies==[(7,{'decision':'cancel'})]


async def test_chat_winner_during_prebyte_authorization_refuses_key_before_transport(approval_case):
    c=approval_case;entered,release=asyncio.Event(),asyncio.Event()
    async def paused(grant,sequence,data):
        entered.set();await release.wait()
        await c.broker_state.peer.call('native.approval_frame',{'grant':grant.grant,'sequence':sequence,
            'alias':c.offer['alias'],'choice':'accept'})
        raise AssertionError('Stale frame passed authorization')
    c.host.write_frames=paused
    task=asyncio.create_task(call(c.client,'terminal.input',key(c)))
    await asyncio.wait_for(entered.wait(),3)
    assert (await chat_wins(c))['result']['state']=='uncertain'
    release.set()
    assert 'error' in await asyncio.wait_for(task,5)
    assert not c.host.writes and c.replies==[(7,{'decision':'cancel'})]


@pytest.mark.parametrize('fault',['expired','revoked','default'])
async def test_live_watch_retires_without_answering_or_claiming_an_input_ack(approval_case,monkeypatch,fault):
    c=approval_case
    if fault=='revoked':c.state['revoked']=True
    elif fault=='default':monkeypatch.setattr('acc.server.session_relay.stock_approval_frames.PUBLIC_STOCK_KEYS_QUALIFIED',False)
    else:
        @write_intent
        def expire():
            with c.db() as db:
                row=db.get(RelayDecision,(SID,'key-question'));row.decision={**row.decision,'expiresAtMs':1};db.commit()
        expire()
    c.approvals.watch()
    async with asyncio.timeout(3):
        while c.approvals.offer is not None:await asyncio.sleep(.01)
    assert not c.replies and not c.host.writes and c.launch.terminal_grants.record.processed==0
    if fault=='expired':
        with c.db() as db:assert db.get(RelayDecision,(SID,'key-question')).decision['status']=='expired'


async def test_late_original_application_after_cookie_revocation_settles_without_restoring_input(approval_case):
    c=approval_case
    assert 'result' in await call(c.client,'terminal.input',key(c))
    submission=c.approvals.submission
    await c.approvals.reply(c.key_grant['inputGrant'],1,c.offer['alias'],'accept')
    c.state['revoked']=True
    event={'kind':'decision.applied','decisionId':'key-question','operationId':submission.operation_id,
        'choiceId':'accept','actionDigest':c.digest,'nativeRef':p.sha256_digest(b'exact late original proof')}
    assert c.launch.publication(event)
    await c.approvals.application_published(event)
    with c.db() as db:assert db.get(RelayOperation,submission.operation_id).receipt['state']=='accepted'
    assert c.launch.terminal_grants.record.processed==0 and len(c.replies)==1


async def test_public_caller_cannot_rebind_server_registered_key_operation_to_another_request(approval_case):
    c=approval_case
    assert 'result' in await call(c.client,'terminal.input',key(c))
    submission=c.approvals.submission
    params={**c.approvals.params(submission),'operationId':submission.operation_id}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app),base_url='http://fixture') as other:
        await attach(other,'other-origin',['decisions'])
        assert 'error' in await call(other,'decision.reply',params)
    assert not c.replies
    with c.db() as db:assert db.get(RelayOperation,submission.operation_id).receipt['state']=='queued'
    assert (await c.approvals.reply(c.key_grant['inputGrant'],1,c.offer['alias'],'accept'))['state']=='uncertain'
    assert len(c.replies)==1
