"""Multi-host, primary side: what one node's link accepts and holds (src/acc/server/nodes.py), without a WebSocket."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from helpers import wait_for

from acc.server import nodes
from acc.server.nodes import FRAME, LinkError, NodeLink, frame


class Wire:
    """The link's `send`: records what goes to the node; `stuck` makes it block like a node that stopped reading."""

    def __init__(self) -> None:
        self.sent: list[bytes | str] = []
        self.stuck = asyncio.Event()
        self.stuck.set()

    async def send(self, data: bytes | str) -> None:
        await self.stuck.wait()
        self.sent.append(data)

    def frames(self) -> list[tuple[bytes, int, bytes]]:
        return [(*FRAME.unpack(d[:FRAME.size]), d[FRAME.size:]) for d in self.sent if isinstance(d, bytes)]


@pytest.fixture
async def link():
    wire = Wire()
    link = NodeLink("n1", "pc", wire.send, {}, ["agent"])
    link.wire = wire
    yield link
    link.close()


async def opened(link: NodeLink, account: str = "agent"):
    """Open a channel and acknowledge it as the node would."""
    task = asyncio.create_task(link.open(account))
    await asyncio.sleep(0)
    channel_id = max(link._channels)
    link.receive(frame(b"k", channel_id))
    reader, writer = await task
    return channel_id, reader, writer


async def test_a_data_frame_over_1_mib_closes_the_link(link):
    channel_id, reader, _ = await opened(link)
    with pytest.raises(LinkError):
        link.receive(frame(b"d", channel_id, b"x" * (nodes.MAX_FRAME + 1)))
    assert link.closed
    assert await reader.read() == b""


async def test_an_oversized_control_frame_closes_the_link(link):
    channel_id, _, _ = await opened(link)
    with pytest.raises(LinkError):
        link.receive(frame(b"e", channel_id, b"x" * (nodes.MAX_CONTROL + 1)))
    assert link.closed


async def test_a_full_size_data_frame_is_accepted(link):
    channel_id, reader, _ = await opened(link)
    link.receive(frame(b"d", channel_id, b"x" * nodes.MAX_FRAME))
    assert len(await reader.readexactly(nodes.MAX_FRAME)) == nodes.MAX_FRAME


async def test_data_before_the_channel_is_opened_is_dropped(link):
    task = asyncio.create_task(link.open("agent"))
    await asyncio.sleep(0)
    channel_id = max(link._channels)
    link.receive(frame(b"d", channel_id, b"early"))
    assert link._channels[channel_id].buffered() == 0
    link.receive(frame(b"k", channel_id))
    reader, _ = await task
    link.receive(frame(b"d", channel_id, b"late"))
    assert await reader.readexactly(4) == b"late"


async def test_data_for_an_unknown_channel_is_dropped(link):
    link.receive(frame(b"d", 99, b"nobody"))
    assert not link.closed and not link._channels


async def test_a_channel_nobody_reads_is_closed_at_its_byte_limit(link, monkeypatch):
    monkeypatch.setattr(nodes, "MAX_CHANNEL_BUFFER", 3 * 1024)
    channel_id, reader, writer = await opened(link)
    for _ in range(3):
        link.receive(frame(b"d", channel_id, b"x" * 1024))
    assert channel_id in link._channels
    link.receive(frame(b"d", channel_id, b"x"))  # one byte too many
    assert channel_id not in link._channels and not link.closed  # only that channel
    with pytest.raises(ConnectionError):
        await reader.read()
    with pytest.raises(ConnectionError):
        writer.write(b"more")
    await link.flush()
    assert (b"c", channel_id, b"") in link.wire.frames()  # the node is told


async def test_a_node_that_stops_reading_loses_its_link_at_the_queue_limit(link, monkeypatch):
    monkeypatch.setattr(nodes, "MAX_OUTBOX", 10 * 1024)
    link.wire.stuck.clear()
    _, _, writer = await opened(link)
    for _ in range(8):
        writer.write(b"x" * 1024)
    assert not link.closed
    writer.write(b"x" * 2048)
    assert link.closed
    with pytest.raises(ConnectionError):
        writer.write(b"x")


async def test_many_tiny_messages_count_towards_the_queue_limit(link, monkeypatch):
    monkeypatch.setattr(nodes, "MAX_OUTBOX", 100 * nodes.QUEUED_OVERHEAD)
    link.wire.stuck.clear()
    for _ in range(200):
        link.queue('{"t": "pong"}')
    assert link.closed


async def test_flush_gives_up_after_its_deadline(link):
    _, _, writer = await opened(link)
    link.wire.stuck.clear()
    writer.write(b"hello")
    with pytest.raises(ConnectionError, match="not reading"):
        await link.flush(timeout=0.05)
    link.wire.stuck.set()
    await link.flush(timeout=1)
    assert (b"d", 1, b"hello") in link.wire.frames()


