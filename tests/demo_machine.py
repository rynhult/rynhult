"""A connected demo machine for the e2e server and the demo instance (never production): enrolled in the database,
its link relays every broker connection to this ACC's own test broker, and it plays acc-node's part for agent
software (a packages frame is "carried out" after a moment, as a node's helper would)."""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from acc.server.models import Node
from acc.server.node_host import light_from
from acc.server.nodes import NodeLink

MAC_OK = {"codex", "copilot"}  # deploy/packages/acc-packages: the catalog entries marked mac_ok
LABELS = {"claude": "Claude Code", "codex": "Codex", "copilot": "GitHub Copilot CLI", "antigravity": "Google Antigravity CLI",
          "cursor": "Cursor CLI", "opencode": "OpenCode", "ollama": "Ollama"}


def demo_snapshot(name: str, calls: int = 0) -> dict:
    """What an unprivileged acc-node reports about a Linux machine (acc.hostinfo.snapshot), with artificial numbers.
    CPU moves a little with each request, so a refresh is visible."""
    now = time.time()
    return {"collected_at": now, "hostname": name, "kernel": "6.8.0-45-generic", "system": "linux",
            "uptime_seconds": 9 * 86400 + 4 * 3600, "reboot_required": False,
            "disks": [{"mount": "/", "device": "/dev/nvme0n1p2", "total": 512e9, "used": 201e9, "free": 311e9, "percent": 39.3},
                      {"mount": "/srv", "device": "/dev/sda1", "total": 2000e9, "used": 1760e9, "free": 240e9, "percent": 88.0}],
            "gpu": None, "tailscale": {"state": "Running", "ips": ["192.0.2.12"], "online": True},
            "services": {"units": {"acc-node": "active", "acc-packages.path": "active", "tailscaled": "active", "ssh": "active"},
                         "failed": []},
            "smart": None, "containers": None, "accounts": None,
            "live": {"cpu_percent": 23.0 + calls % 5, "cpu_count": 16, "load": [1.42, 1.18, 0.97],
                     "memory": {"total": 64e9, "used": 21.5e9, "percent": 33.6}, "swap_percent": 0.0, "measured_at": now},
            "unavailable": ["smart", "containers", "accounts", "gpu"]}


class DemoLink(NodeLink):
    def __init__(self, node_id: str, name: str, info: dict, accounts: list[str], broker_socket: Path,
                 installed: dict[str, str], kind: str = "helper", delay: float = 1.5):
        from acc.node.preview import PreviewRelay
        async def receive(data):
            self.receive(data)
        self.preview_node = PreviewRelay(receive)
        async def nowhere(data):
            self.preview_node.receive(data)
        info = {**info, 'capabilities': ['preview-v1', 'host-v1']}
        super().__init__(node_id, name, nowhere, info, accounts)
        self.broker_socket, self.delay, self.kind = broker_socket, delay, kind
        self.by_hand: dict[str, str] = {}  # e2e: what someone "installed by hand" on the Mac, found by the next check
        self.state = {"kind": kind, "helper": True, "pending": [], "daily_at": time.time() - 3600,
                      "updated_at": time.time(), "packages": {
                          name: {"label": label, "method": "npm" if name in ("codex", "copilot") else "other",
                                 "installed": installed.get(name), "latest": installed.get(name),
                                 **({"mac_ok": name in MAC_OK} if kind == "mac" else {})}
                          for name, label in LABELS.items()}}
        self.packages = json.loads(json.dumps(self.state))
        self.host_calls = 0
        self.host_state({"t": "host-light", "light": light_from(demo_snapshot(name))})
        self._heartbeat = asyncio.create_task(self._alive())

    async def _alive(self) -> None:  # a real node pings every 20 s
        while not self.closed:
            self.heard()
            await asyncio.sleep(20)

    async def open(self, account: str):
        return await asyncio.open_unix_connection(str(self.broker_socket), limit=16 * 1024 * 1024)

    def queue(self, data) -> None:
        message = json.loads(data) if isinstance(data, str) else None
        if isinstance(message, dict) and message.get("t") == "host":  # acc-node's hostinfo.snapshot(), artificial
            self.host_calls += 1
            self.host_state({"t": "host-state", "id": message["id"], "snapshot": demo_snapshot(self.name, self.host_calls)})
        if isinstance(message, dict) and message.get("t") == "packages":
            asyncio.get_running_loop().create_task(self._carry_out(message))

    def _report(self) -> None:
        self.state["updated_at"] = time.time()
        self.packages = json.loads(json.dumps(self.state))

    async def _carry_out(self, message: dict) -> None:
        name, action = message["package"], message["action"]
        entry = self.state["packages"][name]
        if self.kind == "mac" and name not in MAC_OK and action != "check":  # a Mac checks any CLI's version
            self.state["refused"] = {"id": message["id"], "detail": f"{LABELS[name]} can't be installed on a Mac from ACC",
                                     "at": time.time()}
            return self._report()
        self.state["pending"].append({"id": message["id"], "package": name, "action": action})
        self._report()
        await asyncio.sleep(self.delay)
        self.state["pending"] = [p for p in self.state["pending"] if p["id"] != message["id"]]
        if action in ("install", "update"):
            entry["installed"] = entry["latest"] = entry.get("latest") or "1.0.0"
        elif action == "remove":
            entry["installed"] = None
        elif action == "check" and self.kind == "mac" and name not in MAC_OK and name in self.by_hand:
            entry["installed"] = self.by_hand[name]  # installed by hand since, as the steps say
        entry["last"] = {"action": action, "ok": True, "at": time.time(),
                         "detail": f"{LABELS[name]} removed" if action == "remove" else f"{LABELS[name]} {entry['installed']} installed"}
        self._report()

    def close(self, code: int = 1000) -> None:
        self.preview_node.close()
        self._heartbeat.cancel()
        super().close(code)


def attach(app, name: str, info: dict, accounts: list[str], broker_socket: Path, installed: dict[str, str],
           kind: str = "helper") -> DemoLink:
    """Enroll `name` (as if its acc-node had) and connect it. Call on the server's event loop."""
    public = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    node_id = secrets.token_hex(8)
    with app.state.ctx.db() as s:
        s.add(Node(id=node_id, name=name, status="active", public_key=public, info={**info, "accounts": accounts},
                   created_by="demo"))
        s.commit()
    link = DemoLink(node_id, name, info, accounts, broker_socket, installed, kind)
    app.state.ctx.broker.hub.attach(link)
    return link
