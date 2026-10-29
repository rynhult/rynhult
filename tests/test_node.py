"""acc-node against a fake primary (a websockets server with a self-signed certificate) and fake brokers
(Unix echo servers)."""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime
import hashlib
import http.server
import io
import json
import os
import ssl
import stat
import struct
import subprocess
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.x509.oid import NameOID
from helpers import wait_for
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from acc.node import cli
from acc.node import client as client_module
from acc.node.client import AuthRejected, Node, broker_accounts, connect_url
from acc.node.config import ConfigError, NodeConfig, normalize_fingerprint, read_env_file
from acc.node.enroll import EnrollError, enroll
from acc.node.identity import TrustError, load_key

HEADER = struct.Struct(">cI")
PRIMARY_ID = "primary-test"
TIMEOUT = 5
MAX_DATA = 1 << 20  # the spec's cap per frame


def frame(kind: bytes, channel_id: int, payload: bytes = b"") -> bytes:
    return HEADER.pack(kind, channel_id) + payload


def name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


class CA:
    """A private CA like Caddy's internal one: a long-lived root that issues short-lived leaves."""

    def __init__(self, directory: Path, label: str = "Rynhult Test Root"):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.timezone.utc)
        self.certificate = (
            x509.CertificateBuilder().subject_name(name(label)).issuer_name(name(label))
            .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False)
            .sign(self.key, hashes.SHA256()))
        self.pem = self.certificate.public_bytes(serialization.Encoding.PEM)
        self.path = directory / "root.pem"
        self.path.write_bytes(self.pem)
        self.fingerprint = hashlib.sha256(self.certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
        self.issued = 0

    def server_context(self, hostname: str = "localhost") -> ssl.SSLContext:
        """A fresh leaf for `hostname` (valid for hours, as Caddy's are), loaded into a server context."""
        self.issued += 1
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.timezone.utc)
        leaf = (x509.CertificateBuilder().subject_name(name(hostname)).issuer_name(self.certificate.subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(hours=12))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()),
                               critical=False)
                .sign(self.key, hashes.SHA256()))
        cert_path = self.directory / f"leaf{self.issued}.pem"
        key_path = self.directory / f"leaf{self.issued}.key"
        cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        return context


class TLS:
    def __init__(self, ca: CA):
        self.ca = ca
        self.server_context = ca.server_context()


@pytest.fixture
def tls(tmp_path):
    return TLS(CA(tmp_path / "tls"))


class PrimarySide:
    """The primary's end of one authenticated node connection."""

    def __init__(self, ws):
        self.ws = ws
        self.texts: list[str] = []

    async def send(self, kind: bytes, channel_id: int, payload: bytes = b"") -> None:
        await self.ws.send(frame(kind, channel_id, payload))

    async def recv(self) -> tuple[bytes, int, bytes]:
        while True:
            message = await asyncio.wait_for(self.ws.recv(), TIMEOUT)
            if isinstance(message, str):
                self.texts.append(message)
                continue
            kind, channel_id = HEADER.unpack_from(message)
            return kind, channel_id, message[HEADER.size:]

    async def open(self, channel_id: int, account: str) -> tuple[bytes, int, bytes]:
        await self.send(b"o", channel_id, json.dumps({"account": account}).encode())
        return await self.recv()

    async def recv_data(self, channel_id: int, size: int) -> bytes:
        received = bytearray()
        while len(received) < size:
            kind, got_id, payload = await self.recv()
            assert (kind, got_id) == (b"d", channel_id)
            assert len(payload) <= MAX_DATA
            received += payload
        return bytes(received)


class FakePrimary:
    """GET /api/nodes/connect: challenge → hello (verified against `public_key`) → ready, then hands the
    connection to the test."""

    def __init__(self, ssl_context: ssl.SSLContext, public_key: Ed25519PublicKey):
        self.ssl_context, self.public_key = ssl_context, public_key
        self.hellos: list[dict] = []
        self.paths: list[str] = []
        self.connections: asyncio.Queue[PrimarySide] = asyncio.Queue()
        self.rejected = 0
        self.refuse = 0  # reject this many connections even with a good signature

    async def handler(self, ws) -> None:
        self.paths.append(ws.request.path)
        nonce = os.urandom(32)
        await ws.send(json.dumps({"t": "challenge", "nonce": base64.b64encode(nonce).decode(),
                                  "primary": PRIMARY_ID}))
        hello = json.loads(await ws.recv())
        self.hellos.append(hello)
        try:
            self.public_key.verify(base64.b64decode(hello["signature"]), nonce + PRIMARY_ID.encode())
            if self.refuse > 0:
                self.refuse -= 1
                raise InvalidSignature
        except InvalidSignature:
            self.rejected += 1
            await ws.close(4401, "bad signature")
            return
        await ws.send(json.dumps({"t": "ready"}))
        await self.connections.put(PrimarySide(ws))
        await ws.wait_closed()

    async def next_connection(self) -> PrimarySide:
        return await asyncio.wait_for(self.connections.get(), TIMEOUT)

    async def __aenter__(self):
        self.server = await serve(self.handler, "127.0.0.1", 0, ssl=self.ssl_context)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


class FakeBroker:
    """Echoes what it gets; `quit\\n` makes it hang up, `big:<n>\\n` makes it send n bytes."""

    def __init__(self, path: Path):
        self.path = path
        self.connections = 0
        self.closed = 0
        self.closed_event = asyncio.Event()
        self.received = bytearray()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while data := await reader.read(65536):
                self.received += data
                if data == b"quit\n":
                    break
                if data.startswith(b"big:"):
                    writer.write(b"x" * int(data[4:]))
                else:
                    writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass
        writer.close()
        self.closed += 1
        self.closed_event.set()

    async def wait_closed(self) -> None:
        await asyncio.wait_for(self.closed_event.wait(), TIMEOUT)
        self.closed_event.clear()

    async def __aenter__(self):
        self.server = await asyncio.start_unix_server(self.handle, path=str(self.path))
        return self

    async def __aexit__(self, *exc):
        self.server.close()


