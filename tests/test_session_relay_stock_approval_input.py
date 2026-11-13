"""Physical Host boundaries with a fake main receipt bridge, no native qualification claim."""
from dataclasses import replace

import pytest
from test_session_relay_native_input_host import host_case  # noqa: F401

from acc.session_relay import protocol as p
from acc.session_relay.native_approval_aliases import ApprovalOffer


@pytest.fixture
async def stock_host(host_case,monkeypatch):  # noqa: F811
    c=host_case;h=c.host
    monkeypatch.setattr('acc.session_relay.stock_approval_input.STOCK_APPROVAL_INPUT_QUALIFIED',True)
    h.admitted=h.processed=1  # artificial completed prior turn, not native attestation
    c.offer=ApprovalOffer('alias','decision',p.sha256_digest(b'action'),0,frozenset({'accept','cancel'}),'{}')
    c.calls=[];c.applied=False;c.loss=False;c.early=False
    async def bridge(method,params):
        c.calls.append((method,params))
        if method=='native.approval_offer':return True
        if method=='native.approval_frame':
            return {'operationId':'original-operation','sessionId':'session','expectedRunId':'run',
                'decisionId':'decision','choiceId':params['choice'],'expectedRevision':0,'actionDigest':c.offer.action_digest}
        if method=='native.approval_reply':
            if c.early:
                c.applied=True
                await h.approval_input.completed(c.grant.grant,2,'original-operation')
            if c.loss:raise ConnectionError('lost response after possible dispatch')
            return {'state':'uncertain','operationId':'original-operation'}
        if method=='native.approval_processed':return c.applied
        raise AssertionError(method)
    h.approval_input.bridge=bridge
    await h.offer_native_approval(c.offer)
    return c


async def test_delivery_and_native_decode_do_not_process_before_main_application(stock_host):
    c=stock_host;h=c.host
    result=await h.write_frames(c.grant,2,b'\r')
    assert result.watermark.processed==1 and len(c.state['bytes'])==1
    assert (await h.native_approval_response(c.offer,'accept'))['state']=='uncertain'
    assert h.processed==1 and h.input_fenced
    with pytest.raises(p.RelayError):await h.approval_input.completed(c.grant.grant,2,'original-operation')
    c.applied=True
    assert await h.approval_input.completed(c.grant.grant,2,'original-operation')
    assert h.processed==2 and not h.input_fenced and h.approval_input.ticket is None


@pytest.mark.parametrize('fault',['before_frame','choice','offer','actor','revoked','sequence'])
async def test_unbound_native_response_never_reaches_main_cas(stock_host,fault):
    c=stock_host;h=c.host
    if fault!='before_frame':await h.write_frames(c.grant,2,b'\r')
    if fault=='actor':h.grant=replace(c.grant,actor=replace(c.grant.actor,principal='other-person'))
    if fault=='revoked':c.state['authorized']=False
    if fault=='sequence':h.approval_input.ticket=None  # no exact reserved frame
    with pytest.raises(p.RelayError):
        await h.native_approval_response(replace(c.offer) if fault=='offer' else c.offer,'cancel' if fault=='choice' else 'accept')
    assert not any(method=='native.approval_reply' for method,_ in c.calls)


@pytest.mark.parametrize('data',[b'y',b'\n',b'\r\r',b'\x1b[A',b'\x1b[13u',b'hello'])
async def test_only_one_qualified_byte_can_reserve_an_approval_frame(stock_host,data):
    c=stock_host
    with pytest.raises(p.RelayError):await c.host.write_frames(c.grant,2,data)
    assert not c.state['bytes'] and c.host.approval_input.ticket is None


async def test_early_conclusive_application_cannot_be_regressed_by_late_unknown_reply(stock_host):
    c=stock_host;c.early=True
    await c.host.write_frames(c.grant,2,b'\r')
    await c.host.native_approval_response(c.offer,'accept')
    assert c.host.processed==2 and not c.host.input_fenced and c.host.approval_input.ticket is None


async def test_lost_decode_reply_is_one_dispatch_and_never_replayed(stock_host):
    c=stock_host;c.loss=True
    await c.host.write_frames(c.grant,2,b'\r')
    with pytest.raises(ConnectionError):await c.host.native_approval_response(c.offer,'accept')
    with pytest.raises(p.RelayError):await c.host.native_approval_response(c.offer,'accept')
    assert sum(method=='native.approval_reply' for method,_ in c.calls)==1
    assert c.host.input_fenced and c.host.processed==1


async def test_authority_loss_during_main_frame_authorization_refuses_before_byte(stock_host):
    c=stock_host;bridge=c.host.approval_input.bridge
    async def revoked(method,params):
        value=await bridge(method,params)
        if method=='native.approval_frame':c.state['authorized']=False
        return value
    c.host.approval_input.bridge=revoked
    with pytest.raises(p.RelayError):await c.host.write_frames(c.grant,2,b'\r')
    assert not c.state['bytes'] and c.host.input_fenced
