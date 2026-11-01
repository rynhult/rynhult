"""Multi-host, primary side: connected nodes and the broker-socket channels relayed over them (docs/MULTIHOST.md).

A node keeps one authenticated WebSocket open to the primary. Each broker connection the primary needs on that node
(a JSON-lines call client, a terminal attach) is a channel: `open()` returns a reader and writer that behave like an
asyncio Unix connection, so BrokerClient and the terminal bridge work unchanged.

Frames (binary): 1 byte type | 4 byte big-endian channel id | payload.
    o open {"account"} (→ node)   k opened (← node)   e error reason (← node)   d data (both)   c close (both)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import struct
import time
import unicodedata
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from acc.env import environment

from ..preview_sender import PrioritySender
from ..preview_stream import KINDS, Mux
from .node_tasks import NodeTasks
from .supervisor_common import log

FRAME = struct.Struct(">cI")
MAX_FRAME = 1 << 20  # data payload per frame
MAX_CONTROL = 4096  # payload of any other frame (k, e, c)
# uvicorn's ws_max_size, for every WebSocket: browser terminals too (a large paste), so above a node frame
# (FRAME.size + MAX_FRAME), whose own limit receive() enforces.
WS_MAX_SIZE = 4 << 20
MAX_CHANNEL_BUFFER = 16 << 20  # bytes from the node waiting for one channel's reader; past this the channel closes
MAX_OUTBOX = 64 << 20  # bytes queued for a node that isn't reading; past this the link closes
QUEUED_OVERHEAD = 64  # what each queued message costs on top of its bytes (a flood of tiny pongs counts too)
FLUSH_TIMEOUT = 30.0
HEARTBEAT_TIMEOUT = 90.0  # a node pings every 20 s: this long without a frame or ping and its link is closed
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ACCOUNT = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
CODE_TTL = timedelta(minutes=10)
OPEN_TIMEOUT = 15.0
PING_EVERY = 20.0


def frame(kind: bytes, channel: int, payload: bytes = b"") -> bytes:
    return FRAME.pack(kind, channel) + payload


def new_code() -> tuple[str, str]:
    """A one-time enrollment code (shown once) and the hash the database keeps."""
    code = "-".join(secrets.token_hex(3) for _ in range(4))  # e.g. 3fa21c-…: 96 bits, easy to read out
    return code, code_hash(code)



INFO_KEYS = ("os", "arch", "hostname", "version", "build", "wsl", "windows", "release", "platform", "wsl_distro")


def node_text(value, limit: int = 120) -> str:
    """Free text a node reports about itself, safe to store and show: whitespace (line breaks included) becomes one
    space; control, format (bidi overrides, zero-width), surrogate, private-use and unassigned characters go."""
    kept = (" " if c.isspace() else c for c in str(value) if c.isspace() or not unicodedata.category(c).startswith("C"))
    return " ".join("".join(kept).split())[:limit]


def node_lines(value, limit: int = 16384) -> str:
    """Longer free text (an agent's steps, notes, report summaries): node_text's rule, but line breaks, tabs and
    spacing stay. Control, format (bidi overrides, zero-width), surrogate, private-use and unassigned characters go."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text if c in "\n\t" or not unicodedata.category(c).startswith("C"))[:limit]


def node_info_text(info: dict, keys=INFO_KEYS) -> dict:
    """The reported system fields, cleaned (SR6-11). Other values (accounts, update state, isolation) pass through."""
    return {k: node_text(v) if k in keys else v for k, v in info.items()}


def reported_accounts(value) -> list[str]:
    """The agent accounts a node reports, as shown on its row: account names only (a lying node can't put an override
    or a lookalike in a badge), at most 20 (security review 7, SR7-5)."""
    return [a for a in value if isinstance(a, str) and ACCOUNT.fullmatch(a)][:20] if isinstance(value, list) else []


UPDATE_FIELDS = ("ok", "version", "running", "rolled_back", "id", "detail")


def update_result(result: dict, refused: dict | None) -> dict:
    """A node's update result as stored and audited: known fields only, its text cleaned and capped (SR7-7)."""
    if refused is not None:
        return {"ok": False, "detail": node_text(refused.get("detail", "Update refused"), 200)}
    return {k: node_text(result[k], 300 if k == "detail" else 120) if isinstance(result[k], str) else result[k]
            for k in UPDATE_FIELDS if isinstance(result.get(k), (str, bool))}


def code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()


def verify_signature(public_key_b64: str | None, message: bytes, signature_b64: str) -> bool:
    """Whether the node's link key (base64 raw Ed25519) signed this message; False for anything missing or malformed."""
    if not public_key_b64 or not signature_b64:
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(signature_b64, validate=True), message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify(public_key_b64: str, nonce: bytes, primary_id: str, signature_b64: str) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), nonce + primary_id.encode())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


