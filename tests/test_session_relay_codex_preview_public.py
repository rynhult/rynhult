"""Real public router/main/broker grant checks; native Host/probe are explicit fakes."""
# ruff: noqa: F811
import base64
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from test_session_relay_public_writer import SID, attach, call, public_case, register  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.broker.relay_runtime import CLI_DIGEST
from acc.server.api.context import Principal
from acc.server.db import write_intent
from acc.server.models import RelayRun, RelaySession, Session, User
from acc.server.session_relay import release_qualification as rq
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import TerminalRun, Watermark, WriteResult

CAPABILITIES={'text':'qualified','terminal.input':'qualified'}


@pytest.fixture
async def preview_case(public_case):
    c=public_case
    @write_intent
    def probe():
        with c.db() as db:
            run=db.get(RelayRun,c.launch.run_id)
            run.identity={**run.identity,'cliDigest':CLI_DIGEST}  # explicit fake probe, not a native-pin proof
            db.commit()
    probe()
    c.host=SimpleNamespace(claims=[],writes=[])
    c.host.run=TerminalRun(SID,str(c.launch.run_id))
    async def claim(grant):
        assert await c.broker_state.terminal_value(grant.grant,'held')==grant
        c.host.claims.append(grant)
    async def write(grant,sequence,data):
        assert await c.broker_state.terminal_value(grant.grant,'held')==grant
        c.host.writes.append((sequence,data))
        return WriteResult(Watermark(grant.epoch,sequence,0))
    async def detach(_):pass
    c.host.claim_input=claim;c.host.write_frames=write;c.host.detach=detach
    c.broker_state.terminal_host=c.host
    return c


@write_intent
def grant(c):
    with c.db() as db:
        row=db.get(RelaySession,SID)
        result=dict(rq.grant(c.ctx,db,row,db.get(RelayRun,row.current_run_id),c.resolve()[1]))
        db.commit();return result


async def take_and_claim(c,client,view):
    params={'expectedRunId':str(c.launch.run_id),'attachmentId':view['attachmentId'],
        'expectedRevision':1,'view':'terminal','reason':'Explicit owner preview take'}
    registered=await register(client,c,params,'preview-take')
    assert 'result' in registered,registered
    result=await call(client,'writer.take',{**params,'operationId':registered['result']['operationId']})
    assert result['result']['state']=='accepted',result
    claim={'sessionId':SID,'expectedRunId':str(c.launch.run_id),'attachmentId':view['attachmentId'],'expectedRevision':2}
    registered=await call(client,'operation.register',{'expectedRunId':str(c.launch.run_id),'registrationKey':'preview-claim',
        'method':'control.claim','payloadDigest':p.operation_digest('control.claim',claim)})
    assert 'result' in registered,registered
    result=await call(client,'control.claim',{**claim,'operationId':registered['result']['operationId']})
    assert 'result' in result,result
    return result['result']


async def test_codex_public_take_claim_and_input_need_actual_grant(preview_case):
    c=preview_case
    with c.db() as db:assert db.get(RelaySession,SID).capabilities=={}
    assert grant(c)==CAPABILITIES
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app),base_url='http://fixture') as client:
        view=await attach(client,'preview-owner',['writer','terminal'])
        minted=await take_and_claim(c,client,view)
        result=await call(client,'terminal.input',{'attachmentId':view['attachmentId'],'inputGrant':minted['inputGrant'],
            'inputEpoch':minted['inputEpoch'],'frameSeq':1,'data':base64.b64encode(b'unsent draft').decode()})
        assert 'result' in result,result
    assert c.host.writes==[(1,b'unsent draft')] and len(c.host.claims)==1


@pytest.mark.parametrize('fault',['no-grant','member','demoted','other-owner','digest','feature-off','sealed'])
async def test_codex_public_mutations_refuse_when_preview_grant_fails(preview_case,fault):
    c=preview_case;assert grant(c)==CAPABILITIES
    @write_intent
    def fail():
        with c.db() as db:
            row=db.get(RelaySession,SID);session=db.get(Session,SID)
            if fault=='no-grant':row.capabilities={}
            if fault=='member':session.owner='sam'
            if fault=='demoted':db.get(User,1).role='member'
            if fault=='other-owner':db.add(User(id=3,username='other',role='owner',password_hash='unused',totp_secret='unused'))
            if fault=='digest':
                run=db.get(RelayRun,row.current_run_id);run.identity={**run.identity,'cliDigest':p.sha256_digest(b'wrong')}
            if fault=='sealed':row.sealed=True
            db.commit()
    fail()
    if fault=='member':c.state.update(who=Principal('sam','member','fixture',2),actor=p.Actor(p.ActorKind.PERSON,'sam',p.Channel.CHAT))
    if fault=='demoted':c.state['who']=Principal('alex','member','fixture',1)
    if fault=='other-owner':c.state.update(who=Principal('other','owner','fixture',3),actor=p.Actor(p.ActorKind.PERSON,'other',p.Channel.CHAT))
    if fault=='feature-off':c.ctx.settings=replace(c.ctx.settings,feature_overrides={'session_relay':False})
    if fault!='no-grant':assert grant(c)=={}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app),base_url='http://fixture') as client:
        view=await attach(client,'preview-refused',['writer','terminal'])
        take={'expectedRunId':str(c.launch.run_id),'attachmentId':view['attachmentId'],'expectedRevision':1,'view':'terminal','reason':'Refused take'}
        assert 'error' in await register(client,c,take,'refused-take')
        claim={'sessionId':SID,'expectedRunId':str(c.launch.run_id),'attachmentId':view['attachmentId'],'expectedRevision':1}
        assert 'error' in await call(client,'operation.register',{'expectedRunId':str(c.launch.run_id),'registrationKey':'refused-claim',
            'method':'control.claim','payloadDigest':p.operation_digest('control.claim',claim)})
        assert 'error' in await call(client,'terminal.input',{'attachmentId':view['attachmentId'],'inputGrant':'0'*64,
            'inputEpoch':1,'frameSeq':1,'data':base64.b64encode(b'never delivered').decode()})
    assert not c.host.writes and not c.host.claims and c.broker_state.writers.gate.epoch==1


async def test_demotion_after_claim_refuses_old_input_even_with_stale_capabilities(preview_case):
    c=preview_case;assert grant(c)==CAPABILITIES
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app),base_url='http://fixture') as client:
        view=await attach(client,'demoted-holder',['writer','terminal'])
        minted=await take_and_claim(c,client,view)
        @write_intent
        def demote():
            with c.db() as db:db.get(User,1).role='member';db.commit()
        demote();c.state['who']=Principal('alex','member','fixture',1)
        refused=await call(client,'terminal.input',{'attachmentId':view['attachmentId'],'inputGrant':minted['inputGrant'],
            'inputEpoch':minted['inputEpoch'],'frameSeq':1,'data':base64.b64encode(b'never delivered').decode()})
        assert 'error' in refused,refused
    assert not c.host.writes
    with c.db() as db:assert db.get(RelaySession,SID).capabilities=={}
