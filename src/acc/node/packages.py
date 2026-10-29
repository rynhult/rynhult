"""Agent software on this node, asked for by the primary over the link (docs/MULTIHOST.md, "Agent software").

The primary sends {"t": "packages", "id": "pkg-<12 hex>", "action": ..., "package": ...}: a package from the
acc-packages catalog (deploy/packages/acc-packages, the same file the primary's helper runs) and one of its fixed
actions. Never a command, version, URL or path; anything else is refused, and so are more than RATE_LIMIT requests a
minute or MAX_PENDING waiting at once. acc-node answers with {"t": "packages-state", ...} whenever the state changes
and on every connect.

- Linux (and WSL): acc-node writes the request file for this machine's own acc-packages helper
  (/srv/acc/packages/requests/<id>.json, picked up by acc-packages.path) and reports the helper's state.json. The
  helper, running as root, does the work under its own rules; acc-node runs nothing.
- macOS: there is no root helper. acc-node runs the catalog's user-level install itself (npm -g with Homebrew's npm,
  as the signed-in user), only for entries marked mac_ok; the rest are reported as not installable on a Mac.
"""
from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import json
import logging
import os
import platform
import re
import shutil
import time
from collections import deque
from pathlib import Path

from acc.env import environment

from .. import tooling
from .config import write_atomic

log = logging.getLogger("acc.node")

HELPER = Path(__file__).resolve().parents[3] / "deploy/packages/acc-packages"  # in the installed copy of Rynhult
HELPER_ROOT = Path("/srv/acc/packages")
REQUEST_ID = re.compile(r"pkg-[0-9a-f]{12}")
ACTIONS = ("install", "update", "remove", "check")  # "auto" stays on the primary's own machine
FIELDS = {"t", "id", "action", "package"}
MAX_MESSAGE = 512  # a packages frame is far smaller; longer ones aren't parsed
MAX_STATE_FILE = 256 * 1024
MAX_PENDING = 8
RATE_LIMIT = 10  # requests per RATE_WINDOW seconds
RATE_WINDOW = 60.0
MAC_NPM = (Path("/opt/homebrew/bin/npm"), Path("/usr/local/bin/npm"))  # Homebrew's, never whatever PATH finds
MAC_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
VERSION = re.compile(r"(\d+\.\d+(?:\.\d+)?)")
NPM_TIMEOUT = 900


class Refused(Exception):
    pass


