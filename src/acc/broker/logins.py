"""Provider login health and re-login flows, run as the agent user.

Health reports dates and plan names only, never tokens. Re-login drives the CLIs' own
commands through pipes (each provider's login_argv, and `gh auth login --web` for GitHub; a pseudo-terminal on stdin
for CLIs that read the code only from one), so nothing is typed into a terminal and no browser is needed on this
machine. URLs and codes pass through to the browser but are never logged.
"""
from __future__ import annotations

import base64
import json
import os
import pty
import re
import secrets
import signal
import subprocess
import termios
import threading
import time
from pathlib import Path

from .. import providers
from . import ops
from .config import BrokerConfig

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
URL = re.compile(r"https://[^\s\x1b]+")
DEVICE_CODE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,5})\b")
FLOW_TTL = 900
GITHUB_DEVICE_URL = "https://github.com/login/device"
GITHUB = "github"  # gh's own login (agents push and open PRs with it); not an agent provider


class LoginError(Exception):
    pass


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def _gh_health(config: BrokerConfig) -> dict:
    github: dict = {"logged_in": False}
    try:  # gh: agents push branches and open PRs with it
        status = subprocess.run([config.providers.get("gh", "gh"), "auth", "status", "--hostname", "github.com"],
                                capture_output=True, text=True, timeout=20)
        text = status.stdout + status.stderr
        account = re.search(r"account (\S+)", text) or re.search(r"Logged in to github\.com as (\S+)", text)
        scopes = re.search(r"Token scopes: (.*)", text)
        github.update(logged_in=status.returncode == 0, login=account.group(1) if account else None,
                      scopes=scopes.group(1).replace("'", "").strip() if scopes else None)
    except (OSError, subprocess.TimeoutExpired) as error:
        github["error"] = type(error).__name__
    return github


def auth_health(config: BrokerConfig) -> dict:
    """Each installed provider's login (plus GitHub's `gh`), as {provider id: {...}}. Providers whose CLI isn't
    installed for this account are left out."""
    home = Path.home()
    health: dict = {}
    for provider_id, path in ops.installed_providers(config).items():
        if path:
            try:
                health[provider_id] = providers.get(provider_id).auth_health(path, home)
            except Exception as error:  # one CLI misbehaving must not hide the others
                health[provider_id] = {"logged_in": False, "error": type(error).__name__}
    health["github"] = _gh_health(config)
    return {**health, "checked_at": time.time()}


def usage(config: BrokerConfig, provider_id: str) -> dict | None:
    """A provider's plan usage as its CLI reports it (None when it has no source)."""
    provider = providers.get(provider_id)
    return provider.usage(config.providers.get(provider_id, provider.executable), Path.home())


