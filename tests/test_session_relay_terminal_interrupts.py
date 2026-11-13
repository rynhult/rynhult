# ruff: noqa: F811
"""Terminal cancellation uses exact public registration, never raw control bytes."""
import asyncio
from dataclasses import replace

import httpx
import pytest
from test_session_relay_native_ingress import native_case  # noqa: F401
from test_session_relay_public_interrupts import mutate, native_calls
from test_session_relay_public_writer import SID, attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_terminal_grants import stream_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayOwnership, RelaySession
from acc.session_relay import protocol as p


@pytest.fixture
async def terminal_interrupt_case(native_case):
    c = native_case
    @write_intent
    def qualify():
        with c.db() as db:
            row = db.get(RelaySession, SID)
            row.capabilities = {**row.capabilities, 'interrupt': 'qualified'}
            db.commit()
    qualify()
    c.broker_state.adapter.record = replace(c.broker_state.adapter.record, operations={
        **c.broker_state.adapter.record.operations,
        'interrupt': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('artificial Terminal interrupt only',))})
    result = await c.broker_state.native_admit(c.ticket.grant.actor, 'turn.submit',
        {'text': c.ticket.text, 'expectedControlRevision': 2})
    c.active = result['operationId']
    return c


async def test_terminal_holder_interrupts_its_native_target_without_new_input_stream(terminal_interrupt_case):
    c = terminal_interrupt_case
    record = c.launch.terminal_grants.record
    payload, identifier = await mutate(c.client, c, 'turn.interrupt', {
        'turnId': c.active, 'expectedControlRevision': 2}, 'terminal-interrupt')
    result = await call(c.client, 'turn.interrupt', {**payload, 'operationId': identifier})
    assert result['result']['state'] == 'accepted', result
    assert result['result']['payloadDigest'] == p.operation_digest('turn.interrupt', payload)
    assert result['result']['actor'] == p.to_wire(c.ticket.grant.actor)
    assert await call(c.client, 'turn.interrupt', {**payload, 'operationId': identifier}) == result
    assert native_calls(c) == [('turn/interrupt', {'threadId': 'fixture-native-id',
        'turnId': c.broker_state.adapter.turns[c.active]})]
    assert c.launch.terminal_grants.record is record and len(c.host.claims) == 1
    assert c.host.writes == [] and not c.launch.grants
    assert (await call(c.client, 'session.snapshot', {}))['result']['activeTurnId'] == c.active


@pytest.mark.parametrize('boundary', ['stage', 'probe'])
async def test_terminal_origin_is_frozen_across_awaited_owner_change(terminal_interrupt_case, boundary):
    c = terminal_interrupt_case
    reached, release = asyncio.Event(), asyncio.Event()
    original_call, original_probe = c.launch.peer.call, c.broker_state.adapter.probe
    async def held_call(method, params):
        result = await original_call(method, params)
        if method == 'stage' and boundary == 'stage':
            reached.set(); await release.wait()
        return result
    async def held_probe(binding):
        result = await original_probe(binding)
        if boundary == 'probe':
            reached.set(); await release.wait()
        return result
    payload, identifier = await mutate(c.client, c, 'turn.interrupt', {
        'turnId': c.active, 'expectedControlRevision': 2}, 'stale-terminal')
    c.launch.peer.call, c.broker_state.adapter.probe = held_call, held_probe
    pending = asyncio.create_task(call(c.client, 'turn.interrupt', {**payload, 'operationId': identifier}))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        @write_intent
        def replace_owner():
            with c.db() as db:
                db.get(RelayOwnership, SID).view = 'chat'
                db.commit()
        replace_owner()
    finally:
        release.set()
    response = await asyncio.wait_for(pending, 5)
    assert 'error' in response or response['result']['state'] == 'failed', response
    assert native_calls(c) == [] and c.broker_state.adapter.active_turn == c.active


async def test_other_tab_cannot_cancel_terminal_writer(terminal_interrupt_case):
    c = terminal_interrupt_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as other:
        await attach(other, 'terminal-observer', ['writer'])
        payload, identifier = await mutate(other, c, 'turn.interrupt', {
            'turnId': c.active, 'expectedControlRevision': 2}, 'observer-terminal-interrupt')
        result = await call(other, 'turn.interrupt', {**payload, 'operationId': identifier})
        assert result['error']['data']['code'] == 'STALE_AUTHORITY', result
    assert native_calls(c) == []
