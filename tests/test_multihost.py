"""Multi-host, primary side: enrollment, the node link, and sessions on another host driven through it.

The node here is a stand-in written against docs/MULTIHOST.md (the real one is acc.node): it relays each channel to
the test broker's socket, as a node relays to its own brokers.
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct

import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from helpers import reconcile_passes, wait_for, write_db
from test_auth import live  # noqa: F401 (fixture)
from test_supervisor import get, state_is

from acc.node.packages import HelperPackages, load_catalog

FRAME = struct.Struct(">cI")


class StandInNode:
    """Relays every channel to the test broker; answers "packages" frames with acc-node's own code (packages.py), its
    helper folder in `packages_root` (a test plays the helper)."""

    def __init__(self, broker_socket, key: Ed25519PrivateKey, node_id: str, packages_root=None, info=None):
        self.broker_socket, self.key, self.node_id = broker_socket, key, node_id
        self.info = info or {"os": "linux", "hostname": "standin"}
        self.channels: dict[int, asyncio.StreamWriter] = {}
        self.pumps: set[asyncio.Task] = set()
        self.opened: list[str] = []
        self.texts: list[dict] = []
        self.package_gate = None  # test-only transport delay before helper execution
        self.relayed = 0  # data frames from the primary: broker calls that went through this node
        self.reply_handler = None
        self.packages = HelperPackages(load_catalog(), packages_root) if packages_root else None

    async def run(self, url: str, signature_override: bytes | None = None):
        try:
            async with websockets.connect(f"{url}/api/nodes/connect") as ws:
                challenge = json.loads(await ws.recv())
                nonce = base64.b64decode(challenge["nonce"])
                signature = signature_override or self.key.sign(nonce + challenge["primary"].encode())
                await ws.send(json.dumps({"t": "hello", "node_id": self.node_id, "signature": base64.b64encode(signature).decode(),
                                          "info": self.info, "accounts": ["agent"]}))
                ready = json.loads(await ws.recv())
                assert ready == {"t": "ready"}
                self.ws = ws
                if self.packages:
                    await self.report()
                async for message in ws:
                    if isinstance(message, bytes):
                        await self.frame(ws, message)
                    else:
                        if self.package_gate is not None and json.loads(message).get("t") == "packages":
                            await self.package_gate.wait()
                        self.texts.append(json.loads(message))
                        if self.reply_handler and self.texts[-1].get('t') == 'request-replies':
                            await self.reply_handler(ws, message)
                        if self.packages and self.texts[-1].get("t") == "packages":
                            self.packages.handle(self.texts[-1])
                            await self.report()
        finally:
            for writer in self.channels.values():
                writer.close()
            self.channels.clear()
            for task in self.pumps:
                task.cancel()
            await asyncio.gather(*self.pumps, return_exceptions=True)

    async def report(self):
        """What acc-node sends on connect and whenever its helper's state changes."""
        await self.ws.send(json.dumps(self.packages.message()))

    async def frame(self, ws, data: bytes):
        kind, channel = FRAME.unpack(data[:FRAME.size])
        payload = data[FRAME.size:]
        if kind == b"o":
            self.opened.append(json.loads(payload)["account"])
            reader, writer = await asyncio.open_unix_connection(str(self.broker_socket))
            self.channels[channel] = writer
            await ws.send(FRAME.pack(b"k", channel))

            async def pump():
                while chunk := await reader.read(65536):
                    await ws.send(FRAME.pack(b"d", channel) + chunk)
                await ws.send(FRAME.pack(b"c", channel))
            self.pumps.add(asyncio.create_task(pump()))
        elif kind == b"d" and channel in self.channels:
            self.relayed += 1
            self.channels[channel].write(payload)
            await self.channels[channel].drain()
        elif kind == b"c" and channel in self.channels:
            self.channels.pop(channel).close()


def keypair():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return key, base64.b64encode(public).decode()


async def enroll(api, name="pc"):
    added = (await api.post("/api/nodes", json={"name": name})).json()
    assert added["status"] == "pending" and added["code"] in added["command"]
    key, public = keypair()
    enrolled = await api.post("/api/nodes/enroll", json={"name": name, "code": added["code"], "public_key": public,
                                                          "info": {"os": "linux", "hostname": "standin"}})
    assert enrolled.status_code == 200, enrolled.text
    again = await api.post("/api/nodes/enroll", json={"name": name, "code": added["code"], "public_key": public})
    assert again.status_code == 403  # one-time code
    return key, enrolled.json()["node_id"]


async def test_session_on_another_host_runs_through_the_node(api, env, live):  # noqa: F811
    key, node_id = await enroll(api)
    node = StandInNode(env["settings"].broker_socket, key, node_id)
    task = asyncio.create_task(node.run(live))

    async def connected():
        nodes = (await api.get("/api/nodes")).json()
        return nodes if nodes and nodes[0]["connected"] else None
    nodes = await wait_for(connected)
    assert nodes[0]["name"] == "pc" and nodes[0]["info"]["hostname"] == "standin"

    (env["workspaces"] / "remote").mkdir()
    project = (await api.post("/api/projects", json={"slug": "remote", "name": "Remote", "host": "pc",
                                                     "workspace_path": str(env["workspaces"] / "remote")})).json()
    assert project["host"] == "pc"
    assert (await api.post(f"/api/projects/{project['id']}/trust")).status_code == 200  # through the node
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "far"})).json()
    idle = await wait_for(state_is(api, session["id"], "idle"), timeout=20)  # launch, list and hook events: all relayed
    assert idle["status_source"] == "claude_hook" and node.opened
    shots = env["workspaces"] / "remote" / ".acc" / "artifacts"  # artifacts come through the node, like Changes
    shots.mkdir(parents=True)
    (shots / "screen.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x01" * (5 << 20))  # more than one broker reply
    listed = (await api.get(f"/api/sessions/{session['id']}/artifacts")).json()
    assert [f["name"] for f in listed["files"]] == ["screen.png"]
    served = await api.get(f"/api/sessions/{session['id']}/artifacts/screen.png")
    assert served.status_code == 200 and served.content == (shots / "screen.png").read_bytes()

    task.cancel()  # the node goes away: its session is left alone, local ones carry on
    await wait_for(lambda: _disconnected(api))
    await reconcile_passes(api)
    assert (await get(api, session["id"]))["desired_state"] == "running"