def _kill(process: subprocess.Popen) -> None:
    """Kill the whole group: `codex` is a Node wrapper whose native child survives a plain kill."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if process.stdin:
        try:
            process.stdin.close()
        except OSError:
            pass


def _spawn(argv: list[str], tty: bool) -> subprocess.Popen:
    """The login command with its output on a pipe. With `tty`, stdin is a pseudo-terminal (echo off) whose other end
    stands in for process.stdin, so a pasted code is written the same way."""
    options = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True,
                   cwd=str(Path.home()), env={**os.environ, "BROWSER": "true", "NO_COLOR": "1", "NO_OPEN_BROWSER": "1"})
    if not tty:
        return subprocess.Popen(argv, stdin=subprocess.PIPE, **options)
    master, slave = pty.openpty()
    try:
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        process = subprocess.Popen(argv, stdin=slave, **options)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    process.stdin = os.fdopen(master, "w", buffering=1)
    return process


class LoginFlow:
    def __init__(self, provider: str, process: subprocess.Popen, flow_type: str):
        self.provider = provider
        self.flow_type = flow_type  # "paste": a URL, then a code pasted back | "device": a code entered elsewhere |
        #                             "link": a URL to approve in a browser, nothing to type
        self.process = process
        self.started = time.monotonic()
        self.output: list[str] = []
        self.ready = threading.Event()
        self.url: str | None = None
        self.user_code: str | None = None
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for raw in self.process.stdout:
            line = ANSI.sub("", raw)  # Codex colours its output, even with NO_COLOR
            self.output.append(line.rstrip())
            if self.url is None and (match := URL.search(line)):
                self.url = match.group(0).rstrip(".,)")
            if self.flow_type == "device" and self.user_code is None and (code := DEVICE_CODE.search(line)):
                self.user_code = code.group(1)
                if self.provider == GITHUB:  # gh then waits for Enter to open a browser; the page is always this one
                    self.url = GITHUB_DEVICE_URL
                    self.process.stdin.write("\n")
                    self.process.stdin.flush()
            if self.url and (self.flow_type in ("paste", "link") or self.user_code):
                self.ready.set()
        self.ready.set()

    def summary(self) -> dict:
        exit_code = self.process.poll()
        tail = [line for line in self.output[-6:] if "http" not in line and not DEVICE_CODE.search(line)]
        return {"provider": self.provider, "url": self.url, "user_code": self.user_code,
                "done": exit_code is not None, "ok": exit_code == 0, "message": " ".join(tail)[-300:]}


class Logins:
    def __init__(self, config: BrokerConfig):
        self.config = config
        self.flows: dict[str, LoginFlow] = {}
        self.lock = threading.Lock()

    def _reap(self) -> None:
        for flow_id, flow in list(self.flows.items()):
            if time.monotonic() - flow.started > FLOW_TTL:
                _kill(flow.process)
                del self.flows[flow_id]

    def start(self, provider: str) -> dict:
        with self.lock:
            self._reap()
            for flow in self.flows.values():
                _kill(flow.process)  # one login at a time
            self.flows.clear()
            if provider == GITHUB:  # repo, read:org and gist by default; workflow lets agents push CI changes
                argv = [self.config.providers.get("gh", "gh"), "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web",
                        "--scopes", "workflow"]
                flow_type, tty = "device", False
            else:
                try:
                    spec = providers.get(provider)
                    spec.before_login(Path.home())
                    argv, flow_type = spec.login_argv(self.config.providers.get(provider, spec.executable)), spec.login
                    tty = spec.login_tty
                except providers.CompileError as error:
                    raise LoginError(str(error)) from None
            process = _spawn(argv, tty)
            flow = LoginFlow(provider, process, flow_type)
            flow_id = secrets.token_urlsafe(12)
            self.flows[flow_id] = flow
        if not flow.ready.wait(timeout=30) or not flow.url:
            _kill(process)
            raise LoginError("The CLI didn't show a sign-in link within 30 seconds. Try again.")
        return {"flow_id": flow_id, **flow.summary()}

    def submit(self, flow_id: str, code: str) -> dict:
        flow = self._get(flow_id)
        if flow.flow_type != "paste":
            raise LoginError("this login completes on its own once the code is entered on the sign-in page")
        code = code.strip()
        if not code or len(code) > 512 or "\n" in code:
            raise LoginError("That code isn't valid. Copy the whole code from the sign-in page and paste it again.")
        flow.process.stdin.write(code + "\n")
        flow.process.stdin.flush()
        try:
            flow.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pass
        return self.poll(flow_id)

    def poll(self, flow_id: str) -> dict:
        flow = self._get(flow_id)
        result = flow.summary()
        if result["done"]:
            with self.lock:
                self.flows.pop(flow_id, None)
            if result["ok"] and flow.provider == GITHUB:  # git pushes over https use gh's login
                subprocess.run([self.config.providers.get("gh", "gh"), "auth", "setup-git", "--hostname", "github.com"],
                               capture_output=True, timeout=30)
        return result

    def cancel(self, flow_id: str) -> dict:
        flow = self.flows.pop(flow_id, None)
        if flow:
            _kill(flow.process)
        return {"cancelled": flow is not None}

    def _get(self, flow_id: str) -> LoginFlow:
        flow = self.flows.get(flow_id)
        if flow is None:
            raise LoginError("This sign-in has expired. Start it again.")
        return flow