class LinkError(Exception):
    """The node broke the protocol or a budget: its link is closed."""


class ChannelWriter:
    """The writing half of a channel, shaped like asyncio.StreamWriter for what ACC uses."""

    def __init__(self, link: "NodeLink", channel: int, state: "Channel | None" = None):
        self.link, self.channel, self.state = link, channel, state
        self._closed = False
        self._write_eof = False

    def write(self, data: bytes) -> None:
        if self._closed or self._write_eof or (self.state is not None and self.state.closed):
            raise ConnectionError("channel closed")
        for start in range(0, len(data), MAX_FRAME):
            self.link.queue(frame(b"d", self.channel, data[start:start + MAX_FRAME]))

    async def drain(self) -> None:
        await self.link.flush()
        if self.link.closed:
            raise ConnectionError("The machine disconnected.")

    def can_write_eof(self) -> bool:
        return bool(self.state and self.state.eof_supported)

    def write_eof(self) -> None:
        if not self.can_write_eof():
            raise NotImplementedError('node does not support request half-close')
        if not self._closed and not self._write_eof:
            self._write_eof = True
            self.link.queue(frame(b"h", self.channel))

    def is_closing(self) -> bool:
        return self._closed or self.link.closed

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if not self.link.closed:
                self.link.queue(frame(b"c", self.channel))
            self.link.forget(self.channel)

    async def wait_closed(self) -> None:
        try:
            await self.link.flush()
        except ConnectionError:
            pass

    def get_extra_info(self, name: str, default=None):
        return default


class Channel:
    def __init__(self) -> None:
        self.reader = asyncio.StreamReader(limit=MAX_CHANNEL_BUFFER)
        self.opened: asyncio.Future = asyncio.get_running_loop().create_future()
        self.closed = False
        self.request_eof = False
        self.eof_supported = False

    @property
    def acked(self) -> bool:
        return self.opened.done() and not self.opened.cancelled() and self.opened.exception() is None

    def buffered(self) -> int:
        """Bytes received and not yet read. StreamReader has no public count, and no transport here to pause."""
        return len(self.reader._buffer)


