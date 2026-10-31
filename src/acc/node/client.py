"""`acc-node run`: one outbound WebSocket to the primary, relaying channels to this machine's broker sockets.

Binary frames: 1 byte type | 4 byte big-endian channel id | payload (docs/MULTIHOST.md, "Channels").
    o open (primary → node, JSON {"account"})   k opened / e error (node → primary)
    d data (either way, ≤ 1 MiB)                 c close (either way)
    h request EOF (primary → node, negotiated backup channels only)
Text frames: ping / pong, agent software: "packages" (→ node) and "packages-state" (← node), see packages.py, and host
details: "host" (→ node) and "host-state" / "host-light" (← node), see acc.hostinfo.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import struct
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .. import hostinfo
from ..preview_sender import PrioritySender
from ..preview_stream import KINDS
from ..server.broker_client import BrokerClient, BrokerError, uid_of
from ..wsl import isolation_state
from . import machine
from .config import STATUS_FILE, NodeConfig, write_atomic
from .identity import TrustError, node_info, sign_challenge, tls_context
from .packages import MAX_MESSAGE, MacPackages, Packages, backend
from .preview import PreviewRelay
from .updates import Updates

log = logging.getLogger("acc.node")

HEADER = struct.Struct(">cI")
MAX_DATA = 1 << 20  # per frame
MAX_BUFFERED = 16 << 20  # bytes queued for one broker that isn't reading; past this the channel is dropped
ITEM_OVERHEAD = 256  # what one queued frame costs beyond its bytes (asyncio.Queue entry, bytes object)
MAX_RELAY_BUFFERED = 64 << 20  # all channels together (live and draining): well inside the service's memory limit
MAX_CHANNELS = 64  # live plus draining channels; more opens are refused before connecting to a broker
DRAIN_TIMEOUT = 30.0  # a channel the primary closed gets this long to hand its last bytes to the broker
ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,31}")  # a Linux user name
AUTH_REJECTED = 4401
HANDSHAKE_TIMEOUT = 30
MISSED_PONGS = 3
MAX_TEXT = 4096  # text frames from the primary are small; longer ones are ignored unread
PACKAGES_EVERY = 2.0  # how often the agent software state is looked at (sent only when it changed)
HOST_LIGHT_EVERY = 60.0  # the periodic host summary (the status board's CPU, memory and fullest disk)
HOST_MIN_INTERVAL = 10.0  # a full host snapshot is collected at most this often; requests in between get the last one
MAX_HOST_TEXT = 60 * 1024  # under the primary's text frame limit
CAPABILITIES = ["rename-compat-1", "preview-v1", "host-v1", "backup-v1", "git-backup-v1", "update"]
AUTO = object()


class AuthRejected(Exception):
    pass


class ProtocolError(Exception):
    pass


def frame(kind: bytes, channel_id: int, payload: bytes = b"") -> bytes:
    return HEADER.pack(kind, channel_id) + payload


def connect_url(primary: str) -> str:
    parts = urlsplit(primary)
    if parts.scheme not in ("https", "wss"):
        raise ValueError(f"bad primary URL {primary!r}: expected https://host[:port]")
    return urlunsplit(("wss", parts.netloc, parts.path.rstrip("/") + "/api/nodes/connect", "", ""))


def broker_accounts(socket_dir: Path) -> list[str]:
    try:
        return sorted(p.stem for p in socket_dir.glob("*.sock") if ACCOUNT.fullmatch(p.stem) and p.is_socket())
    except OSError:
        return []


class Channel:
    """One relayed connection: the primary's end is the channel id, ours is a Unix socket to a broker."""

    def __init__(self, relay: "Relay", channel_id: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.relay, self.id, self.reader, self.writer = relay, channel_id, reader, writer
        self.outbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.buffered = 0
        self.request_ended = False
        self.request_eof = False
        self.tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self.tasks = [self.relay.own(self._from_broker()), self.relay.own(self._to_broker())]

    async def _from_broker(self) -> None:
        try:
            while chunk := await self.reader.read(MAX_DATA):
                await self.relay.send(frame(b"d", self.id, chunk))
        except ConnectionClosed:
            return  # the WebSocket is gone; Relay.close_all cleans up
        except OSError:
            pass
        await self.relay.broker_closed(self)

    async def _to_broker(self) -> None:
        try:
            while (data := await self.outbox.get()) is not None:
                if data == b"":  # negotiated backup request EOF, after all queued data
                    self.writer.write_eof()
                    await self.writer.drain()
                    return  # keep the response-reading task alive
                self.buffered -= len(data) + ITEM_OVERHEAD
                self.writer.write(data)
                await self.writer.drain()
        except OSError:
            await self.relay.broker_closed(self)
            return
        self.writer.close()  # the primary closed: everything it sent has been written

    def write(self, data: bytes) -> bool:
        """Queue bytes for the broker; False when this channel holds too much (each queued frame also costs
        ITEM_OVERHEAD, so a flood of tiny frames counts). An empty frame carries nothing and is skipped."""
        if self.request_ended:
            return False
        if not data:
            return True
        cost = len(data) + ITEM_OVERHEAD
        if self.buffered + cost > MAX_BUFFERED or self.relay.buffered() + cost > MAX_RELAY_BUFFERED:
            return False
        self.buffered += cost
        self.outbox.put_nowait(data)
        return True

    def end_request(self) -> None:
        if not self.request_ended:
            self.request_ended = True
            self.outbox.put_nowait(b"")

    def finish(self) -> None:
        """The primary closed its side: stop reading, flush what it sent, then close."""
        self._cancel(self.tasks[:1])
        self.outbox.put_nowait(None)

    def abort(self) -> None:
        self._cancel(self.tasks)
        self.writer.close()

    @staticmethod
    def _cancel(tasks: list[asyncio.Task]) -> None:
        current = asyncio.current_task()
        for task in tasks:
            if task is not current and not task.cancelling():
                task.cancel()


class Relay:
    """Channel state for one authenticated connection."""

    def __init__(self, ws: ClientConnection, socket_dir: Path, connect_timeout: float = 10):
        self.ws, self.socket_dir, self.connect_timeout = ws, socket_dir, connect_timeout
        self.channels: dict[int, Channel] = {}
        self.preview = PreviewRelay(self.send)
        self.tasks: set[asyncio.Task] = set()
        self.draining: dict[Channel, asyncio.Task] = {}  # closed by the primary, still flushing to the broker

    def own(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def send(self, data: bytes) -> None:
        await self.ws.send(data)

    def buffered(self) -> int:
        return sum(c.buffered for c in self.channels.values()) + sum(c.buffered for c in self.draining)

    async def handle(self, message: bytes) -> None:
        if message[:1] in KINDS:
            try:
                self.preview.receive(message)
            except (ValueError, KeyError, TypeError, ConnectionError) as error:
                raise ProtocolError("invalid preview frame") from error
            return
        if len(message) < HEADER.size:
            log.warning("ignoring a %d-byte binary message", len(message))
            return
        kind, channel_id = HEADER.unpack_from(message)
        payload = message[HEADER.size:]
        if kind == b"o":
            await self.open(channel_id, payload)
        elif kind == b"d":
            channel = self.channels.get(channel_id)
            if channel is None:
                return  # closed on our side while this was in flight; the primary has (or will get) our "c"
            if len(payload) > MAX_DATA or not channel.write(payload):
                log.warning("channel %d: dropping it (frame too large or broker not reading)", channel_id)
                await self.drop(channel)
        elif kind == b"h":
            channel = self.channels.get(channel_id)
            if channel is not None and channel.request_eof and not payload:
                channel.end_request()
            elif channel is not None:
                await self.drop(channel)  # never half-close ordinary broker channels
        elif kind == b"c":
            if (channel := self.channels.pop(channel_id, None)) is not None:
                channel.finish()
                self.draining[channel] = self.own(self._drain(channel))
        else:
            log.warning("ignoring a frame of unknown type %r", kind)

    async def _drain(self, channel: Channel) -> None:
        """Until the broker has what the primary sent, or DRAIN_TIMEOUT: then the channel is gone for good."""
        try:
            if channel.tasks[1:]:
                await asyncio.wait(channel.tasks[1:], timeout=DRAIN_TIMEOUT)
        finally:
            channel.abort()
            self.draining.pop(channel, None)

    async def open(self, channel_id: int, payload: bytes) -> None:
        if (existing := self.channels.pop(channel_id, None)) is not None:
            existing.abort()
            await self.send(frame(b"e", channel_id, b"channel id already in use"))
            return
        if len(self.channels) + len(self.draining) >= MAX_CHANNELS:
            log.warning("refusing channel %d: %d channels open or closing", channel_id, MAX_CHANNELS)
            await self.send(frame(b"e", channel_id, b"too many channels"))
            return
        try:
            request = json.loads(payload)
            if request.get('type') == 'backup-v1':
                await self.open_backup(channel_id, request)
                return
            account = request.get("account")
        except (ValueError, AttributeError):
            account = None
        if not isinstance(account, str) or not ACCOUNT.fullmatch(account):
            await self.send(frame(b"e", channel_id, b"bad account name"))
            return
        path = self.socket_dir / f"{account}.sock"
        if path.resolve().parent != self.socket_dir.resolve():
            await self.send(frame(b"e", channel_id, b"broker socket is outside the socket dir"))
            return
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path), limit=MAX_DATA),
                                                    self.connect_timeout)
        except (OSError, asyncio.TimeoutError) as error:
            reason = getattr(error, "strerror", None) or "timed out"
            await self.send(frame(b"e", channel_id, f"no broker for {account}: {reason}".encode()))
            return
        channel = Channel(self, channel_id, reader, writer)
        self.channels[channel_id] = channel
        await self.send(frame(b"k", channel_id))
        channel.start()

    async def open_backup(self, channel_id, request):
        from .. import backup_targets
        if any(getattr(c, 'backup', False) for c in [*self.channels.values(), *self.draining]):
            # A normal HTTP reconnect may arrive before the previous channel has
            # drained. Refuse cheaply so the adapter can retry without blocking
            # this node's control/terminal receive loop.
            await self.send(frame(b'e', channel_id, b'backup receiver busy'))
            return
        try:
            port = backup_targets.receiver_binding(backup_targets.BINDING, request.get('target_id'), request.get('installation_id'))
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection('127.0.0.1', port, limit=MAX_DATA), self.connect_timeout)
        except (ValueError, OSError, asyncio.TimeoutError) as error:
            reason = error.reason if isinstance(error, backup_targets.ReceiverRefused) else 'down'
            await self.send(frame(b'e', channel_id, backup_targets.RECEIVER_REFUSALS[reason].encode()))
            return
        channel = Channel(self, channel_id, reader, writer)
        channel.backup = True
        channel.request_eof = request.get('request_eof') is True
        self.channels[channel_id] = channel
        await self.send(frame(b'k', channel_id, b'backup-eof-v1' if channel.request_eof else b''))
        channel.start()

    async def broker_closed(self, channel: Channel) -> None:
        if self.channels.get(channel.id) is channel:
            await self.drop(channel)

    async def drop(self, channel: Channel) -> None:
        self.channels.pop(channel.id, None)
        channel.abort()
        try:
            await self.send(frame(b"c", channel.id))
        except ConnectionClosed:
            pass

    def close_all(self) -> None:
        """The connection is gone: every channel, live or draining, and every task they own."""
        self.preview.close()
        for channel in self.channels.values():
            channel.abort()
        self.channels.clear()
        for channel, task in list(self.draining.items()):
            task.cancel()
            channel.abort()
        self.draining.clear()

    async def aclose(self) -> None:
        tasks = list(self.tasks)
        self.close_all()
        Channel._cancel(tasks)
        await self.preview.aclose()
        await asyncio.gather(*tasks, return_exceptions=True)


