"""Approved exclusive Codex control: scoped routes, live reads, no native guessing."""
import json

import pytest
from test_session_relay_native_socket import initialize
from test_session_relay_runtime import runtime_case  # noqa: F401
from websockets.asyncio.client import unix_connect

from acc.server.db import write_intent
from acc.server.models import RelayOwnership, RelaySession
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p
from acc.session_relay.native_socket import NativeSocket
from acc.session_relay.writer_lease import WriterRetirement
from acc.session_relay.writer_routes import WriterRoutes

ALICE = p.Actor(p.ActorKind.PERSON, 'alice', p.Channel.CHAT)
BOB = p.Actor(p.ActorKind.PERSON, 'bob', p.Channel.PHONE)
SID = 'abc123def456'


async def allow(actor):
    return True


async def retire(lease):
    return WriterRetirement(lease.handle, True, True, True, True, 'fixture-incarnation')


async def test_two_native_connections_keep_reading_but_retired_routes_cannot_mutate(tmp_path):
    calls = []
    async def read(method, params):
        return {'thread': {'id': 'native'}}
    async def admit(actor, method, params):
        calls.append((actor, method, params))
        return {'state': 'accepted'}
    routes = WriterRoutes(run_id='run', execution_identity='fixture-incarnation',
                          authorize=allow, retire=retire, admit=admit, read=read)
    await routes.take(ALICE, 'chat')
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    async def authorize():
        return True
    proxy = NativeSocket(directory, 'native', read, authorize, initialized={'userAgent': 'fixture'})
    await proxy.start()
    try:
        async with unix_connect(str(proxy.path), compression=None) as observer:
            await initialize(observer)
            terminal = await routes.take(ALICE, 'terminal')
            def bind(websocket):
                async def submit(method, params):
                    return await routes.mutate(terminal, method, params)
                return submit
            proxy.writer_route = bind
            async with unix_connect(str(proxy.path), compression=None) as writer:
                await initialize(writer)
                turn = {'id': 1, 'method': 'turn/start', 'params': {'threadId': 'native',
                        'input': [{'type': 'text', 'text': 'artificial'}]}}
                await writer.send(json.dumps(turn))
                assert 'result' in json.loads(await writer.recv())
                await routes.take(BOB, 'chat')
                await writer.send(json.dumps(turn))
                assert 'error' in json.loads(await writer.recv())
                await observer.send(json.dumps(turn))
                assert 'error' in json.loads(await observer.recv())
                await proxy.publish('turn/completed', {'threadId': 'native', 'turn': {'id': 'turn1'}})
                for client in (observer, writer):
                    assert json.loads(await client.recv())['method'] == 'turn/completed'
                    await client.send(json.dumps({'id': 2, 'method': 'thread/read', 'params': {'threadId': 'native'}}))
                    assert json.loads(await client.recv())['result']['thread']['id'] == 'native'
        assert len(calls) == 1 and calls[0][0] == ALICE
        assert routes.snapshot()['holder']['principal'] == 'bob'
        assert proxy.attested is False
    finally:
        await proxy.close()


async def test_unknown_mutation_result_blocks_takeover_and_further_input():
    async def admit(*args):
        return {'state': 'uncertain'}
    async def read(*args):
        return 'still readable'
    routes = WriterRoutes(run_id='run', execution_identity='fixture-incarnation',
                          authorize=allow, retire=retire, admit=admit, read=read)
    first = await routes.take(ALICE, 'chat')
    await routes.mutate(first, 'turn.submit', {'text': 'artificial'})
    with pytest.raises(p.RelayError):
        await routes.take(BOB, 'terminal')
    with pytest.raises(p.RelayError):
        await routes.mutate(first, 'turn.submit', {'text': 'later'})
    assert await routes.observe('thread/read', {}) == 'still readable'


