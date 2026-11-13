"""Real cookie/CSRF/main/broker; owned fake CLI and artificial native callback."""
import asyncio
import importlib.util
from pathlib import Path

import pytest
from test_session_relay_claude_agent import until
from test_session_relay_ownership import SID, ownership_case, select_owner  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation, RelaySession
from acc.session_relay import protocol as p
from acc.session_relay.claude_hook import exchange


@pytest.fixture
async def public_claude(ownership_case,monkeypatch):  # noqa: F811
    c=ownership_case
    await select_owner(c,'terminal','public-review-terminal')
    c.ctx.relay_runtime=c.manager
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED',True)
    @write_intent
    def qualify():
        with c.db() as db:
            db.get(RelaySession,SID).capabilities={'decision.tool_approval':'qualified'}
            db.commit()
    qualify()
    spec=importlib.util.spec_from_file_location('public_http_fixture',Path(__file__).resolve().parents[1]/
        'scripts/session_relay_qualification/public_native_http.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    c.first=await module.Public.create(c,c.manager,SID,features=['decisions'])
    c.second=await module.Public.create(c,c.manager,SID,features=['decisions'])
    await asyncio.to_thread(exchange,c.owned_factory.directory/'owner-channel.json',{
        'session_id':'fixture-conversation','prompt_id':'fixture-prompt','hook_event_name':'PreToolUse',
        'tool_use_id':'fixture-public-tool','tool_name':'Bash','tool_input':{'command':'echo artificial review'}})
    c.question_task=asyncio.create_task(asyncio.to_thread(exchange,c.owned_factory.directory/'owner-channel.json',{
        'session_id':'fixture-conversation','prompt_id':'fixture-prompt','hook_event_name':'PermissionRequest',
        'tool_name':'Bash','tool_input':{'command':'echo artificial review'}}))
    await until(lambda:bool(c.owned_factory.hooks.pending))
    c.question=next(iter(c.owned_factory.hooks.pending))
    async with asyncio.timeout(3):
        while True:
            with c.db() as db:
                record=db.get(RelayDecision,(SID,c.question))
                if record:
                    c.card=dict(record.decision)
                    break
            await asyncio.sleep(.01)
    try:yield c
    finally:
        await c.first.close();await c.second.close()
        await c.owned_factory.hooks.close()
        await c.question_task


async def register(c,client,key,**changes):
    fields={'sessionId':SID,'expectedRunId':str(c.launch.run_id),'decisionId':c.question,
        'choiceId':'decline','expectedRevision':c.card['revision'],'actionDigest':c.card['actionDigest'],**changes}
    reply=await client.call('operation.register',{'expectedRunId':str(c.launch.run_id),'registrationKey':key,
        'method':'decision.reply','payloadDigest':p.operation_digest('decision.reply',fields)})
    assert 'result' in reply,reply
    return {**fields,'operationId':reply['result']['operationId']}


async def test_two_cookie_reviewers_have_one_exact_winner_and_replay_only_observes(public_claude):
    c=public_claude
    fields=[await register(c,c.first,'first',choiceId='accept'),await register(c,c.second,'second')]
    replies=await asyncio.gather(c.first.call('decision.reply',fields[0]),c.second.call('decision.reply',fields[1]))
    assert sum('result' in result for result in replies)==1,replies
    winner=next(i for i,result in enumerate(replies) if 'result' in result)
    assert replies[winner]['result']['state']=='uncertain'
    assert (await c.question_task)['behavior']==('allow' if winner==0 else 'deny')
    with c.db() as db:
        receipts=[db.get(RelayOperation,f['operationId']).receipt for f in fields]
        assert receipts[winner]['state']=='uncertain' and receipts[1-winner]['state']=='failed'
        assert db.get(RelayDecision,(SID,c.question)).decision['status']=='outcome_unknown'
    replay=await (c.first if winner==0 else c.second).call('decision.reply',fields[winner])
    assert replay==replies[winner] and not c.owned_factory.hooks.pending


async def test_cancelled_http_waiter_does_not_cancel_committed_answer_or_authorize_replay(public_claude):
    c=public_claude
    fields=await register(c,c.first,'cancelled-waiter')
    entered,release=asyncio.Event(),asyncio.Event()
    original=c.launch.peer.call
    async def pause(method,params):
        if method=='owner.answer':
            entered.set();await release.wait()
        return await original(method,params)
    c.launch.peer.call=pause
    task=asyncio.create_task(c.first.call('decision.reply',fields))
    await asyncio.wait_for(entered.wait(),3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    release.set()
    assert (await c.question_task)['behavior']=='deny'
    await until(lambda:not c.manager.background)
    with c.db() as db:assert db.get(RelayOperation,fields['operationId']).receipt['state']=='uncertain'
    replay=await c.first.call('decision.reply',fields)
    assert replay['result']['state']=='uncertain'


@pytest.mark.parametrize('changes',[{'choiceId':'acceptForSession'},{'actionDigest':'sha256:'+'0'*64},{'expectedRevision':99}])
async def test_invalid_exact_registered_answer_fails_without_claiming_native_question(public_claude,changes):
    c=public_claude
    fields=await register(c,c.first,'invalid',**changes)
    value=await c.first.call('decision.reply',fields)
    assert 'error' in value,value
    with c.db() as db:
        assert db.get(RelayOperation,fields['operationId']).receipt['state']=='failed'
        assert db.get(RelayDecision,(SID,c.question)).decision['status']=='pending'
    assert not c.owned_factory.hooks.pending[c.question]['claimed'] and not c.question_task.done()


async def test_real_revoked_cookie_cannot_answer_a_current_native_question(public_claude):
    c=public_claude
    fields=await register(c,c.first,'revoked')
    c.first.revoke()
    response=await c.first.client.post(f'/api/sessions/{SID}/relay',json={
        'jsonrpc':'2.0','id':'revoked','method':'ry/decision.reply','params':fields})
    assert response.status_code==401
    assert not c.owned_factory.hooks.pending[c.question]['claimed']


async def test_cookie_revoked_after_cas_before_native_verification_cannot_release_allow(public_claude):
    c=public_claude
    fields=await register(c,c.first,'revoke-after-cas',choiceId='accept')
    entered,release=asyncio.Event(),asyncio.Event()
    original=c.launch.peer.call
    async def pause(method,params):
        if method=='owner.answer':
            entered.set();await release.wait()
        return await original(method,params)
    c.launch.peer.call=pause
    task=asyncio.create_task(c.first.client.post(f'/api/sessions/{SID}/relay',json={
        'jsonrpc':'2.0','id':'revoked-after-cas','method':'ry/decision.reply','params':fields}))
    await asyncio.wait_for(entered.wait(),3)
    c.first.revoke()
    release.set()
    response=await asyncio.wait_for(task,5)
    assert response.status_code==401,response.text
    assert not c.owned_factory.hooks.pending[c.question]['claimed'] and not c.question_task.done()
    with c.db() as db:
        # CAS may have committed; lost authority must never invent no-submission.
        assert db.get(RelayOperation,fields['operationId']).receipt['state']=='uncertain'
        assert db.get(RelayDecision,(SID,c.question)).decision['status']=='outcome_unknown'