async def test_flush_returns_when_the_link_closes(link):
    link.wire.stuck.clear()
    link.queue("x")
    waiter = asyncio.create_task(link.flush(timeout=5))
    await wait_for(lambda: not link._drained.is_set())
    link.close()
    await asyncio.wait_for(waiter, 1)


def test_the_server_caps_websocket_messages(monkeypatch):
    import uvicorn

    from acc.server import app
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda application, **options: seen.update(options))
    monkeypatch.setattr(app, "create_app", lambda: None)
    monkeypatch.setenv("ACC_LISTEN", "127.0.0.1:1")
    app.main()
    assert seen["ws_max_size"] == nodes.WS_MAX_SIZE >= FRAME.size + nodes.MAX_FRAME  # a terminal paste fits too


# ------------------------------------------------------------------ lifecycle (review #5)


async def test_close_stops_the_sender_and_closes_the_socket_once(link):
    codes = []
    link.on_close = codes.append
    link.wire.stuck.clear()
    link.queue(b"x" * 100)
    await asyncio.sleep(0)
    link.close(4409)
    link.close(4401)
    await asyncio.sleep(0)
    assert codes == [4409]
    assert link._sender.done() and link._watchdog.done()
    assert link._outbox.empty()


async def test_a_failed_send_closes_the_link(link):
    codes = []
    link.on_close = codes.append

    async def broken(data):
        raise RuntimeError("socket gone")
    link._send = broken
    link.queue("x")
    await wait_for(lambda: link.closed)
    assert link.closed and codes == [1011]


@pytest.mark.parametrize("outcome", ["timeout", "cancel", "refused", "disconnect"])
async def test_a_failed_open_forgets_its_channel(link, monkeypatch, outcome):
    monkeypatch.setattr(nodes, "OPEN_TIMEOUT", 0.05)
    task = asyncio.create_task(link.open("agent"))
    await asyncio.sleep(0)
    channel_id = max(link._channels)
    if outcome == "cancel":
        task.cancel()
    elif outcome == "refused":
        link.receive(frame(b"e", channel_id, b"no broker"))
    elif outcome == "disconnect":
        link.close()
    with pytest.raises((ConnectionError, asyncio.CancelledError)):
        await task
    assert channel_id not in link._channels
    if outcome in ("timeout", "cancel"):
        await link.flush()
        assert (b"c", channel_id, b"") in link.wire.frames()  # the node closes it if it opens it late
    link.receive(frame(b"k", channel_id))  # a late "opened" finds nothing
    assert channel_id not in link._channels


async def test_the_heartbeat_deadline(link, monkeypatch):
    monkeypatch.setattr(nodes, "HEARTBEAT_TIMEOUT", 0.3)
    link._watchdog.cancel()
    await asyncio.gather(link._watchdog, return_exceptions=True)
    clock = 0.0
    waits = []
    monkeypatch.setattr(link, "_loop", SimpleNamespace(time=lambda: clock))
    link.heard()

    async def advance(delay):
        nonlocal clock
        waits.append(delay)
        assert not link.closed
        clock += delay
        if len(waits) <= 4:
            link.heard()  # a heartbeat at each deadline keeps the link alive
        await asyncio.sleep(0)
    monkeypatch.setattr(nodes, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": advance}))
    await link._watch()
    assert len(waits) == 5
    assert link.closed


@pytest.mark.parametrize('ack', [b'', b'backup-eof-v1', b'unknown'])
async def test_backup_request_eof_requires_explicit_node_acknowledgement(link, ack):
    link.info['capabilities'] = ['backup-v1']
    task = asyncio.create_task(link.open_backup('a' * 32, 'b' * 32))
    await asyncio.sleep(0)
    identifier = max(link._channels)
    link.receive(frame(b'k', identifier, ack))
    reader, writer = await task
    assert writer.can_write_eof() is (ack == b'backup-eof-v1')
    if writer.can_write_eof():
        writer.write(b'ciphertext')
        writer.write_eof()
        writer.write_eof()  # one FIN, after all queued ciphertext
        await writer.drain()
        sent = [(kind, payload) for kind, channel, payload in link.wire.frames() if channel == identifier]
        assert sent[-2:] == [(b'd', b'ciphertext'), (b'h', b'')]
        with pytest.raises(ConnectionError):
            writer.write(b'after FIN')
        link.receive(frame(b'd', identifier, b'delayed response'))
        link.receive(frame(b'c', identifier))
        assert await reader.read() == b'delayed response'
    else:
        with pytest.raises(NotImplementedError):
            writer.write_eof()
    writer.close()
    await writer.wait_closed()
    assert not link._channels


async def test_backup_eof_ack_cannot_enable_half_close_on_broker_channel(link):
    task = asyncio.create_task(link.open('agent'))
    await asyncio.sleep(0)
    identifier = max(link._channels)
    link.receive(frame(b'k', identifier, b'backup-eof-v1'))
    _, writer = await task
    assert not writer.can_write_eof()
    with pytest.raises(NotImplementedError):
        writer.write_eof()
    writer.close()
    await writer.wait_closed()
