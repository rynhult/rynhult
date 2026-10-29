"""Multi-host: copying sessions' hook spools from nodes (Supervisor.fetch_remote_spools) never lets one node hold up,
fill up or break ingestion for the others."""
from __future__ import annotations

import asyncio
import base64
import json

import pytest
from sqlalchemy import select

from acc.server import supervisor as supervisor_module
from acc.server.app import create_app
from acc.server.broker_client import BrokerClient, BrokerError, BrokerUnavailable
from acc.server.models import Event, LaunchSpec, Project, Session
from acc.server.nodes import NodeHub, NodeLink
from acc.server.supervisor import spool_path_for


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class FakeBrokers:
    """Answers read_spool per host with `replies[host]`: a dict, an exception, a callable or "hang"."""

    def __init__(self, replies: dict):
        self.replies = replies
        self.hub = NodeHub()
        async def send(_data):
            pass
        for host in replies:
            self.hub.attach(NodeLink(host, host, send, {}, []))
        self.calls: list[tuple[str, str, int]] = []

    async def call(self, method, account=None, host=None, **params):
        assert method == "read_spool"
        self.calls.append((host, params["acc_id"], params["offset"]))
        reply = self.replies[host]
        if callable(reply):
            reply = reply(params["acc_id"], params["offset"])
        if reply == "hang":
            await asyncio.sleep(3600)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def sup(env):
    app = create_app(env["settings"])
    return app.state.supervisor


def add_session(sup, host: str, acc_id: str) -> None:
    with sup.db() as db:
        project = db.scalar(select(Project).where(Project.host == host))
        if project is None:
            project = Project(slug=f"p-{host}", name=host, host=host, workspace_path=f"/w/{host}", trusted=True, account="agent")
            db.add(project)
        spec = LaunchSpec(provider="claude", inputs={}, files={}, digest="x")
        db.add(Session(id=acc_id, project=project, provider="claude", name=acc_id, tmux_name=f"acc-{acc_id}",
                       launch_spec=spec, run_as="agent"))
        db.commit()


def mirrored(sup, acc_id: str) -> bytes:
    path = spool_path_for(sup.settings, acc_id)
    return path.read_bytes() if path.exists() else b""


async def test_a_host_that_never_answers_does_not_hold_up_the_others(sup, monkeypatch):
    monkeypatch.setattr(supervisor_module, "REMOTE_SPOOL_BUDGET", 0.3)
    add_session(sup, "slow", "aaaaaaaa1")
    add_session(sup, "good", "bbbbbbbb1")
    sup.broker = FakeBrokers({"slow": "hang", "good": {"data": b64(b'{"event": "x"}\n')}})
    started = asyncio.get_running_loop().time()
    await sup.fetch_remote_spools()
    assert asyncio.get_running_loop().time() - started < 2
    assert mirrored(sup, "bbbbbbbb1") == b'{"event": "x"}\n'
    assert "slow" in sup._host_backoff and "good" not in sup._host_backoff
    sup.broker.calls.clear()
    await sup.fetch_remote_spools()  # the slow host waits out its backoff
    assert [c[0] for c in sup.broker.calls] == ["good"]


async def test_backoff_doubles_and_resets(sup, monkeypatch):
    add_session(sup, "flaky", "cccccccc1")
    sup.broker = FakeBrokers({"flaky": BrokerUnavailable("offline")})
    clock = asyncio.get_running_loop().time
    await sup.fetch_remote_spools()
    failures, until = sup._host_backoff["flaky"]
    assert failures == 1 and until - clock() == pytest.approx(supervisor_module.REMOTE_BACKOFF_MIN, abs=1)
    sup._host_backoff["flaky"] = (failures, 0.0)  # time's up
    await sup.fetch_remote_spools()
    failures, until = sup._host_backoff["flaky"]
    assert failures == 2 and until - clock() == pytest.approx(2 * supervisor_module.REMOTE_BACKOFF_MIN, abs=1)
    sup._host_backoff["flaky"] = (failures, 0.0)
    sup.broker.replies["flaky"] = {"data": ""}
    await sup.fetch_remote_spools()
    assert "flaky" not in sup._host_backoff


@pytest.mark.parametrize("reply", [
    ["not", "an", "object"],
    {"data": 42},
    {"data": "not base64!"},
    {"data": "aGVsbG8=\n"},  # strict: no stray characters
    {"data": b64(b"x" * (512 * 1024 + 1))},
    {"data": "A" * (4 * 1024 * 1024)},  # far too long to even decode
], ids=["list", "number", "garbage", "newline", "over-512k", "huge"])
async def test_bad_replies_are_refused_and_stay_with_their_session(sup, reply):
    add_session(sup, "pc", "dddddddd1")
    add_session(sup, "pc", "dddddddd2")
    good = {"data": b64(b"ok\n")}
    sup.broker = FakeBrokers({"pc": lambda acc_id, offset: reply if acc_id == "dddddddd1" else good})
    await sup.fetch_remote_spools()
    assert mirrored(sup, "dddddddd1") == b""
    assert mirrored(sup, "dddddddd2") == b"ok\n"


async def test_a_full_512k_chunk_is_accepted_and_offsets_follow_the_mirror(sup):
    add_session(sup, "pc", "eeeeeeee1")
    chunk = b"y" * (512 * 1024)
    sup.broker = FakeBrokers({"pc": {"data": b64(chunk)}})
    await sup.fetch_remote_spools()
    await sup.fetch_remote_spools()
    assert [c[2] for c in sup.broker.calls] == [0, len(chunk)]
    assert mirrored(sup, "eeeeeeee1") == chunk * 2


