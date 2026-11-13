"""Opaque native aliases never infer an actor or forward a provider request ID."""
import json
from types import SimpleNamespace

import pytest

from acc.session_relay import protocol as p
from acc.session_relay.native_approval_aliases import ApprovalAliases


@pytest.fixture
async def aliases():
    calls=[]
    async def offer(value):return True
    async def reply(value,choice):calls.append((value,choice));return {'state':'accepted'}
    route=SimpleNamespace(offer_native_approval=offer,native_approval_response=reply)
    registry=ApprovalAliases('native-1');peer=object()
    question={'decision_id':'decision-1','action_digest':p.sha256_digest(b'reviewed-native-question'),
        'revision':0,'params':{'threadId':'native-1','turnId':'turn-1','itemId':'item-1','command':'echo fixture',
            'startedAtMs':1},'choices':frozenset({'accept','cancel'})}
    return SimpleNamespace(registry=registry,peer=peer,route=route,question=question,calls=calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('choice',['accept','cancel'])
async def test_exact_alias_and_route_consumed_once(aliases,choice):
    c=aliases;request=await c.registry.offer(c.peer,c.route,**c.question)
    assert request['id']!=c.question['decision_id'] and request['method']=='item/commandExecution/requestApproval'
    assert request['params']['startedAtMs']==1
    payload={'id':request['id'],'result':{'decision':choice}}
    assert await c.registry.reply(c.peer,c.route,payload)=={'state':'accepted'}
    assert len(c.calls)==1 and c.calls[0][1]==choice
    offer=c.calls[0][0]
    assert offer.decision_id=='decision-1' and json.loads(offer.params_json)['command']=='echo fixture'
    with pytest.raises(p.RelayError):await c.registry.reply(c.peer,c.route,payload)
    assert len(c.calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['peer','route','alias','scoped','extra','error','no_result','bool_id'])
async def test_unbound_response_never_reaches_the_private_route(aliases,fault):
    c=aliases;request=await c.registry.offer(c.peer,c.route,**c.question)
    peer,route=c.peer,c.route;payload={'id':request['id'],'result':{'decision':'accept'}}
    if fault=='peer':peer=object()
    elif fault=='route':route=object()
    elif fault=='alias':payload['id']='provider-request-id'
    elif fault=='scoped':payload['result']['decision']='acceptForSession'
    elif fault=='extra':payload['actor']='current-holder'
    elif fault=='error':payload={'id':request['id'],'error':{'code':-1}}
    elif fault=='no_result':payload={'id':request['id']}
    elif fault=='bool_id':payload['id']=True
    with pytest.raises(p.RelayError):await c.registry.reply(peer,route,payload)
    assert not c.calls


@pytest.mark.asyncio
async def test_lost_route_result_cannot_replay_a_native_answer(aliases):
    c=aliases;request=await c.registry.offer(c.peer,c.route,**c.question)
    async def uncertain(value,choice):c.calls.append((value,choice));raise ConnectionError('after route dispatch')
    c.route.native_approval_response=uncertain
    payload={'id':request['id'],'result':{'decision':'accept'}}
    with pytest.raises(ConnectionError):await c.registry.reply(c.peer,c.route,payload)
    with pytest.raises(p.RelayError):await c.registry.reply(c.peer,c.route,payload)
    assert len(c.calls)==1


@pytest.mark.asyncio
async def test_retirement_and_capacity_do_not_forget_used_aliases(aliases):
    c=aliases;c.registry=ApprovalAliases('native-1',capacity=1,lifetime_capacity=2)
    request=await c.registry.offer(c.peer,c.route,**c.question)
    with pytest.raises(p.RelayError):await c.registry.offer(c.peer,c.route,**c.question)
    c.registry.retire(c.peer)
    other=await c.registry.offer(c.peer,c.route,**{**c.question,'decision_id':'decision-2'})
    assert request['id']!=other['id']
    c.registry.retire(c.peer)
    with pytest.raises(p.RelayError):await c.registry.offer(c.peer,c.route,**c.question)
    with pytest.raises(p.RelayError):await c.registry.reply(c.peer,c.route,{'id':request['id'],'result':{'decision':'accept'}})
    assert not c.calls


@pytest.mark.asyncio
async def test_offer_does_not_supply_authority_to_a_read_only_route(aliases):
    c=aliases
    with pytest.raises(p.RelayError):await c.registry.offer(c.peer,object(),**c.question)
    async def denied(value):return False
    c.route.offer_native_approval=denied
    with pytest.raises(p.RelayError):await c.registry.offer(c.peer,c.route,**c.question)
    assert not c.registry.pending and not c.calls


@pytest.mark.asyncio
async def test_identical_questions_keep_distinct_immutable_offers(aliases):
    c=aliases
    first=await c.registry.offer(c.peer,c.route,**c.question)
    second=await c.registry.offer(c.peer,c.route,**{**c.question,'decision_id':'decision-twin'})
    c.question['params']['command']='changed after review'
    assert first['id']!=second['id']
    for request in (second,first):
        await c.registry.reply(c.peer,c.route,{'id':request['id'],'result':{'decision':'cancel'}})
    assert [offer.decision_id for offer,_ in c.calls]==['decision-twin','decision-1']
    assert all(json.loads(offer.params_json)['command']=='echo fixture' for offer,_ in c.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('params',[{'config':{}},{'additionalPermissions':{}},{'turnId':True},{'command':''}])
async def test_unqualified_question_profiles_refuse_before_route_consent(aliases,params):
    c=aliases
    with pytest.raises(p.RelayError):
        await c.registry.offer(c.peer,c.route,**{**c.question,'params':{**c.question['params'],**params}})
    assert not c.registry.pending and not c.registry.used and not c.calls


@pytest.mark.asyncio
async def test_concurrent_duplicate_offer_reserves_before_route_consent(aliases):
    import asyncio
    c=aliases;entered=asyncio.Event();release=asyncio.Event()
    async def offer(value):entered.set();await release.wait();return True
    c.route.offer_native_approval=offer
    first=asyncio.create_task(c.registry.offer(c.peer,c.route,**c.question))
    await entered.wait()
    try:
        with pytest.raises(p.RelayError):
            await asyncio.wait_for(c.registry.offer(c.peer,c.route,**c.question),.5)
        assert len(c.registry.used)==1 and not c.registry.pending
    finally:
        release.set()
    result=await first
    assert len(c.registry.pending)==1 and result['id'] in c.registry.pending


@pytest.mark.asyncio
async def test_retirement_during_consent_cannot_resurrect_an_offer(aliases):
    import asyncio
    c=aliases;entered=asyncio.Event();release=asyncio.Event()
    async def offer(value):entered.set();await release.wait();return True
    c.route.offer_native_approval=offer
    task=asyncio.create_task(c.registry.offer(c.peer,c.route,**c.question))
    await entered.wait();c.registry.retire(c.peer);release.set()
    with pytest.raises(p.RelayError):await task
    assert not c.registry.pending and not c.registry.reserved and len(c.registry.used)==1
    with pytest.raises(p.RelayError):await c.registry.offer(c.peer,c.route,**c.question)


@pytest.mark.asyncio
@pytest.mark.parametrize('ending',['accepted','refused','cancelled','exception'])
async def test_finished_offer_cannot_be_reissued_under_a_fresh_alias(aliases,ending):
    import asyncio
    c=aliases
    if ending=='accepted':
        request=await c.registry.offer(c.peer,c.route,**c.question)
        await c.registry.reply(c.peer,c.route,{'id':request['id'],'result':{'decision':'accept'}})
    else:
        async def offer(value):
            if ending=='refused':return False
            if ending=='cancelled':raise asyncio.CancelledError()
            raise ConnectionError('route consent uncertain')
        c.route.offer_native_approval=offer
        with pytest.raises((p.RelayError,asyncio.CancelledError,ConnectionError)):
            await c.registry.offer(c.peer,c.route,**c.question)
    with pytest.raises(p.RelayError):await c.registry.offer(c.peer,c.route,**c.question)
    assert len(c.registry.used)==1 and not c.registry.pending and not c.registry.reserved


@pytest.mark.asyncio
async def test_inflight_consent_consumes_capacity_and_other_peer_is_independent(aliases):
    import asyncio
    c=aliases;entered=asyncio.Event();release=asyncio.Event()
    c.registry.capacity=1
    async def offer(value):entered.set();await release.wait();return True
    c.route.offer_native_approval=offer
    task=asyncio.create_task(c.registry.offer(c.peer,c.route,**c.question))
    await entered.wait()
    try:
        with pytest.raises(p.RelayError):
            await asyncio.wait_for(c.registry.offer(object(),c.route,**{**c.question,'decision_id':'other'}),.5)
        assert len(c.registry.used)==1
    finally:release.set()
    request=await task
    c.registry.retire(c.peer)
    assert request['id'] not in c.registry.pending
    other=await c.registry.offer(object(),c.route,**c.question)
    assert other['id']!=request['id']


@pytest.mark.asyncio
async def test_exact_withdrawal_keeps_other_question_and_tombstones_the_alias(aliases):
    from dataclasses import replace
    c=aliases
    first=await c.registry.offer(c.peer,c.route,**c.question)
    second=await c.registry.offer(c.peer,c.route,**{**c.question,'decision_id':'decision-twin'})
    offer=c.registry.pending[first['id']].offer
    assert c.registry.withdraw(object(),offer) is None
    assert c.registry.withdraw(c.route,replace(offer)) is None
    assert len(c.registry.pending)==2
    assert c.registry.withdraw(c.route,offer).offer is offer
    assert c.registry.withdraw(c.route,offer) is None
    assert second['id'] in c.registry.pending and first['id'] in c.registry.used
    with pytest.raises(p.RelayError):
        await c.registry.reply(c.peer,c.route,{'id':first['id'],'result':{'decision':'accept'}})
    assert not c.calls
    await c.registry.reply(c.peer,c.route,{'id':second['id'],'result':{'decision':'cancel'}})
    assert len(c.calls)==1 and c.calls[0][0].decision_id=='decision-twin'


@pytest.mark.asyncio
async def test_withdrawal_during_consent_never_publishes_the_question(aliases):
    c=aliases
    async def offer(value):
        assert c.registry.withdraw(c.route,value) is None
        return True
    c.route.offer_native_approval=offer
    with pytest.raises(p.RelayError):await c.registry.offer(c.peer,c.route,**c.question)
    assert not c.registry.reserved and not c.registry.pending and len(c.registry.used)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['none','authority','placement','route','send','default'])
async def test_socket_withdrawal_cannot_resend_or_infer_application(aliases,monkeypatch,fault):
    from acc.session_relay import native_approval_aliases as module
    from acc.session_relay.native_socket import NativeSocket
    c=aliases;sent=[]
    async def send(value):
        sent.append(json.loads(value))
        if fault=='send':raise ConnectionError('send uncertain')
    class Peer:
        pass
    peer=Peer();peer.send=send
    request=await c.registry.offer(peer,c.route,**c.question)
    offer=c.registry.pending[request['id']].offer
    async def authorize():return fault!='authority'
    def validate():
        if fault=='placement':raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    socket=SimpleNamespace(approval_aliases=c.registry,native_id='native-1',
        validate=validate,authorize=authorize,client_routes={peer:object() if fault=='route' else c.route})
    monkeypatch.setattr(module,'NATIVE_APPROVAL_DECODER_QUALIFIED',fault!='default')
    if fault=='none':assert await NativeSocket.withdraw_approval(socket,c.route,offer) is True
    else:
        with pytest.raises((p.RelayError,ConnectionError)):
            await NativeSocket.withdraw_approval(socket,c.route,offer)
    if fault=='default':assert request['id'] in c.registry.pending and not sent
    else:
        assert request['id'] not in c.registry.pending
        assert await NativeSocket.withdraw_approval(socket,c.route,offer) is False
        assert len(sent)==(1 if fault in ('none','send') else 0)
        if sent:assert sent[0]=={'method':'serverRequest/resolved','params':{'threadId':'native-1','requestId':request['id']}}
    assert not c.calls
