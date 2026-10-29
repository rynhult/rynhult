"""Fault injection at the broker wire boundary."""
import asyncio
import json

import pytest

from acc.server.broker_client import BrokerClient, BrokerUnavailable


@pytest.mark.parametrize("method", ["send_text", "launch", "tool_change", "github_api", "files"])
async def test_delivered_mutation_is_not_replayed_when_reply_is_lost(tmp_path, method):
    delivered = []

    async def handle(reader, writer):
        delivered.append(json.loads(await reader.readline()))
        writer.close()  # side effect applied, reply lost
        await writer.wait_closed()

    path = tmp_path / "b.sock"
    server = await asyncio.start_unix_server(handle, str(path))
    async with server:
        client = BrokerClient(path)
        try:
            with pytest.raises(BrokerUnavailable, match="delivery uncertain"):
                await client.call(method)
            assert len(delivered) == 1
        finally:
            await client.close()


async def test_read_reconnects_after_reply_loss(tmp_path):
    delivered = []

    async def handle(reader, writer):
        request = json.loads(await reader.readline())
        delivered.append(request)
        if len(delivered) == 2:
            writer.write(json.dumps({"id": request["id"], "ok": True, "result": "ready"}).encode() + b"\n")
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    path = tmp_path / "b.sock"
    server = await asyncio.start_unix_server(handle, str(path))
    async with server:
        client = BrokerClient(path)
        try:
            assert await client.call("ping") == "ready"
            assert len(delivered) == 2
        finally:
            await client.close()


@pytest.mark.parametrize("envelope", [
    [], None, 42, True, "ping", {}, {"id": True, "method": "ping", "params": {}},
    {"id": 1 << 60, "method": "ping", "params": {}},
    {"id": 1, "method": [], "params": {}},
    *({"id": 1, "method": method, "params": params}
      for method in ("ping", "attach") for params in (None, [], False, 3, "bad")),
])
async def test_invalid_requests_are_protocol_refusals_and_stream_continues(env, tmp_path, envelope, caplog):
    from acc.broker.server import Broker

    broker = Broker(env["broker"])
    path = tmp_path / "b.sock"
    server = await asyncio.start_unix_server(broker.handle, str(path))
    async with server:
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            writer.write(json.dumps(envelope).encode() + b"\n")
            writer.write(b'{"id":2,"method":"ping","params":{}}\n')
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(reader.readline(), 1))
            assert reply["ok"] is False and "protocol" in reply["error"]
            assert json.loads(await asyncio.wait_for(reader.readline(), 1))["ok"] is True
            assert "method failed" not in caplog.text
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize("reply", [
    {"id": 1, "ok": "yes", "result": 1}, {"id": True, "ok": True, "result": 1},
    {"id": 1, "ok": True}, {"id": 1, "ok": False, "error": []},
    {"id": 1, "result": 1},
])
async def test_invalid_reply_types_are_unavailable(tmp_path, reply):
    from test_remote_spool import one_shot_broker

    server, path, _ = await one_shot_broker(tmp_path, lambda _: json.dumps(reply).encode() + b"\n")
    async with server:
        client = BrokerClient(path)
        try:
            with pytest.raises(BrokerUnavailable, match="malformed"):
                await client.call("ping")
        finally:
            await client.close()


@pytest.mark.parametrize("reply", [[], None, {"ok": "yes"}, {"ok": True},
                                   {"ok": True, "readonly": 1}, {"ok": False, "error": []}])
async def test_terminal_refuses_invalid_attach_reply(tmp_path, reply):
    from test_remote_spool import one_shot_broker

    from acc.server.tty import Writers, bridge

    class Browser:
        async def send_text(self, value):
            pytest.fail("invalid attach reply must not enable the terminal")

    server, path, _ = await one_shot_broker(tmp_path, lambda _: json.dumps(reply).encode() + b"\n")
    async with server:
        with pytest.raises(ConnectionError, match="malformed"):
            await bridge(Browser(), lambda: asyncio.open_unix_connection(str(path)), "s", "acc-s",
                         Writers(), want_write=True, take=False, rows=40, cols=120)
