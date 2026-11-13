"""A dead account pipe cannot revoke the enclosing typed node transport."""
import asyncio
from types import SimpleNamespace

from acc.node.client import Relay
from acc.server.nodes import NodeLink


async def until(condition):
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(.001)


async def test_account_write_failure_closes_only_its_channel_and_fresh_stream_works(tmp_path, monkeypatch):
    received, handlers = [], set()
    async def broker(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            while data := await reader.read(1024):
                received.append(data)
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)
    server = await asyncio.start_unix_server(broker, str(tmp_path / 'fixture.sock'))
    async def to_node(data):
        if isinstance(data, bytes):
            await relay.handle(data)
    async def to_main(data):
        main.receive(data)
    main = NodeLink('fixture-id', 'fixture-host', to_node,
        {'capabilities': ['session-runtime-v1']}, ['fixture'])
    relay = Relay(SimpleNamespace(send=to_main), tmp_path)
    try:
        reader, writer = await main.open_session_runtime('fixture')
        old_id, channel = next(iter(relay.channels.items()))
        async def reset():
            raise ConnectionResetError('Artificial killed account broker pipe')
        monkeypatch.setattr(channel.writer, 'drain', reset)
        writer.write(b'possibly delivered before pipe loss')
        await writer.drain()
        await until(lambda: not relay.channels and not main._channels)
        assert await asyncio.wait_for(reader.read(), 5) == b''
        assert not main.closed
        # A new explicit typed open is not a resend on the old account pipe.
        fresh_reader, fresh_writer = await main.open_session_runtime('fixture')
        assert next(iter(relay.channels)) != old_id
        fresh_writer.write(b'fresh explicit stream')
        await fresh_writer.drain()
        await until(lambda: b'fresh explicit stream' in received)
        assert not main.closed
        fresh_writer.close()
        await fresh_writer.wait_closed()
        await until(lambda: not relay.channels and not main._channels)
        assert fresh_writer.is_closing()
    finally:
        main.close()
        await relay.aclose()
        await main.wait_closed()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*tuple(handlers))
