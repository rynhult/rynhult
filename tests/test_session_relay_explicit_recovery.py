"""Fresh public recovery intent refuses before IO when its review is stale."""
from dataclasses import replace

import pytest
from fastapi import HTTPException
from test_session_relay_ownership import ownership_case  # noqa: F401
from test_session_relay_public_l1 import public_l1  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.models import RelayOperation, RelayOwnership, RelaySession
from acc.session_relay import protocol as p


@pytest.fixture
async def failed_l1(public_l1,monkeypatch):  # noqa: F811
    c=public_l1
    async def refuse(*_args,**_kwargs):raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
    monkeypatch.setattr(c.owned_factory,'terminal',refuse)
    with c.db() as db:revision=db.get(RelaySession,c.launch.session_id).revision
    with pytest.raises((p.RelayError,ConnectionError)):
        await c.manager.select_owner(c.resolve,c.launch.session_id,target='terminal',revision=revision,request_key='failed-for-recovery')
    with c.db() as db:
        assert db.get(RelayOwnership,c.launch.session_id).phase=='recovery_required'
        assert db.get(RelaySession,c.launch.session_id).sealed
    c.owned_factory.base=c.owned_factory.spec
    c.owned_factory.incarnation='fixture-factory'
    monkeypatch.setattr('acc.server.session_relay.exclusive_recovery.RECOVERY_QUALIFIED',True)
    return c


async def register(c):
    sid=c.launch.session_id;run=str(c.launch.run_id)
    snapshot=(await c.public.call('session.snapshot',{}))['result']
    fields={'sessionId':sid,'expectedRunId':run,'target':'chat','expectedRevision':snapshot['l1']['revision'],
        'reason':'Reviewed failed owner and same conversation recovery'}
    response=await c.public.call('operation.register',{'expectedRunId':run,'registrationKey':'recovery-review',
        'method':'switch.recover','payloadDigest':p.operation_digest('switch.recover',fields)})
    assert 'result' in response,response
    return fields,response['result']['operationId']


@pytest.mark.parametrize('failure',['revision','digest','qualification','cookie','policy'])
async def test_review_loss_refuses_before_cleanup_or_resume(failed_l1,monkeypatch,failure):
    c=failed_l1;fields,operation=await register(c);calls=[]
    original=c.launch.peer.call
    async def observe(method,params):
        if method=='owner.recover':calls.append(method)
        return await original(method,params)
    monkeypatch.setattr(c.launch.peer,'call',observe)
    if failure=='revision':fields['expectedRevision']+=1
    elif failure=='digest':fields['reason']='Another reason than the registered complete review'
    elif failure=='qualification':monkeypatch.setattr('acc.server.session_relay.exclusive_recovery.RECOVERY_QUALIFIED',False)
    elif failure=='policy':
        c.ctx.settings=replace(c.ctx.settings,feature_overrides={'session_relay':False})
    if failure=='cookie':
        c.public.revoke()
        response=await c.public.client.post(f'/api/sessions/{c.launch.session_id}/relay',json={
            'jsonrpc':'2.0','id':'revoked','method':'ry/switch.recover','params':{**fields,'operationId':operation}})
        assert response.status_code in (401,403,404)
    else:
        response=await c.public.call('switch.recover',{**fields,'operationId':operation})
        assert 'error' in response,response
    assert calls==[]
    with c.db() as db:
        assert db.get(RelaySession,c.launch.session_id).sealed
        assert db.get(RelayOwnership,c.launch.session_id).phase=='recovery_required'
        assert db.get(RelayOperation,operation).receipt['state']=='queued'


async def test_unknown_cleanup_cannot_start_a_replacement(failed_l1,monkeypatch):
    c=failed_l1;fields,operation=await register(c);calls=[]
    async def unknown():
        calls.append('cleanup')
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    monkeypatch.setattr(c.owned_factory,'close',unknown)
    try:
        response=await c.public.call('switch.recover',{**fields,'operationId':operation})
        assert response['error']['data']['code']=='INPUT_CONFLICT'
        assert calls==['cleanup']
        with c.db() as db:
            assert db.get(RelaySession,c.launch.session_id).sealed
            assert db.get(RelayOperation,operation).receipt['state']=='uncertain'
    finally:monkeypatch.undo()


async def test_default_gate_refuses_registration_without_stranding_a_receipt(failed_l1,monkeypatch):
    c=failed_l1;monkeypatch.setattr('acc.server.session_relay.exclusive_recovery.RECOVERY_QUALIFIED',False)
    response=await c.public.call('operation.register',{'expectedRunId':str(c.launch.run_id),
        'registrationKey':'unqualified-recovery','method':'switch.recover','payloadDigest':p.sha256_digest(b'review')})
    assert response['error']['data']['code']=='UNSUPPORTED'
    with c.db() as db:
        assert db.query(RelayOperation).filter_by(registration_key='unqualified-recovery').count()==0


