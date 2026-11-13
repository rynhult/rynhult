"""Processing requires exact decoder consumption and conclusive admission, never drain."""
import asyncio
from types import SimpleNamespace

import pytest

from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import InputGrant, TerminalRun
from acc.session_relay.native_input_host import NativeInputHost
from acc.session_relay.writer_routes import WriterRoutes

PERSON = p.Actor(p.ActorKind.PERSON, 'fixture-person', p.Channel.TERMINAL)


@pytest.fixture
async def host_case():
    state = {'authorized': True, 'idle': True, 'processed': [], 'bytes': [], 'admissions': []}
    async def allow(*_):
        return state['authorized']
    async def idle():
        return state['idle']
    async def processed(grant, sequence, result):
        state['processed'].append(sequence)
        return True
    async def admit(actor, method, params):
        state['admissions'].append((actor, method, params))
        return {'state': 'accepted', 'turn': {'id': 'fixture-turn'}}
    async def retire(lease):
        return await host.retire(lease)
    async def observe(*_):
        return {}
    routes = WriterRoutes(run_id='run', execution_identity='fixture-owner', authorize=allow,
                          retire=retire, admit=admit, read=observe)
    await routes.take(PERSON, 'terminal')
    class Transport:
        def write(self, data):
            state['bytes'].append(data)
        async def drain(self):
            pass
        def close(self):
            pass
        async def wait_closed(self):
            pass
    async def opener(*_):
        return asyncio.StreamReader(), Transport()
    run = TerminalRun('session', 'run')
    host = NativeInputHost(run, opener, allow, allow, validate_read=allow, routes=routes,
                          idle=idle, processed=processed)
    grant = InputGrant(run, 'attachment', PERSON, 'fixture-opaque-grant', 1, p.Fence(1, 'broker', 'main'))
    await host.claim_input(grant)
    yield SimpleNamespace(host=host, grant=grant, routes=routes, state=state)
    await host.detach(host.run)


async def test_exact_decoder_consumption_is_the_only_processing_barrier(host_case):
    c = host_case
    typed = await c.host.write_frames(c.grant, 1, b'fixture text')
    entered = await c.host.write_frames(c.grant, 2, b'\r')
    assert typed.watermark.processed == entered.watermark.processed == 0
    with pytest.raises(p.RelayError):
        await c.host.drain(c.grant, 2)
    result = await c.host.native_request('turn.submit', {'text': 'fixture text'}, 'native-message')
    assert result['state'] == 'accepted'
    watermark = await c.host.drain(c.grant, 2)
    assert watermark.admitted == watermark.processed == 2
    assert c.state['processed'] == [2]
    assert c.state['admissions'][0][0] == PERSON
    await c.routes.take(PERSON, 'chat')
    with pytest.raises(p.RelayError):
        await c.host.write_frames(c.grant, 3, b'late')
    with pytest.raises(p.RelayError):
        await c.host.native_request('turn.submit', {'text': 'fixture text'}, 'late-native-message')
    assert len(c.state['admissions']) == 1
    assert c.host.attested is False


@pytest.mark.parametrize('data', [b'/model', b'  !command', b'?', b'  ?help', b'\x1by', b'\x03', b'\xc3\xa9', b'a\rb', b'\r'])
async def test_unsupported_controls_are_refused_before_transport(host_case, data):
    c = host_case
    with pytest.raises(p.RelayError):
        await c.host.write_frames(c.grant, 1, data)
    assert not c.state['bytes'] and not c.state['admissions']
    assert c.host.admitted == c.host.processed == 0


async def test_mismatched_native_text_cannot_be_attributed_to_the_ticket_actor(host_case):
    c = host_case
    await c.host.write_frames(c.grant, 1, b'expected')
    await c.host.write_frames(c.grant, 2, b'\r')
    with pytest.raises(p.RelayError):
        await c.host.native_request('turn.submit', {'text': 'foreign'}, 'native-message')
    assert not c.state['admissions'] and not c.state['processed']
    assert c.routes.gate.recovery_required and c.host.input_fenced


async def test_pending_or_unknown_native_request_never_allows_retirement(host_case):
    c = host_case
    await c.host.write_frames(c.grant, 1, b'fixture')
    await c.host.write_frames(c.grant, 2, b'\r')
    async def uncertain(*_):
        return {'state': 'uncertain'}
    c.routes.admit = uncertain
    with pytest.raises(p.RelayError):
        await c.host.native_request('turn.submit', {'text': 'fixture'}, 'unknown-native')
    assert not c.state['processed']
    with pytest.raises(p.RelayError):
        await c.routes.take(PERSON, 'chat')
    assert c.host.processed == 0


async def test_old_native_message_id_cannot_consume_an_identical_new_ticket(host_case):
    c = host_case
    for sequence in (1, 3):
        await c.host.write_frames(c.grant, sequence, b'fixture')
        await c.host.write_frames(c.grant, sequence + 1, b'\r')
        if sequence == 3:
            with pytest.raises(p.RelayError):
                await c.host.native_request('turn.submit', {'text': 'fixture'}, 'old-native')
        await c.host.native_request('turn.submit', {'text': 'fixture'}, 'old-native' if sequence == 1 else 'new-native')
    assert c.state['processed'] == [2, 4] and len(c.state['admissions']) == 2


