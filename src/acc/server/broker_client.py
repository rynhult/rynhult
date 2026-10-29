from __future__ import annotations

import asyncio
import itertools
import json
import pwd
import re
from contextlib import nullcontext
from pathlib import Path

from .. import peercred
from ..broker_protocol import response_from

# Deliberately exclude mixed read/write APIs (files, github_api) and unknown methods.
# Add a method only after checking that repeating it cannot repeat a side effect.
RETRYABLE_READS = frozenset({
    "ping", "list", "auth_health", "model_catalog", "image_identity", "usage", "installed_providers",
    "read_spool", "workspace_changes", "automation_screen", "screen_prompt", "terminal_output",
    "transcript_messages", "transcript_exists", "claude_agents", "claude_remote",
    "compose_digest", "compose_ps", "codex_plugins", "wiki_tree", "wiki_page",
    "wiki_attachment", "list_artifacts", "read_artifact", "explanation_result",
    "scan_docs", "read_doc", "git_refs", "team_inspect", "team_handoffs", "team_bundle",
})

ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,31}")  # a Linux user name


class BrokerError(Exception):
    pass


class BrokerUnavailable(BrokerError):
    pass


class MachineOffline(BrokerUnavailable):
    """Expected absence of an outward-connected node; no transport retry is possible."""


class BrokerClient:
    """Async JSON-lines client for acc-broker. One connection, requests serialized."""

    def __init__(self, socket_path: Path, trusted_uids: frozenset[int] | None = None, opener=None,
                 timeout: float = 60.0, authority=None):
        self.authority = authority
        self.socket_path = socket_path
        self.timeout = timeout  # seconds to wait for one answer
        self.trusted_uids = trusted_uids  # who may own the listening end (None: anyone, for tests)
        self.opener = opener  # a node's channel instead of a local socket (multi-host): () -> (reader, writer)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)

    async def _connect(self) -> None:
        if self.opener is not None:
            try:
                self._reader, self._writer = await self.opener()
            except (OSError, ConnectionError) as error:
                raise BrokerUnavailable(str(error)) from error
            return  # the node is authenticated; its broker checks its own callers
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(str(self.socket_path), limit=16 * 1024 * 1024)
        except OSError as error:
            raise BrokerUnavailable(f"broker socket {self.socket_path}: {error}") from error
        try:
            uid = peer_uid(self._writer)
        except OSError as error:  # PeerCredError included: who holds the socket is unknown, so don't trust it
            await self.close()
            raise BrokerUnavailable(f"broker socket {self.socket_path}: cannot read its owner ({error}); refusing it") from error
        if self.trusted_uids is not None and uid not in self.trusted_uids:
            await self.close()
            raise BrokerUnavailable(f"broker socket {self.socket_path} is held by uid {uid}, not the broker; refusing it")

    async def call(self, method: str, **params):
        with self.authority.activity() if self.authority else nullcontext():
            return await self._call(method, **params)

    def _uncertain(self, method):
        if self.authority and method not in RETRYABLE_READS:
            self.authority.mark_uncertain('broker.' + method)

    async def _call(self, method: str, **params):
        async with self._lock:
            for attempt in (1, 2):
                if self._writer is None:
                    await self._connect()
                request_id = next(self._ids)
                try:
                    self._writer.write(json.dumps({"id": request_id, "method": method, "params": params}).encode() + b"\n")
                    await self._writer.drain()
                    line = await asyncio.wait_for(self._reader.readline(), timeout=self.timeout)
                    if not line:
                        raise ConnectionError("broker closed the connection")
                except (OSError, ConnectionError, asyncio.TimeoutError) as error:
                    await self.close()
                    if method not in RETRYABLE_READS:
                        self._uncertain(method)
                        raise BrokerUnavailable(f"broker delivery uncertain for {method}; not replayed: {error}") from error
                    if attempt == 2:
                        raise BrokerUnavailable(str(error)) from error
                    continue
                except ValueError as error:  # a line over the reader's limit: the stream is out of step now
                    self._uncertain(method)
                    await self.close()
                    raise BrokerUnavailable(f"broker answer too long: {error}") from error
                except asyncio.CancelledError:  # an answer may still come: never read it as the next call's
                    self._uncertain(method)
                    await self.close()
                    raise
                try:
                    response = response_from(line, request_id)
                except ValueError as error:
                    self._uncertain(method)
                    await self.close()
                    raise BrokerUnavailable(str(error)) from error
                if not response.get("ok"):
                    raise BrokerError(str(response.get("error", "unknown broker error"))[:500])
                return response.get("result")

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = self._writer = None


def peer_uid(writer: asyncio.StreamWriter) -> int:
    """Who created the listening socket: root for a systemd-made one, else the broker's own user."""
    return peercred.peer_uid(writer.get_extra_info("socket"))


