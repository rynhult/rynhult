"""Real public/main cancellation ordering; physical decoder/provider are fakes."""
# ruff: noqa: F811
import asyncio

import httpx
import pytest
from test_session_relay_public_writer import SID, attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_stock_approval_frames import approval_case, key  # noqa: F401
from test_session_relay_terminal_grants import stream_case  # noqa: F401

from acc.server.models import RelayOperation
from acc.session_relay import protocol as p


async def cancel(c,operation,revision):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app),base_url='http://fixture') as viewer:
        await attach(viewer,'explicit-cancel-view',['queue'])
        return await call(viewer,'operation.cancel',{'operationId':operation,'expectedRevision':revision})


async def test_cancel_delivered_key_before_decoded_cas_retires_offer_without_provider_answer(approval_case):
    c=approval_case
    assert 'result' in await call(c.client,'terminal.input',key(c))
    submission=c.approvals.submission
    with c.db() as db:before=dict(db.get(RelayOperation,submission.operation_id).receipt)
    result=await cancel(c,submission.operation_id,before['revision'])
    assert result['result']['state']=='failed' and result['result']['reason']=='cancelled',result
    c.approvals.watch()
    async with asyncio.timeout(3):
        while c.approvals.offer is not None:await asyncio.sleep(.01)
    with pytest.raises(p.RelayError):
        await c.approvals.reply(c.key_grant['inputGrant'],1,c.offer['alias'],'accept')
    assert c.replies==[] and len(c.host.writes)==1
    assert c.launch.terminal_grants.record.processed==0
    assert 'error' in await call(c.client,'terminal.input',key(c,frameSeq=2))
    assert len(c.host.writes)==1 and c.replies==[]


@pytest.mark.parametrize('applied',[False,True])
async def test_cancel_after_key_cas_never_undoes_original_unknown_or_accepted_winner(approval_case,applied):
    c=approval_case
    assert 'result' in await call(c.client,'terminal.input',key(c))
    submission=c.approvals.submission
    assert (await c.approvals.reply(c.key_grant['inputGrant'],1,c.offer['alias'],'accept'))['state']=='uncertain'
    if applied:
        assert c.launch.publication({'kind':'decision.applied','decisionId':'key-question',
            'operationId':submission.operation_id,'choiceId':'accept','actionDigest':c.digest,
            'nativeRef':p.sha256_digest(b'fake exact application')})
    with c.db() as db:before=dict(db.get(RelayOperation,submission.operation_id).receipt)
    assert before['state']==('accepted' if applied else 'uncertain')
    result=await cancel(c,submission.operation_id,before['revision'])
    assert result['error']['data']['code']=='REVISION_CONFLICT',result
    with c.db() as db:assert db.get(RelayOperation,submission.operation_id).receipt==before
    with pytest.raises(p.RelayError):
        await c.approvals.reply(c.key_grant['inputGrant'],1,c.offer['alias'],'accept')
    assert c.replies==[(7,{'decision':'accept'})] and len(c.host.writes)==1


async def test_cancelled_delivered_key_requires_fresh_review_and_never_replays_original_allow(approval_case):
    c=approval_case
    assert 'result' in await call(c.client,'terminal.input',key(c))
    submission=c.approvals.submission
    with c.db() as db:before=dict(db.get(RelayOperation,submission.operation_id).receipt)
    cancelled=await cancel(c,submission.operation_id,before['revision'])
    assert cancelled['result']['reason']=='cancelled'
    c.approvals.watch()
    async with asyncio.timeout(3):
        while c.approvals.offer is not None:await asyncio.sleep(.01)
    assert c.replies==[]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app),base_url='http://fixture') as reviewer:
        await attach(reviewer,'fresh-reviewed-cancel',['decisions'])
        params={**c.approvals.params(submission),'choiceId':'cancel'}
        registered=await call(reviewer,'operation.register',{'expectedRunId':str(c.launch.run_id),
            'registrationKey':'fresh-reviewed-cancel','method':'decision.reply',
            'payloadDigest':p.operation_digest('decision.reply',params)})
        fields={**params,'operationId':registered['result']['operationId']}
        result=await call(reviewer,'decision.reply',fields)
        assert result['result']['state']=='uncertain',result
        assert await call(reviewer,'decision.reply',fields)==result
    with c.db() as db:
        original=db.get(RelayOperation,submission.operation_id).receipt
        assert original['state']=='failed' and original['reason']=='cancelled'
    assert c.replies==[(7,{'decision':'cancel'})]
    assert c.launch.terminal_grants.record.processed==0 and len(c.host.writes)==1
