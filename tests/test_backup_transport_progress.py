"""Slow fake receiver streams, private fixture sockets only; no real backup target."""
import asyncio
import socket
import time

import pytest

from acc import backup_targets


@pytest.fixture
def short_idle(monkeypatch):
    # Accelerate both the original literal timeout and the fixed named budget.
    wait_for = asyncio.wait_for
    async def short_wait(awaitable, timeout):
        return await wait_for(awaitable, .15 if timeout == 60 else timeout)
    monkeypatch.setattr(asyncio, 'wait_for', short_wait)
    monkeypatch.setattr(backup_targets, 'STREAM_IDLE_SECONDS', .15, raising=False)


@pytest.mark.parametrize('direction', ['download', 'upload', 'stalled'])
async def test_adapter_counts_progress_in_either_direction(short_idle, direction):
    tasks = set()
    payload = b'x' * 12

    async def receive(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            assert await reader.readexactly(1) == b'H'
            if direction == 'download':
                for byte in payload:
                    writer.write(bytes([byte]))
                    await writer.drain()
                    await asyncio.sleep(.04)
            elif direction == 'upload':
                assert await reader.readexactly(len(payload)) == payload
                writer.write(b'OK')
                await writer.drain()
            else:
                assert await reader.read() == b''
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # old adapter truncates the active transfer; client assertion fails
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(receive, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]

    class Link:
        _loop = asyncio.get_running_loop()
        async def open_backup(self, *args):
            return await asyncio.open_connection('127.0.0.1', port)

    def client():
        with backup_targets.node_transport(Link(), {'id': 'a' * 32}, 'b' * 32) as url:
            path = url[len('http+unix://'):].split(':', 1)[0]
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(3)
                connection.connect(path)
                connection.sendall(b'H')
                if direction == 'upload':
                    for byte in payload:
                        connection.sendall(bytes([byte]))
                        time.sleep(.04)
                result = b''
                while chunk := connection.recv(1024):
                    result += chunk
                assert result == {'download': payload, 'upload': b'OK', 'stalled': b''}[direction]

    try:
        await asyncio.wait_for(asyncio.to_thread(client), 5)
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('respond', [True, False], ids=['slow-response', 'stalled-peer'])
async def test_client_request_half_close_does_not_truncate_slow_receiver_response(short_idle, respond):
    payload = b'completed-backup-response'
    tasks = set()

    async def receive(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            assert await reader.readexactly(5) == b'HELLO'
            if not respond:
                await reader.read()
                return
            await asyncio.sleep(.04)  # requester finished writing before any reply is ready
            for chunk in (payload[:8], payload[8:16], payload[16:]):
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(.04)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            tasks.discard(task)

    server = await asyncio.start_server(receive, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]

    class Link:
        _loop = asyncio.get_running_loop()

        async def open_backup(self, *args):
            return await asyncio.open_connection('127.0.0.1', port)

    def client():
        with backup_targets.node_transport(Link(), {'id': 'a' * 32}, 'b' * 32) as url:
            path = url[len('http+unix://'):].split(':', 1)[0]
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(3)
                connection.connect(path)
                connection.sendall(b'HELLO')
                connection.shutdown(socket.SHUT_WR)
                response = bytearray()
                while chunk := connection.recv(1024):
                    response.extend(chunk)
                assert response == (payload if respond else b'')

    try:
        await asyncio.wait_for(asyncio.to_thread(client), 5)
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_real_relay_forwards_request_eof_and_releases_receiver_gate(short_idle, tmp_path, monkeypatch):
    """A relayed receiver must see FIN, reply completely, and allow the next connection."""
    from acc.node.client import Relay
    from acc.server.nodes import NodeLink

    tasks = set()
    requests = []
    payload = b'delayed-complete-response'

    async def receive(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            request = await reader.read()  # HTTP keep-alive ends only on forwarded request EOF
            if request:
                requests.append(request)
                await asyncio.sleep(.04)
                writer.write(payload[:7])
                await writer.drain()
                await asyncio.sleep(.04)
                writer.write(payload[7:])
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(receive, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(backup_targets, 'receiver_binding', lambda *args: port)

    class Socket:
        async def send(self, data):
            link.receive(data)

    relay = Relay(Socket(), tmp_path / 'no-brokers')
    link = NodeLink('a' * 32, 'archive-node', relay.handle, {'capabilities': ['backup-v1']}, [])

    def client():
        with backup_targets.node_transport(link, {'id': 'a' * 32}, 'b' * 32) as url:
            path = url[len('http+unix://'):].split(':', 1)[0]
            for request in (b'first encrypted request', b'second encrypted request'):
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.settimeout(3)
                    connection.connect(path)
                    connection.sendall(request)
                    connection.shutdown(socket.SHUT_WR)
                    response = bytearray()
                    while chunk := connection.recv(1024):
                        response.extend(chunk)
                    assert response == payload

    try:
        await asyncio.wait_for(asyncio.to_thread(client), 5)
        assert requests == [b'first encrypted request', b'second encrypted request']
        assert not link._channels and not relay.channels and not relay.draining
    finally:
        link.close()
        relay.close_all()
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('backup', [False, True], ids=['broker', 'legacy-backup'])
async def test_unnegotiated_request_eof_closes_channel_without_half_closing_socket(tmp_path, backup):
    from acc.node.client import Channel, Relay, frame

    sent = []

    class Socket:
        async def send(self, data):
            sent.append(data)

    class Writer:
        closed = False

        def close(self):
            self.closed = True

        def write_eof(self):
            pytest.fail('unnegotiated channel must never forward half-close')

    relay = Relay(Socket(), tmp_path)
    writer = Writer()
    channel = Channel(relay, 1, asyncio.StreamReader(), writer)
    channel.backup = backup
    relay.channels[1] = channel
    await relay.handle(frame(b'h', 1))
    assert writer.closed and not relay.channels
    assert sent == [frame(b'c', 1)]