def uid_of(account: str) -> int | None:
    try:
        return pwd.getpwnam(account).pw_uid
    except KeyError:
        return None


class Brokers:
    """One broker per agent account (acc-broker@<account>), each on its own socket in `socket_dir`.

    Without a socket_dir (tests, single-user installs) every account shares `default_socket`."""

    def __init__(self, default_socket: Path, socket_dir: Path | None, default_account: str, services_account: str,
                 hub=None, authority=None):
        self.default_socket, self.socket_dir = default_socket, socket_dir
        self.default_account, self.services_account = default_account, services_account
        self.authority = authority
        self.hub = hub  # connected nodes (multi-host); None: this machine only
        self._clients: dict[Path, BrokerClient] = {}
        self._remote: dict[tuple[str, str], tuple[object, BrokerClient]] = {}

    def socket_for(self, account: str | None = None) -> Path:
        account = account or self.default_account
        if self.socket_dir is None:
            return self.default_socket
        if not ACCOUNT.fullmatch(account):
            raise BrokerUnavailable(f"bad account name {account!r}")
        return self.socket_dir / f"{account}.sock"

    def get(self, account: str | None = None, host: str | None = None) -> BrokerClient:
        self._check_host(host)
        account = account or self.default_account
        if host and host != "local":
            return self._remote_client(account, host)
        path = self.socket_for(account)
        if path not in self._clients:
            trusted = None
            if self.socket_dir is not None:  # a systemd-made socket (root), or one its own broker bound
                trusted = frozenset(u for u in (0, uid_of(account)) if u is not None)
            self._clients[path] = BrokerClient(path, trusted, authority=self.authority)
        return self._clients[path]

    def _remote_client(self, account: str, host: str) -> BrokerClient:
        """A client whose connection is a channel over the node's link; renewed when the node reconnects."""
        if self.hub is None:
            raise MachineOffline(f"Machine offline: {host}. It checks in when it returns.")
        try:
            link = self.hub.get(host)
        except ConnectionError:
            raise MachineOffline(f"Machine offline: {host}. It checks in when it returns.") from None
        cached = self._remote.get((host, account))
        if cached is None or cached[0] is not link:
            client = BrokerClient(Path(f"{host}:{account}"), None, opener=lambda: link.open(account), authority=self.authority)
            cached = self._remote[(host, account)] = (link, client)
        return cached[1]

    def fresh(self, account: str, host: str | None = None, timeout: float = 60.0) -> BrokerClient:
        """A client on its own new connection (close it when done), for long work such as a backup push: the shared
        client's calls (reconcile, Changes) don't wait behind it."""
        self._check_host(host)
        account = account or self.default_account
        if host and host != "local":
            if self.hub is None:
                raise MachineOffline(f"Machine offline: {host}. It checks in when it returns.")
            try:
                link = self.hub.get(host)
            except ConnectionError:
                raise MachineOffline(f"Machine offline: {host}. It checks in when it returns.") from None
            return BrokerClient(Path(f"{host}:{account}"), None, opener=lambda: link.open(account), timeout=timeout, authority=self.authority)
        shared = self.get(account)
        return BrokerClient(shared.socket_path, shared.trusted_uids, timeout=timeout, authority=self.authority)

    def _check_host(self, host):
        if self.authority:
            self.authority.check()
            if not host or host == 'local':
                from .ha import Fenced
                raise Fenced('HA requires a stable execution node; local broker dispatch is disabled')

    async def open_stream(self, account: str, host: str | None = None):
        """A fresh connection to an account's broker (terminal attach): local socket, or a channel on a node."""
        self._check_host(host)
        if host and host != "local":
            if self.hub is None:
                raise MachineOffline(f"Machine offline: {host}. It checks in when it returns.")
            try:
                return await self.hub.get(host).open(account)
            except ConnectionError:
                raise MachineOffline(f"Machine offline: {host}. It checks in when it returns.") from None
        return await asyncio.open_unix_connection(str(self.socket_for(account)))

    async def call(self, method: str, account: str | None = None, host: str | None = None, **params):
        return await self.get(account, host).call(method, **params)

    def accounts(self) -> list[str]:
        """Agent accounts with a broker socket (the services account runs no agents)."""
        if self.authority:
            return []  # execution accounts belong to enrolled nodes in HA mode
        if self.socket_dir is None:
            return [self.default_account]
        found = sorted(p.stem for p in self.socket_dir.glob("*.sock") if ACCOUNT.fullmatch(p.stem))
        return [a for a in found if a != self.services_account or a == self.default_account] or [self.default_account]

    async def close(self) -> None:
        if self.hub is not None:
            await self.hub.close()
        for _, client in list(self._remote.values()):
            await client.close()
        self._remote.clear()
        for client in self._clients.values():
            await client.close()