class NodeLink:
    """One authenticated node connection. `send` sends one WebSocket message; `on_close(code)`, set by whoever owns
    the WebSocket, closes it. close() is the one way a link ends: replaced, revoked, over a budget, silent, or gone."""

    def __init__(self, node_id: str, name: str, send, info: dict, accounts: list[str]):
        self.node_id, self.name, self.info, self.accounts = node_id, name, info, accounts
        self.tasks = NodeTasks(name)
        self.preview_sender = PrioritySender(send)
        self.tasks.track(self.preview_sender.task)
        self._send = self.preview_sender.send
        self.preview = Mux(self.preview_sender.send)
        self.preview_next = 1
        self.on_close = None
        self._outbox: asyncio.Queue[bytes | str | None] = asyncio.Queue()
        self._queued = 0  # bytes (plus QUEUED_OVERHEAD each) in _outbox or being sent
        self._drained = asyncio.Event()
        self._drained.set()
        self._channels: dict[int, Channel] = {}
        self._next = 1
        self.closed = False
        self.connected_at = time.time()
        self.last_seen = time.time()
        self.packages: dict | None = None  # its agent software, as its last "packages-state" said (cleaned)
        self.package_requests: dict[str, dict] = {}  # request id -> {package, action, at}: sent from here
        self.tool_waiters: dict[str, asyncio.Future] = {}
        self.folder_waiters: dict[str, asyncio.Future] = {}
        self.git_backup_waiters: dict[str, asyncio.Future] = {}
        # Host details (node_host.py): the last periodic summary and full snapshot, each with when it arrived
        self.host_light: dict | None = None
        self.host_light_at: float | None = None
        self.host_snapshot: dict | None = None
        self.host_snapshot_at: float | None = None
        self._host_request: tuple[str, asyncio.Future, float] | None = None  # one full snapshot asked for at a time
        self._loop = asyncio.get_running_loop()
        self._heard = self._loop.time()
        self._sender = self.tasks.start(self._pump(), "sender")
        self._watchdog = self.tasks.start(self._watch(), "heartbeat")

    async def _pump(self) -> None:
        try:
            while (data := await self._outbox.get()) is not None:
                await self._send(data)
                self._queued = max(0, self._queued - len(data) - QUEUED_OVERHEAD)
                if self._outbox.empty():
                    self._drained.set()
        except Exception:  # the socket went away
            self.close(1011)
        finally:
            self._drained.set()

    async def _watch(self) -> None:
        """The heartbeat deadline: a node that sends nothing (not even a ping) for HEARTBEAT_TIMEOUT is gone."""
        while not self.closed:
            left = self._heard + HEARTBEAT_TIMEOUT - self._loop.time()
            if left <= 0:
                log.warning("node %s: nothing for %gs; closing its link", self.name, HEARTBEAT_TIMEOUT)
                self.close(1001)
                return
            await asyncio.sleep(left)

    def heard(self) -> None:
        """Anything from the node (a frame, a ping): it's alive."""
        self.last_seen = time.time()
        self._heard = self._loop.time()

    def queue(self, data: bytes | str) -> None:
        if self.closed:
            return
        self._queued += len(data) + QUEUED_OVERHEAD
        if self._queued > MAX_OUTBOX:  # the node isn't reading what it asked for: give up on it
            log.warning("node %s: over %d bytes queued and not read; closing its link", self.name, MAX_OUTBOX)
            self.close()
            return
        self._drained.clear()
        self._outbox.put_nowait(data)

    async def flush(self, timeout: float = FLUSH_TIMEOUT) -> None:
        """Wait until everything queued has been sent (or the link closed), for at most `timeout` seconds."""
        if self.closed:
            return
        try:
            await asyncio.wait_for(self._drained.wait(), timeout)
        except asyncio.TimeoutError:
            raise ConnectionError(f"Machine {self.name} is not reading what Rynhult sends; it may be overloaded or stuck.") from None

    def request_packages(self, request_id: str, action: str, package: str) -> None:
        """Ask the node's acc-node for agent software: a catalog name and an action, nothing it could run."""
        self.package_requests[request_id] = {"package": package, "action": action, "at": time.time()}
        while len(self.package_requests) > 64:
            self.package_requests.pop(next(iter(self.package_requests)))
        self.queue(json.dumps({"t": "packages", "id": request_id, "action": action, "package": package}))

    async def git_backup(self, payload: dict, timeout: float) -> dict:
        if self.closed or 'git-backup-v1' not in self.info.get('capabilities', []):
            raise ConnectionError('Update Rynhult on this machine so it can back up projects.')
        if self.git_backup_waiters:
            raise RuntimeError('Another project backup is already running on this machine. Try again when it finishes.')
        rid = secrets.token_hex(12)
        waiter = self.git_backup_waiters[rid] = self._loop.create_future()
        self.queue(json.dumps({'t': 'git-backup', 'id': rid, **payload}))
        try:
            return await asyncio.wait_for(waiter, timeout)
        finally:
            self.git_backup_waiters.pop(rid, None)

    def git_backup_result(self, message: dict):
        rid = message.get('id')
        waiter = self.git_backup_waiters.get(rid) if isinstance(rid, str) else None
        if waiter is None or waiter.done():
            return
        result = message.get('result')
        if message.get('ok') is not True:
            waiter.set_exception(RuntimeError('The machine could not back up the project: ' + str(message.get('detail', 'refused'))[:200]))
        elif isinstance(result, dict) and result.get('retry') is True and len(result) == 1:
            waiter.set_result(result)  # no push occurred: token endpoint was temporarily unavailable
        elif not isinstance(result, dict) or result.get('via') not in ('private', 'public'):
            waiter.set_exception(RuntimeError("The machine sent a project backup result Rynhult can't use."))
        else:
            waiter.set_result(result)

    async def folder_policy(self, payload):
        if self.closed or len(self.folder_waiters) >= 1 or payload.get('machine_id') != self.node_id:
            raise ConnectionError('The folder service is unavailable or busy.')
        rid = secrets.token_hex(16)
        waiter = self.folder_waiters[rid] = asyncio.get_running_loop().create_future()
        frame = json.dumps({'t': 'folder-policy', 'id': rid, 'payload': payload})
        if len(frame.encode()) > 65536:
            self.folder_waiters.pop(rid, None)
            raise ValueError('The folder control frame is too large.')
        self.queue(frame)
        try:
            return await asyncio.wait_for(waiter, 40)
        finally:
            self.folder_waiters.pop(rid, None)

    def folder_policy_result(self, message):
        waiter = self.folder_waiters.get(message.get('id')) if isinstance(message.get('id'), str) else None
        if waiter is None or waiter.done():
            return
        if message.get('ok') is not True or not isinstance(message.get('result'), dict):
            waiter.set_exception(ValueError('The target refused the folder operation.'))
        else:
            waiter.set_result(message['result'])

    async def tool(self, payload: dict, timeout: float = 900) -> dict:
        from ..tooling import helper_request
        helper_request(payload)
        if self.closed:
            raise ConnectionError(f"Machine {self.name} is not connected.")
        rid = payload["id"]
        if rid in self.tool_waiters or len(self.tool_waiters) >= 8:
            raise ValueError("too many tool requests or duplicate id")
        waiter = self.tool_waiters[rid] = asyncio.get_running_loop().create_future()
        self.queue(json.dumps({"t": "tool", **payload}))
        try:
            return await asyncio.wait_for(waiter, timeout)
        finally:
            self.tool_waiters.pop(rid, None)

    def tool_result(self, message: dict):
        rid = message.get("id")
        waiter = self.tool_waiters.get(rid) if isinstance(rid, str) else None
        if waiter is None or waiter.done():
            return
        if not isinstance(message.get("ok"), bool) or set(message) - {"t", "id", "ok", "version", "detail"}:
            waiter.set_exception(ValueError("invalid tool result from node"))
            return
        version = message.get("version")
        if version is not None and (not isinstance(version, str) or len(version) > 100):
            waiter.set_exception(ValueError("invalid installed version from node"))
            return
        waiter.set_result({"ok": message["ok"], "version": version, "detail": str(message.get("detail") or "")[:1000]})

    @property
    def host_capable(self) -> bool:
        from .node_host import HOST_CAPABILITY
        return HOST_CAPABILITY in (self.info.get("capabilities") or [])

    async def request_host(self, timeout: float | None = None) -> dict | None:
        """A fresh full snapshot from the node, or the cached one when it is younger than MIN_REFRESH. Callers at the
        same time share one request; None when the node didn't answer in time (the cache is then all there is)."""
        from .node_host import REQUEST_TIMEOUT, fresh
        if fresh(self.host_snapshot_at) or self.closed or not self.host_capable:
            return self.host_snapshot
        request = self._host_request
        if request is None or request[1].done() or self._loop.time() - request[2] > REQUEST_TIMEOUT:
            if request is not None and not request[1].done():
                request[1].cancel()  # unanswered: ask again
            rid = f"host-{secrets.token_hex(6)}"
            self._host_request = (rid, self._loop.create_future(), self._loop.time())
            self.queue(json.dumps({"t": "host", "id": rid}))
        try:
            return await asyncio.wait_for(asyncio.shield(self._host_request[1]), timeout or REQUEST_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError, ConnectionError):
            if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                raise  # our caller was cancelled, not the request
            return self.host_snapshot

    def host_state(self, message: dict) -> None:
        """A "host-state" (full, answering request_host) or "host-light" (periodic) text frame from the node."""
        from .node_host import clean_light, clean_snapshot, light_from
        if message.get("t") == "host-light":
            light = clean_light(message.get("light"))
            if light is not None:
                self.host_light, self.host_light_at = light, time.time()
            return
        snapshot = clean_snapshot(message.get("snapshot"))
        if snapshot is None:
            return
        self.host_snapshot, self.host_snapshot_at = snapshot, time.time()
        self.host_light, self.host_light_at = light_from(snapshot), self.host_snapshot_at
        request = self._host_request
        if request is not None and not request[1].done() and message.get("id") == request[0]:
            request[1].set_result(snapshot)

    def forget(self, channel: int) -> None:
        self._channels.pop(channel, None)

    async def open(self, account: str) -> tuple[asyncio.StreamReader, ChannelWriter]:
        if self.closed:
            raise ConnectionError(f"Machine {self.name} is not connected.")
        if not ACCOUNT.match(account):
            raise ConnectionError(f"{account!r} is not a valid account name.")
        return await self._open_payload({'account': account})

    async def open_backup(self, target_id, installation_id):
        if 'backup-v1' not in self.info.get('capabilities', []):
            raise ConnectionError('Node update required for backup targets')
        if self.closed:
            raise ConnectionError('The backup machine disconnected.')
        return await self._open_payload({'type': 'backup-v1', 'target_id': target_id, 'installation_id': installation_id, 'request_eof': True})

    async def _open_payload(self, payload):
        channel_id = self._next
        self._next += 2  # the primary's channels are odd
        channel = self._channels[channel_id] = Channel()
        channel.request_eof = payload.get('type') == 'backup-v1' and payload.get('request_eof') is True
        self.queue(frame(b"o", channel_id, json.dumps(payload).encode()))
        try:
            await asyncio.wait_for(channel.opened, OPEN_TIMEOUT)
        except BaseException as error:  # timed out, refused, disconnected or cancelled: the channel is no more
            channel.closed = True
            self.forget(channel_id)
            if not self.closed and not isinstance(error, ConnectionError):
                self.queue(frame(b"c", channel_id))  # the node may open it yet: have it close it again
            if isinstance(error, asyncio.TimeoutError):
                raise ConnectionError(f"Machine {self.name} did not answer in time.") from None
            raise
        return channel.reader, ChannelWriter(self, channel_id, channel)

    def receive(self, data: bytes) -> None:
        """One binary frame from the node. LinkError (and the link closed) when it breaks the frame limits."""
        self.heard()
        if data[:1] in KINDS:
            try:
                self.preview.receive(data)
            except ValueError as error:
                self.close()
                raise LinkError(str(error)) from error
            return
        if len(data) < FRAME.size:
            return
        kind, channel_id = FRAME.unpack(data[:FRAME.size])
        payload = data[FRAME.size:]
        if len(payload) > (MAX_FRAME if kind == b"d" else MAX_CONTROL):
            self.close()
            raise LinkError(f"node {self.name} sent a {len(payload)}-byte {kind!r} frame")
        channel = self._channels.get(channel_id)
        if channel is None:
            return  # closed already, or never ours
        if kind == b"k" and not channel.opened.done():
            channel.eof_supported = channel.request_eof and payload == b"backup-eof-v1"
            channel.opened.set_result(True)
        elif kind == b"e":
            if not channel.opened.done():
                channel.opened.set_exception(ConnectionError(payload.decode(errors="replace")[:300] or "refused"))
            channel.closed = True
            channel.reader.feed_eof()
            self.forget(channel_id)
        elif kind == b"d":
            if not channel.acked:
                return  # data before "opened": not a channel yet
            if channel.buffered() + len(payload) > MAX_CHANNEL_BUFFER:
                log.warning("node %s: channel %d has %d bytes nobody reads; closing it", self.name, channel_id,
                            channel.buffered())
                self.abort_channel(channel_id, channel)
                return
            channel.reader.feed_data(payload)
        elif kind == b"c":
            channel.closed = True
            channel.reader.feed_eof()
            self.forget(channel_id)

    def abort_channel(self, channel_id: int, channel: Channel) -> None:
        """Our side gives up on a channel: its reader fails, its writer refuses, and the node is told to close it."""
        channel.closed = True
        channel.reader.set_exception(ConnectionError(f"node {self.name}: channel over its buffer limit"))
        self.forget(channel_id)
        self.queue(frame(b"c", channel_id))

    def close(self, code: int = 1000) -> None:
        """End the link (idempotent): fail its channels, drop what's queued, stop its tasks, close the WebSocket."""
        if self.closed:
            return
        self.closed = True
        self.preview.close()
        self.preview_sender.close()
        for waiter in [*self.tool_waiters.values(), *self.git_backup_waiters.values(), *self.folder_waiters.values()]:
            if not waiter.done():
                waiter.set_exception(ConnectionError(f"Machine {self.name} disconnected during a tool change."))
        if self._host_request is not None and not self._host_request[1].done():
            self._host_request[1].set_exception(ConnectionError(f"Machine {self.name} disconnected."))
            self._host_request[1].exception()  # retrieved: nobody may be waiting any more
        self.tasks.cancel()
        if self.on_close is not None:
            self.on_close(code)
        for channel in list(self._channels.values()):
            if not channel.opened.done():
                channel.opened.set_exception(ConnectionError(f"Machine {self.name} disconnected."))
            channel.closed = True
            channel.reader.feed_eof()
        self._channels.clear()
        while not self._outbox.empty():  # nothing more goes out: drop what's queued
            self._outbox.get_nowait()
        self._queued = 0
        self._drained.set()  # flush() waiters: nothing more will be sent


    async def wait_closed(self):
        await self.tasks.join()