def load_catalog(path: Path = HELPER) -> dict[str, dict]:
    """The helper's CATALOG (its file only defines things when imported; main() runs only as a script)."""
    loader = importlib.machinery.SourceFileLoader("acc_packages_catalog", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module.CATALOG


def parse(message: object, catalog: dict[str, dict]) -> tuple[str, str, str]:
    """(id, action, package) from a packages frame, or Refused: exactly these keys, each a string of the right form."""
    if not isinstance(message, dict) or set(message) != FIELDS or message.get("t") != "packages":
        raise Refused("This machine's Rynhult didn't understand the request. Update Rynhult on that machine.")
    request_id, action, package = message["id"], message["action"], message["package"]
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise Refused("This machine's Rynhult didn't understand the request. Update Rynhult on that machine.")
    if not isinstance(action, str) or action not in ACTIONS:
        raise Refused("This machine's Rynhult didn't understand the request. Update Rynhult on that machine.")
    if not isinstance(package, str) or package not in catalog:
        raise Refused("This machine's Rynhult doesn't know that tool. Update Rynhult on that machine.")
    return request_id, action, package


class RateLimit:
    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.times: deque[float] = deque()

    def allow(self) -> bool:
        now = self.clock()
        while self.times and now - self.times[0] > self.window:
            self.times.popleft()
        if len(self.times) >= self.limit:
            return False
        self.times.append(now)
        return True


class Packages:
    """What both kinds share: the catalog, the limits, and the last refusal (reported with the state)."""

    kind = ""

    def __init__(self, catalog: dict[str, dict]):
        self.catalog = catalog
        self.rate = RateLimit()
        self.refused: dict | None = None

    def handle(self, message: object) -> None:
        """One packages frame: queue it, or remember why not (the next state says so)."""
        request_id = message.get("id") if isinstance(message, dict) and isinstance(message.get("id"), str) else ""
        try:
            if not self.rate.allow():
                raise Refused("Too many requests. Wait a minute and try again.")
            self.request(*parse(message, self.catalog))
        except Refused as error:
            log.warning("refused a packages request: %s", error)
            self.refused = {"id": request_id if REQUEST_ID.fullmatch(request_id) else "", "detail": str(error),
                            "at": time.time()}

    def request(self, request_id: str, action: str, package: str) -> None:
        raise NotImplementedError

    def state(self) -> dict:
        raise NotImplementedError

    def message(self) -> dict:
        return {"t": "packages-state", "kind": self.kind, **self.state(),
                **({"refused": self.refused} if self.refused else {})}

    async def tool(self, message: dict, timeout: float = 900) -> dict:
        raise ValueError("This machine can't install system tools from Rynhult.")


class HelperPackages(Packages):
    """Linux: requests as files for this machine's acc-packages helper (root), its state.json read back."""

    kind = "helper"

    def __init__(self, catalog: dict[str, dict], root: Path = HELPER_ROOT):
        super().__init__(catalog)
        self.root = root

    @property
    def requests(self) -> Path:
        return self.root / "requests"

    def installed(self) -> bool:
        return self.requests.is_dir() and os.access(self.requests, os.W_OK)

    def pending(self) -> list[dict]:
        out = []
        try:
            paths = sorted(self.requests.glob("pkg-*.json"))[:64]
        except OSError:
            return []
        for path in paths:
            if not REQUEST_ID.fullmatch(path.stem):
                continue
            try:
                request = json.loads(path.read_text()[:1024])
            except (OSError, ValueError):
                continue
            if isinstance(request, dict) and request.get("package") in self.catalog and request.get("action") in ACTIONS:
                out.append({"id": path.stem, "package": request["package"], "action": request["action"]})
        return out

    def request(self, request_id: str, action: str, package: str) -> None:
        from ..backup_targets import BINDING
        if BINDING.exists() or BINDING.is_symlink():
            raise Refused("This machine is a backup target, so Rynhult doesn't change its software. An owner updates it on that machine after review.")
        if not self.installed():
            raise Refused("The acc-packages helper isn't installed on this machine. Update Rynhult there (deploy/node/install.sh).")
        pending = self.pending()
        if len(pending) >= MAX_PENDING:
            raise Refused("Too many requests are waiting. Try again when they're done.")
        if any(p["package"] == package for p in pending):
            raise Refused(f"{package} already has a request waiting. Wait for it to finish.")
        target = self.requests / f"{request_id}.json"
        if target.exists():
            raise Refused("That request was already sent.")
        # Written under a name the helper's path unit ignores, then renamed: it never sees half a request.
        write_atomic(target, json.dumps({"id": request_id, "package": package, "action": action}).encode(), 0o640)

    def state(self) -> dict:
        try:
            with (self.root / "state.json").open("rb") as file:
                data = json.loads(file.read(MAX_STATE_FILE))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        packages = data.get("packages") if isinstance(data.get("packages"), dict) else {}
        return {"helper": self.installed(), "pending": self.pending(),
                "packages": {name: packages.get(name) or {"label": spec["label"], "method": spec["method"],
                                                          "note": spec.get("note", "")}
                             for name, spec in self.catalog.items()},
                "daily_at": data.get("daily_at"), "updated_at": data.get("updated_at")}

    async def tool(self, message: dict, timeout: float = 900) -> dict:
        from ..backup_targets import BINDING
        if BINDING.exists() or BINDING.is_symlink():
            raise ValueError("This machine is a backup target, so Rynhult doesn't change its software. An owner updates it on that machine after review.")
        if not isinstance(message, dict) or message.get("t") != "tool":
            raise ValueError("bad tool frame")
        body = tooling.helper_request({k: v for k, v in message.items() if k != "t"})
        if not self.rate.allow() or len(list(self.requests.glob("*.json"))) >= MAX_PENDING:
            raise ValueError("Too many tool requests are waiting. Try again when they're done.")
        if not self.installed():
            raise ValueError("The acc-packages helper isn't installed on this machine. Update Rynhult there (deploy/node/install.sh).")
        path = self.requests / f"{body['id']}.json"
        result_path = self.root / "results" / path.name
        if path.exists() or result_path.exists():
            raise ValueError("tool request id already used")
        write_atomic(path, json.dumps(body).encode(), 0o640)
        for _ in range(max(1, int(timeout / .1))):
            if result_path.exists():
                result = json.loads(result_path.read_text()[:4096])
                if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                    raise ValueError("bad helper result")
                return {k: result[k] for k in ("ok", "version", "detail") if k in result}
            await asyncio.sleep(.1)
        raise TimeoutError("The acc-packages helper on this machine didn't answer. Check the machine before trying again.")


class MacPackages(Packages):
    """macOS: the catalog's mac_ok entries installed by acc-node itself, as the signed-in user, with Homebrew's npm.
    One request runs at a time; the state lives in the node's state dir."""

    kind = "mac"

    def __init__(self, catalog: dict[str, dict], state_dir: Path, npm: Path | None = None, run=None):
        super().__init__(catalog)
        self.path = state_dir / "packages.json"
        self.npm = npm or next((p for p in MAC_NPM if p.exists()), None)
        self.run = run or self._run
        self.queue: deque[tuple[str, str, str]] = deque()
        self.worker: asyncio.Task | None = None
        try:
            saved = json.loads(self.path.read_text())
        except (OSError, ValueError):
            saved = {}
        self.data: dict = saved if isinstance(saved, dict) and isinstance(saved.get("packages"), dict) else {"packages": {}}
        for name, spec in catalog.items():
            entry = self.data["packages"].setdefault(name, {})
            entry.pop("running", None)
            entry.update(label=spec["label"], method=spec["method"], mac_ok=bool(spec.get("mac_ok")),
                         note=self.note(spec))

    @staticmethod
    def note(spec: dict) -> str:
        if spec.get("mac_ok"):
            return f"{spec.get('note', '')} Installed for you with Homebrew's npm (Node.js from Homebrew)."
        return f"Rynhult can't install {spec['label']} on a Mac; install it there by hand (Providers shows the steps), then check."

    async def _run(self, argv: list[str], timeout: float = NPM_TIMEOUT) -> tuple[int, str]:
        env = {"PATH": f"{Path.home() / '.local/bin'}:{MAC_PATH}", "HOME": environment.get("HOME", "/"),
               "LANG": "en_US.UTF-8"}
        process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                       stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.STDOUT, env=env)
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return 124, "timed out"
        return process.returncode or 0, out.decode(errors="replace")

    def request(self, request_id: str, action: str, package: str) -> None:
        spec = self.catalog[package]
        manual = not spec.get("mac_ok") or spec["method"] != "npm"
        # "check" only reads the installed version, so it works for a CLI installed by hand too (after its steps)
        if manual and action != "check":
            raise Refused(f"{spec['label']} can't be installed on a Mac from Rynhult. Install it on the Mac yourself; Providers shows the steps.")
        if self.npm is None and not manual:
            raise Refused("npm isn't installed on this Mac. Install it with brew install node, then try again.")
        if len(self.queue) >= MAX_PENDING:
            raise Refused("Too many requests are waiting. Try again when they're done.")
        if any(p == package for _, _, p in self.queue) or self.data["packages"][package].get("running"):
            raise Refused(f"{package} already has a request waiting. Wait for it to finish.")
        self.queue.append((request_id, action, package))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.get_running_loop().create_task(self._work())

    async def _work(self) -> None:
        while self.queue:
            request_id, action, package = self.queue[0]
            entry, spec = self.data["packages"][package], self.catalog[package]
            entry["running"] = action
            try:
                ok, detail = await self._act(action, spec)
            except OSError as error:
                ok, detail = False, str(error)
            entry.pop("running", None)
            entry["last"] = {"action": action, "ok": ok, "detail": detail[-500:], "at": time.time(), "id": request_id}
            self.queue.popleft()
            self.save()

    async def _act(self, action: str, spec: dict) -> tuple[bool, str]:
        npm, package = str(self.npm), spec.get("package", "")
        if action in ("install", "update"):
            code, out = await self.run([npm, "install", "-g", "--ignore-scripts", "--no-audit", "--no-fund",
                                        f"{package}@{spec['pin']}"])
        elif action == "remove":
            code, out = await self.run([npm, "uninstall", "-g", package])
        else:
            code, out = 0, ""
        if code:
            return False, f"npm {action} failed: {out.strip()[-400:]}"
        await self.refresh(spec, latest=action != "remove")
        entry = self.data["packages"][next(k for k, v in self.catalog.items() if v is spec)]
        if action == "remove":
            return True, f"{spec['label']} removed"
        if action == "check":
            return True, f"{spec['label']}: installed {entry.get('installed') or 'no'}, latest {entry.get('latest') or '?'}"
        return True, f"{spec['label']} {entry.get('installed') or '?'} installed"

    async def refresh(self, spec: dict, latest: bool) -> None:
        name = next(k for k, v in self.catalog.items() if v is spec)
        entry = self.data["packages"][name]
        if spec["method"] != "npm":  # installed by hand (or not at all): its own --version, if it's there
            found = shutil.which(spec["bin"], path=f"{Path.home() / '.local/bin'}:{MAC_PATH}")
            code, out = await self.run([found, "--version"], 30) if found else (1, "")
            match = VERSION.search(out) if code == 0 else None
            entry["installed"] = (match.group(1) if match else "installed") if found else None
        elif self.npm is not None:
            code, out = await self.run([str(self.npm), "ls", "-g", "--json", "--depth=0"], 120)
            try:
                deps = json.loads(out or "{}").get("dependencies") or {}
            except (ValueError, AttributeError):
                deps = {}
            info = deps.get(spec["package"]) if spec["method"] == "npm" else None
            entry["installed"] = info.get("version") if isinstance(info, dict) else None
            if latest and spec["method"] == "npm":
                code, out = await self.run([str(self.npm), "view", spec["package"], "version"], 120)
                upstream = (out.strip()[:40] or None) if code == 0 else None
                entry["upstream_latest"] = upstream
                entry["latest"] = spec["pin"]
                entry["channel"] = "pinned"
                entry["note"] = self.note(spec) + f" Upstream latest: {upstream or 'not checked'}; changes require Rynhult qualification."
                entry["checked_at"] = time.time()

    async def refresh_all(self) -> None:
        """Installed versions (once a run, on the first connect): what the Mac has now."""
        for spec in self.catalog.values():
            try:
                await self.refresh(spec, latest=False)
            except OSError as error:
                log.warning("cannot read the installed version of %s: %s", spec["label"], error)
        self.save()

    def save(self) -> None:
        self.data["updated_at"] = time.time()
        try:
            write_atomic(self.path, json.dumps(self.data).encode(), 0o600)
        except OSError as error:
            log.warning("cannot save %s: %s", self.path, error)

    def state(self) -> dict:
        return {"helper": self.npm is not None, "packages": self.data["packages"],
                "pending": [{"id": i, "package": p, "action": a} for i, a, p in self.queue],
                "updated_at": self.data.get("updated_at")}


def backend(state_dir: Path | None) -> Packages | None:
    """This machine's kind, or None when the catalog can't be read (the link works without it)."""
    try:
        catalog = load_catalog()
    except (OSError, SyntaxError, AttributeError) as error:
        log.warning("agent software can't be managed from the primary: %s", error)
        return None
    if platform.system() == "Darwin":
        return MacPackages(catalog, state_dir or Path.home())
    return HelperPackages(catalog, Path(environment.get("ACC_PACKAGES_ROOT") or HELPER_ROOT))
