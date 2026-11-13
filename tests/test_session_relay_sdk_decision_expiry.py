"""SDK absolute deadlines and authenticated main expiry, with no native clock claim."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from test_session_relay_ownership import ownership_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayOperation, RelaySession
from acc.server.session_relay import store
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p
from acc.session_relay.adapters import claude_agent
from acc.session_relay.adapters.base import AdapterOperation


@pytest.mark.parametrize('phase',['publication','final_allow','lost_expiry_publication'])
async def test_publication_cannot_extend_deadline_or_late_allow(phase,monkeypatch):
    from claude_agent_sdk import PermissionResultDeny

    now=[1000.0]
    monkeypatch.setattr(claude_agent,'time',SimpleNamespace(time=lambda:now[0],monotonic=lambda:now[0]))
    events=[]
    async def publish(event):
        events.append(event)
        if event['kind']=='decision':
            if phase in ('publication','lost_expiry_publication'):
                now[0]+=301
            else:
                adapter.applications.records[event['id']].update(
                    answer=SimpleNamespace(run_id='run', session_id='session'), effect='allow_once')
                adapter.pending[event['id']].set_result('allow')
        elif phase=='lost_expiry_publication':
            raise ConnectionError('artificial publication loss')
    checks=0
    async def authorize(*_):
        nonlocal checks
        checks+=1
        if phase=='final_allow' and checks==2:
            now[0]+=301
        return True
    adapter=claude_agent.ClaudeAgentAdapter(policy=lambda *_:True,publish=publish,authorize=authorize)
    adapter.spec=SimpleNamespace(run_id='run',spec={'session_id':'session'})
    result=await adapter._permission('Bash',{'command':'fixture'},None)
    assert isinstance(result,PermissionResultDeny)
    assert [event['kind'] for event in events]==['decision','decision.expired']
    assert events[0]['expiresAtMs']==1_300_000
    assert not adapter.pending and not adapter.pending_meta


@pytest.mark.parametrize('fault',['monotonic','wall','replaced','revoked'])
async def test_verified_answer_rechecks_deadline_and_exact_future_after_authority(fault,monkeypatch):
    now={'wall':1000.0,'mono':1000.0}
    monkeypatch.setattr(claude_agent,'time',SimpleNamespace(time=lambda:now['wall'],monotonic=lambda:now['mono']))
    async def authorize(*_):
        if fault=='monotonic':now['mono']=1301
        if fault=='wall':now['wall']=1301
        if fault=='replaced':adapter.pending['decision']=asyncio.get_running_loop().create_future()
        return fault!='revoked'
    adapter=claude_agent.ClaudeAgentAdapter(policy=lambda *_:True,publish=None,authorize=authorize)
    adapter.spec=SimpleNamespace(run_id='run',spec={'session_id':'session'})
    future=asyncio.get_running_loop().create_future()
    digest=p.sha256_digest(b'fixture')
    adapter.pending['decision']=future
    adapter.pending_meta['decision']=(digest,1_300_000,1300)
    params={'decisionId':'decision','actionDigest':digest,'choiceId':'accept','expectedRevision':0}
    operation=AdapterOperation('op','session','run','decision.reply',p.operation_digest('decision.reply',params),
        p.Actor(p.ActorKind.PERSON,'alex',p.Channel.CHAT),p.Fence(1,'broker','main'),9999999999999,params)
    with pytest.raises(p.RelayError):await adapter.reply_verified(operation,'allow_once')
    assert not future.done()


async def card(case,expires):
    from acc.session_relay.approval_facts import native_facts
    inputs={'command':'echo artificial expiry'}
    digest=p.sha256_digest(p.canonical_json({'tool':'Bash','input':inputs}))
    await case.launch.handle('publication',{'kind':'decision','runId':str(case.launch.run_id),
        'decisionId':'expiry-card','actionDigest':digest,'expiresAtMs':expires,'choices':['accept','decline'],
        'description':'Artificial Bash action with exact frozen input.',
        'approval':native_facts('Bash', inputs, '/tmp/expiry-fixture')})
    return {'kind':'decision.expired','runId':str(case.launch.run_id),'decisionId':'expiry-card','actionDigest':digest}


async def test_main_expiry_is_exact_idempotent_and_cannot_accept_an_answer(ownership_case):  # noqa: F811
    c=ownership_case
    event=await card(c,int(time.time()*1000)-1)
    await c.launch.handle('publication',event)
    with c.db() as db:
        row=db.get(RelayDecision,(c.launch.session_id,'expiry-card'))
        original=dict(row.decision)
        sequence=db.get(RelaySession,c.launch.session_id).event_seq
        assert original['status']=='expired' and original.get('winner') is None
    await c.launch.handle('publication',event)
    with c.db() as db:
        assert db.get(RelayDecision,(c.launch.session_id,'expiry-card')).decision==original
        assert db.get(RelaySession,c.launch.session_id).event_seq==sequence


@pytest.mark.parametrize('fault',['early','digest','run'])
async def test_main_refuses_unbound_or_early_expiry(ownership_case,fault):  # noqa: F811
    c=ownership_case
    event=await card(c,int(time.time()*1000)+300_000)
    if fault=='digest':event['actionDigest']=p.sha256_digest(b'wrong')
    if fault=='run':event['runId']='old-run'
    with pytest.raises(p.RelayError):await c.launch.handle('publication',event)
    with c.db() as db:
        assert db.get(RelayDecision,(c.launch.session_id,'expiry-card')).decision['status']=='pending'


@pytest.mark.parametrize('status',['reserved','outcome_unknown'])
async def test_expiry_never_overwrites_an_already_chosen_unknown_answer(ownership_case,status):  # noqa: F811
    c=ownership_case;sid=c.launch.session_id;run=str(c.launch.run_id)
    event=await card(c,int(time.time()*1000)+300_000)
    params={'sessionId':sid,'expectedRunId':run,'decisionId':'expiry-card','choiceId':'accept',
        'expectedRevision':0,'actionDigest':event['actionDigest']}
    hub=Hub(c.ctx)
    registered=hub.register(c.resolve,sid,run,'expiry-answer','decision.reply',p.operation_digest('decision.reply',params))
    hub.answer(c.resolve,sid,'expiry-card',expected_run=run,attached=True,choice_id='accept',revision=0,
        digest=event['actionDigest'],operation_id=registered.operation_id)
    if status=='outcome_unknown':
        @write_intent
        def unknown():
            with c.db() as db:
                row=db.get(RelayDecision,(sid,'expiry-card'))
                row.decision=p.to_wire(p.advance_decision(store.decision_from(row.decision),p.DecisionStatus.OUTCOME_UNKNOWN))
                store.transition(db,store.lock(db,sid),db.get(RelayOperation,registered.operation_id),p.ReceiptState.UNCERTAIN)
                db.commit()
        unknown()
    with c.db() as db:original=dict(db.get(RelayDecision,(sid,'expiry-card')).decision)
    await c.launch.handle('publication',event)
    with c.db() as db:
        assert db.get(RelayDecision,(sid,'expiry-card')).decision==original


async def test_late_exact_answer_is_terminal_failed_before_dispatch_not_a_queue_blocker(ownership_case):  # noqa: F811
    c=ownership_case;sid=c.launch.session_id;run=str(c.launch.run_id)
    event=await card(c,int(time.time()*1000)-1)
    await c.launch.handle('publication',event)
    fields={'sessionId':sid,'expectedRunId':run,'decisionId':'expiry-card','choiceId':'accept',
        'expectedRevision':0,'actionDigest':event['actionDigest']}
    registered=Hub(c.ctx).register(c.resolve,sid,run,'late-terminal-refusal','decision.reply',p.operation_digest('decision.reply',fields))
    previous=None
    for attempt in range(2):
        with pytest.raises(p.RelayError) as error:
            await c.launch.answer(c.resolve,{**fields,'operationId':registered.operation_id})
        assert error.value.code is (p.ErrorCode.DECISION_EXPIRED if attempt==0 else p.ErrorCode.DECISION_RESOLVED)
        with c.db() as db:
            receipt=dict(db.get(RelayOperation,registered.operation_id).receipt)
            assert receipt['state']=='failed' and receipt['reason']=='expired'
            if previous:assert receipt==previous
            previous=receipt
    assert not c.owned_factory.current.pending