@pytest.fixture(autouse=True)
def isolated_update_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ACC_UPDATE_ROOT", str(tmp_path / "updates"))


@pytest.fixture
def node_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def socket_dir(tmp_path):
    directory = tmp_path / "s"
    directory.mkdir()
    return directory


def node_for(port: int, tls: TLS, key, socket_dir: Path, state_dir: Path | None = None, *, host: str = "localhost",
             trust: str = "ca", **options) -> Node:
    config = NodeConfig(primary=f"https://{host}:{port}", node_id="node-7", trust=trust,
                        ca_file=tls.ca.path if trust == "ca" else None, ca_fingerprint=tls.ca.fingerprint,
                        name="lab", socket_dir=socket_dir)
    options.setdefault("min_backoff", 0.05)
    options.setdefault("packages", None)
    node = Node(config, key, state_dir, **options)
    node.updates.root = socket_dir.parent / "updates"
    # Transport tests exercise reporting, not the gate machine's disk/home/service inventory.
    node.host_collect = (lambda: {"hostname": "test-node", "collected_at": 1},
                         lambda: {"collected_at": 1, "cpu_percent": 0, "memory_percent": 10, "disk": None})
    return node


class Running:
    """A Node.run_forever task, cancelled on exit."""

    def __init__(self, node: Node):
        self.node = node

    async def __aenter__(self):
        self.task = asyncio.create_task(self.node.run_forever())
        return self.node

    async def __aexit__(self, *exc):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass


@pytest.fixture
async def world(tls, node_key, socket_dir, tmp_path):
    """Primary + an `alice` broker + a running node; yields (primary, broker, node, side)."""
    context = tls.server_context
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    async with FakePrimary(context, node_key.public_key()) as primary, \
            FakeBroker(socket_dir / "alice.sock") as broker, \
            Running(node_for(primary.port, tls, node_key, socket_dir, state_dir)) as node:
        side = await primary.next_connection()
        yield primary, broker, node, side


# ---- handshake -------------------------------------------------------------------------------------------------

async def test_handshake_sends_signed_hello(world, tmp_path):
    primary, _broker, _node, _side = world
    assert primary.paths == ["/api/nodes/connect"]
    hello = primary.hellos[0]
    assert hello["t"] == "hello" and hello["node_id"] == "node-7"
    assert hello["accounts"] == ["alice"]
    assert set(hello["info"]) - {"windows"} == {"os", "release", "arch", "hostname", "version", "build", "wsl", "capabilities"}
    assert hello["info"]["capabilities"] == ["rename-compat-1", "preview-v1", "host-v1", "backup-v1", "git-backup-v1", "update"]
    status = json.loads((tmp_path / "state/status.json").read_text())
    assert status["state"] == "connected"


async def test_bad_signature_is_rejected_and_retried(tls, socket_dir):
    context = tls.server_context
    registered = Ed25519PrivateKey.generate()  # the primary knows a different key
    async with FakePrimary(context, registered.public_key()) as primary:
        node = node_for(primary.port, tls, Ed25519PrivateKey.generate(), socket_dir)
        with pytest.raises(AuthRejected, match="rejected"):
            await node.connect_once()
        assert primary.rejected == 1 and primary.connections.empty()

        delays = []

        async def record(delay):
            delays.append(delay)
            if len(delays) == 3:
                raise asyncio.CancelledError
            await asyncio.sleep(0)

        node.sleep = record
        with pytest.raises(asyncio.CancelledError):
            await node.run_forever()
        assert primary.rejected == 4
        assert delays == [0.05, 0.1, 0.2]


# ---- TLS trust -------------------------------------------------------------------------------------------------

async def test_leaf_from_another_ca_is_refused(tls, node_key, socket_dir, tmp_path):
    impostor = CA(tmp_path / "impostor")  # same name as the real root, different key
    async with FakePrimary(impostor.server_context(), node_key.public_key()) as primary:
        node = node_for(primary.port, tls, node_key, socket_dir)
        with pytest.raises(ssl.SSLCertVerificationError, match="certificate verify failed"):
            await asyncio.wait_for(node.connect_once(), TIMEOUT)
        assert primary.paths == []  # the upgrade request never went out


async def test_hostname_is_checked(tls, node_key, socket_dir):
    async with FakePrimary(tls.server_context, node_key.public_key()) as primary:
        node = node_for(primary.port, tls, node_key, socket_dir, host="127.0.0.1")  # the leaf names localhost only
        with pytest.raises(ssl.SSLCertVerificationError, match="IP address mismatch|hostname"):
            await asyncio.wait_for(node.connect_once(), TIMEOUT)
        assert primary.paths == []


async def test_renewed_leaf_is_accepted(tls, node_key, socket_dir):
    """The reason for trusting the CA rather than pinning the leaf: Caddy's internal leaves last hours."""
    primary = FakePrimary(tls.server_context, node_key.public_key())
    async with primary:
        port = primary.port
        async with Running(node_for(port, tls, node_key, socket_dir, max_backoff=0.2)):
            await primary.next_connection()
            primary.server.close()
            await primary.server.wait_closed()
            primary.server = await serve(primary.handler, "127.0.0.1", port, ssl=tls.ca.server_context())
            await primary.next_connection()
    assert tls.ca.issued == 2 and len(primary.hellos) == 2


async def test_system_trust(tls, node_key, socket_dir, tmp_path, monkeypatch):
    async with FakePrimary(tls.server_context, node_key.public_key()) as primary:
        node = node_for(primary.port, tls, node_key, socket_dir, trust="system")
        # the private CA is not a system CA
        monkeypatch.setenv("SSL_CERT_FILE", str(CA(tmp_path / "public").path))
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.wait_for(node.connect_once(), TIMEOUT)
        assert primary.paths == []
        # pretend it is one (as a Let's Encrypt root would be)
        monkeypatch.setenv("SSL_CERT_FILE", str(tls.ca.path))
        task = asyncio.create_task(node.connect_once())
        await primary.next_connection()
        task.cancel()