class NodeHub:
    """The nodes connected right now, by name. One link per node: a newer one replaces the old."""

    def __init__(self) -> None:
        self.links: dict[str, NodeLink] = {}
        self.retiring = {}
        self.jobs = {}
        self.closed = False
        self.primary_id = environment.get("ACC_PRIMARY_ID") or secrets.token_hex(8)

    def attach(self, link: NodeLink) -> None:
        if self.closed:
            self._retire(link, 1012)
            raise ConnectionError("Rynhult is shutting down.")
        self._stop_jobs(link.name)
        old = self.links.get(link.name)
        if old is not None:
            self._retire(old, 4409)  # replaced by a newer connection
        self.links[link.name] = link

    def detach(self, link: NodeLink) -> None:
        if self.links.get(link.name) is link:
            del self.links[link.name]
        self._retire(link)

    def _retire(self, link, code=1000):
        link.close(code)
        self._drain_scope(link.tasks)

    def _drain_scope(self, scope):
        if scope not in self.retiring:
            task = asyncio.create_task(scope.join(), name=f"node:{scope.name}:drain")
            self.retiring[scope] = task
            def finished(done):
                self.retiring.pop(scope, None)
                if not done.cancelled() and (error := done.exception()) is not None:
                    log.error("node %s: drain failed", scope.name,
                              exc_info=(type(error), error, error.__traceback__))
            task.add_done_callback(finished)

    def _stop_jobs(self, name):
        scope = self.jobs.pop(name, None)
        if scope:
            scope.cancel()
            self._drain_scope(scope)

    def start(self, name, work, label, *, report=False):
        link = self.links.get(name)
        scope = link.tasks if link and not link.closed else self.jobs.setdefault(name, NodeTasks(name))
        if self.closed:
            scope.cancel()
        return scope.start(work, label, report=report)

    async def drain(self, name=None):
        while pending := [task for link, task in self.retiring.items() if (name is None or link.name == name) and not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self):
        self.closed = True
        for name in set(self.links) | set(self.jobs):
            self.disconnect(name, code=1001)  # going away: enrollment remains valid
        await self.drain()

    def get(self, name: str) -> NodeLink:
        link = self.links.get(name)
        if link is None or link.closed:
            raise ConnectionError(f"Machine {name} is not connected.")
        return link

    def disconnect(self, name: str, code: int = 4401) -> None:
        self._stop_jobs(name)
        link = self.links.pop(name, None)
        if link is not None:
            self._retire(link, code)  # removal revokes; shutdown only asks for reconnect


def now() -> datetime:
    return datetime.now(timezone.utc)