async def test_a_broker_error_skips_only_that_session(sup):
    add_session(sup, "pc", "ffffffff1")
    add_session(sup, "pc", "ffffffff2")

    def reply(acc_id, offset):
        return BrokerError("OpError: nope") if acc_id == "ffffffff1" else {"data": b64(b"z\n")}
    sup.broker = FakeBrokers({"pc": reply})
    await sup.fetch_remote_spools()
    assert mirrored(sup, "ffffffff2") == b"z\n" and "pc" not in sup._host_backoff


async def test_the_mirror_stops_at_its_cap_with_one_error_event(sup, monkeypatch):
    monkeypatch.setattr(supervisor_module, "MAX_MIRROR", 10)
    add_session(sup, "pc", "gggggggg1")
    sup.broker = FakeBrokers({"pc": {"data": b64(b"1234\n")}})
    for _ in range(4):
        await sup.fetch_remote_spools()
    assert mirrored(sup, "gggggggg1") == b"1234\n1234\n"
    with sup.db() as db:
        events = db.scalars(select(Event).where(Event.kind == "remote_spool_full")).all()
        assert len(events) == 1 and events[0].session_id == "gggggggg1" and events[0].payload["limit"] == 10


async def test_local_ingest_runs_even_when_fetching_fails(sup, monkeypatch):
    async def broken():
        raise BrokerUnavailable("every node is down")
    ingested = []
    monkeypatch.setattr(sup, "fetch_remote_spools", broken)
    monkeypatch.setattr(sup, "ingest_all", lambda: ingested.append(True))
    await sup.ingest_pass()
    assert ingested


# ------------------------------------------------------------------ BrokerClient: replies it can't trust


async def one_shot_broker(tmp_path, answer):
    """A Unix socket that answers each request line with `answer(request)` (bytes; None: never answer)."""
    path = tmp_path / "b.sock"
    seen: list[dict] = []

    async def handle(reader, writer):
        while line := await reader.readline():
            request = json.loads(line)
            seen.append(request)
            reply = answer(request)
            if reply is not None:
                writer.write(reply)
                await writer.drain()
        writer.close()
    server = await asyncio.start_unix_server(handle, str(path))
    return server, path, seen


@pytest.mark.parametrize("answer", [
    lambda r: b"not json\n",
    lambda r: b"[1, 2]\n",
    lambda r: json.dumps({"id": r["id"] + 1, "ok": True, "result": 1}).encode() + b"\n",
], ids=["not-json", "not-object", "wrong-id"])
async def test_malformed_broker_answers_are_unavailable_not_crashes(tmp_path, answer):
    server, path, _ = await one_shot_broker(tmp_path, answer)
    async with server:
        client = BrokerClient(path)
        with pytest.raises(BrokerUnavailable):
            await client.call("ping")
        assert client._writer is None  # the connection is dropped, not reused out of step


async def test_an_overlong_answer_is_unavailable(tmp_path):
    server, path, _ = await one_shot_broker(tmp_path, lambda r: b"x" * (17 * 1024 * 1024) + b"\n")
    async with server:
        client = BrokerClient(path)
        with pytest.raises(BrokerUnavailable, match="too long"):
            await client.call("ping")


async def test_a_cancelled_call_drops_its_connection(tmp_path):
    answers = iter([None, b""])

    def answer(request):
        reply = next(answers)
        return None if reply is None else json.dumps({"id": request["id"], "ok": True, "result": 2}).encode() + b"\n"
    server, path, seen = await one_shot_broker(tmp_path, answer)
    async with server:
        client = BrokerClient(path)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.call("slow"), 0.2)
        assert client._writer is None
        assert await client.call("next") == 2
        await client.close()


async def test_the_mirror_is_redacted_as_it_is_written(sup):
    """Review D-low: the mirror in data/remote-spool is ACC's own copy, and it held the session's key values."""
    from acc.server import vault
    from acc.server.models import VaultKey
    secret = "sk-remote-canary-0123456789abcdef"
    with sup.db() as db:
        key = vault.create_key(db, sup.vault, name="Remote", variable="REMOTE_KEY", value=secret, owner="api")
        db.commit()
        key_id = key.id
    add_session(sup, "pc", "abababab1")
    with sup.db() as db:
        db.get(Session, "abababab1").vault_keys = [key_id]
        db.commit()
    first = json.dumps({"hook": {"cmd": f"curl -H 'Authorization: Bearer {secret}'"}}).encode() + b"\n"
    second = json.dumps({"hook": {"cmd": f"echo {secret[:20]}"}}).encode() + b"\n"
    replies = {0: first + second[:10], len(first): second}  # the second line arrives cut, then whole
    sup.broker = FakeBrokers({"pc": lambda acc_id, offset: {"data": b64(replies.get(offset, b""))}})
    await sup.fetch_remote_spools()
    await sup.fetch_remote_spools()
    await sup.fetch_remote_spools()
    assert [c[2] for c in sup.broker.calls] == [0, len(first), len(first) + len(second)]  # the node's offsets
    data = mirrored(sup, "abababab1")
    assert secret.encode() not in data and secret[:20].encode() not in data
    lines = [json.loads(line) for line in data.splitlines()]  # still JSON, line by line
    assert lines[0]["hook"]["cmd"] == "curl -H 'Authorization: Bearer «key: Remote»'"
    assert lines[1]["hook"]["cmd"] == "echo «key: Remote»"
    with sup.db() as db:
        assert db.get(VaultKey, key_id) is not None