@pytest.mark.parametrize('failure',['before_cleanup','wrong_identity','qualification_after_claim','revoked_after_claim'])
async def test_main_requires_fresh_cleanup_and_exact_new_owner_at_every_checkpoint(failed_l1,monkeypatch,failure):
    from acc.server.session_relay import exclusive_recovery as recovery
    c=failed_l1;fields,operation=await register(c)
    class Attached:
        attachment_id='component-review-attachment'
        def __call__(self):return c.resolve()
    token='component-opaque-plan'
    recovery.claim(c.launch,Attached(),{**fields,'operationId':operation},token)
    plan=c.launch.recovery_plans[token]
    evidence={'binding_id':c.launch.binding_id,'run_id':plan['targetRunId'],
        'native_conversation':'fixture-conversation','process_identity':'component-process'}
    if failure=='wrong_identity':
        recovery.observe(c.launch,'authority.recovery_clean',{'plan':token,'factory':'component-factory'})
        evidence['native_conversation']='foreign-conversation'
    elif failure=='qualification_after_claim':
        monkeypatch.setattr(recovery,'RECOVERY_QUALIFIED',False)
    elif failure=='revoked_after_claim':
        c.state['revoked']=True
    try:
        with pytest.raises((p.RelayError,HTTPException)):
            recovery.observe(c.launch,'authority.recovery_owned',{'plan':token,'evidence':evidence})
        with c.db() as db:
            assert db.get(RelaySession,c.launch.session_id).sealed
            assert db.get(RelayOperation,operation).receipt['state']=='submitting'
    finally:
        c.state['revoked']=False
        recovery.failed(c.launch,token)
        c.launch.recovery_plans.pop(token,None)


async def test_cancellation_after_claim_joins_transaction_and_retires_plan_without_broker_io(failed_l1,monkeypatch):
    import asyncio

    from acc.server.session_relay import exclusive_recovery as recovery
    c=failed_l1;fields,operation=await register(c);calls=[]
    class Attached:
        attachment_id='cancelled-review-attachment'
        def __call__(self):return c.resolve()
    loop=asyncio.get_running_loop();holder={};original=recovery.claim
    def cancel_after_commit(*args,**kwargs):
        result=original(*args,**kwargs)
        loop.call_soon_threadsafe(holder['task'].cancel)
        return result
    monkeypatch.setattr(recovery,'claim',cancel_after_commit)
    original_call=c.launch.peer.call
    async def observe(method,params):
        if method=='owner.recover':calls.append(method)
        return await original_call(method,params)
    monkeypatch.setattr(c.launch.peer,'call',observe)
    holder['task']=asyncio.create_task(recovery.recover(c.launch,Attached(),{**fields,'operationId':operation}))
    with pytest.raises(asyncio.CancelledError):await holder['task']
    assert calls==[] and c.launch.recovery_plans=={}
    with c.db() as db:
        assert db.get(RelayOwnership,c.launch.session_id).phase=='recovery_required'
        assert db.get(RelaySession,c.launch.session_id).sealed
        assert db.get(RelayOperation,operation).receipt['state']=='uncertain'


async def test_stop_reclaims_partial_recovery_factory_even_with_retired_controller(failed_l1):
    from types import SimpleNamespace

    c=failed_l1;runtime=c.owned_factory.runtime;closed=[]
    async def close_partial():closed.append('partial-target')
    runtime.owners=SimpleNamespace(close=close_partial)
    assert runtime.controller.closed
    await runtime.close()
    assert closed==['partial-target']


@pytest.mark.parametrize('ownership_case',['subscription'],indirect=True)
async def test_subscription_recovery_uses_fake_cli_and_refuses_withdrawn_person_consent(failed_l1,monkeypatch):
    from acc.server import claude_subscription
    from acc.server.db import write_intent

    c=failed_l1;fields,operation=await register(c);calls=[]
    @write_intent
    def withdraw():
        with c.db() as db:
            claude_subscription.update(db,c.resolve()[0],False,claude_subscription.VERSION)
            db.commit()
    withdraw()
    original=c.launch.peer.call
    async def observe(method,params):
        if method=='owner.recover':calls.append(method)
        return await original(method,params)
    monkeypatch.setattr(c.launch.peer,'call',observe)
    response=await c.public.call('switch.recover',{**fields,'operationId':operation})
    assert response['error']['data']['code']=='POLICY_REFUSED'
    assert calls==[]
    with c.db() as db:
        assert db.get(RelaySession,c.launch.session_id).sealed
        assert db.get(RelayOperation,operation).receipt['state']=='queued'