async def test_backoff_doubles_to_sixty_seconds(tls, socket_dir, node_key, unused_tcp_port):
    node = node_for(unused_tcp_port, tls, node_key, socket_dir, min_backoff=1, max_backoff=60)
    delays = []

    async def record(delay):
        delays.append(delay)
        if len(delays) == 9:
            raise asyncio.CancelledError

    node.sleep = record
    with pytest.raises(asyncio.CancelledError):
        await node.run_forever()
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]


async def test_backoff_resets_after_ready(tls, node_key, socket_dir):
    context = tls.server_context
    async with FakePrimary(context, node_key.public_key()) as primary:
        primary.refuse = 2
        node = node_for(primary.port, tls, node_key, socket_dir)
        delays = []

        async def record(delay):
            delays.append(delay)
            await asyncio.sleep(0.01)

        node.sleep = record
        async with Running(node):
            side = await primary.next_connection()
            assert delays == [0.05, 0.1]
            await side.ws.close()
            await primary.next_connection()
            assert delays == [0.05, 0.1, 0.05]


@pytest.fixture
def unused_tcp_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- ping / pong -----------------------------------------------------------------------------------------------

async def test_answers_ping(world):
    _primary, _broker, _node, side = world
    await side.ws.send(json.dumps({"t": "ping"}))
    assert await next_control(side.ws) == {"t": "pong"}


async def next_control(ws) -> dict:
    """The next text frame that isn't the node's agent-software report (sent on connect when this machine has the
    package helper's state, as acc-host does)."""
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT))
        if message.get("t") not in ("packages-state", "update-state", "host-light"):
            return message


async def test_three_missed_pongs_end_the_connection(tls, node_key, socket_dir):
    context = tls.server_context
    async with FakePrimary(context, node_key.public_key()) as primary, \
            Running(node_for(primary.port, tls, node_key, socket_dir, ping_interval=0.05)):
        side = await primary.next_connection()
        pings = 0
        with pytest.raises(ConnectionClosed):
            while True:
                assert await next_control(side.ws) == {"t": "ping"}
                pings += 1
        assert pings == 3
        await primary.next_connection()  # and it comes back


async def test_pongs_keep_the_connection(tls, node_key, socket_dir):
    context = tls.server_context
    async with FakePrimary(context, node_key.public_key()) as primary, \
            Running(node_for(primary.port, tls, node_key, socket_dir, ping_interval=0.02)):
        side = await primary.next_connection()
        for _ in range(8):
            assert await next_control(side.ws) == {"t": "ping"}
            await side.ws.send(json.dumps({"t": "pong"}))
        assert primary.connections.empty() and len(primary.hellos) == 1


# ---- channels --------------------------------------------------------------------------------------------------

async def test_open_and_relay_both_ways(world):
    _primary, broker, _node, side = world
    assert await side.open(1, "alice") == (b"k", 1, b"")
    await side.send(b"d", 1, b'{"id": 1, "method": "ping"}\n')
    assert await side.recv_data(1, 28) == b'{"id": 1, "method": "ping"}\n'
    # a second channel to the same broker is its own connection
    assert await side.open(3, "alice") == (b"k", 3, b"")
    await side.send(b"d", 3, b"three\n")
    assert await side.recv_data(3, 6) == b"three\n"
    assert broker.connections == 2


async def test_broker_output_is_split_into_1mib_frames(world):
    _primary, _broker, _node, side = world
    await side.open(5, "alice")
    size = 3 * MAX_DATA + 123
    await side.send(b"d", 5, f"big:{size}".encode())
    assert await side.recv_data(5, size) == b"x" * size


@pytest.mark.parametrize("payload", [
    {"account": "../etc/passwd"}, {"account": "Alice"}, {"account": ""}, {"account": "a" * 33},
    {"account": "alice/x"}, {"account": 7}, {}, "not json",
])
async def test_bad_account_names_are_refused(world, payload):
    _primary, broker, _node, side = world
    raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
    await side.send(b"o", 9, raw)
    kind, channel_id, reason = await side.recv()
    assert (kind, channel_id) == (b"e", 9) and b"account" in reason
    assert broker.connections == 0


async def test_missing_broker_is_an_error(world):
    _primary, _broker, _node, side = world
    kind, channel_id, reason = await side.open(11, "bob")
    assert (kind, channel_id) == (b"e", 11)
    assert reason.startswith(b"no broker for bob")


async def test_socket_outside_the_socket_dir_is_refused(world, tmp_path, socket_dir):
    _primary, _broker, _node, side = world
    elsewhere = tmp_path / "x"
    elsewhere.mkdir()
    async with FakeBroker(elsewhere / "carol.sock") as outside:
        (socket_dir / "carol.sock").symlink_to(elsewhere / "carol.sock")
        kind, _, reason = await side.open(13, "carol")
        assert kind == b"e" and b"outside" in reason
        assert outside.connections == 0


async def test_close_from_primary_flushes_then_closes(world):
    _primary, broker, node, side = world
    await side.open(1, "alice")
    await side.send(b"d", 1, b"last words\n")
    await side.send(b"c", 1)
    await broker.wait_closed()
    assert broker.received.endswith(b"last words\n")
    assert node.relay.channels == {}
    # data for the forgotten channel is ignored, and the connection still works
    await side.send(b"d", 1, b"late\n")
    assert await side.open(3, "alice") == (b"k", 3, b"")


async def test_close_from_broker(world):
    _primary, broker, node, side = world
    await side.open(1, "alice")
    await side.send(b"d", 1, b"quit\n")
    assert await side.recv() == (b"c", 1, b"")
    await broker.wait_closed()
    assert node.relay.channels == {}


async def test_reused_channel_id_is_an_error(world):
    _primary, broker, _node, side = world
    await side.open(1, "alice")
    assert (await side.open(1, "alice"))[0] == b"e"
    await broker.wait_closed()  # the old one is gone too