async def take(case, view, key, revision):
    params = {'sessionId': SID, 'expectedRunId': str(case.launch.run_id), 'view': view,
              'expectedRevision': revision, 'reason': 'Artificial person take-over'}
    registration = Hub(case.ctx).register(case.resolve, SID, str(case.launch.run_id), key,
                                         'control.take', p.operation_digest('control.take', params))
    return await case.manager.take_writer(case.resolve, SID, {**params, 'operationId': registration.operation_id})


async def test_production_broker_opaque_take_changes_writer_without_restarting_app_server(runtime_case):  # noqa: F811
    case = runtime_case
    await case.launch.launch(SID)
    state = next(iter(case.factory.active.values()))
    original = state.owner
    @write_intent
    def qualify_fixture():
        with case.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
            db.commit()
    qualify_fixture()
    state.writers.retire = retire
    assert state.writers.gate.lease.view == 'chat'
    receipt = await take(case, 'terminal', 'terminal-take', 1)
    assert receipt.state.value == 'accepted'
    assert state.writers.snapshot()['writer'] == 'terminal'
    with case.db() as db:
        owner = db.get(RelayOwnership, SID)
        assert owner.view == 'terminal' and owner.phase == 'owned'
    await take(case, 'chat', 'chat-take', 2)
    assert state.writers.snapshot()['epoch'] == 3
    with pytest.raises(p.RelayError):
        Hub(case.ctx).take(case.resolve, SID, revision=3, reason='Legacy path fixture', drain_settled=True)
    assert state.owner is original and len(case.owners) == 1
    assert sum(method == 'thread/start' for method, _ in original.calls) == 1


async def test_unqualified_take_is_pre_effect_refusal_not_a_poisoned_session(runtime_case):  # noqa: F811
    case = runtime_case
    await case.launch.launch(SID)
    with pytest.raises(p.RelayError):
        await take(case, 'terminal', 'unqualified', 1)
    with case.db() as db:
        assert not db.get(RelaySession, SID).sealed
        assert db.get(RelayOwnership, SID).view == 'chat'


async def test_initial_writer_is_the_explicit_launch_person_and_does_not_launch_another_owner(runtime_case):  # noqa: F811
    import traceback
    case = runtime_case
    errors = []
    original = case.launch.handle
    async def recorded(method, params):
        try:
            return await original(method, params)
        except Exception:
            errors.append(traceback.format_exc())
            raise
    case.launch.handle = recorded
    from acc.broker.relay_runtime import BrokerRuntime
    previous = BrokerRuntime.handle
    async def broker_recorded(self, method, params):
        try:
            return await previous(self, method, params)
        except Exception:
            errors.append(traceback.format_exc())
            raise
    from unittest.mock import patch
    with patch.object(BrokerRuntime, 'handle', broker_recorded):
        try:
            await case.launch.launch(SID)
        except Exception as error:
            raise AssertionError('Artificial writer fixture: ' + '\n'.join(errors)) from error
    state = next(iter(case.factory.active.values()))
    assert state.writers.snapshot()['writer'] == 'chat'
    assert state.writers.gate.lease.actor == case.resolve()[1]
    assert len(case.owners) == 1
    async with unix_connect(state.native_socket.connect_path, compression=None) as reader:
        await initialize(reader)
        await reader.send(json.dumps({'id': 2, 'method': 'thread/read', 'params': {'threadId': state.binding.native_conversation}}))
        assert 'result' in json.loads(await reader.recv())


async def test_delayed_chat_send_keeps_old_epoch_after_roundtrip_and_cannot_reach_native(runtime_case):  # noqa: F811
    case = runtime_case
    await case.launch.launch(SID)
    state = next(iter(case.factory.active.values()))
    @write_intent
    def qualify_fixture():
        with case.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
            db.commit()
    qualify_fixture()
    state.writers.retire = retire
    await take(case, 'terminal', 'terminal', 1)
    await take(case, 'chat', 'chat', 2)
    params = {'sessionId': SID, 'expectedRunId': str(case.launch.run_id), 'text': 'Delayed old draft',
              'expectedConfigurationRevision': 0, 'expectedControlRevision': 1}
    registered = Hub(case.ctx).register(case.resolve, SID, str(case.launch.run_id), 'delayed',
                                       'turn.submit', p.operation_digest('turn.submit', params))
    with pytest.raises(p.RelayError):
        await case.manager.submit(case.resolve, SID, {**params, 'operationId': registered.operation_id})
    assert not any(method == 'turn/start' for method, _ in state.owner.calls)