async def test_authority_loss_after_conclusive_admission_cannot_ack_or_restore_input(host_case):
    c = host_case
    await c.host.write_frames(c.grant, 1, b'fixture')
    await c.host.write_frames(c.grant, 2, b'\r')
    async def revoke(*_):
        c.state['authorized'] = False
        return True
    c.host.on_processed = revoke
    with pytest.raises(p.RelayError):
        await c.host.native_request('turn.submit', {'text': 'fixture'}, 'native-message')
    assert c.host.processed == 0 and c.host.input_fenced
    assert c.routes.gate.recovery_required


async def test_unverified_idle_cannot_retire_even_an_empty_host(host_case):
    c = host_case
    c.state['idle'] = False
    with pytest.raises(p.RelayError):
        await c.routes.take(PERSON, 'chat')
    assert c.host.input_fenced and c.routes.gate.recovery_required
    assert c.routes.gate.epoch == 1


async def test_native_id_capacity_refuses_instead_of_forgetting_old_requests(host_case):
    c = host_case
    c.host.capacity = 1
    for sequence in (1, 3):
        await c.host.write_frames(c.grant, sequence, b'fixture')
        await c.host.write_frames(c.grant, sequence + 1, b'\r')
        if sequence == 1:
            await c.host.native_request('turn.submit', {'text': 'fixture'}, 'old')
        else:
            with pytest.raises(p.RelayError):
                await c.host.native_request('turn.submit', {'text': 'fixture'}, 'new')
    assert c.host.processed == 2 and c.host.input_fenced
    assert len(c.state['admissions']) == 1 and c.host.seen == {'old'}


async def test_retired_host_keeps_unsent_draft_and_can_never_claim_a_later_grant(host_case):
    from dataclasses import replace
    c = host_case
    await c.host.write_frames(c.grant, 1, b'keep this draft')
    proof = await c.host.retire(c.routes.gate.lease)
    assert proof.draft_empty is False and proof.channel_closed is True
    assert c.host.retired and c.host.draft == 'keep this draft'
    assert c.host.admitted == 1 and c.host.processed == 0
    # Even a freshly valid later epoch cannot reactivate this particular Host.
    for grant in (c.grant, replace(c.grant, epoch=2, grant='fresh-opaque-grant')):
        with pytest.raises(p.RelayError):
            await c.host.claim_input(grant)
        with pytest.raises(p.RelayError):
            await c.host.write_frames(grant, 2, b'\r')
    with pytest.raises(p.RelayError):
        await c.host.native_request('turn.submit', {'text': 'keep this draft'}, 'late-native')
    assert c.state['bytes'] and not c.state['admissions'] and not c.state['processed']
    assert c.host.draft == 'keep this draft'


async def test_entered_but_unconsumed_native_ticket_cannot_be_retained_as_an_unsent_draft(host_case):
    c = host_case
    await c.host.write_frames(c.grant, 1, b'pending native request')
    await c.host.write_frames(c.grant, 2, b'\r')
    with pytest.raises(p.RelayError):
        await c.host.retire(c.routes.gate.lease)
    assert c.host.ticket is not None and c.host.processed == 0
    assert not c.host.retired  # unknown admission cannot produce a retirement proof


async def test_even_empty_retired_host_cannot_rebind_to_a_valid_future_terminal_lease(host_case):
    from dataclasses import replace
    c = host_case
    proof = await c.host.retire(c.routes.gate.lease)
    assert proof.draft_empty and c.host.retired
    async def fixture_retirement(lease):
        return replace(proof, handle=lease.handle)
    c.routes.retire = fixture_retirement
    await c.routes.take(PERSON, 'terminal')
    new = replace(c.grant, epoch=2, grant='valid-new-epoch')
    c.routes.gate.admit(c.routes.gate.lease, actor=new.actor, view='terminal')
    assert await c.host.validate_input(new) is True
    with pytest.raises(p.RelayError):
        await c.host.claim_input(new)
    assert c.host.retired and c.host.input_fenced and not c.state['admissions']


async def test_input_bridge_consumes_duplicate_tty_output_without_processing_input(host_case):
    from acc.broker.attach import frame
    c = host_case
    await c.host.write_frames(c.grant, 1, b'unsent')
    reader = c.host.reader
    for _ in range(32):
        reader.feed_data(frame(b'd', b'x' * 65536))
    for _ in range(100):
        if not reader._buffer:
            break
        await asyncio.sleep(0)
    assert not reader._buffer  # no unread duplicate output can backpressure tmux
    assert c.host.processed == 0 and c.host.draft == 'unsent'
    assert not c.state['processed'] and not c.state['admissions']
    task = c.host.output_task
    await c.host.detach(c.host.run)
    assert task.done() and c.host.output_task is None


async def test_input_bridge_output_eof_fences_input_without_claiming_execution_exit(host_case):
    from acc.session_relay.adapters.base import ExitState
    c = host_case
    await c.host.write_frames(c.grant, 1, b'unsent')
    c.host.reader.feed_eof()
    await c.host.output_task
    assert c.host.partial and c.host.exit.state is ExitState.UNKNOWN
    with pytest.raises(p.RelayError):
        await c.host.write_frames(c.grant, 2, b'late')
    assert c.host.processed == 0 and not c.state['admissions']


@pytest.mark.parametrize('data', [b'\x08', b'\x7f', b'a\x7f\x7f', *[x.encode() for x in '/!?#@&']])
async def test_security_a3_refused_frames_never_reach_native_bytes(host_case, data):
    c = host_case
    with pytest.raises(p.RelayError) as error:
        await c.host.write_frames(c.grant, 1, data)
    assert error.value.code is p.ErrorCode.UNSUPPORTED
    assert not c.state['bytes'] and not c.state['admissions']
    assert c.host.draft == '' and c.host.ticket is None