async def test_oversized_frame_ends_the_connection(world):
    primary, _broker, _node, side = world
    await side.open(1, "alice")
    await side.send(b"d", 1, b"y" * (MAX_DATA + 1))
    with pytest.raises(ConnectionClosed) as closed:
        while True:
            await side.recv()
    assert closed.value.rcvd.code == 1009  # message too big
    await primary.next_connection()


class StuckBroker:
    """Accepts connections and never reads them (until `release`), like a broker that hung."""

    def __init__(self, path: Path):
        self.path = path
        self.release = asyncio.Event()
        self.ended = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self.release.wait()
        while await reader.read(65536):
            pass
        self.ended += 1
        writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_unix_server(self.handle, path=str(self.path))
        return self

    async def __aexit__(self, *exc):
        self.release.set()
        self.server.close()


async def stuck_draining(side, node, channel_id: int) -> None:
    """Open a channel to `stuck`, fill the socket so the broker side stops taking bytes, then close it."""
    assert await side.open(channel_id, "stuck") == (b"k", channel_id, b"")
    for _ in range(8):
        await side.send(b"d", channel_id, b"z" * MAX_DATA)
    await side.send(b"c", channel_id)
    await wait_for(lambda: any(c.id == channel_id for c in node.relay.draining))


async def test_channels_are_capped_draining_ones_included(world, socket_dir, monkeypatch):
    monkeypatch.setattr(client_module, "MAX_CHANNELS", 3)
    monkeypatch.setattr(client_module, "DRAIN_TIMEOUT", 0.5)
    _primary, broker, node, side = world
    async with StuckBroker(socket_dir / "stuck.sock"):
        await stuck_draining(side, node, 1)
        assert await side.open(3, "alice") == (b"k", 3, b"")
        assert await side.open(5, "alice") == (b"k", 5, b"")
        kind, channel_id, reason = await side.open(7, "alice")  # two live and one closing: full
        assert (kind, channel_id, reason) == (b"e", 7, b"too many channels")
        assert broker.connections == 2  # refused before connecting
        await wait_for(lambda: not node.relay.draining)
        assert await side.open(9, "alice") == (b"k", 9, b"")


async def test_a_drained_channel_leaves_the_count(world):
    _primary, broker, node, side = world
    await side.open(1, "alice")
    await side.send(b"d", 1, b"bye\n")
    await side.send(b"c", 1)
    await broker.wait_closed()
    await wait_for(lambda: not node.relay.draining)
    assert not node.relay.draining and not node.relay.channels


async def test_disconnect_aborts_every_channel_and_task(world, socket_dir):
    primary, broker, node, side = world
    async with StuckBroker(socket_dir / "stuck.sock"):
        await stuck_draining(side, node, 1)
        await side.open(3, "alice")
        relay = node.relay
        channels = [*relay.channels.values(), *relay.draining]
        drains = list(relay.draining.values())
        await side.ws.close()
        await broker.wait_closed()  # the live channel's broker connection is closed
        await primary.next_connection()  # reconnected
        assert not relay.channels and not relay.draining
        assert all(task.done() for c in channels for task in c.tasks) and all(t.done() for t in drains)
        assert all(c.writer.is_closing() for c in channels)


async def test_reconnects_after_the_primary_drops(world):
    primary, broker, node, side = world
    await side.open(1, "alice")
    await side.ws.close()
    await broker.wait_closed()  # channels die with the connection
    again = await primary.next_connection()
    assert len(primary.hellos) == 2
    assert await again.open(1, "alice") == (b"k", 1, b"")
    await again.send(b"d", 1, b"back\n")
    assert await again.recv_data(1, 5) == b"back\n"


async def test_reconnects_after_the_primary_restarts(tls, node_key, socket_dir):
    context = tls.server_context
    primary = FakePrimary(context, node_key.public_key())
    async with primary:
        port = primary.port
        node = node_for(port, tls, node_key, socket_dir, max_backoff=0.2)
        refused = asyncio.Event()
        real_sleep = node.sleep

        async def retry(delay):
            refused.set()
            await real_sleep(delay)
        node.sleep = retry
        async with Running(node):
            await primary.next_connection()
            primary.server.close()  # closes the open connection too
            await primary.server.wait_closed()
            await asyncio.wait_for(refused.wait(), TIMEOUT)
            primary.server = await serve(primary.handler, "127.0.0.1", port, ssl=context)
            await primary.next_connection()
            assert len(primary.hellos) == 2


# ---- enroll, config, status ------------------------------------------------------------------------------------

