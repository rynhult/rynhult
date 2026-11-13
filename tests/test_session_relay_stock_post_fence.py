"""Delivered stock-key authority failure fences locally before watch/withdrawal."""
# ruff: noqa: F811
import asyncio

import pytest
from test_session_relay_native_input_host import host_case  # noqa: F401
from test_session_relay_stock_approval_input import stock_host  # noqa: F401

from acc.session_relay import protocol as p


@pytest.mark.parametrize('fault',['refused','lost','cancelled'])
async def test_delivered_matching_ticket_fences_on_pre_cas_authority_failure(stock_host,monkeypatch,fault):
    c=stock_host;h=c.host
    await h.write_frames(c.grant,2,b'\r')
    ticket=h.approval_input.ticket
    async def failed(_grant):
        if fault=='refused':return False
        if fault=='lost':raise ConnectionError('Fixture loses authority verification')
        raise asyncio.CancelledError()
    monkeypatch.setattr(h,'validate_input',failed)
    with pytest.raises((p.RelayError,ConnectionError,asyncio.CancelledError)):
        await h.native_approval_response(c.offer,'accept')
    assert h.input_fenced and h.processed==1 and h.admitted==2
    assert h.approval_input.ticket is ticket and not ticket.inflight
    assert not any(method=='native.approval_reply' for method,_ in c.calls)
    c.applied=True
    with pytest.raises(p.RelayError):await h.approval_input.completed(c.grant.grant,2,ticket.operation_id)
    assert h.input_fenced and h.processed==1


@pytest.mark.parametrize('fault',['offer','choice'])
async def test_unbound_response_cannot_fence_the_legitimate_delivered_ticket(stock_host,fault):
    from dataclasses import replace
    c=stock_host;h=c.host
    await h.write_frames(c.grant,2,b'\r')
    with pytest.raises(p.RelayError):
        await h.native_approval_response(replace(c.offer) if fault=='offer' else c.offer,
            'cancel' if fault=='choice' else 'accept')
    assert not h.input_fenced and not h.approval_input.ticket.inflight and h.processed==1
    assert not any(method=='native.approval_reply' for method,_ in c.calls)
    assert (await h.native_approval_response(c.offer,'accept'))['state']=='uncertain'
    assert h.input_fenced and h.processed==1