async def test_revoked_takeover_after_retirement_seals_without_another_native_owner(runtime_case):  # noqa: F811
    case = runtime_case
    await case.launch.launch(SID)
    state = next(iter(case.factory.active.values()))
    @write_intent
    def qualify_fixture():
        with case.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
            db.commit()
    qualify_fixture()
    async def revoked(lease):
        result = await retire(lease)
        case.state['revoked'] = True
        return result
    state.writers.retire = revoked
    with pytest.raises(Exception):
        await take(case, 'terminal', 'revoked', 1)
    with case.db() as db:
        assert db.get(RelaySession, SID).sealed
    assert len(case.owners) == 1


async def test_explicit_same_person_take_rotates_epoch_and_never_rebinds_an_old_route():
    async def admit(*args):
        return {'state': 'accepted'}
    async def read(*args):
        return 'readable'
    routes = WriterRoutes(run_id='run', execution_identity='fixture-incarnation',
                          authorize=allow, retire=retire, admit=admit, read=read)
    old = await routes.take(ALICE, 'chat')
    current = await routes.take(ALICE, 'chat')
    assert current.lease.epoch == old.lease.epoch + 1
    with pytest.raises(p.RelayError):
        await routes.mutate(old, 'turn.submit', {'text': 'delayed'})
    assert (await routes.mutate(current, 'turn.submit', {'text': 'current'}))['state'] == 'accepted'


async def test_production_writer_reads_are_scoped_redacted_and_cannot_tunnel_mutations(runtime_case):  # noqa: F811
    case = runtime_case
    await case.launch.launch(SID)
    state = next(iter(case.factory.active.values()))
    calls = []
    async def read(method, params):
        calls.append((method, params))
        return {'config': {'model': 'fixture', 'developer_instructions': 'private fixture instruction'}, 'origins': {'private': '/fixture'}}
    state.owner.request = read
    before = state.writers.gate.lease
    result = await state.writers.observe('config/read', {'includeLayers': True, 'cwd': '/foreign-fixture'})
    assert result == {'config': {'model': 'fixture'}, 'origins': {}}
    assert calls == [('config/read', {'includeLayers': False})]
    with pytest.raises(p.RelayError):
        await state.writers.observe('turn/start', {'threadId': state.binding.native_conversation})
    with pytest.raises(p.RelayError):
        await state.writers.observe('thread/read', {'threadId': 'foreign-fixture'})
    assert len(calls) == 1 and state.writers.gate.lease is before


async def test_bad_or_expired_opaque_take_grant_cannot_fall_back_to_the_current_holder(runtime_case):  # noqa: F811
    from dataclasses import replace
    case = runtime_case
    await case.launch.launch(SID)
    with pytest.raises(p.RelayError):
        await case.launch.handle('authority.writer', {'actor': p.to_wire(case.resolve()[1]), 'grant': 'invalid'})
    state = next(iter(case.factory.active.values()))
    @write_intent
    def qualify_fixture():
        with case.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
            db.commit()
    qualify_fixture()
    old = state.writers.gate.lease
    async def expire(lease):
        result = await retire(lease)
        active = next(iter(case.launch.grants.values()))
        active.operation = replace(active.operation, deadline_ms=0)
        return result
    state.writers.retire = expire
    with pytest.raises(Exception):
        await take(case, 'terminal', 'expired', 1)
    assert state.writers.gate.lease is old
    assert len(case.owners) == 1