class EnrollServer:
    """GET /api/nodes/ca and POST /api/nodes/enroll over HTTPS (https://localhost:<port>) in a thread."""

    def __init__(self, tls: TLS, status: int = 200, server_context: ssl.SSLContext | None = None):
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def reply(self, status: int, data: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append({"method": "GET", "path": self.path})
                if self.path == "/api/nodes/ca":
                    self.reply(200, tls.ca.pem, "application/x-pem-file")
                else:
                    self.reply(404, b"{}", "application/json")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"method": "POST", "path": self.path, "body": body})
                answer = {"node_id": "42"} if status == 200 else {"detail": "bad or expired code"}
                self.reply(status, json.dumps(answer).encode(), "application/json")

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        context = server_context or tls.server_context
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.url = f"https://localhost:{self.httpd.server_address[1]}"

    @property
    def posts(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST"]

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def run_enroll(url, tmp_path, name="lab-1", **trust):
    trust = trust or {"trust": "system"}
    return enroll(primary=url, code="c0de", name=name, config_path=tmp_path / "node.env",
                  state_dir=tmp_path / "state", socket_dir=tmp_path / "s", **trust)


def pasted(fingerprint: str) -> str:
    """The fingerprint as a person might paste it: colons, capitals."""
    return ":".join(fingerprint[i:i + 2] for i in range(0, 64, 2)).upper()


async def test_enroll_with_a_private_ca(tls, tmp_path, socket_dir):
    with EnrollServer(tls) as server:
        config = await asyncio.to_thread(run_enroll, server.url + "/", tmp_path, trust="ca",
                                         ca_fingerprint=pasted(tls.ca.fingerprint))
    assert [(r["method"], r["path"]) for r in server.requests] == [("GET", "/api/nodes/ca"),
                                                                    ("POST", "/api/nodes/enroll")]
    body = server.posts[0]["body"]
    assert body["name"] == "lab-1" and body["code"] == "c0de"
    assert set(body["info"]) - {"windows"} == {"os", "release", "arch", "hostname", "version", "build", "wsl"}
    key_path = tmp_path / "state/node.key"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    raw = load_key(key_path).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert base64.b64decode(body["public_key"]) == raw and len(raw) == 32
    assert (tmp_path / "state/ca.pem").read_bytes() == tls.ca.pem
    assert config.node_id == "42" and config.trust == "ca"
    env = read_env_file(tmp_path / "node.env")
    assert env["ACC_NODE_PRIMARY"] == server.url and env["ACC_NODE_ID"] == "42"
    assert env["ACC_NODE_TRUST"] == "ca" and env["ACC_NODE_CA_FILE"] == str(tmp_path / "state/ca.pem")
    assert env["ACC_NODE_CA_FINGERPRINT"] == tls.ca.fingerprint
    assert env["ACC_NODE_SOCKET_DIR"] == str(tmp_path / "s")
    loaded = NodeConfig.load(tmp_path / "node.env")
    assert loaded == config

    # and `run` with that configuration reaches a primary whose leaf the same CA issued
    key = load_key(key_path)
    async with FakePrimary(tls.ca.server_context(), key.public_key()) as primary:
        loaded = dataclasses.replace(loaded, primary=f"https://localhost:{primary.port}", socket_dir=socket_dir)
        task = asyncio.create_task(Node(loaded, key).connect_once())
        await primary.next_connection()
        task.cancel()


async def test_enroll_refuses_a_ca_with_the_wrong_fingerprint(tls, tmp_path):
    with EnrollServer(tls) as server:
        with pytest.raises(TrustError, match="fingerprint"):
            await asyncio.to_thread(run_enroll, server.url, tmp_path, trust="ca", ca_fingerprint="cd" * 32)
    assert server.posts == []  # the code never left the node
    assert not (tmp_path / "state").exists() and not (tmp_path / "node.env").exists()


async def test_enroll_verifies_the_primary_against_the_fetched_ca(tls, tmp_path):
    """The CA checks out, but the server's leaf comes from somewhere else: the POST must not happen."""
    impostor = CA(tmp_path / "impostor")
    with EnrollServer(tls, server_context=impostor.server_context()) as server:
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.to_thread(run_enroll, server.url, tmp_path, trust="ca", ca_fingerprint=tls.ca.fingerprint)
    assert [r["method"] for r in server.requests] == ["GET"]


async def test_enroll_with_system_trust(tls, tmp_path, monkeypatch):
    with EnrollServer(tls) as server:
        monkeypatch.setenv("SSL_CERT_FILE", str(CA(tmp_path / "public").path))  # our CA isn't a system one...
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.to_thread(run_enroll, server.url, tmp_path)
        assert server.requests == []
        monkeypatch.setenv("SSL_CERT_FILE", str(tls.ca.path))  # ...until it is
        config = await asyncio.to_thread(run_enroll, server.url, tmp_path)
    assert [r["method"] for r in server.requests] == ["POST"]  # no CA download
    assert config.trust == "system" and config.ca_file is None
    env = read_env_file(tmp_path / "node.env")
    assert env["ACC_NODE_TRUST"] == "system" and "ACC_NODE_CA_FILE" not in env
    assert not (tmp_path / "state/ca.pem").exists()
    assert NodeConfig.load(tmp_path / "node.env") == config


async def test_refused_enroll_keeps_the_old_key_and_ca(tls, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state/node.key").write_text("old")
    (tmp_path / "state/ca.pem").write_text("old ca")
    with EnrollServer(tls, status=403) as server:
        with pytest.raises(EnrollError, match="403"):
            await asyncio.to_thread(run_enroll, server.url, tmp_path, trust="ca", ca_fingerprint=tls.ca.fingerprint)
    assert (tmp_path / "state/node.key").read_text() == "old"
    assert (tmp_path / "state/ca.pem").read_text() == "old ca"


async def test_enroll_command_line(tls, tmp_path, capsys):
    common = ["enroll", "--code", "c0de", "--name", "lab-1", "--config", str(tmp_path / "node.env"),
              "--state-dir", str(tmp_path / "state")]
    with EnrollServer(tls) as server:
        # --fingerprint still works, as a deprecated --ca-fingerprint
        args = common + ["--primary", server.url, "--fingerprint", tls.ca.fingerprint]
        assert await asyncio.to_thread(cli.main, args) == 0
    captured = capsys.readouterr()
    assert "deprecated" in captured.err and "Enrolled as lab-1" in captured.out
    assert read_env_file(tmp_path / "node.env")["ACC_NODE_TRUST"] == "ca"
    with pytest.raises(SystemExit):  # one trust option, not two
        cli.main(common + ["--primary", server.url, "--trust", "system", "--ca-fingerprint", tls.ca.fingerprint])
    with pytest.raises(SystemExit):  # and not none
        cli.main(common + ["--primary", server.url])
    assert cli.main(common + ["--primary", server.url, "--ca-fingerprint", "xyz"]) == 1
    assert "bad fingerprint" in capsys.readouterr().err


async def test_enroll_reads_the_code_from_stdin(tls, tmp_path, monkeypatch, capsys):
    """--code - keeps the one-time code off every command line (process list, sudo's log); --code CODE still works."""
    common = ["enroll", "--name", "lab-1", "--config", str(tmp_path / "node.env"), "--state-dir", str(tmp_path / "state")]
    with EnrollServer(tls) as server:
        args = common + ["--primary", server.url, "--ca-fingerprint", tls.ca.fingerprint, "--code", "-"]
        monkeypatch.setattr("sys.stdin", io.StringIO("1a2b3c-4d5e6f-7a8b9c-0d1e2f\n"))
        assert await asyncio.to_thread(cli.main, args) == 0
        assert server.requests[-1]["body"]["code"] == "1a2b3c-4d5e6f-7a8b9c-0d1e2f"
    assert "1a2b3c" not in capsys.readouterr().out
    for stdin in ("", "\n", "x" * 300):  # nothing, a blank line, or far more than a code: refused before any request
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        assert cli.main(common + ["--primary", "https://acc.example.lan", "--trust", "system", "--code", "-"]) == 1
        assert "code" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["Lab", "-lab", "lab_1", "a" * 41, ""])
def test_enroll_checks_the_name(tmp_path, name):
    with pytest.raises(ConfigError):
        run_enroll("https://acc.example.lan", tmp_path, name=name)


def test_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="enroll"):
        NodeConfig.load(tmp_path / "missing.env")
    (tmp_path / "partial.env").write_text("ACC_NODE_PRIMARY=https://acc.example.lan\nACC_NODE_ID=4\n")
    with pytest.raises(ConfigError, match="ACC_NODE_TRUST"):
        NodeConfig.load(tmp_path / "partial.env")
    (tmp_path / "partial.env").write_text("ACC_NODE_PRIMARY=https://acc.example.lan\nACC_NODE_ID=4\n"
                                          "ACC_NODE_TRUST=ca\n")
    with pytest.raises(ConfigError, match="ACC_NODE_CA_FILE"):
        NodeConfig.load(tmp_path / "partial.env")
    with pytest.raises(ConfigError, match="trust mode"):
        NodeConfig(primary="https://acc.example.lan", node_id="4", trust="pin")
    with pytest.raises(ConfigError):
        normalize_fingerprint("abc")
    assert connect_url("https://acc.example.lan:8443/acc/") == "wss://acc.example.lan:8443/acc/api/nodes/connect"


