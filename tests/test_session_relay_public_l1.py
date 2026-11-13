"""Real cookie public L1 original receipts over real main/broker and fake CLI."""
import importlib.util
from pathlib import Path

import pytest
from test_session_relay_ownership import SID, ownership_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelayOwnership, RelaySession
from acc.session_relay import protocol as p


@pytest.fixture
async def public_l1(ownership_case,monkeypatch):  # noqa: F811
    c=ownership_case;c.ctx.relay_runtime=c.manager
    monkeypatch.setattr('acc.server.session_relay.exclusive_public.PUBLIC_L1_QUALIFIED',True)
    @write_intent
    def qualify():
        with c.db() as db:
            row=db.get(RelaySession,SID)
            row.capabilities={**row.capabilities,'switch.to_chat':'qualified','switch.to_terminal':'qualified'}
            db.commit()
    c.qualify_l1=qualify;qualify()
    spec=importlib.util.spec_from_file_location('l1_http',Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/public_native_http.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    c.public=await module.Public.create(c,c.manager,SID,features=['switch'])
    c.other=await module.Public.create(c,c.manager,SID,features=['switch'])
    try:yield c
    finally:await c.public.close();await c.other.close()


async def mutation(c,client,method,fields,key):
    result=await client.call('operation.register',{'expectedRunId':str(c.launch.run_id),
        'registrationKey':key,'method':method,'payloadDigest':p.operation_digest(method,fields)})
    assert 'result' in result,result
    operation=result['result']['operationId']
    return await client.call(method,{**fields,'operationId':operation}),operation


async def prepare(c):
    snapshot=(await c.public.call('session.snapshot',{}))['result']
    fields={'sessionId':SID,'expectedRunId':str(c.launch.run_id),'target':'terminal',
        'expectedRevision':snapshot['l1']['revision']}
    return await mutation(c,c.public,'switch.prepare',fields,'public-l1-prepare')


async def test_public_l1_uses_original_registered_receipts_and_new_run_rejects_old_connection(public_l1):
    c=public_l1;source=c.owned_factory.current
    response,prepare_id=await prepare(c);assert 'result' in response,response
    plan=response['result'];old_run=str(c.launch.run_id)
    assert plan['prepareOperationId']==prepare_id
    fields={'sessionId':SID,'expectedRunId':old_run,'planId':plan['planId'],'planDigest':plan['planDigest'],
        'expectedRevision':plan['revision']}
    response,commit_id=await mutation(c,c.public,'switch.commit',fields,'public-l1-commit')
    assert 'result' in response,response
    assert response['result']['phase']=='owned' and response['result']['view']=='terminal'
    assert response['result']['operationId']==commit_id and str(c.launch.run_id)!=old_run
    assert source.exit_verified and c.owned_factory.current is not source
    with c.db() as db:
        assert db.get(RelayOperation,prepare_id).receipt['state']=='accepted'
        assert db.get(RelayOperation,commit_id).receipt['state']=='accepted'
    stale=await c.public.call('session.snapshot',{})
    assert stale['error']['data']['code']=='NOT_FOUND_OR_DENIED'
    assert await c.owned_factory.authorize()  # fresh person lifecycle survives source-run scope retirement


async def test_another_window_cannot_commit_the_first_windows_plan(public_l1):
    c=public_l1;response,_=await prepare(c);plan=response['result']
    fields={'sessionId':SID,'expectedRunId':str(c.launch.run_id),'planId':plan['planId'],
        'planDigest':plan['planDigest'],'expectedRevision':plan['revision']}
    response,operation=await mutation(c,c.other,'switch.commit',fields,'wrong-window')
    assert 'error' in response
    with c.db() as db:
        assert db.get(RelayOwnership,SID).phase=='prepared'
        assert db.get(RelayOperation,operation).receipt['state']=='failed'


async def test_explicit_preclaim_cancel_restores_source_authority_without_starting_target(public_l1):
    c=public_l1;source=c.owned_factory.current
    with c.db() as db:before=dict(db.get(RelayOwnership,SID).authority)
    response,_=await prepare(c);plan=response['result']
    fields={'sessionId':SID,'expectedRunId':str(c.launch.run_id),'planId':plan['planId']}
    response,operation=await mutation(c,c.public,'switch.cancel',fields,'public-l1-cancel')
    assert response['result']['phase']=='owned'
    with c.db() as db:
        assert db.get(RelayOwnership,SID).authority==before
        assert db.get(RelayOperation,operation).receipt['state']=='accepted'
    assert c.owned_factory.current is source and source.process.returncode is None


async def test_default_qualification_gate_off_never_negotiates_public_l1(public_l1,monkeypatch):
    c=public_l1;monkeypatch.setattr('acc.server.session_relay.exclusive_public.PUBLIC_L1_QUALIFIED',False)
    reply=await c.public.call('initialize',{'major':1,'minor':0,'features':['switch'],'clientId':'gate-off'})
    assert 'switch' not in reply['result']['features']


async def test_qualification_loss_after_registration_never_stops_source(public_l1,monkeypatch):
    c=public_l1;source=c.owned_factory.current
    response,_=await prepare(c);plan=response['result']
    fields={'sessionId':SID,'expectedRunId':str(c.launch.run_id),'planId':plan['planId'],
        'planDigest':plan['planDigest'],'expectedRevision':plan['revision']}
    registered=await c.public.call('operation.register',{'expectedRunId':str(c.launch.run_id),
        'registrationKey':'qualification-lost','method':'switch.commit','payloadDigest':p.operation_digest('switch.commit',fields)})
    operation=registered['result']['operationId']
    monkeypatch.setattr('acc.server.session_relay.exclusive_public.PUBLIC_L1_QUALIFIED',False)
    response=await c.public.call('switch.commit',{**fields,'operationId':operation})
    assert 'error' in response
    with c.db() as db:
        assert db.get(RelayOwnership,SID).phase=='prepared'
        assert db.get(RelayOperation,operation).receipt['state']=='failed'
    assert c.owned_factory.current is source and source.process.returncode is None


async def test_cookie_revocation_after_prepare_never_stops_source(public_l1):
    c=public_l1;source=c.owned_factory.current
    response,_=await prepare(c);plan=response['result']
    c.public.revoke()
    response=await c.public.client.post(f'/api/sessions/{SID}/relay',json={
        'jsonrpc':'2.0','id':'revoked','method':'ry/switch.commit','params':{'sessionId':SID,
        'expectedRunId':str(c.launch.run_id),'planId':plan['planId'],'planDigest':plan['planDigest'],
        'expectedRevision':plan['revision'],'operationId':'revoked-operation'}})
    assert response.status_code in (401,403,404)
    with c.db() as db:assert db.get(RelayOwnership,SID).phase=='prepared'
    assert c.owned_factory.current is source and source.process.returncode is None
