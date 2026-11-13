"""Native text uses the full production Hub path, bound to its live input ticket."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_session_relay_public_writer import public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_terminal_grants import claim, frame, stream_case  # noqa: F401

from acc.server.db import write_in_thread
from acc.server.models import RelayOperation
from acc.session_relay import protocol as p


@pytest.fixture
async def native_case(stream_case):  # noqa: F811
    case = stream_case
    value = (await claim(case))['result']
    record = case.launch.terminal_grants.record
    # Model the physical Host's exact decoded Enter ticket. Byte/codec behavior
    # is separately qualified through real attach/tmux/CLI, not invented here.
    await write_in_thread(lambda: case.launch.terminal_grants.frame(
        record.resolve, value['inputGrant'], 2, 1, b'Artificial native text\r'))
    case.broker_state.adapter.record = replace(case.broker_state.adapter.record, qualification_id='fixture-native-ingress',
        qualification_date='2026-11-11', operations={'text': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('fixture only',))})
    ticket = SimpleNamespace(grant=record.value, sequence=1, text='Artificial native text', inflight=True)
    case.broker_state.terminal_host.ticket = ticket
    case.ticket = ticket
    return case


async def test_native_ingress_registers_full_identity_and_conclusive_receipt_without_direct_forward(native_case):
    c = native_case
    result = await c.broker_state.native_admit(c.ticket.grant.actor, 'turn.submit',
        {'text': c.ticket.text, 'expectedControlRevision': 2})
    assert result['state'] == 'accepted' and result['turn']['id']
    with c.db() as db:
        op = db.get(RelayOperation, result['operationId'])
        assert op.receipt['actor'] == p.to_wire(c.ticket.grant.actor)
        assert op.receipt['method'] == 'turn.submit' and op.receipt['state'] == 'accepted'
        assert op.receipt['payloadDigest'] == p.operation_digest('turn.submit', c.launch.terminal_grants.record.native.params)
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1
    assert await c.broker_state.native_processed(c.ticket.grant, 1, result)
    assert c.launch.terminal_grants.record.processed == 1
    assert c.broker_state.operation_origin == {}


async def test_duplicate_native_submit_only_observes_existing_receipt(native_case):
    c = native_case
    token = c.ticket.grant.grant
    first = await c.launch.terminal_grants.native_submit(token, 1, c.ticket.text, 2)
    second = await c.launch.terminal_grants.native_submit(token, 1, c.ticket.text, 2)
    assert first == second and first['state'] == 'accepted'
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1


async def test_native_cannot_select_an_actor_text_revision_or_unrelated_receipt(native_case):
    c = native_case
    for actor, params in ((p.Actor(p.ActorKind.PERSON, 'foreign', p.Channel.CHAT),
                           {'text': c.ticket.text, 'expectedControlRevision': 2}),
                          (c.ticket.grant.actor, {'text': 'foreign text', 'expectedControlRevision': 2}),
                          (c.ticket.grant.actor, {'text': c.ticket.text, 'expectedControlRevision': 1})):
        with pytest.raises(p.RelayError):
            await c.broker_state.native_admit(actor, 'turn.submit', params)
    with pytest.raises(p.RelayError):
        await write_in_thread(lambda: c.launch.terminal_grants.native_processed(c.ticket.grant.grant, 1, 'unrelated-operation'))
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)


async def test_host_transport_count_alone_cannot_prove_processing(stream_case):  # noqa: F811
    from test_session_relay_public_writer import call
    c = stream_case
    grant = (await claim(c))['result']
    async def false_ack(value, sequence, data):
        return {'partial': False, 'watermark': {'epoch': value.epoch, 'admitted': sequence, 'processed': sequence}}
    c.host.write_frames = false_ack
    assert 'error' in await call(c.client, 'terminal.input', frame(c, grant))
    assert c.launch.terminal_grants.record is None


async def test_bound_production_host_decodes_ticket_and_proves_processing_through_main(stream_case):  # noqa: F811
    c = stream_case
    await claim(c)
    record = c.launch.terminal_grants.record
    runtime = c.broker_state
    runtime.adapter.record = replace(runtime.adapter.record, qualification_id='fixture-composed-host',
        qualification_date='2026-11-11', operations={'text': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('fixture only',))})
    # This test supplies the owned physical opener/peer boundary, never claiming
    # native placement. The body after binding is the production composition.
    runtime.terminal_host = None
    class Writer:
        def write(self, data):
            pass
        async def drain(self):
            pass
        def close(self):
            pass
        async def wait_closed(self):
            pass
    async def opener(_run):
        return None, Writer()
    host = runtime.bind_terminal_host(opener, lambda _ws: False)
    await host.claim_input(record.value)
    await write_in_thread(lambda: c.launch.terminal_grants.frame(record.resolve, record.value.grant, 2, 1, b'Actual composed ticket'))
    await host.write_frames(record.value, 1, b'Actual composed ticket')
    await write_in_thread(lambda: c.launch.terminal_grants.acknowledge(record.resolve, record.value.grant,
        {'partial': False, 'watermark': {'epoch': 2, 'admitted': 1, 'processed': 0}}))
    await write_in_thread(lambda: c.launch.terminal_grants.frame(record.resolve, record.value.grant, 2, 2, b'\r'))
    await host.write_frames(record.value, 2, b'\r')
    result = await host.native_request('turn.submit', {'text': 'Actual composed ticket'}, 'fixture-native-message')
    assert result['state'] == 'accepted' and host.processed == record.processed == 2
    # The old delivery snapshot may arrive after the conclusive processing
    # callback. It cannot regress or independently advance processing.
    reply = await write_in_thread(lambda: c.launch.terminal_grants.acknowledge(record.resolve, record.value.grant,
        {'partial': False, 'watermark': {'epoch': 2, 'admitted': 2, 'processed': 0}}))
    assert reply['watermark']['processed'] == 2
    assert host.attested is False and runtime.native_socket.attested is False
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1


async def test_uncertain_native_admission_never_processes_or_retries(native_case):
    c = native_case
    original = c.owners[0].request
    async def unknown(method, params):
        if method == 'turn/start':
            c.owners[0].calls.append((method, params))
            return {}  # transport success without native admission evidence
        return await original(method, params)
    c.owners[0].request = unknown
    result = await c.broker_state.native_admit(c.ticket.grant.actor, 'turn.submit',
        {'text': c.ticket.text, 'expectedControlRevision': 2})
    assert result['state'] == 'uncertain'
    with pytest.raises((p.RelayError, ConnectionError)):
        await c.broker_state.native_processed(c.ticket.grant, 1, result)
    assert c.launch.terminal_grants.record is None or c.launch.terminal_grants.record.processed == 0
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1


async def test_conclusive_native_admission_survives_later_authority_loss_without_new_grant(native_case):
    from sqlalchemy import select
    c = native_case
    original = c.owners[0].request
    async def revoke_after_admission(method, params):
        result = await original(method, params)
        if method == 'turn/start':
            c.state['revoked'] = True
        return result
    c.owners[0].request = revoke_after_admission
    with pytest.raises((p.RelayError, ConnectionError)):
        await c.broker_state.native_admit(c.ticket.grant.actor, 'turn.submit',
            {'text': c.ticket.text, 'expectedControlRevision': 2})
    with c.db() as db:
        op = db.scalar(select(RelayOperation).where(RelayOperation.receipt['method'].as_string() == 'turn.submit'))
        assert op.receipt['state'] == 'accepted'
        assert op.receipt['actor'] == p.to_wire(c.ticket.grant.actor)
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1


async def test_native_client_exit_during_grant_await_refuses_before_transport_write(stream_case):  # noqa: F811
    c = stream_case
    await claim(c)
    runtime = c.broker_state
    runtime.terminal_host = None
    living = True
    async def unopened(_run):
        raise AssertionError('Dead native client must never open a transport')
    host = runtime.bind_terminal_host(unopened, lambda _ws: False, alive=lambda: living)
    grant = c.launch.terminal_grants.record.value
    await host.claim_input(grant)
    class ConnectedWriter:
        def write(self, _frame):
            raise AssertionError('Dead native client must never receive input')
        def close(self):
            pass
        async def wait_closed(self):
            pass
    host.writer = ConnectedWriter()  # already-open bridge, as after a delivered prefix
    original = runtime.terminal_value
    async def exited_during_authority(*args):
        nonlocal living
        value = await original(*args)
        living = False
        return value
    runtime.terminal_value = exited_during_authority
    with pytest.raises(p.RelayError) as failure:
        await host.write_frames(grant, 1, b'No dead-client input')
    assert failure.value.code is p.ErrorCode.STALE_AUTHORITY
    assert host.admitted == host.processed == 0 and host.input_fenced


@pytest.mark.parametrize('boundary', ['none', 'open', 'authority', 'drain', 'unreserved-sequence', 'policy', 'owner-budget'])
async def test_batched_public_frame_has_one_final_sequence_check_and_no_stale_ack(stream_case, boundary, monkeypatch):  # noqa: F811
    from test_session_relay_public_writer import SID
    c = stream_case
    claimed = (await claim(c))['result']
    record, runtime = c.launch.terminal_grants.record, c.broker_state
    runtime.terminal_host = None
    writes, checks = [], []
    living = True
    opened = False
    from acc.server import budgets
    original_paused = budgets.paused
    def paused(db, username, provider, **kwargs):
        if boundary == 'owner-budget' and opened:
            return ['Artificial owner budget pause']
        return original_paused(db, username, provider, **kwargs)
    monkeypatch.setattr(budgets, 'paused', paused)

    class Writer:
        def write(self, data):
            writes.append(data)
        async def drain(self):
            if boundary == 'drain':
                c.state['revoked'] = True
        def close(self):
            pass
        async def wait_closed(self):
            pass

    async def opener(_run):
        nonlocal opened
        opened = True
        if boundary == 'policy':
            from acc.server.models import SecurityRevision
            def tighten():
                with c.db() as db:
                    db.get(SecurityRevision, 1).revision += 1
                    db.commit()
            await write_in_thread(tighten)
        if boundary == 'open':
            c.state['revoked'] = True
        return None, Writer()

    host = runtime.bind_terminal_host(opener, lambda _ws: False, alive=lambda: living)
    await host.claim_input(record.value)
    original = runtime.terminal_value

    async def checked(token, action, sequence=None):
        nonlocal living
        checks.append((action, sequence))
        value = await original(token, action, sequence)
        if boundary == 'authority':
            living = False
        return value

    runtime.terminal_value = checked
    if boundary == 'unreserved-sequence':
        with pytest.raises(p.RelayError):
            await runtime.terminal_request('terminal.input', {'grant': record.value.grant,
                'sequence': 1, 'data': 'YWJj'})
    else:
        response = await c.client.post(f'/api/sessions/{SID}/relay', json={
            'jsonrpc': '2.0', 'id': 'batch', 'method': 'ry/terminal.input',
            'params': {'sessionId': SID, **frame(c, claimed), 'data': 'YmF0Y2hlZCBwcmVmaXg='}})
        result = response.json()
        if boundary == 'none':
            assert result['result']['watermark'] == {'epoch': 2, 'admitted': 1, 'processed': 0}
            assert len(writes) == 1
        else:
            assert response.status_code == 401 or 'error' in result
            assert c.launch.terminal_grants.record is None
    assert checks == [('input', 1)]
    assert len(writes) == (1 if boundary in ('none', 'drain') else 0)
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)
    assert runtime.input_frame.get() is None