async def test_broker_accounts_lists_sockets_only(socket_dir):
    (socket_dir / "notes.sock").write_text("not a socket")
    (socket_dir / "Bad.sock").write_text("")
    async with FakeBroker(socket_dir / "alice.sock"), FakeBroker(socket_dir / "bob.sock"):
        assert broker_accounts(socket_dir) == ["alice", "bob"]
    assert broker_accounts(socket_dir / "nope") == []


def test_status_command(tmp_path, capsys, node_key):
    config = NodeConfig(primary="https://acc.example.lan", node_id="42", trust="ca", ca_file=tmp_path / "ca.pem",
                        ca_fingerprint="00" * 32, name="lab-1", socket_dir=tmp_path / "s")
    config.write(tmp_path / "node.env")
    args = ["status", "--config", str(tmp_path / "node.env"), "--state-dir", str(tmp_path / "state")]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "lab-1 (42)" in out and "MISSING" in out and "unknown" in out and "private CA" in out
    (tmp_path / "state").mkdir()
    (tmp_path / "state/status.json").write_text(json.dumps({"state": "disconnected", "since": 0, "error": "boom"}))
    assert cli.main(args) == 3
    assert "disconnected" in capsys.readouterr().out
    assert cli.main(["status", "--config", str(tmp_path / "missing.env")]) == 1
    assert "enroll" in capsys.readouterr().err


def test_path_defaults_come_from_the_environment(tmp_path, monkeypatch):
    for variable in ("ACC_NODE_CONFIG", "ACC_NODE_STATE_DIR", "ACC_NODE_SOCKET_DIR"):
        monkeypatch.delenv(variable, raising=False)
    enroll_args = ["enroll", "--primary", "https://acc.example.lan", "--trust", "system", "--code", "c", "--name", "n"]
    args = cli.parser().parse_args(enroll_args)
    assert (args.config, args.state_dir, args.socket_dir) == (Path("/etc/acc-node.env"), Path("/var/lib/acc-node"),
                                                              Path("/run/acc-broker"))
    monkeypatch.setenv("ACC_NODE_CONFIG", str(tmp_path / "env/acc-node.env"))
    monkeypatch.setenv("ACC_NODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ACC_NODE_SOCKET_DIR", str(tmp_path / "sock"))
    args = cli.parser().parse_args(enroll_args)
    assert (args.config, args.state_dir, args.socket_dir) == (tmp_path / "env/acc-node.env", tmp_path / "state",
                                                              tmp_path / "sock")
    for command in ("run", "status"):
        args = cli.parser().parse_args([command])
        assert (args.config, args.state_dir) == (tmp_path / "env/acc-node.env", tmp_path / "state")
    # options win
    args = cli.parser().parse_args(enroll_args + ["--config", "/x.env", "--state-dir", "/x", "--socket-dir", "/s"])
    assert (args.config, args.state_dir, args.socket_dir) == (Path("/x.env"), Path("/x"), Path("/s"))
    # an empty variable counts as unset
    monkeypatch.setenv("ACC_NODE_STATE_DIR", "")
    assert cli.parser().parse_args(["run"]).state_dir == Path("/var/lib/acc-node")


async def test_enroll_and_status_use_the_environment(tls, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACC_NODE_CONFIG", str(tmp_path / "etc/acc-node.env"))
    monkeypatch.setenv("ACC_NODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ACC_NODE_SOCKET_DIR", str(tmp_path / "sock"))
    with EnrollServer(tls) as server:
        args = ["enroll", "--primary", server.url, "--ca-fingerprint", tls.ca.fingerprint, "--code", "c0de",
                "--name", "mac-1"]
        assert await asyncio.to_thread(cli.main, args) == 0
    assert (tmp_path / "state/node.key").exists() and (tmp_path / "state/ca.pem").exists()
    env = read_env_file(tmp_path / "etc/acc-node.env")
    assert env["ACC_NODE_SOCKET_DIR"] == str(tmp_path / "sock")
    assert env["ACC_NODE_CA_FILE"] == str(tmp_path / "state/ca.pem")
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "mac-1 (42)" in out and f"{tmp_path / 'state/node.key'} (present)" in out


# ---- files written as root or as the node user (review #8) ------------------------------------------------------

def test_write_atomic_replaces_a_link_and_uses_a_fresh_temporary(tmp_path):
    from acc.node.config import write_atomic
    outside = tmp_path / "outside"
    outside.write_text("keep")
    state = tmp_path / "state"
    state.mkdir()
    (state / "node.key").symlink_to(outside)
    (state / ".node.key.tmp").symlink_to(outside)  # the old, predictable temporary name: irrelevant now
    write_atomic(state / "node.key", b"new", 0o600)
    assert outside.read_text() == "keep"
    assert not (state / "node.key").is_symlink() and (state / "node.key").read_bytes() == b"new"
    assert stat.S_IMODE((state / "node.key").stat().st_mode) == 0o600
    assert sorted(p.name for p in state.iterdir()) == [".node.key.tmp", "node.key"]  # no temporary left behind


def test_saving_the_key_never_changes_ownership(tmp_path, monkeypatch, node_key):
    from acc.node import identity
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *a, **k: pytest.fail("chown by path"))
    identity.save_key(node_key, tmp_path / "node.key")
    assert load_key(tmp_path / "node.key")


