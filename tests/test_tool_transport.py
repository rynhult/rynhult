import asyncio
import json
import subprocess

import pytest
from helpers import wait_for

from acc.broker.server import Broker
from acc.node.packages import HelperPackages
from acc.server.nodes import NodeLink

PAYLOAD = {"id": "tool-000000000001", "action": "tool", "manager": "apt", "name": "ripgrep",
           "version": "14.1.0-1", "operation": "install"}


async def test_node_tool_frames_are_correlated_and_disconnect_fails():
    sent = []

    async def send(message):
        sent.append(json.loads(message))

    link = NodeLink("node1", "build", send, {}, ["alex-agent"])
    try:
        assert hasattr(link, "tool"), "node tool transport is not implemented"
        task = asyncio.create_task(link.tool(PAYLOAD, timeout=1))
        await asyncio.sleep(0)
        await link.flush()
        assert sent == [{"t": "tool", **PAYLOAD}]
        link.tool_result({"t": "tool-result", "id": "tool-000000000002", "ok": True, "version": "999"})
        assert not task.done()
        link.tool_result({"t": "tool-result", "id": PAYLOAD["id"], "ok": True, "version": "14.1.0-1", "detail": "done"})
        assert (await task)["version"] == "14.1.0-1"
        task = asyncio.create_task(link.tool({**PAYLOAD, "id": "tool-000000000003"}, timeout=1))
        await asyncio.sleep(0)
        link.close()
        with pytest.raises(ConnectionError):
            await task
    finally:
        link.close()


async def test_node_queues_only_validated_system_tools(tmp_path):
    (tmp_path / "requests").mkdir()
    (tmp_path / "results").mkdir()
    packages = HelperPackages({}, tmp_path)
    assert hasattr(packages, "tool"), "node helper tool path is not implemented"
    task = asyncio.create_task(packages.tool({"t": "tool", **PAYLOAD}, timeout=1))
    path = tmp_path / "requests" / f"{PAYLOAD['id']}.json"
    await wait_for(path.exists)
    body = json.loads(path.read_text())
    assert body == PAYLOAD
    (tmp_path / "results" / f"{PAYLOAD['id']}.json").write_text(json.dumps({"ok": True, "version": "14.1.0-1"}))
    assert (await task)["ok"]
    for bad in ({"manager": "npm"}, {"command": "id"}, {"name": "../jq"}, {"version": "1;id"}):
        with pytest.raises(ValueError):
            await packages.tool({"t": "tool", **PAYLOAD, **bad}, timeout=.01)


async def test_broker_user_tool_scope_and_no_root(env, monkeypatch):
    import acc.broker.server as server
    broker = Broker(env["broker"])
    monkeypatch.setattr(server.os, "geteuid", lambda: 0)
    body = {"id": 1, "method": "tool_change", "params": {"manager": "npm", "name": "prettier",
                                              "version": "3.6.2", "operation": "install"}}
    response = await broker.dispatch(json.dumps(body).encode())
    assert "unknown method" not in response.get("error", ""), "broker tool execution is not wired"
    assert not response["ok"]  # this test runner is root: never execute a user install as root
    monkeypatch.setattr(server.os, "geteuid", lambda: 1001)
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"dependencies":{"prettier":{"version":"3.6.2"}}}', "")
    monkeypatch.setattr(server.tooling, "_default_run", run)
    response = await broker.dispatch(json.dumps(body).encode())
    assert response["ok"] and response["result"]["version"] == "3.6.2"
    body["params"]["manager"] = "apt"
    before = len(commands)
    assert not (await broker.dispatch(json.dumps(body).encode()))["ok"]
    assert len(commands) == before


async def test_tool_request_and_result_cross_authenticated_node_link(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from test_node import CA, TLS, FakePrimary, Running, node_for
    key = Ed25519PrivateKey.generate()
    tls = TLS(CA(tmp_path / "tls"))
    sockets = tmp_path / "s"
    sockets.mkdir()
    root = tmp_path / "packages"
    (root / "requests").mkdir(parents=True)
    (root / "results").mkdir()
    async with FakePrimary(tls.server_context, key.public_key()) as primary:
        node = node_for(primary.port, tls, key, sockets, tmp_path / "state", packages=HelperPackages({}, root))
        node.updates.root = tmp_path / "updates"
        async with Running(node):
            side = await primary.next_connection()
            await side.ws.send(json.dumps({"t": "tool", **PAYLOAD}))
            path = root / "requests" / f"{PAYLOAD['id']}.json"
            await wait_for(path.exists)
            assert json.loads(path.read_text()) == PAYLOAD
            (root / "results" / path.name).write_text(json.dumps({"ok": True, "version": "14.1.0-1", "detail": "installed"}))
            while True:
                message = json.loads(await asyncio.wait_for(side.ws.recv(), 5))
                if message.get("t") == "tool-result":
                    break
            assert message == {"t": "tool-result", "id": PAYLOAD["id"], "ok": True,
                               "version": "14.1.0-1", "detail": "installed"}
