"""Disconnect must join owned work, including asynchronous cancellation cleanup."""
import asyncio

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from acc.node.client import Channel, Node, Relay, frame
from acc.node.config import NodeConfig


@pytest.mark.parametrize('cancel', [False, True])
async def test_serve_joins_heartbeat_and_reporters(tmp_path, cancel):
    started = {name: asyncio.Event() for name in ('pinger', 'report_packages', 'report_host')}
    cleaned = set()
    release = asyncio.Event()

    async def worker(name):
        started[name].set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(.01)
            cleaned.add(name)

    class Socket:
        async def send(self, data):
            pass

        async def close(self):
            pass

        async def __aiter__(self):
            await release.wait()
            return
            yield

    node = Node(NodeConfig(primary='https://acc.example.lan', node_id='1', socket_dir=tmp_path),
                Ed25519PrivateKey.generate(), packages=object())
    for name in started:
        setattr(node, name, lambda ws, name=name: worker(name))
    serving = asyncio.create_task(node.serve(Socket()))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 1)
        if cancel:
            serving.cancel()
            with pytest.raises(asyncio.CancelledError):
                await serving
        else:
            release.set()
            await serving
        assert cleaned == set(started)
        assert node.relay is None
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        await asyncio.sleep(.02)


@pytest.mark.parametrize('draining', [False, True])
async def test_relay_close_joins_channel_and_drain_cleanup(tmp_path, draining):
    class Socket:
        async def send(self, data):
            pass

    class Writer:
        def close(self):
            pass

    relay = Relay(Socket(), tmp_path)
    channel = Channel(relay, 1, asyncio.StreamReader(), Writer())
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def blocked():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(.01)
            cleaned.set()

    # Exercise start's task ownership, with a blocked broker writer.
    channel._to_broker = blocked
    relay.channels[1] = channel
    channel.start()
    await entered.wait()
    if draining:
        await relay.handle(frame(b'c', 1))
    tasks = list(channel.tasks) + list(relay.draining.values())
    try:
        await relay.aclose()
        assert cleaned.is_set()
        assert all(task.done() for task in tasks)
        assert not relay.channels and not relay.draining
    finally:
        relay.close_all()
        await asyncio.gather(*tasks, return_exceptions=True)
        await relay.preview.aclose()


async def test_sender_close_joins_in_flight_send():
    from acc.preview_sender import PrioritySender

    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def wire(data):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(.01)
            cleaned.set()

    sender = PrioritySender(wire)
    sending = asyncio.create_task(sender.send('ping'))
    await entered.wait()
    try:
        await sender.aclose()
        assert cleaned.is_set()
        assert sender.task.done()
        with pytest.raises(ConnectionError):
            await sending
    finally:
        sender.close()
        await asyncio.gather(sender.task, sending, return_exceptions=True)


async def test_relay_close_does_not_interrupt_preview_finalizers(tmp_path):
    from acc.preview_stream import pack

    class Socket:
        async def send(self, data):
            pass

    relay = Relay(Socket(), tmp_path)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def preview(stream, payload):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(.03)
            cleaned.set()

    async def channel_work():
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(.01)

    relay.preview._run = preview
    relay.preview.receive(pack(b'O', 1, b'{}'))
    relay.own(channel_work())
    await entered.wait()
    await relay.aclose()
    assert cleaned.is_set()