def adopt(tmp_path, file: str, data: bytes, monkeypatch) -> int:
    import io
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(data)))
    return cli.main(["adopt", "--state-dir", str(tmp_path / "state"), file])


def test_adopt_stores_a_key_or_ca_from_stdin(tmp_path, monkeypatch, node_key, tls):
    pem = node_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
    assert adopt(tmp_path, "node.key", pem, monkeypatch) == 0
    assert stat.S_IMODE((tmp_path / "state/node.key").stat().st_mode) == 0o600
    assert adopt(tmp_path, "ca.pem", tls.ca.pem, monkeypatch) == 0
    assert (tmp_path / "state/ca.pem").read_bytes() == tls.ca.pem
    assert stat.S_IMODE((tmp_path / "state/ca.pem").stat().st_mode) == 0o644


@pytest.mark.parametrize("file, data", [("node.key", b"not a key"), ("ca.pem", b"not a cert"),
                                         ("node.key", b"x" * (64 * 1024 + 1))])
def test_adopt_refuses_what_isnt_a_key_or_ca(tmp_path, monkeypatch, file, data):
    assert adopt(tmp_path, file, data, monkeypatch) == 1
    assert not (tmp_path / "state" / file).exists()


def test_adopt_takes_only_its_two_files(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        adopt(tmp_path, "../etc/passwd", b"", monkeypatch)


async def test_tiny_and_empty_frames_count_against_the_budgets(monkeypatch):
    """Release review #2: empty data frames are skipped, and every queued frame costs its overhead, so a flood of
    tiny frames hits the channel's (and the relay's) budget instead of growing the queue without limit."""
    from types import SimpleNamespace

    from acc.node import client
    relay = client.Relay(SimpleNamespace(), Path("/unused"))
    channel = client.Channel(relay, 1, None, None)  # its tasks never start: a broker that isn't reading
    relay.channels[1] = channel
    for _ in range(10_000):
        assert channel.write(b"")
    assert channel.outbox.qsize() == 0 and channel.buffered == 0
    accepted = 0
    while channel.write(b"x"):
        accepted += 1
    assert accepted == client.MAX_BUFFERED // (1 + client.ITEM_OVERHEAD)
    monkeypatch.setattr(client, "MAX_RELAY_BUFFERED", 20 << 20)  # the first channel holds ~16 MiB already
    other = client.Channel(relay, 2, None, None)
    relay.channels[2] = other
    big = b"x" * (1 << 20)
    while other.write(big):
        pass
    assert other.buffered < 5 << 20 and relay.buffered() <= 20 << 20  # stopped by the relay's total, not its own


async def test_updates_travel_on_the_signed_link(world, tmp_path):
    """The node receives identity/hash only, queues its helper, and reports the persisted result."""
    _primary, _broker, node, side = world
    root = tmp_path / 'updater'
    (root / 'requests').mkdir(parents=True)
    node.updates.root = root
    await side.ws.send(json.dumps({'t':'update','version':'99.0.0','build':'a'*64,'sha256':'b'*64}))
    async def update_state(done):
        # Skip status frames sent before this request was handled (e.g. on connect).
        while True:
            message = json.loads(await asyncio.wait_for(side.ws.recv(), TIMEOUT))
            if message.get('t') == 'update-state' and message.get('helper') and done(message):
                return message
    assert not (await update_state(lambda m: m['refused'] or any((root / 'requests').glob('*.json'))))['refused']
    request = next((root / 'requests').glob('*.json'))
    assert json.loads(request.read_text())['sha256'] == 'b'*64
    (root / 'state.json').write_text(json.dumps({'id':'test-result','ok':True,'version':'99.0.0'}))
    request.unlink()
    # A further control request elicits the persisted helper result without running any installer.
    await side.ws.send(json.dumps({'t':'update','version':'99.0.0','build':'a'*64,'sha256':'b'*64}))
    assert (await update_state(lambda m: m['result'].get('ok')))['result']['ok']


# ------------------------------------------------------------------ WSL or plain Linux (owner request)


@pytest.mark.parametrize("release, environ, interop, want", [
    ("6.6.36.6-microsoft-standard-WSL2", {}, False, "2"),
    ("5.15.153.1-microsoft-standard-WSL2", {"WSL_DISTRO_NAME": "Ubuntu-24.04"}, True, "2"),
    ("4.4.0-19041-Microsoft", {}, False, "1"),
    ("6.8.0-147-generic", {"WSL_DISTRO_NAME": "Ubuntu"}, False, "2"),  # a custom kernel: WSL's environment says so
    ("6.8.0-147-generic", {"WSL_INTEROP": "/run/WSL/1_interop"}, False, "2"),
    ("6.8.0-147-generic", {}, True, "2"),  # WSL's interop handler is registered
    ("6.8.0-147-generic", {}, False, "0"),  # a server or VM
    ("6.6.0-azure", {}, False, "0"),
])
def test_wsl_is_told_apart_from_linux(tmp_path, release, environ, interop, want):
    from acc.node.identity import wsl_version
    handler = tmp_path / "WSLInterop"
    if interop:
        handler.write_text("enabled\n")
    assert wsl_version(release, environ, handler) == want


@pytest.mark.parametrize("out, want", [
    ("\r\nMicrosoft Windows [Version 10.0.22631.4317]\r\n", "Windows 11 (build 22631)"),
    ("Microsoft Windows [Version 10.0.19045.5011]", "Windows 10 (build 19045)"),
    ("Microsoft Windows [Version 10.0.26100.2033]", "Windows 11 (build 26100)"),
    ("<3>WSL (1) ERROR: UtilBindVsockAnyPort", None),  # interop off
])
def test_the_windows_host_version_through_interop(tmp_path, out, want):
    import subprocess as sp

    from acc.node.identity import windows_version
    cmd = tmp_path / "cmd.exe"
    cmd.write_text("")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        return sp.CompletedProcess(argv, 0, stdout=out)
    assert windows_version(run=run, cmd=cmd) == want
    assert calls == [([str(cmd), "/d", "/c", "ver"], 3)]  # quick: a hung interop never delays connecting


def test_no_interop_means_no_windows_version(tmp_path):
    from acc.node.identity import windows_version

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 3)
    assert windows_version(run=run, cmd=tmp_path / "missing.exe") is None
    (tmp_path / "cmd.exe").write_text("")
    assert windows_version(run=run, cmd=tmp_path / "cmd.exe") is None


