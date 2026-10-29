"""Peer uid of a UNIX socket: Linux (real sockets), macOS (a mocked LOCAL_PEERCRED), anything else refused."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import struct

import pytest
from isolated_settings import isolated_broker

from acc import peercred
from acc.broker.server import Broker
from acc.server.broker_client import BrokerClient, BrokerUnavailable


class FakeSocket:
    """Answers getsockopt with fixed bytes and records what was asked."""

    def __init__(self, answer: bytes):
        self.answer = answer
        self.asked = []

    def getsockopt(self, level, option, buflen):
        self.asked.append((level, option, buflen))
        return self.answer[:buflen]


def xucred(uid: int, version: int = 0, groups=(20, 12, 61)) -> bytes:
    padded = list(groups) + [0] * (16 - len(groups))
    return struct.pack("@IIh16I", version, uid, len(groups), *padded)


@pytest.fixture
def darwin(monkeypatch):
    monkeypatch.setattr(peercred, "PLATFORM", "darwin")


def test_xucred_layout_matches_macos():
    # u_int, uid_t, short (+2 padding), gid_t[16]: 76 bytes, uid at offset 4
    assert peercred.XUCRED.size == 76
    assert struct.unpack_from("@I", xucred(501), 4) == (501,)


def test_darwin_reads_uid_from_local_peercred(darwin):
    sock = FakeSocket(xucred(501))
    assert peercred.peer_uid(sock) == 501
    assert sock.asked == [(0, 0x001, 76)]  # SOL_LOCAL, LOCAL_PEERCRED, sizeof(struct xucred)


def test_darwin_accepts_a_short_answer_with_the_uid(darwin):
    assert peercred.peer_uid(FakeSocket(xucred(502)[:12])) == 502


@pytest.mark.parametrize("answer, reason", [
    (xucred(501, version=1), "version 1"),
    (b"\x00\x00\x00\x00\xf5\x01", "6 bytes"),
    (b"", "0 bytes"),
])
def test_darwin_refuses_answers_it_cannot_trust(darwin, answer, reason):
    with pytest.raises(peercred.PeerCredError, match=reason):
        peercred.peer_uid(FakeSocket(answer))


def test_darwin_passes_kernel_errors_on(darwin):
    class Failing:
        def getsockopt(self, *args):
            raise OSError(22, "Invalid argument")
    with pytest.raises(OSError):
        peercred.peer_uid(Failing())


@pytest.mark.parametrize("platform", ["win32", "freebsd14", "openbsd7", "sunos5", "cygwin"])
def test_other_platforms_are_refused(monkeypatch, platform):
    monkeypatch.setattr(peercred, "PLATFORM", platform)
    sock = FakeSocket(xucred(0))
    with pytest.raises(peercred.PeerCredError, match="no trusted way"):
        peercred.peer_uid(sock)
    assert sock.asked == []  # never guessed from whatever the socket would say


@pytest.mark.skipif(not peercred.PLATFORM.startswith("linux"), reason="SO_PEERCRED is Linux-only")
def test_linux_reads_uid_from_so_peercred():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with left, right:
        assert peercred.peer_uid(left) == os.getuid()


def test_linux_refuses_a_short_answer(monkeypatch):
    monkeypatch.setattr(peercred, "PLATFORM", "linux")
    if not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("socket.SO_PEERCRED missing here")
    with pytest.raises(peercred.PeerCredError):
        peercred.peer_uid(FakeSocket(b"\x01\x00"))


async def _serve_once(tmp_path):
    """A broker (ping only) on a socket in tmp_path; returns its answer to one ping, or b"" if it hung up."""
    config = isolated_broker(tmp_path, socket_path=tmp_path / "b.sock", run_dir=tmp_path, allowed_roots=(tmp_path,),
                          allowed_uids=frozenset({os.getuid()}), providers={})
    broker = Broker(config)
    server = await asyncio.start_unix_server(broker.handle, path=str(config.socket_path))
    async with server:
        reader, writer = await asyncio.open_unix_connection(str(config.socket_path))
        writer.write(json.dumps({"id": 1, "method": "ping", "params": {}}).encode() + b"\n")
        await writer.drain()
        try:
            answer = await asyncio.wait_for(reader.readline(), timeout=5)
        except ConnectionResetError:
            answer = b""
        writer.close()
    return answer


@pytest.mark.skipif(not peercred.PLATFORM.startswith("linux"), reason="needs a real SO_PEERCRED")
async def test_broker_answers_its_allowed_uid(tmp_path):
    answer = json.loads(await _serve_once(tmp_path))
    assert answer["ok"] is True


async def test_broker_hangs_up_when_the_peer_uid_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(peercred, "PLATFORM", "sunos5")
    assert await _serve_once(tmp_path) == b""


async def test_client_refuses_a_socket_whose_owner_is_unknown(tmp_path, monkeypatch):
    path = tmp_path / "c.sock"
    server = await asyncio.start_unix_server(lambda r, w: w.close(), path=str(path))
    monkeypatch.setattr(peercred, "PLATFORM", "win32")
    async with server:
        client = BrokerClient(path, trusted_uids=frozenset({os.getuid()}))
        with pytest.raises(BrokerUnavailable, match="cannot read its owner"):
            await client.call("ping")
        assert client._writer is None  # closed, nothing sent