async def test_a_node_with_its_own_layout_gets_files_and_arguments_for_it(api, env, live, tmp_path):  # noqa: F811
    """Q4: the primary built --settings, the hook commands and the run folder from its own paths (/srv/acc/...), while a
    node keeps them elsewhere (a Mac: ~/Library/Application Support/ACC). Here the node's broker has a layout unlike the
    primary's, and the launch must name only the node's paths."""
    import dataclasses

    from acc.broker import ops
    from acc.broker.server import serve
    from acc.server.models import Session

    mac = tmp_path / "Library/Application Support/ACC"
    (mac / "bin").mkdir(parents=True)
    (mac / "run/agent").mkdir(parents=True)
    node_broker = dataclasses.replace(env["broker"], socket_path=tmp_path / "node.sock", run_dir=mac / "run/agent",
                                      tmux_socket=env["broker"].tmux_socket + "-node",
                                      hook_path=str(mac / "bin/acc-hook"))
    serving = asyncio.create_task(serve(node_broker))
    await wait_for(lambda: _exists(node_broker.socket_path))
    key, node_id = await enroll(api)
    node = StandInNode(node_broker.socket_path, key, node_id)
    task = asyncio.create_task(node.run(live))
    try:
        await wait_for(lambda: _connected(api))
        (env["workspaces"] / "remote").mkdir()
        project = (await api.post("/api/projects", json={"slug": "remote", "name": "Remote", "host": "pc",
                                                         "workspace_path": str(env["workspaces"] / "remote")})).json()
        assert (await api.post(f"/api/projects/{project['id']}/trust")).status_code == 200
        session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude",
                                                         "name": "mac"})).json()
        await wait_for(state_is(api, session["id"], "idle"), timeout=20)
        run = mac / "run/agent" / session["id"]
        with api.app.state.db() as s:
            assert s.get(Session, session["id"]).run_dir == str(run)
        settings = json.loads((run / "settings.json").read_text())
        commands = json.dumps(settings)
        assert str(mac / "bin/acc-hook") in commands and str(mac / "bin/acc-guard") in commands
        assert str(mac / "bin/acc-statusline") in commands and "/srv/acc/bin" not in commands
        events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
        command = next(e["payload"]["command"] for e in events if e["kind"] == "run_launch")
        assert f"--settings '{run}/settings.json'" in command or f"--settings {run}/settings.json" in command
        assert not (env["run_dir"] / session["id"]).exists()  # nothing where the primary keeps its own

        # The broker refuses a launch whose folder isn't the one it would use (an older primary, a moved install)
        with pytest.raises(ops.OpError, match="keeps session folders in"):
            ops.launch(node_broker, {"acc_id": "0123456789ab", "tmux_name": "acc-x-01234567", "provider": "claude",
                                     "args": [], "cwd": str(env["workspaces"] / "remote"), "files": {},
                                     "run_dir": "/srv/acc/run/agent/0123456789ab"})
    finally:
        task.cancel()
        serving.cancel()
        # owned_tmux collects this remote broker too.


async def _exists(path):
    return path.exists()


async def _disconnected(api):
    nodes = (await api.get("/api/nodes")).json()
    return nodes if nodes and not nodes[0]["connected"] else None


async def test_a_wrong_signature_is_refused(api, env, live):  # noqa: F811
    key, node_id = await enroll(api, "evil")
    node = StandInNode(env["settings"].broker_socket, key, node_id)
    with pytest.raises((websockets.ConnectionClosed, AssertionError)):
        await node.run(live, signature_override=b"\0" * 64)
    assert not (await api.get("/api/nodes")).json()[0]["connected"]


async def test_projects_need_an_enrolled_host_and_removal_revokes(api, env):
    bad = await api.post("/api/projects", json={"slug": "x", "name": "X", "host": "nowhere", "workspace_path": "/tmp/x"})
    assert bad.status_code == 422
    await enroll(api, "spare")
    assert (await api.post("/api/nodes/spare/remove")).json() == {"removed": "spare"}
    assert (await api.get("/api/nodes")).json() == []
    assert (await api.post("/api/nodes", json={"name": "local"})).status_code == 422