def test_node_info_carries_the_system(monkeypatch):
    from acc.node import identity
    identity.host_system.cache_clear()
    monkeypatch.setattr(identity.platform, "system", lambda: "Linux")
    monkeypatch.setattr(identity, "wsl_version", lambda: "2")
    monkeypatch.setattr(identity, "windows_version", lambda: "Windows 11 (build 22631)")
    monkeypatch.setattr(identity, "interop_enabled", lambda: True)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    try:
        info = identity.node_info()
        assert info["wsl_isolation"]["interop_enabled"] is True
        monkeypatch.setattr(identity, "interop_enabled", lambda: False)
        assert identity.node_info()["wsl_isolation"]["interop_enabled"] is False
    finally:
        identity.host_system.cache_clear()
    assert info["wsl"] == "2" and info["windows"] == "Windows 11 (build 22631)"
    assert info["wsl_distro"] == "Ubuntu-24.04"


async def test_node_reports_windows_drive_access_from_each_agents_broker(env, tmp_path, monkeypatch):
    from acc.broker.server import Broker
    from acc.node import identity
    from acc.wsl import isolation_state

    folder = tmp_path / "sock"
    folder.mkdir()
    broker = Broker(env["broker"])
    monkeypatch.setattr("acc.wsl.windows_drive_readable", lambda: False)
    monkeypatch.setattr(client_module, "node_info", lambda: {
        "os": "linux", "wsl": "2", "wsl_isolation": isolation_state(False, {})})
    monkeypatch.setattr(identity, "interop_enabled", lambda: False)
    monkeypatch.setattr(client_module, "uid_of", lambda account: os.getuid())
    async with await asyncio.start_unix_server(broker.handle, str(folder / "alex-agent.sock")):
        node = Node(NodeConfig("https://acc.example.lan", "node", socket_dir=folder),
                    Ed25519PrivateKey.generate(), state_dir=None, packages=None)
        info = await node.machine_info()
    assert info["wsl_isolation"]["accounts"] == {"alex-agent": False}
    assert info["wsl_isolation"]["windows_drive_readable"] is False
    assert info["wsl_isolation"]["interop_enabled"] is False


@pytest.mark.parametrize("old_broker", [True, False])
async def test_unavailable_or_old_agent_broker_cannot_report_safe_windows_access(tmp_path, monkeypatch, old_broker):
    from unittest.mock import AsyncMock

    from acc.server.broker_client import BrokerError
    from acc.wsl import isolation_state

    broker = AsyncMock()
    broker.call.return_value = {"role": "agents"}
    if not old_broker:
        broker.call.side_effect = BrokerError("unavailable")
    monkeypatch.setattr(client_module, "BrokerClient", lambda *a, **kw: broker)
    monkeypatch.setattr(client_module, "uid_of", lambda _: os.getuid())
    monkeypatch.setattr(client_module, "broker_accounts", lambda _: ["alex-agent"])
    monkeypatch.setattr(client_module, "node_info", lambda: {"wsl_isolation": isolation_state(False, {})})
    node = Node(NodeConfig("https://acc.example.lan", "node", socket_dir=tmp_path),
                Ed25519PrivateKey.generate(), state_dir=None, packages=None)
    info = await node.machine_info()
    assert info["wsl_isolation"]["accounts"] == {"alex-agent": None}
    assert info["wsl_isolation"]["windows_drive_readable"] is None
    broker.close.assert_awaited_once()


async def test_node_reconnects_to_preapproved_standby(tls, node_key, socket_dir):
    from dataclasses import replace
    async with (FakePrimary(tls.server_context, node_key.public_key()) as first,
                FakePrimary(tls.server_context, node_key.public_key()) as second):
        node = node_for(first.port, tls, node_key, socket_dir, max_backoff=0.1)
        node.config = replace(node.config, alternate_primaries=(f'https://localhost:{second.port}',))
        async with Running(node):
            original = await first.next_connection()
            await original.ws.close(1012, 'manual demotion')
            replacement = await second.next_connection()
            assert len(second.hellos) == 1
            await replacement.ws.send(json.dumps({'t': 'ping'}))


def test_alternate_main_urls_require_https_and_round_trip(tmp_path):
    config = NodeConfig(primary='https://acc.example.lan', node_id='ab',
                        alternate_primaries=('https://standby.example.lan',))
    config.write(tmp_path / 'node.env')
    assert NodeConfig.load(tmp_path / 'node.env').alternate_primaries == ('https://standby.example.lan',)
    with pytest.raises(ConfigError):
        NodeConfig(primary='https://acc.example.lan', node_id='ab', alternate_primaries=('http://standby.example.lan',))
