"""Real private Unix/WebSocket mediation; no native provider or account."""
import json
import os
from pathlib import Path

import pytest
from websockets.asyncio.client import unix_connect

from acc.session_relay.native_socket import NativeSocket
from acc.session_relay.protocol import RelayError


@pytest.fixture
async def native_socket(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    reads = []
    allowed = [True]
    async def read(method, params):
        reads.append((method, params))
        return {'thread': {'id': 'native'}, 'config': {'model': 'fixture', 'developer_instructions': 'private-system-prompt'}}
    async def authorize():
        return allowed[0]
    proxy = NativeSocket(directory, 'native', read, authorize, initialized={'userAgent': 'fixture'})
    await proxy.start()
    yield proxy, reads, allowed
    await proxy.close()
    assert not proxy.path.exists()


async def initialize(client):
    await client.send(json.dumps({'id': 'initialize', 'method': 'initialize', 'params': {
        'clientInfo': {'name': 'codex-tui', 'version': '0.167.1'},
        'capabilities': {'experimentalApi': True, 'requiredHumanApproval': True}}}))
    assert 'result' in json.loads(await client.recv())


async def test_native_unix_proxy_refuses_uncorrelated_turns_config_and_approval_replies(native_socket):
    proxy, reads, _ = native_socket
    assert proxy.path.stat().st_mode & 0o777 == 0o600
    assert proxy.attested is False
    async with unix_connect(str(proxy.path), compression=None) as client:
        await initialize(client)
        messages = [
            {'id': 1, 'method': 'turn/start', 'params': {'threadId': 'native', 'input': [{'type': 'text', 'text': 'fixture'}]}},
            {'id': 2, 'method': 'config/value/write', 'params': {'keyPath': 'approval_policy', 'value': 'never'}},
            {'id': 3, 'result': {'decision': 'accept'}},
            {'id': 4, 'method': 'thread/start', 'params': {}},
            {'id': 5, 'method': 'thread/resume', 'params': {'threadId': 'other'}},
            {'id': 6, 'method': 'thread/read', 'params': {'threadId': 'native', 'actor': 'forged'}},
        ]
        for request in messages:
            await client.send(json.dumps(request))
            assert 'error' in json.loads(await client.recv())
    assert reads == []


async def test_native_config_projection_omits_private_instructions_and_auth_rechecks_after_read(native_socket):
    proxy, reads, allowed = native_socket
    async with unix_connect(str(proxy.path), compression=None) as client:
        await initialize(client)
        await client.send(json.dumps({'id': 1, 'method': 'config/read', 'params': {}}))
        response = json.loads(await client.recv())
        assert response['result'] == {'config': {'model': 'fixture'}, 'origins': {}}
        assert 'private-system-prompt' not in str(response)
        async def revoke(method, params):
            allowed[0] = False
            return {'thread': {'id': 'native'}}
        proxy.read = revoke
        await client.send(json.dumps({'id': 2, 'method': 'thread/read', 'params': {'threadId': 'native'}}))
        assert 'error' in json.loads(await client.recv())


async def test_native_socket_directory_swap_cannot_reuse_the_listener(native_socket, tmp_path):
    proxy, *_ = native_socket
    original = proxy.directory
    moved = tmp_path / 'moved'
    original.rename(moved)
    original.mkdir(mode=0o700)
    with pytest.raises((RelayError, FileNotFoundError)):
        proxy.validate()
    os.rmdir(original)
    moved.rename(original)


async def test_native_read_cannot_send_an_unbounded_history_frame(native_socket):
    proxy, *_ = native_socket
    async def oversized(*_):
        return {'thread': {'id': 'native', 'turns': ['x' * (1 << 20)]}}
    proxy.read = oversized
    async with unix_connect(str(proxy.path), compression=None) as client:
        await initialize(client)
        await client.send(json.dumps({'id': 1, 'method': 'thread/read', 'params': {'threadId': 'native'}}))
        response = json.loads(await client.recv())
        assert 'error' in response and 'result' not in response


@pytest.mark.relay_native
async def test_captured_stock_native_turn_has_no_actor_grant_and_is_refused(native_socket):
    proxy, reads, _ = native_socket
    evidence = Path(__file__).resolve().parents[1] / 'docs/handoffs/2026-11-10-relay-m3-native-tui.json'
    frames = json.loads(evidence.read_text())['first_frames']
    request = next(frame for frame in frames if frame.get('method') == 'turn/start')
    assert request['params']['clientUserMessageId']
    assert not {'actor', 'inputEpoch', 'processedWatermark'} & request['params'].keys()
    proxy.native_id = request['params']['threadId']
    async with unix_connect(str(proxy.path), compression=None) as client:
        await initialize(client)
        await client.send(json.dumps(request))
        assert 'error' in json.loads(await client.recv())
    assert not reads


async def test_closed_observer_during_notification_does_not_break_other_readers(native_socket):
    from websockets.exceptions import ConnectionClosed
    proxy, _, _ = native_socket
    class Closed:
        async def send(self, data):
            raise ConnectionClosed(None, None)
        async def close(self, **kwargs):
            pass
    closed = Closed()
    async with unix_connect(str(proxy.path), compression=None) as reader:
        await initialize(reader)
        proxy.clients.add(closed)
        try:
            await proxy.publish('turn/completed', {'threadId': 'native', 'turn': {'id': 'turn1'}})
            assert json.loads(await reader.recv())['method'] == 'turn/completed'
            await reader.send(json.dumps({'id': 1, 'method': 'thread/read', 'params': {'threadId': 'native'}}))
            assert 'result' in json.loads(await reader.recv())
        finally:
            proxy.clients.discard(closed)


async def test_stock_resume_null_overrides_are_projected_but_changes_refused(native_socket):
    proxy, reads, _ = native_socket
    async with unix_connect(str(proxy.path), compression=None) as client:
        await initialize(client)
        await client.send(json.dumps({'method': 'initialized', 'params': None}))
        for request_id, params in enumerate((
            {'threadId': 'native', 'model': None, 'config': None, 'excludeTurns': True},
            {'threadId': 'native', 'model': 'other'},
            {'threadId': 'native', 'config': {'approval_policy': 'never'}},
            {'threadId': 'native', 'excludeTurns': 'true'},
            {'threadId': 'native', 'unexpected': None},
        )):
            await client.send(json.dumps({'id': request_id, 'method': 'thread/resume', 'params': params}))
            reply = json.loads(await client.recv())
            assert ('result' in reply) == (request_id == 0)
    assert reads == [('thread/resume', {'threadId': 'native'})]


async def test_stock_history_reads_are_scoped_and_bounded(native_socket):
    proxy, reads, _ = native_socket
    async with unix_connect(str(proxy.path), compression=None) as client:
        await initialize(client)
        cases = [
            ('thread/turns/list', {'threadId': 'native', 'limit': 5, 'itemsView': 'notLoaded'}, True),
            ('thread/items/list', {'threadId': 'native', 'turnId': None, 'limit': 100}, True),
            ('thread/items/list', {'threadId': 'other', 'turnId': None}, False),
            ('thread/turns/list', {'threadId': 'native', 'itemsView': 'unreviewed'}, False),
            ('thread/items/list', {'threadId': 'native', 'limit': 101}, False),
            ('thread/items/list', {'threadId': 'native', 'limit': True}, False),
            ('thread/items/list', {'threadId': 'native', 'cursor': 'unscoped'}, False),
        ]
        for index, (method, params, succeeds) in enumerate(cases):
            await client.send(json.dumps({'id': index, 'method': method, 'params': params}))
            assert ('result' in json.loads(await client.recv())) is succeeds
    assert len(reads) == 2


async def test_native_frozen_turn_envelope_is_checked_before_owned_route(native_socket):
    from acc.session_relay.native_turn import NativeTurnPolicy
    proxy, _, _ = native_socket
    proxy.turn_policy = NativeTurnPolicy.from_start('native', {'thread': {'id': 'native'},
        'model': 'fixture', 'approvalPolicy': 'untrusted'})
    admitted = []
    async def route(method, params):
        admitted.append((method, params))
        return {'state': 'accepted', 'turn': {'id': 'turn1'}}
    proxy.writer_route = lambda _websocket: route
    async with unix_connect(str(proxy.path), compression=None) as client:
        await initialize(client)
        base = {'threadId': 'native', 'model': 'fixture', 'approvalPolicy': 'untrusted',
                'input': [{'type': 'text', 'text': 'fixture', 'text_elements': []}]}
        for index, changes in enumerate(({}, {'model': 'other'}, {'approvalPolicy': 'never'}, {'actor': None})):
            await client.send(json.dumps({'id': index, 'method': 'turn/start', 'params': {**base, **changes}}))
            assert ('result' in json.loads(await client.recv())) == (index == 0)
    assert admitted == [('turn.submit', {'text': 'fixture'})]


async def test_refused_owned_route_releases_native_reader_capacity(native_socket):
    import asyncio

    from websockets.exceptions import ConnectionClosed

    from acc.session_relay.protocol import ErrorCode
    proxy, _, _ = native_socket
    def refuse(_websocket):
        raise RelayError(ErrorCode.POLICY_REFUSED)
    proxy.writer_route = refuse
    async with unix_connect(str(proxy.path), compression=None) as client:
        with pytest.raises(ConnectionClosed):
            await initialize(client)
    for _ in range(10):
        if not proxy.clients:
            break
        await asyncio.sleep(.01)
    assert not proxy.clients


async def test_exited_native_view_without_close_frame_releases_capacity(native_socket, caplog):
    import asyncio
    proxy, _, _ = native_socket
    client = await unix_connect(str(proxy.path), compression=None)
    await initialize(client)
    assert proxy.clients
    client.transport.abort()  # native process exit, without a WebSocket close frame
    await client.wait_closed()
    for _ in range(100):
        if not proxy.clients:
            break
        await asyncio.sleep(.01)
    assert not proxy.clients
    assert not any(record.getMessage() == 'connection handler failed' for record in caplog.records)


@pytest.mark.parametrize('phase', ['request', 'reply', 'notification'])
async def test_placement_change_during_authority_await_refuses_before_io(native_socket, phase):
    proxy, reads, _ = native_socket
    async with unix_connect(str(proxy.path), compression=None) as client:
        await initialize(client)
        checks = 0
        async def changed_placement():
            nonlocal checks
            checks += 1
            if checks == (2 if phase == 'reply' else 1):
                proxy.directory.chmod(0o755)
            return True
        proxy.authorize = changed_placement
        try:
            if phase == 'notification':
                with pytest.raises(RelayError):
                    await proxy.publish('turn/completed', {'threadId': 'native'})
            else:
                await client.send(json.dumps({'id': 1, 'method': 'thread/read', 'params': {'threadId': 'native'}}))
                if phase == 'request':
                    from websockets.exceptions import ConnectionClosed
                    with pytest.raises(ConnectionClosed):
                        await client.recv()
                else:
                    assert 'error' in json.loads(await client.recv())
                assert len(reads) == (1 if phase == 'reply' else 0)
        finally:
            proxy.directory.chmod(0o700)


async def test_slow_native_reader_does_not_lose_connection_but_revocation_still_refuses(tmp_path, monkeypatch):
    import asyncio

    import websockets.asyncio.server as server
    from websockets.exceptions import ConnectionClosed

    original = server.unix_serve
    def quick_keepalive(handler, path, **options):
        # Accelerate only transport timers. Preserve the production choice to
        # disable a pong deadline, if present; never change authority checks.
        options['ping_interval'] = .02
        if options.get('ping_timeout', 20) is not None:
            options['ping_timeout'] = .04
        return original(handler, path, **options)
    monkeypatch.setattr(server, 'unix_serve', quick_keepalive)
    directory = tmp_path / 'slow-private'
    directory.mkdir(mode=0o700)
    allowed = True
    async def authorize():
        return allowed
    async def read(*_):
        return {'thread': {'id': 'native'}}
    proxy = NativeSocket(directory, 'native', read, authorize, initialized={'userAgent': 'fixture'})
    await proxy.start()
    try:
        async with unix_connect(str(proxy.path), compression=None, ping_interval=None) as client:
            await initialize(client)
            await client.send(json.dumps({'id': 1, 'method': 'thread/resume', 'params': {'threadId': 'native'}}))
            assert 'result' in json.loads(await client.recv())
            assert len(proxy.bootstrapped_clients) == 1
            # A healthy owned native process can temporarily stop consuming its
            # socket under load. No reconnect, re-enrollment or replay is allowed.
            client.transport.pause_reading()
            await asyncio.sleep(.15)
            client.transport.resume_reading()
            await client.send(json.dumps({'id': 2, 'method': 'thread/read', 'params': {'threadId': 'native'}}))
            assert 'result' in json.loads(await asyncio.wait_for(client.recv(), 1))
            assert len(proxy.bootstrapped_clients) == 1
            allowed = False
            await client.send(json.dumps({'id': 3, 'method': 'thread/read', 'params': {'threadId': 'native'}}))
            # Authorization is checked before parsing the next native request:
            # revocation closes the connection, rather than returning its data.
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(client.recv(), 1)
            await asyncio.wait_for(client.wait_closed(), 1)
            assert client.close_code == 1008
    finally:
        await proxy.close()


async def test_notification_backpressure_slows_without_closing_healthy_reader(native_socket, monkeypatch):
    import asyncio

    proxy, _, _ = native_socket
    original_wait = asyncio.wait_for
    async def accelerated_wait(awaitable, timeout):
        return await original_wait(awaitable, .02 if timeout == 1 else timeout)
    monkeypatch.setattr(asyncio, 'wait_for', accelerated_wait)
    entered, release = asyncio.Event(), asyncio.Event()
    sent, closed = [], []
    class SlowReader:
        async def send(self, data):
            entered.set()
            await release.wait()
            sent.append(json.loads(data))
        async def close(self, *, code):
            closed.append(code)
    reader = SlowReader()
    proxy.clients.add(reader)
    task = asyncio.create_task(proxy.publish('turn/completed', {'threadId': 'native', 'turn': {'id': 'slow'}}))
    try:
        await entered.wait()
        await asyncio.sleep(.06)
        assert closed == [] and not task.done()
        release.set()
        await task
        assert sent == [{'method': 'turn/completed', 'params': {'threadId': 'native', 'turn': {'id': 'slow'}}}]
    finally:
        release.set()
        await task
        proxy.clients.discard(reader)