async def test_trust_mode_in_the_enroll_command(api, env, tmp_path):
    """A pinned leaf certificate goes stale at renewal: nodes verify a public cert normally, or pin a private root CA."""
    assert (await api.get("/api/nodes/ca")).status_code == 404  # no private CA configured
    added = (await api.post("/api/nodes", json={"name": "pub"})).json()
    assert "--trust system" in added["command"] and added["trust"] == "system"
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "Test Root")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    (tmp_path / "root.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    object.__setattr__(api.app.state.ctx.settings, "node_ca_file", tmp_path / "root.pem")
    served = await api.get("/api/nodes/ca")
    assert served.status_code == 200 and "BEGIN CERTIFICATE" in served.text
    added = (await api.post("/api/nodes", json={"name": "priv"})).json()
    expected = cert.fingerprint(hashes.SHA256()).hex()
    assert f"--ca-fingerprint {expected}" in added["command"] and added["trust"] == "ca"


async def test_terminal_of_a_session_on_another_host(api, env, live):  # noqa: F811
    """The browser terminal streams through the node too (attach is a channel like any broker connection)."""
    from test_auth import ORIGIN, do_setup
    await do_setup(api, env)
    csrf = {"X-ACC-CSRF": api.cookies.get("acc_csrf"), **ORIGIN}
    added = (await api.post("/api/nodes", headers=csrf, json={"name": "pc"})).json()
    key, public = keypair()
    node_id = (await api.post("/api/nodes/enroll", json={"name": "pc", "code": added["code"], "public_key": public})).json()["node_id"]
    node = StandInNode(env["settings"].broker_socket, key, node_id)
    task = asyncio.create_task(node.run(live))
    (env["workspaces"] / "remote").mkdir()
    await wait_for(lambda: _connected(api))
    project = (await api.post("/api/projects", headers=csrf, json={"slug": "remote", "name": "Remote", "host": "pc",
                                                                   "workspace_path": str(env["workspaces"] / "remote")})).json()
    await api.post(f"/api/projects/{project['id']}/trust", headers=csrf)
    session = (await api.post("/api/sessions", headers=csrf, json={"project_id": project["id"], "provider": "claude",
                                                                   "name": "far"})).json()
    await wait_for(state_is(api, session["id"], "idle"), timeout=20)
    opened = len(node.opened)
    headers = {"Cookie": f"acc_session={api.cookies.get('acc_session')}", "Origin": "http://127.0.0.1:18740"}
    async with websockets.connect(f"{live}/api/sessions/{session['id']}/tty", additional_headers=headers) as ws:
        assert json.loads(await asyncio.wait_for(ws.recv(), 5)) == {"t": "mode", "write": True}
        seen = b""
        while b"fake-claude" not in seen:  # tmux's screen setup first, then the agent's own output
            seen += await asyncio.wait_for(ws.recv(), 5)
    assert len(node.opened) > opened  # the attach went through the node
    task.cancel()


async def _connected(api):
    nodes = (await api.get("/api/nodes")).json()
    return nodes if nodes and nodes[0]["connected"] else None


# ------------------------------------------------------------------ the link's lifecycle (review #5)


async def handshake(live: str, key, node_id: str, hello: dict | None = None, info: dict | None = None):  # noqa: F811 (fixture)
    """A node's WebSocket up to "ready" (or whatever the primary answers instead)."""
    ws = await websockets.connect(f"{live}/api/nodes/connect")
    challenge = json.loads(await ws.recv())
    signature = key.sign(base64.b64decode(challenge["nonce"]) + challenge["primary"].encode())
    await ws.send(json.dumps(hello if hello is not None else
                             {"t": "hello", "node_id": node_id, "signature": base64.b64encode(signature).decode(),
                              "info": info or {}, "accounts": []}))
    return ws


async def closed_with(ws, timeout: float = 5) -> int | None:
    try:
        while True:
            await asyncio.wait_for(ws.recv(), timeout)
    except websockets.ConnectionClosed as closed:
        return closed.rcvd.code if closed.rcvd else None


async def test_a_newer_connection_closes_the_old_websocket(api, env, live):  # noqa: F811
    key, node_id = await enroll(api)
    first = await handshake(live, key, node_id)
    assert json.loads(await first.recv()) == {"t": "ready"}
    old_link = api.app.state.ctx.broker.hub.links["pc"]
    second = await handshake(live, key, node_id)
    assert json.loads(await second.recv()) == {"t": "ready"}
    assert await closed_with(first) == 4409
    assert old_link.closed and old_link._sender.done()
    assert api.app.state.ctx.broker.hub.links["pc"] is not old_link
    await second.close()


async def test_removing_a_host_closes_its_websocket(api, env, live):  # noqa: F811
    key, node_id = await enroll(api)
    ws = await handshake(live, key, node_id)
    assert json.loads(await ws.recv()) == {"t": "ready"}
    assert (await api.post("/api/nodes/pc/remove")).status_code == 200
    assert await closed_with(ws) == 4401
    assert "pc" not in api.app.state.ctx.broker.hub.links


async def test_a_revoke_during_the_handshake_wins(api, env, live, monkeypatch):  # noqa: F811
    """The signature checks out against the key read before the revoke; the recheck after attaching catches it."""
    from acc.server.api import nodes as nodes_api
    from acc.server.models import Node
    key, node_id = await enroll(api)
    db = api.app.state.ctx.db

    def verify_then_revoke(*args):
        from acc.server.db import WRITE_INTENT
        token = WRITE_INTENT.set(True)
        with db() as s:
            s.get(Node, node_id).status = "revoked"
            s.commit()
        WRITE_INTENT.reset(token)
        return True
    monkeypatch.setattr(nodes_api, "verify", verify_then_revoke)
    ws = await handshake(live, key, node_id)
    assert await closed_with(ws) == 4401
    assert "pc" not in api.app.state.ctx.broker.hub.links


async def test_a_silent_node_loses_its_link(api, env, live, monkeypatch):  # noqa: F811
    from acc.server import nodes
    monkeypatch.setattr(nodes, "HEARTBEAT_TIMEOUT", 0.5)
    key, node_id = await enroll(api)
    ws = await handshake(live, key, node_id)
    assert json.loads(await ws.recv()) == {"t": "ready"}
    for _ in range(4):  # pings keep it
        await ws.send(json.dumps({"t": "ping"}))
        assert json.loads(await asyncio.wait_for(ws.recv(), 5))["t"] == "pong"
    assert "pc" in api.app.state.ctx.broker.hub.links
    assert await closed_with(ws) == 1001  # then silence ends it
    await wait_for(lambda: asyncio.sleep(0, result="pc" not in api.app.state.ctx.broker.hub.links))


# ------------------------------------------------------------------ admission (review #12)


@pytest.mark.parametrize("hello", [
    ["hello"], "hello", 7, None,
    {"t": "hello", "node_id": 5, "signature": "AA=="},
    {"t": "hello", "node_id": "a" * 65, "signature": "AA=="},
    {"t": "hello", "node_id": "../x", "signature": "AA=="},
    {"t": "hello", "node_id": "abc", "signature": ["AA=="]},
    {"t": "hello", "node_id": "abc", "signature": "A" * 129},
    {"t": "hello", "node_id": "abc", "signature": ""},
    {"t": "goodbye", "node_id": "abc", "signature": "AA=="},
], ids=lambda h: type(h).__name__ if not isinstance(h, dict) else ",".join(f"{k}={str(v)[:6]}" for k, v in h.items()))
async def test_malformed_hellos_are_refused_before_any_work(api, env, live, monkeypatch, hello):  # noqa: F811
    from acc.server.api import nodes as nodes_api
    checked = []
    monkeypatch.setattr(nodes_api, "verify", lambda *a: checked.append(a) or False)
    ws = await websockets.connect(f"{live}/api/nodes/connect")
    await ws.recv()
    await ws.send(json.dumps(hello))
    assert await closed_with(ws) == 4400
    assert not checked


async def test_odd_hello_fields_are_dropped_not_fatal(api, env, live):  # noqa: F811
    key, node_id = await enroll(api)
    ws = await websockets.connect(f"{live}/api/nodes/connect")
    challenge = json.loads(await ws.recv())
    signature = base64.b64encode(key.sign(base64.b64decode(challenge["nonce"]) + challenge["primary"].encode())).decode()
    await ws.send(json.dumps({"t": "hello", "node_id": node_id, "signature": signature, "info": ["x"],
                              "accounts": {"agent": 1}}))
    assert json.loads(await ws.recv()) == {"t": "ready"}
    link = api.app.state.ctx.broker.hub.links["pc"]
    assert link.info == {} and link.accounts == []
    await ws.close()


async def test_unauthenticated_sockets_are_capped(api, env, live, monkeypatch):  # noqa: F811
    from acc.server.api import nodes as nodes_api
    monkeypatch.setattr(nodes_api, "MAX_HANDSHAKES", 2)
    waiting = [await websockets.connect(f"{live}/api/nodes/connect") for _ in range(2)]
    for ws in waiting:
        await ws.recv()  # the challenge: both are past accept
    with pytest.raises(websockets.InvalidStatus):
        await websockets.connect(f"{live}/api/nodes/connect")
    await waiting[0].send("not json")
    assert await closed_with(waiting[0]) == 4400
    again = await websockets.connect(f"{live}/api/nodes/connect")  # a slot is free again
    assert json.loads(await again.recv())["t"] == "challenge"
    await again.close()
    await waiting[1].close()


async def test_one_address_cannot_hold_every_handshake_slot(api, env, live, monkeypatch):  # noqa: F811
    """D19: silent sockets from one host used to fill every slot, so each reconnecting node got 1013."""
    from acc.server.api import nodes as nodes_api
    monkeypatch.setattr(nodes_api, "MAX_HANDSHAKES_PER_ADDRESS", 2, raising=False)
    monkeypatch.setattr(nodes_api, "HELLO_TIMEOUT", 0.5, raising=False)
    ctx = api.app.state.ctx
    real = ctx.client_key
    flood = [True]
    monkeypatch.setattr(ctx, "client_key", lambda conn: "192.0.2.66" if flood[0] else real(conn))
    silent = [await websockets.connect(f"{live}/api/nodes/connect") for _ in range(2)]
    for ws in silent:
        await ws.recv()  # the challenge; they never answer
    with pytest.raises(websockets.InvalidStatus):
        await websockets.connect(f"{live}/api/nodes/connect")  # a third from the same address
    flood[0] = False
    key, node_id = await enroll(api)
    ws = await handshake(live, key, node_id)  # a node from another address still gets in
    assert json.loads(await ws.recv()) == {"t": "ready"}
    await ws.close()
    assert await closed_with(silent[0], 3) == 4400  # silent sockets are dropped quickly


async def test_non_object_text_after_ready_is_ignored(api, env, live):  # noqa: F811
    key, node_id = await enroll(api)
    ws = await handshake(live, key, node_id)
    assert json.loads(await ws.recv()) == {"t": "ready"}
    for junk in ("[1, 2]", "5", '"ping"', "null", "{not json"):
        await ws.send(junk)
    await ws.send(json.dumps({"t": "ping"}))
    assert json.loads(await asyncio.wait_for(ws.recv(), 5)) == {"t": "pong"}
    assert not api.app.state.ctx.broker.hub.links["pc"].closed
    await ws.close()


@pytest.mark.parametrize("code", ["not-a-code-at-all", "abcdef-abcdef-abcdef-abcdef"], ids=["malformed", "wrong"])
async def test_a_bad_enroll_code_never_waits_for_the_write_lock(api, env, code):
    import sqlite3
    import time
    added = (await api.post("/api/nodes", json={"name": "pc"})).json()
    assert added["code"] != code
    _, public = keypair()
    holder = sqlite3.connect(env["settings"].data_dir / "acc.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # someone else is writing for a while
    try:
        started = time.monotonic()
        refused = await api.post("/api/nodes/enroll", json={"name": "pc", "code": code, "public_key": public})
        assert refused.status_code == 403
        assert time.monotonic() - started < 5  # busy_timeout is 15 s: it never asked for the lock
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    good = await api.post("/api/nodes/enroll", json={"name": "pc", "code": added["code"], "public_key": public})
    assert good.status_code == 200


def test_legacy_project_host_becomes_local(tmp_path):
    """Before v0.9.0 a project's host defaulted to "acc-host" (this machine). Migration 0020: a host that names no
    enrolled node is "local" again, so its sessions run here instead of waiting for a machine that doesn't exist."""
    from alembic import command
    from sqlalchemy import text

    from acc.server.db import alembic_config, make_engine, migrate
    url = f"sqlite:///{tmp_path / 'acc.db'}"
    migrate(url)
    command.downgrade(alembic_config(url), "0019")
    engine = make_engine(url)
    with engine.begin() as db:
        columns = {c[1]: c for c in db.execute(text("PRAGMA table_info(project)"))}
        required = [name for name, c in columns.items() if c[3] and c[4] is None and name not in ("id",)]
        values = {name: "x" for name in required}
        for slug, host in (("old", "acc-host"), ("far", "pc"), ("here", "local")):
            row = {**values, "slug": slug, "name": slug, "host": host, "workspace_path": f"/w/{slug}"}
            db.execute(text(f"INSERT INTO project ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})"), row)
        db.execute(text("INSERT INTO node (id, name, status, info, created_at) VALUES ('n1', 'pc', 'active', '{}', '2026-10-22')"))
    command.upgrade(alembic_config(url), "head")
    with engine.begin() as db:
        hosts = dict(db.execute(text("SELECT slug, host FROM project")).all())
    assert hosts == {"old": "local", "far": "pc", "here": "local"}


async def connect_standin(api, env, live, tmp_path, name="pc", info=None):  # noqa: F811
    key, node_id = await enroll(api, name)
    root = tmp_path / f"packages-{name}"
    (root / "requests").mkdir(parents=True)
    node = StandInNode(env["settings"].broker_socket, key, node_id, packages_root=root, info=info)
    task = asyncio.create_task(node.run(live))

    async def connected():
        nodes = {n["name"]: n for n in (await api.get("/api/nodes")).json()}
        if not nodes.get(name, {}).get("connected"):
            return None
        # Authentication precedes the initial asynchronous packages-state frame.
        if node.packages:
            view = await api.get("/api/packages", params={"machine": name})
            if view.status_code != 200 or not view.json()["reported"]:
                return None
        return nodes[name]
    await wait_for(connected)
    return node, task, root


async def audits(api, action):
    return [a for a in (await api.get("/api/audit")).json() if a["action"] == action]


async def test_sign_in_on_a_machine_runs_through_its_link(api, env, live, tmp_path):  # noqa: F811
    node, task, _ = await connect_standin(api, env, live, tmp_path)
    listing = (await api.get("/api/accounts", params={"machine": "pc"})).json()
    assert listing["machine"] == "pc" and listing["account"] == "agent" and listing["accounts"] == ["agent"]
    assert "claude" not in listing  # nothing cached for that machine yet

    health = (await api.get("/api/accounts", params={"machine": "pc", "refresh": 1})).json()
    assert health["claude"]["logged_in"] and node.opened == ["agent"]  # asked of the machine's broker
    cached = (await api.get("/api/accounts", params={"machine": "pc"})).json()
    assert cached["claude"]["logged_in"] and cached["machine"] == "pc"
    here = (await api.get("/api/accounts")).json()
    assert here["machine"] == "local"

    before = node.relayed
    flow = (await api.post("/api/accounts/claude/login", params={"machine": "pc"})).json()
    assert flow["flow_id"].startswith("agent.") and node.relayed > before  # login_start went to the machine
    before = node.relayed
    done = (await api.post(f"/api/accounts/login/{flow['flow_id']}/code", params={"machine": "pc"},
                           json={"code": "good-code"})).json()
    assert done["done"] and done["ok"] and node.relayed > before
    started = (await audits(api, "account.login_start"))[0]
    assert started["detail"] == {"account": "agent", "machine": "pc"}
    assert (await audits(api, "account.login_finished"))[0]["detail"]["machine"] == "pc"
    flow = (await api.post("/api/accounts/codex/login", params={"machine": "pc"})).json()
    assert (await api.post(f"/api/accounts/login/{flow['flow_id']}/cancel", params={"machine": "pc"})).status_code == 200

    machines = {n["name"]: n for n in (await api.get("/api/nodes")).json()}
    assert machines["pc"]["logins"]["agent"]["claude"] is True  # Host shows it at a glance

    # Refused: a machine that isn't one, an account the machine didn't report, a machine that's offline.
    assert (await api.get("/api/accounts", params={"machine": "nowhere"})).status_code == 404
    assert (await api.post("/api/accounts/claude/login", params={"machine": "nowhere"})).status_code == 404
    assert (await api.get("/api/accounts", params={"machine": "pc", "account": "ghost"})).status_code == 404
    assert (await api.get("/api/accounts/login/ghost.x", params={"machine": "pc"})).status_code == 404
    await enroll(api, "spare")  # enrolled, never connected
    assert (await api.post("/api/accounts/claude/login", params={"machine": "spare"})).status_code == 409
    task.cancel()


async def test_agent_software_on_a_machine_goes_to_its_helper(api, env, live, tmp_path):  # noqa: F811
    node, task, root = await connect_standin(api, env, live, tmp_path)
    view = (await api.get("/api/packages", params={"machine": "pc"})).json()
    assert view["machine"] == "pc" and view["reported"] and view["helper"] and view["kind"] == "helper"
    # restic is a backup tool, needed only on a backup target: not listed among a machine's agent software
    assert set(view["packages"]) == {"claude", "codex", "copilot", "antigravity", "cursor", "opencode", "ollama"}
    assert all(p["installable"] and p["group"] == "agent" and "guide" not in p for p in view["packages"].values())
    assert (await api.post("/api/packages/restic/install", params={"machine": "pc"})).status_code == 409

    node.package_gate = asyncio.Event()
    queued = await api.post("/api/packages/codex/install", params={"machine": "pc"})
    assert queued.status_code == 202
    request_id = queued.json()["request_id"]
    assert not (root / "requests" / f"{request_id}.json").exists()
    node.package_gate.set()

    async def helper_received():
        return (root / "requests" / f"{request_id}.json").is_file()
    await wait_for(helper_received)  # HTTP 202 acknowledges queueing, not remote execution
    sent = [t for t in node.texts if t.get("t") == "packages"]
    assert sent == [{"t": "packages", "id": request_id, "action": "install", "package": "codex"}]
    files = list((root / "requests").iterdir())
    assert [f.name for f in files] == [f"{request_id}.json"]  # exactly the helper's request file, nothing else
    assert json.loads(files[0].read_text()) == {"id": request_id, "package": "codex", "action": "install"}
    assert not (env["settings"].packages_dir / "requests" / f"{request_id}.json").exists()  # not this machine's

    async def pending():
        view = (await api.get("/api/packages", params={"machine": "pc"})).json()
        return view if view["pending"] else None
    assert (await wait_for(pending))["pending"][0]["package"] == "codex"
    assert (await api.get(f"/api/packages/requests/{request_id}", params={"machine": "pc"})).json()["done"] is False

    # The machine's helper does its work: state.json changes, the request file goes, acc-node reports it.
    files[0].unlink()
    (root / "state.json").write_text(json.dumps({"packages": {"codex": {
        "label": "Codex", "method": "npm", "installed": "0.170.0", "auto_update": True, "evil": "x" * 10,
        "last": {"action": "install", "ok": True, "detail": "Codex 0.170.0 installed", "at": 9e12}}}}))
    await node.report()

    async def installed():
        view = (await api.get("/api/packages", params={"machine": "pc"})).json()
        return view if view["packages"]["codex"].get("installed") else None
    codex = (await wait_for(installed))["packages"]["codex"]
    assert codex["installed"] == "0.170.0" and "evil" not in codex  # only the fields the dashboard shows
    result = (await api.get(f"/api/packages/requests/{request_id}", params={"machine": "pc"})).json()
    assert result["done"] and result["ok"]
    machines = {n["name"]: n for n in (await api.get("/api/nodes")).json()}
    assert machines["pc"]["software"]["codex"]["installed"] == "0.170.0"

    audit = (await audits(api, "package.install"))[0]
    assert audit["target"] == "codex" and audit["detail"] == {"machine": "pc"}

    # Refused on the primary: unknown packages and actions, auto-update remotely, unknown or offline machines.
    assert (await api.post("/api/packages/rm-rf/install", params={"machine": "pc"})).status_code == 404
    assert (await api.post("/api/packages/codex/exec", params={"machine": "pc"})).status_code == 404
    assert (await api.post("/api/packages/codex/auto", params={"machine": "pc"}, json={"enabled": False})).status_code == 422
    assert (await api.post("/api/packages/codex/install", params={"machine": "nowhere"})).status_code == 404
    await enroll(api, "spare")
    assert (await api.get("/api/packages", params={"machine": "spare"})).status_code == 409
    assert len([t for t in node.texts if t.get("t") == "packages"]) == 1  # none of those reached the node

    # Refused on the node: a second request for a package that's still waiting is reported back, not queued.
    (root / "requests" / "pkg-00000000000a.json").write_text(json.dumps({"id": "pkg-00000000000a", "package": "copilot", "action": "install"}))
    again = (await api.post("/api/packages/copilot/update", params={"machine": "pc"})).json()["request_id"]

    async def refused():
        result = (await api.get(f"/api/packages/requests/{again}", params={"machine": "pc"})).json()
        return result if result["done"] else None
    assert "already has a request waiting" in (await wait_for(refused))["detail"]
    task.cancel()


async def test_new_project_on_another_machine_is_made_through_its_broker(api, env, live, tmp_path):  # noqa: F811
    """New project with a machine: the folder is made there (make_workspace, clone_url through that machine's
    broker, under its account's workspace root), trusted there, and registered on that machine."""
    node, task, _ = await connect_standin(api, env, live, tmp_path)
    folder = (await api.get("/api/projects/new/folder", params={"host": "pc"})).json()
    assert folder == {"host": "pc", "account": "agent", "root": str(env["workspaces"])}  # the broker's first root

    before = node.relayed
    made = await api.post("/api/projects/new", json={"name": "Remote app", "host": "pc"})
    assert made.status_code == 201, made.text
    project = made.json()
    assert project["host"] == "pc" and project["account"] == "agent" and project["trusted"] is True
    assert project["workspace_path"] == str(env["workspaces"] / "remote-app")
    assert (env["workspaces"] / "remote-app/.git").is_dir() and node.relayed > before  # made over the link
    claude = json.loads((env["home"] / ".claude.json").read_text())
    assert claude["projects"][project["workspace_path"]]["hasTrustDialogAccepted"] is True  # trusted there
    audit = (await audits(api, "project.create"))[0]
    assert audit["target"] == "remote-app" and audit["detail"]["machine"] == "pc" and audit["detail"]["account"] == "agent"

    # the same rules as on this machine: a clone isn't trusted unless asked, URLs are checked, slugs are free
    refused = await api.post("/api/projects/new", json={"name": "Bad", "source": "clone", "host": "pc",
                                                        "url": "https://user:secret@git.example.com/x.git"})
    assert refused.status_code == 422
    assert (await api.post("/api/projects/new", json={"name": "Remote app", "host": "pc"})).status_code == 409
    assert (await api.post("/api/projects/new", json={"name": "X", "host": "nowhere"})).status_code == 404
    await enroll(api, "spare")  # enrolled, not connected
    assert (await api.post("/api/projects/new", json={"name": "X", "host": "spare"})).status_code == 409
    assert (await api.post("/api/projects/new", json={"name": "X", "host": "pc", "account": "ghost"})).status_code == 422
    assert (await api.post("/api/projects/new", json={"name": "X", "account": "ghost"})).status_code == 422  # here: yours
    task.cancel()


async def test_new_project_on_an_older_machine_uses_the_installers_default_root(api, env, live, tmp_path, monkeypatch):  # noqa: F811
    from acc.server.broker_client import Brokers
    node, task, _ = await connect_standin(api, env, live, tmp_path)
    real = Brokers.call

    async def old_ping(self, method, account=None, host=None, **params):
        result = await real(self, method, account=account, host=host, **params)
        if method == "ping" and host == "pc":
            result = {k: v for k, v in result.items() if k != "roots"}  # a node from before "roots"
        return result
    monkeypatch.setattr(Brokers, "call", old_ping)
    assert (await api.get("/api/projects/new/folder", params={"host": "pc"})).json()["root"] == "/srv/acc/workspaces"
    task.cancel()


async def test_wsl_machines_read_apart_from_plain_linux(api, env, live, tmp_path):  # noqa: F811
    """Owner: a Windows PC's WSL node shows as "Linux (WSL2) · Windows 11", not plain linux/x86_64."""
    tasks = []
    for name, info in (("wsl-pc", {"os": "linux", "arch": "x86_64", "wsl": "2", "windows": "Windows 11 (build 22631)"}),
                       ("server", {"os": "linux", "arch": "x86_64", "wsl": "0"}),
                       ("old-box", {"os": "linux", "arch": "x86_64"})):  # an older node: never said
        tasks.append((await connect_standin(api, env, live, tmp_path, name=name, info=info))[1])
    machines = {n["name"]: n for n in (await api.get("/api/nodes")).json()}
    assert machines["wsl-pc"]["system"] == {"kind": "wsl", "label": "Linux (WSL2) · Windows 11 (build 22631)",
                                            "wsl": "2", "windows": "Windows 11 (build 22631)"}
    assert machines["wsl-pc"]["info"]["wsl"] == "2"
    assert machines["server"]["system"] == {"kind": "linux", "label": "Linux"}
    assert machines["old-box"]["system"] == {"kind": "unknown", "label": "Linux (WSL or not: unknown)"}
    # and the by-hand steps for that machine are WSL's
    link = api.app.state.ctx.broker.hub.links["wsl-pc"]
    link.packages = {**link.packages, "helper": False}
    view = (await api.get("/api/packages", params={"machine": "wsl-pc"})).json()
    assert view["packages"]["claude"]["guide"]["system"] == "wsl"
    for task in tasks:
        task.cancel()


async def test_a_mac_is_never_offered_what_it_would_refuse(api, env, live, tmp_path, monkeypatch):  # noqa: F811
    """demo-mac refused "Cursor CLI can't be installed on a Mac from Rynhult": the API now refuses it itself, before
    anything reaches the Mac, and the view gives the steps to install it there by hand instead."""
    node, task, _ = await connect_standin(api, env, live, tmp_path, name="mac")
    link = api.app.state.ctx.broker.hub.links["mac"]
    # The stand-in is a Linux helper. Its replies must keep the Mac capabilities this case simulates;
    # otherwise a queued check/install can report helper=True after we switch it off below.
    helper = True
    message = node.packages.message
    monkeypatch.setattr(node.packages, "message", lambda: {**message(), "kind": "mac", "helper": helper})
    link.info = {**link.info, "os": "darwin"}
    link.packages = {**link.packages, "kind": "mac", "helper": True}
    view = (await api.get("/api/packages", params={"machine": "mac", "account": "agent"})).json()
    assert "restic" not in view["packages"]
    for name in ("codex", "copilot"):  # acc-node installs these with Homebrew's npm
        assert view["packages"][name]["installable"] and "guide" not in view["packages"][name]
    for name in ("claude", "antigravity", "cursor", "opencode", "ollama"):
        entry = view["packages"][name]
        assert not entry["installable"] and entry["guide"]["system"] == "macos", name
        assert entry["guide"]["docs"].startswith("https://") and entry["guide"]["commands"], name
    cursor = view["packages"]["cursor"]["guide"]
    assert cursor["commands"][0]["command"] == "curl https://cursor.com/install -fsS | bash"
    assert cursor["docs"] == "https://cursor.com/docs/cli/installation" and cursor["after"] == "signin"
    assert view["packages"]["claude"]["guide"]["commands"][0] == {
        "how": "Homebrew", "command": "brew install --cask claude-code", "link": False}

    sent = len([t for t in node.texts if t.get("t") == "packages"])
    for action in ("install", "update", "remove"):
        refused = await api.post(f"/api/packages/cursor/{action}", params={"machine": "mac"})
        assert refused.status_code == 409, action
        assert refused.json()["detail"].startswith("Cursor CLI can't be installed on a Mac from Rynhult")
    assert len([t for t in node.texts if t.get("t") == "packages"]) == sent  # nothing reached the Mac
    # Checking a CLI installed by hand only reads its version: allowed
    assert (await api.post("/api/packages/cursor/check", params={"machine": "mac"})).status_code == 202
    assert (await api.post("/api/packages/codex/install", params={"machine": "mac"})).status_code == 202

    # A Mac without Homebrew's npm installs nothing: every CLI gets steps, and requests are refused.
    helper = False
    link.packages = {**link.packages, "helper": False}
    view = (await api.get("/api/packages", params={"machine": "mac"})).json()
    assert not view["packages"]["codex"]["installable"]
    assert view["packages"]["codex"]["guide"]["commands"][0]["command"] == "brew install --cask codex"
    assert (await api.post("/api/packages/codex/install", params={"machine": "mac"})).status_code == 409
    task.cancel()


async def test_a_linux_machine_without_its_helper_gets_steps(api, env, live, tmp_path):  # noqa: F811
    node, task, _ = await connect_standin(api, env, live, tmp_path, name="wsl-pc")
    link = api.app.state.ctx.broker.hub.links["wsl-pc"]
    link.info = {**link.info, "os": "linux", "wsl": "2"}
    link.packages = {**link.packages, "helper": False}
    view = (await api.get("/api/packages", params={"machine": "wsl-pc", "account": "agent"})).json()
    guide = view["packages"]["claude"]["guide"]
    assert guide["system"] == "wsl" and guide["shell_command"] == "sudo -iu agent" and "Ubuntu (WSL)" in guide["shell"]
    assert guide["commands"][0]["command"] == "curl -fsSL https://claude.ai/install.sh | bash"
    refused = await api.post("/api/packages/claude/install", params={"machine": "wsl-pc"})
    assert refused.status_code == 409 and "Update Rynhult there" in refused.json()["detail"]
    task.cancel()


async def test_a_node_that_floods_text_is_ignored_not_trusted(api, env, live):  # noqa: F811
    key, node_id = await enroll(api)
    ws = await handshake(live, key, node_id)
    assert json.loads(await ws.recv()) == {"t": "ready"}
    await ws.send(json.dumps({"t": "packages-state", "packages": {"codex": {"installed": "1"}}, "pad": "x" * 70000}))
    await ws.send(json.dumps({"t": "packages-state", "kind": "root", "pending": [{"id": "../x", "package": "codex", "action": "install"}],
                              "packages": {"sh": {"installed": "1"}, "codex": "nope", "copilot": {"installed": ["x"], "running": "rm"}}}))

    async def reported():
        view = (await api.get("/api/packages", params={"machine": "pc"}))
        return view.json() if view.status_code == 200 and view.json()["reported"] else None
    view = await wait_for(reported)
    reported = {n: {k: v for k, v in p.items() if k not in ("group", "installable", "guide")}
                for n, p in view["packages"].items()}  # what the node said, without the primary's own notes
    assert view["kind"] == "helper" and view["pending"] == [] and reported["codex"] == {}
    assert reported["copilot"] == {} and "sh" not in view["packages"]
    await ws.close()


async def test_a_project_on_another_machine_runs_as_one_of_its_accounts(api, env):
    """Found live: a machine whose installer named its agent account differently (acc-agent, installed as root) left
    the project on the owner's own account name, which the machine doesn't have. Now: yours if it has it, else its
    only one, else name one; never an account it didn't report."""
    from acc.server.models import Node
    with write_db(api.app.state.db) as s:
        s.add(Node(id="n" * 16, name="box", status="active", info={"accounts": ["acc-agent"]}))
        s.add(Node(id="m" * 16, name="two", status="active", info={"accounts": ["a-agent", "b-agent"]}))
        s.commit()
    base = {"name": "R", "workspace_path": "/srv/acc/workspaces/r"}
    only = (await api.post("/api/projects", json={**base, "slug": "r1", "host": "box"})).json()
    assert only["account"] == "acc-agent"
    choose = await api.post("/api/projects", json={**base, "slug": "r2", "host": "two"})
    assert choose.status_code == 422 and "a-agent, b-agent" in choose.text
    named = (await api.post("/api/projects", json={**base, "slug": "r3", "host": "two", "account": "b-agent"})).json()
    assert named["account"] == "b-agent"
    bad = await api.post("/api/projects", json={**base, "slug": "r4", "host": "two", "account": "root"})
    assert bad.status_code == 422
    here = await api.post("/api/projects", json={**base, "slug": "r5", "account": "b-agent",
                                                 "workspace_path": str(env["workspaces"] / "demo")})
    assert here.status_code == 422  # on this machine, always your own account


async def test_update_completion_is_audited_after_running_state(api, env, live):  # noqa: F811
    import time
    key, node_id = await enroll(api)
    ws = await handshake(live, key, node_id)
    try:
        result = {'id':'example-update','version':'0.12.0','at':time.time(),'running':True}
        await ws.send(json.dumps({'t':'update-state','info':{'version':'0.12.0','build':'a'*64},'result':result}))
        async def running():
            row=(await api.get('/api/nodes')).json()[0]
            return row.get('update',{}).get('running')
        await wait_for(running)
        result.update(running=False,ok=True)
        for _ in range(2):
            await ws.send(json.dumps({'t':'update-state','info':{'version':'0.12.0','build':'a'*64},'result':result}))
        async def complete():
            return len(await audits(api,'update.result')) == 1
        assert await wait_for(complete)
    finally:
        await ws.close()


async def test_follow_sends_the_mains_build_on_reconnect(api, env, live, monkeypatch):  # noqa: F811
    key, node_id = await enroll(api)
    service = api.app.state.ctx.updates
    descriptor = {'version':'0.12.0','build':service.build,'sha256':'b'*64}
    monkeypatch.setattr(service,'bundle_descriptor',lambda:descriptor)
    assert (await api.post('/api/updates/machines/pc/follow',json={'enabled':True})).status_code == 200
    for _ in range(2):
        ws = await handshake(live,key,node_id, info={"os": "linux", "build": "a" * 64, "capabilities": ["update"]})
        try:
            assert json.loads(await ws.recv()) == {'t':'ready'}
            assert json.loads(await asyncio.wait_for(ws.recv(), 5)) == {'t':'update',**descriptor}
        finally:
            await ws.close()

async def test_authenticated_link_negotiates_preview_and_cancels_on_disconnect(api, env, live, preview_socket):  # noqa: F811
    """Capability sanitation and actual binary dispatch must preserve preview frames."""
    from acc.node.preview import PreviewRelay
    from acc.preview_stream import pack
    key, node_id = await enroll(api)
    ready = asyncio.Event()
    async def node_run():
        async with websockets.connect(f'{live}/api/nodes/connect') as ws:
            challenge = json.loads(await ws.recv())
            signature = key.sign(base64.b64decode(challenge['nonce']) + challenge['primary'].encode())
            await ws.send(json.dumps({'t':'hello','node_id':node_id,
                'signature':base64.b64encode(signature).decode(),
                'info':{'capabilities':['preview-v1', 'unknown-future']},'accounts':[]}))
            assert json.loads(await ws.recv()) == {'t':'ready'}
            relay = PreviewRelay(ws.send)
            ready.set()
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        relay.receive(message)
            finally:
                await relay.aclose()
    task = asyncio.create_task(node_run())
    server = None
    try:
        await asyncio.wait_for(ready.wait(), 5)
        link = api.app.state.ctx.broker.hub.get('pc')
        assert link.info['capabilities'] == ['preview-v1']
        stream = link.preview.create(1)
        # No reservation grant: the node itself refuses and returns cancellation through the real socket.
        await link.preview_sender.send(pack(b'O', 1, b'{"project":1,"host":"127.0.0.1","port":8000,"mode":"probe"}'))
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(stream.recv(), 2)
        from websockets.asyncio.server import serve
        async def echo(ws):
            async for message in ws:
                await ws.send(message)
        port = preview_socket.getsockname()[1]
        server = await serve(echo, sock=preview_socket, subprotocols=['hmr'])
        await link.preview_sender.send(pack(b'R', 0, json.dumps({'project':1,'ports':[port]}).encode()))
        socket = link.preview.create(3)
        await link.preview_sender.send(pack(b'O', 3, json.dumps({'project':1, 'host':'127.0.0.1',
            'port':port, 'mode':'ws', 'method':'GET', 'path':'/hmr', 'headers':{}, 'protocols':['hmr']}).encode()))
        kind, value = await socket.recv()
        assert kind == b'H' and json.loads(value) == {'protocol':'hmr'}
        await socket.send(b'T', b'through the authenticated machine link')
        assert await socket.recv() == (b'T', b'through the authenticated machine link')
        await socket.send(b'B', b'\x00\xff')
        assert await socket.recv() == (b'B', b'\x00\xff')
        await socket.close()
        waiting = link.preview.create(5)
        api.app.state.ctx.broker.hub.disconnect('pc')
        with pytest.raises(ConnectionError):
            await waiting.recv()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if server:
            server.close()
            await server.wait_closed()


async def test_rename_capability_survives_handshake_and_is_cleared_on_old_reconnect(api, live):  # noqa: F811
    key, node_id = await enroll(api)
    ws = await handshake(live, key, node_id, info={'capabilities': ['rename-compat-1', 'unknown-future']})
    try:
        assert (await api.get('/api/nodes')).json()[0]['info']['capabilities'] == ['rename-compat-1']
    finally:
        await ws.close()
    ws = await handshake(live, key, node_id, info={})
    try:
        assert (await api.get('/api/nodes')).json()[0]['info'].get('capabilities', []) == []
    finally:
        await ws.close()


async def test_request_acknowledgement_uses_authenticated_node_service_not_agent_spool(api, env, live, tmp_path):  # noqa: F811
    from test_publish import agent_asks

    from acc.node.client import Node as Daemon
    from acc.node.config import NodeConfig
    from acc.server.models import ToolRequest

    key, node_id = await enroll(api)
    protected = tmp_path / 'node-protected-replies'
    protected.mkdir(mode=0o711)
    daemon = Daemon(NodeConfig(primary='https://main.example.lan', node_id=node_id), key,
                    packages=None, request_replies_root=protected)
    node = StandInNode(env['settings'].broker_socket, key, node_id,
                      info={'os': 'linux', 'capabilities': daemon.capabilities()})
    node.reply_handler = daemon.handle_text
    task = asyncio.create_task(node.run(live))
    session = None
    try:
        await wait_for(lambda: _connected(api))
        assert 'request-replies-v1' in (await api.get('/api/nodes')).json()[0]['info']['capabilities']
        root = env['workspaces'] / 'remote-ack'
        root.mkdir()
        project = (await api.post('/api/projects', json={'slug': 'remote-ack', 'name': 'Remote ack',
                                  'host': 'pc', 'workspace_path': str(root)})).json()
        assert (await api.post(f"/api/projects/{project['id']}/trust")).status_code == 200
        response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Remote requester'})
        assert response.status_code == 201, response.text
        session = response.json()
        await wait_for(state_is(api, session['id'], 'idle'), timeout=20)
        rid = 'f' * 32
        # A forged legacy attachment cannot become a daemon-owned acknowledgement.
        spool = env['run_dir'] / session['id']
        (spool / 'request-replies.json').write_text(json.dumps({rid: {'status': 'accepted', 'approval_id': 'forged'}}))
        file = protected / session['id'] / (rid + '.json')
        assert not file.exists()
        agent_asks(env, session, {'type': 'tool', 'manager': 'npm', 'name': 'prettier',
                                'reason': 'Format output', 'request_id': rid})
        await wait_for(lambda: file.exists())
        envelope = json.loads(file.read_text())
        assert envelope['session_id'] == session['id'] and envelope['request_id'] == rid
        assert envelope['reply']['status'] == 'accepted' and envelope['reply']['approval_id'] != 'forged'
        with api.app.state.db() as db:
            approval = db.get(ToolRequest, envelope['reply']['approval_id'])
            assert approval.session_id == session['id'] and approval.status == 'pending_approval'
    finally:
        if session:
            await api.post(f"/api/sessions/{session['id']}/stop")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_missing_github_cli_names_the_node_and_install_remedy(api, env, live, tmp_path):  # noqa: F811
    env['broker'].providers['gh'] = str(tmp_path / 'missing-gh')
    node, _, _ = await connect_standin(api, env, live, tmp_path)
    before = node.relayed
    result = await api.post('/api/accounts/github/login', params={'machine': 'pc'})
    assert result.status_code == 400
    assert result.json()['detail'].startswith('gh is not installed on pc:')
    assert '--install-prereqs' in result.json()['detail']
    assert node.relayed > before
    assert not await audits(api, 'account.login_start')