class Node:
    def __init__(self, config: NodeConfig, key: Ed25519PrivateKey, state_dir: Path | None = None, *,
                 ping_interval: float = 20, min_backoff: float = 1, max_backoff: float = 60,
                 packages: Packages | None = AUTO, request_replies_root: Path | None = None):
        from ..request_replies import ROOT
        self.request_replies_root = request_replies_root or ROOT
        self.updates = Updates()
        self.config, self.key, self.state_dir = config, key, state_dir
        self.packages = backend(state_dir) if packages is AUTO else packages
        self._packages_sent: str | None = None
        self._refreshed = False
        self.url = connect_url(config.primary)
        self.ping_interval = ping_interval
        self.min_backoff, self.max_backoff = min_backoff, max_backoff
        self.relay: Relay | None = None
        self.host_collect = hostinfo.snapshot, hostinfo.light  # tests replace these
        self._host_lock = asyncio.Lock()
        self._host_last: tuple[float, dict] | None = None
        self.unanswered = 0
        self.sleep = asyncio.sleep  # tests watch the backoff through this

    async def run_forever(self) -> None:
        delay = self.min_backoff
        primaries = tuple(dict.fromkeys((self.config.primary, *self.config.alternate_primaries)))
        endpoint = 0
        while True:
            self.url = connect_url(primaries[endpoint])
            self.write_status("connecting")
            reached_ready = False
            try:
                reached_ready = await self.connect_once()
                error = "connection closed"
            except AuthRejected as failure:
                error = str(failure)
            except (OSError, WebSocketException, TrustError, ProtocolError, asyncio.TimeoutError) as failure:
                error = f"{type(failure).__name__}: {failure}" if str(failure) else type(failure).__name__
            if reached_ready:
                delay = self.min_backoff
            log.warning("disconnected from %s (%s); retrying in %gs", self.url, error, delay)
            self.write_status("disconnected", error=error, retry_in=delay)
            await self.sleep(delay)
            delay = min(delay * 2, self.max_backoff)
            endpoint = (endpoint + 1) % len(primaries)

    async def connect_once(self) -> bool:
        """One connection: handshake, then relay until it ends. True when it got as far as "ready"."""
        # Re-read both: the CA file may be replaced, and a rotated machine identity is switched in between links.
        context = tls_context(self.config.trust, self.config.ca_file, identity=machine.current(self.state_dir))
        ws = await connect(self.url, ssl=context, proxy=None, ping_interval=None, max_size=HEADER.size + MAX_DATA,
                           open_timeout=HANDSHAKE_TIMEOUT)
        async with ws:
            await self.handshake(ws)
            log.info("connected to %s as node %s", self.url, self.config.node_id)
            self.write_status("connected")
            try:
                await ws.send(json.dumps(await self.update_message()))
                await self.serve(ws)
            except ConnectionClosed:
                pass  # ready was reached: reconnect with the minimum backoff
        return True

    async def handshake(self, ws: ClientConnection) -> None:
        try:
            challenge = json.loads(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))
            if not isinstance(challenge, dict) or challenge.get("t") != "challenge":
                raise ProtocolError("expected a challenge")
            signature = sign_challenge(self.key, challenge["nonce"], str(challenge["primary"]))
            presented = machine.fingerprint(self.state_dir)
            await ws.send(json.dumps({"t": "hello", "node_id": self.config.node_id, "signature": signature,
                                      **({"machine": presented} if presented else {}),
                                      "info": {**await self.machine_info(), "capabilities": self.capabilities()},
                                      "accounts": broker_accounts(self.config.socket_dir)}))
            answer = json.loads(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))
        except ConnectionClosed as closed:
            if closed.rcvd is not None and closed.rcvd.code == AUTH_REJECTED:
                raise AuthRejected(f"the primary rejected this node ({closed.rcvd.reason or 'not authorized'}); "
                                   "is it revoked? Re-enroll with a new code.") from None
            raise
        except (ValueError, KeyError, TypeError) as error:
            raise ProtocolError(f"bad handshake message: {error}") from None
        if not isinstance(answer, dict) or answer.get("t") != "ready":
            raise ProtocolError("expected ready")

    def capabilities(self):
        capabilities = list(CAPABILITIES)
        try:
            info = self.request_replies_root.lstat()
            import stat
            if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o066:
                capabilities.append('request-replies-v1')
        except OSError:
            pass  # older/unprovisioned or same-account platforms remain unconfirmed
        return capabilities

    async def machine_info(self) -> dict:
        info = node_info()
        if "wsl_isolation" not in info:
            return info

        async def access(account):
            uid = uid_of(account)
            if uid is None:
                log.warning("Cannot check WSL drive access: agent account %s is missing", account)
                return None
            client = BrokerClient(self.config.socket_dir / f"{account}.sock", trusted_uids=frozenset({0, uid}))
            try:
                result = await asyncio.wait_for(client.call("ping", wsl_isolation=True), timeout=1)
                if (isinstance(result, dict) and result.get("role") == "agents"
                        and type(result.get("windows_drive_readable")) is bool):
                    return result["windows_drive_readable"]
                log.warning("Agent broker %s did not report Windows drive access", account)
            except (BrokerError, OSError, TimeoutError):
                log.warning("Cannot check WSL drive access through agent broker %s", account)
            finally:
                await client.close()
            return None

        accounts = broker_accounts(self.config.socket_dir)[:20]
        results = await asyncio.gather(*(access(account) for account in accounts))
        info["wsl_isolation"] = isolation_state(info["wsl_isolation"]["interop_enabled"], dict(zip(accounts, results)))
        return info

    async def update_message(self) -> dict:
        return {**self.updates.message(), "info": await self.machine_info()}

    async def serve(self, ws: ClientConnection) -> None:
        sender = PrioritySender(ws.send)
        class Socket:
            send = sender.send
            close = ws.close
        output = Socket()
        self.relay = relay = Relay(output, self.config.socket_dir)
        self.unanswered = 0
        pinger = asyncio.create_task(self.pinger(output))
        self._packages_sent = None
        tool_tasks: set[asyncio.Task] = set()
        self._tool_tasks = tool_tasks
        reporter = asyncio.create_task(self.report_packages(output)) if self.packages is not None else None
        host_reporter = asyncio.create_task(self.report_host(output))
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    await relay.handle(message)
                else:
                    await self.handle_text(output, message)
        finally:
            tasks = [pinger, host_reporter, *tool_tasks]
            if reporter is not None:
                tasks.append(reporter)
            for task in tasks:
                task.cancel()
            sender.close()
            await asyncio.gather(*tasks, relay.aclose(), sender.aclose(), return_exceptions=True)
            self.relay = None

    async def handle_text(self, ws: ClientConnection, message: str) -> None:
        if len(message) > 65536:
            log.warning("ignoring a %d-character text message", len(message))
            return
        try:
            data = json.loads(message)
            kind = data.get("t")
        except (ValueError, AttributeError):
            data, kind = None, None
        if len(message) > MAX_TEXT and kind != 'request-replies':
            return  # only the bounded server acknowledgement batch gets the larger frame
        if kind == "update":
            await asyncio.to_thread(self.updates.handle, data)
            await ws.send(json.dumps(await self.update_message()))
        elif kind == 'request-replies':
            from ..request_replies import publish
            try:
                await asyncio.to_thread(publish, self.request_replies_root, data.get('session_id'), data.get('replies'))
            except (OSError, ValueError, TypeError):
                log.warning('Could not persist request acknowledgements; requests remain unconfirmed')
        elif kind == "ping":
            await ws.send(json.dumps({"t": "pong"}))
        elif kind == "pong":
            self.unanswered = 0
        elif kind == "packages" and self.packages is not None and len(message) <= MAX_MESSAGE:
            self.packages.handle(data)  # checked there: catalog names and fixed actions only, rate-limited
            await self.send_packages(ws)
        elif kind == "host" and isinstance(data.get("id"), str) and len(data["id"]) <= 64:
            tasks = getattr(self, "_tool_tasks", None)
            if tasks is None:
                self._tool_tasks = tasks = set()
            if not any(getattr(t, "host_request", False) for t in tasks):  # one at a time; the primary asks again
                task = asyncio.create_task(self.send_host(ws, data["id"]))
                task.host_request = True
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        elif kind == "machine-csr-request" and self.state_dir is not None:
            csr = await asyncio.to_thread(machine.new_request, self.state_dir, self.config.node_id)
            await ws.send(json.dumps({"t": "machine-csr", "csr": csr}))
        elif kind == "machine-certificate" and self.state_dir is not None:
            if not await asyncio.to_thread(machine.accept, self.state_dir, data.get("certificate")):
                log.warning("ignoring a machine certificate for a key this node does not hold")
        elif kind == "machine-ready" and self.state_dir is not None:
            if await asyncio.to_thread(machine.switch, self.state_dir, str(data.get("fingerprint", ""))):
                log.info("switched to the rotated machine identity; reconnecting with it")
                await ws.close()  # every later link and download presents the successor
        elif kind == 'git-backup':
            tasks = getattr(self, '_tool_tasks', None)
            if tasks is None:
                self._tool_tasks = tasks = set()
            if any(getattr(t, 'git_backup', False) for t in tasks):
                await ws.send(json.dumps({'t': 'git-backup-result', 'id': data.get('id'), 'ok': False,
                                          'detail': 'A git backup is already running'}))
            else:
                task = asyncio.create_task(self.git_backup_request(ws, data))
                task.git_backup = True
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        elif kind == "tool" and len(message) <= 2048:
            tasks = getattr(self, "_tool_tasks", None)
            if tasks is None:
                self._tool_tasks = tasks = set()
            if len(tasks) >= 8:
                await ws.send(json.dumps({"t": "tool-result", "id": data.get("id"), "ok": False,
                                          "detail": "too many tool requests"}))
            else:
                task = asyncio.create_task(self.tool_request(ws, data))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        else:
            log.warning("ignoring text message %.80r", message)

    async def host_snapshot(self) -> dict:
        """The full read-only snapshot, collected at most every HOST_MIN_INTERVAL seconds (else the last one)."""
        async with self._host_lock:
            last = self._host_last
            if last is not None and time.monotonic() - last[0] < HOST_MIN_INTERVAL:
                return last[1]
            snapshot = await asyncio.to_thread(self.host_collect[0])
            self._host_last = (time.monotonic(), snapshot)
            return snapshot

    async def send_host(self, ws, request_id: str) -> None:
        try:
            snapshot = await self.host_snapshot()
        except Exception as error:  # a reader failed in a way it didn't foresee: say nothing rather than drop the link
            log.warning("host snapshot failed: %s", error)
            return
        text = json.dumps({"t": "host-state", "id": request_id, "snapshot": snapshot})
        if len(text) > MAX_HOST_TEXT:  # caps make this unlikely; the bulky optional parts go first
            snapshot = {**snapshot, "services": None, "gpu": None,
                        "unavailable": [*snapshot.get("unavailable", []), "services", "gpu"]}
            text = json.dumps({"t": "host-state", "id": request_id, "snapshot": snapshot})
        if len(text) <= MAX_HOST_TEXT:
            await ws.send(text)

    async def report_host(self, ws) -> None:
        """On connect and every HOST_LIGHT_EVERY seconds: the light summary."""
        while True:
            try:
                light = await asyncio.to_thread(self.host_collect[1])
                await ws.send(json.dumps({"t": "host-light", "light": light}))
            except ConnectionClosed:
                return
            except Exception as error:
                log.warning("host summary failed: %s", error)
            await asyncio.sleep(HOST_LIGHT_EVERY)

    async def git_backup_request(self, ws, data):
        from .git_backup import run_job
        try:
            async with asyncio.timeout(1800):
                result = await run_job(self.config, self.state_dir, data, link_url=self.url, key=self.key)
            message = {'t': 'git-backup-result', 'id': data.get('id'), 'ok': True, 'result': result}
            if len(json.dumps(message)) > 60000:
                raise ValueError('Git backup result exceeds the control message limit')
        except Exception as error:
            message = {'t': 'git-backup-result', 'id': data.get('id'), 'ok': False, 'detail': str(error)[:300]}
        await ws.send(json.dumps(message))

    async def tool_request(self, ws, data):
        try:
            if self.packages is None:
                raise ValueError("no system tool helper")
            result = await self.packages.tool(data)
        except (ValueError, OSError, TimeoutError) as error:
            log.warning("tool request refused: %s", error)
            result = {"ok": False, "detail": str(error)[:500]}
        await ws.send(json.dumps({"t": "tool-result", "id": data.get("id"), **result}))

    async def send_packages(self, ws: ClientConnection) -> None:
        """The agent software state, when it differs from what the primary last got on this connection."""
        # The helper's files are read off the event loop; a Mac's state is in memory (and changed on the loop).
        state = self.packages.message() if isinstance(self.packages, MacPackages) else \
            await asyncio.to_thread(self.packages.message)
        text = json.dumps(state, sort_keys=True)
        if text != self._packages_sent:
            self._packages_sent = text
            await ws.send(text)

    async def report_packages(self, ws: ClientConnection) -> None:
        """On connect and whenever it changes: the helper works on its own time, so its state is looked at often."""
        while True:
            try:
                await self.send_packages(ws)
            except ConnectionClosed:
                return
            if isinstance(self.packages, MacPackages) and not self._refreshed:
                self._refreshed = True  # once a run, after the first report: npm can take a while
                await self.packages.refresh_all()
                continue
            await asyncio.sleep(PACKAGES_EVERY)

    async def pinger(self, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(self.ping_interval)
            if self.unanswered >= MISSED_PONGS:
                log.warning("%d pings without a pong; dropping the connection", self.unanswered)
                await ws.close(1011, "missed pongs")
                return
            self.unanswered += 1
            await ws.send(json.dumps(await self.update_message()))
            await ws.send(json.dumps({"t": "ping"}))

    def write_status(self, state: str, **extra) -> None:
        if self.state_dir is None:
            return
        status = {"state": state, "since": time.time(), "primary": self.config.primary, "pid": os.getpid(), **node_info(), **extra}
        try:
            write_atomic(self.state_dir / STATUS_FILE, json.dumps(status).encode(), 0o644)
        except OSError as error:
            log.debug("cannot write status: %s", error)
