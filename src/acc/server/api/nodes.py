"""Hosts: other machines (nodes) this Rynhult runs sessions on. Owners add and remove them; nodes enroll with a one-time
code and then keep an authenticated WebSocket open (docs/MULTIHOST.md)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography import x509
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field, StrictBool, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import object_session

from ... import __version__
from ...wsl import clean_isolation
from .. import devices, edge, passkeys
from ..agent_setup import BACKUP_TOOLS
from ..db import WRITE_INTENT
from ..machine_presence import connected, default_role, evaluate, planned_downtime, policy_revision, transition
from ..models import AuditLog, Device, Node, Observation, Project, Session, Setting, User
from ..node_host import HOST_CAPABILITY
from ..node_tasks import joined_thread
from ..nodes import (
    CODE_TTL,
    INFO_KEYS,
    NAME,
    LinkError,
    NodeLink,
    code_hash,
    new_code,
    node_info_text,
    node_text,
    now,
    reported_accounts,
    update_result,
    verify,
)
from ..supervisor_common import log
from ..updating import node_update_state
from .context import Context
from .devices import PRIVATE
from .packages import clean_state


class NodeIn(BaseModel):
    name: str = Field(pattern=NAME.pattern)
    role: Literal['always_on', 'workstation'] | None = None


class WslExposure(BaseModel):
    interop_enabled: bool | None
    accounts: dict[str, bool | None] = Field(max_length=20)


class WslIsolationIn(BaseModel):
    dismissed: bool
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    # The exposure the owner saw and accepts: a dismissal never covers a state it was not shown (SR6-7).
    accepted: WslExposure | None = None

    @model_validator(mode='after')
    def names_what_it_accepts(self):
        if self.dismissed and self.accepted is None:
            raise ValueError('A dismissal must name the exposure it accepts')
        return self


class DowntimeWindow(BaseModel):
    days: list[int] = Field(min_length=1, max_length=7)
    start: str = Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    end: str | None = Field(None, pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    until_reconnect: bool = Field(False, strict=True)

    @field_validator('days')
    @classmethod
    def weekdays(cls, value):
        if any(d < 0 or d > 6 for d in value) or len(set(value)) != len(value):
            raise ValueError('Choose distinct weekdays from Monday (0) through Sunday (6)')
        return value

    @model_validator(mode='after')
    def duration(self):
        if self.until_reconnect:
            if 'end' in self.model_fields_set:
                raise ValueError('Choose an end time or until reconnect, not both')
        elif self.end is None:
            raise ValueError('End is required unless until reconnect is selected')
        elif self.start == self.end:
            raise ValueError('Start and end must differ')
        return self


class DowntimePolicy(BaseModel):
    timezone: str = Field(max_length=100)
    windows: list[DowntimeWindow] = Field(default_factory=list, max_length=28)

    @field_validator('timezone')
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Choose a time zone by its full name, such as America/Chicago.')
        return value


class PolicyIn(BaseModel):
    public_git_backup: StrictBool = False
    role: Literal['always_on', 'workstation']
    expected_revision: str | None = Field(None, pattern=r'^[a-f0-9]{64}$')
    downtime_policy: DowntimePolicy | None = None
    downtime_until_reconnect: bool | None = None
    offline_threshold_seconds: int = Field(600, ge=60, le=604800)
    snooze_minutes: int | None = Field(None, ge=0, le=43200)
    idle_sleep_minutes: int | None = Field(None, ge=1, le=10080)


CODE = re.compile(r"[0-9a-f]{6}(-[0-9a-f]{6}){3}")  # nodes.new_code()
NODE_ID = re.compile(r"[0-9a-f]{1,64}")
MAX_SIGNATURE = 128  # base64 of a 64-byte Ed25519 signature is 88
MAX_HANDSHAKES = 32  # sockets waiting for a hello at once; more are refused before accepting
MAX_HANDSHAKES_PER_ADDRESS = 4  # of those from one address (D19: one host can't hold every slot)
HELLO_TIMEOUT = 5.0  # seconds a node has to answer the challenge (a real one answers at once)
KNOWN_CAPABILITIES = ("request-replies-v1", "preview-v1", HOST_CAPABILITY, "backup-v1", "git-backup-v1", "update", "rename-compat-1")  # what a node may say it can do; anything else is dropped
MAX_TEXT = 64 * 1024  # a text frame from a node (its agent software state is the largest); longer ones are dropped


def valid_hello(hello) -> bool:
    """A hello worth a database lookup and a signature check."""
    return (isinstance(hello, dict) and hello.get("t", "hello") == "hello"
            and isinstance(hello.get("node_id"), str) and NODE_ID.fullmatch(hello["node_id"]) is not None
            and isinstance(hello.get("signature"), str) and 0 < len(hello["signature"]) <= MAX_SIGNATURE)


class EnrollIn(BaseModel):
    name: str = Field(pattern=NAME.pattern)
    code: str = Field(min_length=8, max_length=64)
    public_key: str = Field(min_length=40, max_length=64)
    info: dict = Field(default_factory=dict)


def publicly_trusted(url: str) -> bool:
    """Whether this Rynhult's certificate passes ordinary TLS verification (system CAs, hostname): then nodes verify it
    the normal way and nothing goes stale at renewal (a pinned leaf certificate would)."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=5) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname):
                return True
    except (OSError, ssl.SSLError):
        return False


def ca_der(path) -> bytes | None:
    """A private root CA's DER, from a file that holds that one certificate (anything else is no CA: a second one
    would otherwise decode into one garbled blob)."""
    try:
        text = path.read_text()
        if text.count("-----BEGIN CERTIFICATE-----") != 1:
            return None
        der = ssl.PEM_cert_to_DER_cert(text)
        x509.load_der_x509_certificate(der)
        return der
    except (OSError, ValueError, AttributeError):
        return None


def ca_fingerprint(path) -> str | None:
    """SHA-256 of a private root CA's DER (long-lived, unlike the leaf certificates it signs)."""
    der = ca_der(path)
    return hashlib.sha256(der).hexdigest() if der else None


BOOTSTRAP = Path(__file__).resolve().parents[1] / "node_bootstrap.sh"
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")


def bootstrap_script(version: str = __version__) -> str:
    """GET /api/nodes/install.sh: the Linux bootstrap with this Rynhult's version filled in."""
    if not VERSION.fullmatch(version):
        raise ValueError(f"unexpected version {version!r}")
    return BOOTSTRAP.read_text().replace("@VERSION@", version)


# The Linux line, run as the signed-in user, with the one-time code piped in (echo is a shell builtin): it stays on
# stdin through sudo, the bootstrap (--code -) and deploy/acc-install to acc-node enroll, so it is on no command line
# (process list, sudo's log). With a private CA nothing is trusted until the CA the main Rynhult serves has the
# fingerprint written into the command ($2). The CA is rewritten to hold that one certificate (openssl x509 keeps
# the first), so nothing appended to it is trusted too. Only then is the bootstrap fetched, verified against it.
PINNED = ('set -e; t=$(mktemp -d); trap "rm -rf $t" EXIT; '
          'curl -fsSk --proto =https --max-filesize 65536 "$1/api/nodes/ca" -o "$t/served.pem"; '
          'openssl x509 -in "$t/served.pem" -out "$t/ca.pem"; '
          '[ "$(openssl x509 -in "$t/ca.pem" -outform DER | sha256sum | cut -d" " -f1)" = "$2" ] '
          '|| { echo "Rynhult: the CA fingerprint does not match: stopping" >&2; exit 1; }; '
          'curl -fsS --proto =https --cacert "$t/ca.pem" "$1/api/nodes/install.sh" -o "$t/install.sh"; '
          'sudo sh "$t/install.sh" --primary "$1" --ca-fingerprint "$2" --code - --name "$3"')
# With a public certificate: curl's own verification, the same steps otherwise.
SYSTEM = ('set -e; t=$(mktemp -d); trap "rm -rf $t" EXIT; '
          'curl -fsS --proto =https "$1/api/nodes/install.sh" -o "$t/install.sh"; '
          'sudo sh "$t/install.sh" --primary "$1" --trust system --code - --name "$2"')


def linux_command(primary: str, pin: str | None, code: str, name: str) -> str:
    """The Linux line Add machine shows. Every value is checked (https origin, hex, code and name patterns), so none
    needs quoting; the script itself is single-quoted and has no single quote inside."""
    if not primary.startswith("https://") or not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?", primary):
        raise ValueError(f"unexpected primary {primary!r}")
    if not CODE.fullmatch(code) or not NAME.fullmatch(name) or (pin and not re.fullmatch(r"[0-9a-f]{64}", pin)):
        raise ValueError("unexpected enroll values")
    if not pin:
        return f"echo {code} | sh -c '{SYSTEM}' - {primary} {name}"
    return f"echo {code} | sh -c '{PINNED}' - {primary} {pin} {name}"


ORIGIN = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?")


def source_command(system: Literal["wsl", "mac"], primary: str, ca: bytes | None, code: str | None,
                   name: str | None) -> str:
    """Add machine before an installer exists (pre-v1): download this main's source bundle, run the node installer from
    it, then enroll. "wsl" runs as root in Ubuntu (sudo, so the agent account is <you>-agent); "mac" as the signed-in
    user. Without a code and name ("mac" only) it is the Mac's update: the installer again, keeping the enrollment.
    A private CA travels inside the command (base64 DER) and is checked against its fingerprint before curl trusts
    it, so nothing is fetched unverified (no curl -k). Every value is checked, so the script has no quote of either
    kind inside and pastes intact. The code is piped in (echo is a shell builtin) and handed to acc-node enroll on
    stdin, so it is on no command line: not in the process list, nor in sudo's log."""
    update = code is None and name is None
    if not ORIGIN.fullmatch(primary) or (update and system != "mac") or (
            not update and not (CODE.fullmatch(code or "") and NAME.fullmatch(name or ""))):
        raise ValueError("unexpected enroll values")
    if ca is not None and not 64 <= len(ca) <= 8192:
        raise ValueError("unexpected CA certificate")
    sha = "sha256sum" if system == "wsl" else "shasum -a 256"
    folder = "rynhult-update" if update else "rynhult-add"
    steps = ["set -eu"]
    if not update:  # the code from the pipe, then the terminal back for the installer's questions (when there is one)
        steps += ["read code", "if (: </dev/tty) 2>/dev/null; then exec </dev/tty; fi"]
    steps += [f"work=$(mktemp -d /tmp/{folder}.XXXXXX)", "cleanup() { rm -rf -- $work; }",
             "trap cleanup EXIT"]
    if ca is not None:
        pin = hashlib.sha256(ca).hexdigest()
        steps += [f"printf %s {base64.b64encode(ca).decode()} | openssl base64 -d -A > $work/ca.der",
                  "openssl x509 -inform DER -in $work/ca.der -out $work/ca.pem",
                  f"test x$(openssl x509 -in $work/ca.pem -outform DER | {sha} | cut -c1-64) = x{pin} "
                  "|| { echo Rynhult: the CA in this command does not match its fingerprint, stopping >&2; exit 1; }"]
        trust, cacert = f"--ca-fingerprint {pin}", "--cacert $work/ca.pem "
    else:
        trust, cacert = "--trust system", ""
    steps += [f"curl -fsS --proto =https {cacert}--max-filesize 268435456 {primary}/api/nodes/bundle "
              "-o $work/bundle.tar.gz",
              "mkdir $work/source",
              "tar -xzf $work/bundle.tar.gz -C $work/source --strip-components=1 --no-same-owner --no-same-permissions"]
    enroll = f"enroll --primary {primary} {trust} --code - --name {name}"
    piped = "" if update else f"echo {code} | "
    if system == "wsl":
        # This command may run through wsl.exe in PowerShell, where console prompts/tty relaying are unreliable.
        # Keep isolation unchanged explicitly; opt-in remains available in a direct Ubuntu terminal afterwards.
        steps += ["echo WSL isolation stays unchanged: re-run the installer with --wsl-isolate on a dedicated node",
                  "ACC_INSTALL_ENROLLS=1 bash $work/source/deploy/node/install.sh --install-prereqs --no-wsl-isolate",
                  f"echo $code | /usr/local/bin/acc-node {enroll}"]
        return piped + "sudo sh -c '" + "; ".join(steps) + "'"
    steps += ["bash $work/source/deploy/node/macos/install.sh"]
    if not update:
        # cd (no argument) goes home without splitting $HOME, which can't be quoted here
        steps += ["cd", f"echo $code | ./Library/Application\\ Support/ACC/bin/acc-node {enroll}"]
    return piped + "sh -c '" + "; ".join(steps) + "'"


# Optional, after a from-source WSL install: what Rynhult Setup registers (Register-AccKeepAliveTask in
# deploy/windows/AccWindows.psm1), as a PowerShell block the owner runs as administrator, signed in as the PC's user.
# A per-user task at logon keeps Ubuntu-24.04 running (WSL stops when nothing runs in it). Fixed text: no values.
WSL_KEEPALIVE = "\n".join([
    "$user = \"$env:USERDOMAIN\\$env:USERNAME\"",
    "$wsl = Join-Path $env:SystemRoot 'System32\\wsl.exe'",
    "$console = Join-Path $env:SystemRoot 'System32\\conhost.exe'",
    "$action = New-ScheduledTaskAction -Execute $console -Argument \"--headless `\"$wsl`\" -d Ubuntu-24.04 --exec /bin/sleep infinity\"",
    "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user",
    "$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries "
    "-DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 999 "
    "-RestartInterval (New-TimeSpan -Minutes 1)",
    "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited",
    "Register-ScheduledTask -TaskName 'Rynhult node (WSL)' -Action $action -Trigger $trigger -Settings $settings "
    "-Principal $principal -Description 'Keeps WSL (and Rynhult inside it) running while you are signed in.' -Force "
    "| Out-Null",
    "Start-ScheduledTask -TaskName 'Rynhult node (WSL)'",
])


def prerelease(version: str | None = None) -> bool:
    return "-" in (version or __version__).split("+", 1)[0]


def release_links(source_url: str, version: str | None = None) -> dict:
    """Where the Windows and Mac installers for this version are (GitHub releases, built in CI). Release candidates
    (a hyphenated version) are handed to testers privately, never as public release assets, so they get none."""
    version = version or __version__
    base = source_url.rstrip("/")
    if (not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", base)
            or not VERSION.fullmatch(version) or not re.match(r"[1-9][0-9]*\.", version) or prerelease(version)):
        return {}
    tag = f"{base}/releases/download/v{version}"
    return {"release": f"{base}/releases/tag/v{version}", "windows": f"{tag}/Rynhult-Setup-{version}.exe",
            "mac": f"{tag}/Rynhult-{version}.pkg"}


def update_recovery(info: dict, settings, installer: str | None) -> dict:
    """Recovery uses public source, never an enrollment code or a node's private identity."""
    if info.get("os") == "darwin" and installer:
        return {"detail": "Download and run the Mac installer. Keep the existing enrollment."}
    if info.get("os") not in ("linux", "darwin"):
        return {"detail": "Check this machine's operating system before updating. See the recovery guide."}
    primary = next((origin.rstrip("/") for origin in settings.allowed_origins if origin.startswith("https://")), "")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?", primary):
        return {"detail": "An administrator must configure this main's HTTPS origin before a recovery command can be shown."}
    if info.get("os") == "darwin":
        # No installer for this version: the Mac installer again from this main's source, as the signed-in user.
        ca = ca_der(settings.node_ca_file) if settings.node_ca_file else None
        if settings.node_ca_file and ca is None:
            return {"detail": "This main's private CA can't be read, so no update command can be shown. See the recovery guide."}
        return {"command": source_command("mac", primary, ca, None, None),
                "detail": "This build has no Mac installer. Run in Terminal on this Mac as the signed-in user (not with "
                          "sudo). Keeps the existing enrollment; no new code is needed."}
    # A private CA travels inside the command and is checked against its fingerprint, as in the Mac command: root never
    # trusts /var/lib/acc-node/ca.pem, which the acc-node account can replace (final RC security audit, F-2).
    pin = ""
    if settings.node_ca_file:
        ca = ca_der(settings.node_ca_file)
        if ca is None or not 64 <= len(ca) <= 8192:
            return {"detail": "This main's private CA can't be read, so no update command can be shown. See the recovery guide."}
        fingerprint = hashlib.sha256(ca).hexdigest()
        pin = (f"printf %s {base64.b64encode(ca).decode()} | openssl base64 -d -A > $work/ca.der; "
               "openssl x509 -inform DER -in $work/ca.der -out $work/ca.pem; "
               f"test x$(openssl x509 -in $work/ca.pem -outform DER | sha256sum | cut -c1-64) = x{fingerprint} "
               "|| { echo Rynhult: the CA in this command does not match its fingerprint, stopping >&2; exit 1; }; ")
    cacert = "--cacert $work/ca.pem " if pin else ""
    # No nested quotes: PowerShell must pass this script intact to WSL, including on Windows PowerShell 5. WSL runs it
    # with --exec: without it, root's login shell gets the line first and expands $(mktemp) and $work too early.
    system = describe_system(info)
    isolation = " --no-wsl-isolate" if system["kind"] == "wsl" else ""
    script = (
        "set -eu; work=$(mktemp -d /tmp/rynhult-update.XXXXXX); "
        "cleanup() { rm -rf -- $work; }; trap cleanup EXIT; " + pin +
        f"curl -fsS --proto =https {cacert}--max-filesize 268435456 {primary}/api/nodes/bundle -o $work/bundle.tar.gz; "
        "mkdir $work/source; "
        "tar -xzf $work/bundle.tar.gz -C $work/source --strip-components=1 --no-same-owner --no-same-permissions; "
        f"bash $work/source/deploy/node/install.sh --update{isolation}"
    )
    detail = "Keeps the existing enrollment, agent accounts and workspace folders. "
    if settings.node_ca_file:
        detail += "Carries this main's private CA and checks its fingerprint before downloading. "
    if system["kind"] == "wsl":
        distro = info.get("wsl_distro")
        known = isinstance(distro, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", distro))
        command = f"wsl -d '{distro if known else '<distro>'}' -u root --exec sh -c '{script}'"
        detail += "Run in PowerShell on this Windows PC. "
        if not known:
            detail += "Replace <distro> with the distribution running this node (find it with wsl --list --quiet)."
    else:
        command = f"sudo sh -c '{script}'"
        detail += "Run in a Linux terminal on this machine."
        if system["kind"] == "unknown":
            detail += " If this is a Windows PC, open its existing WSL distribution and run it there, not in PowerShell."
    return {"command": command, "detail": detail,
            **({"root_command": f"sh -c '{script}'"} if system["kind"] == "wsl" else {})}



def exposure(state: dict) -> dict:
    """What an owner's WSL dismissal accepts: effective interop and each agent account's drive access."""
    return {"interop_enabled": state["interop_enabled"], "accounts": dict(state["accounts"])}


def _safe_report(value):
    """JSON permits escaped lone surrogates; SQL text parameters and UTF-8 responses do not.

    Normalize node reports before persistence/use and old reports before projection.
    Valid Unicode (including emoji) is unchanged; this does not attest their contents.
    """
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_safe_report(item) for item in value]
    if isinstance(value, dict):
        return {_safe_report(key): _safe_report(item) for key, item in value.items()}
    return value


def describe_system(info: dict) -> dict:
    """How a machine's system reads on the dashboard: a Mac, a plain Linux, or Linux inside WSL with its Windows host
    ("Linux (WSL2) · Windows 11 (build 22631)"). A Linux node from before it reported WSL is "unknown"."""
    os_name, wsl, windows = info.get("os"), str(info.get("wsl", "")), info.get("windows")
    if os_name == "darwin":
        return {"kind": "macos", "label": "macOS"}
    if os_name == "linux" and wsl in ("1", "2"):
        return {"kind": "wsl", "label": f"Linux (WSL{wsl}) · {windows or 'Windows version unknown'}", "wsl": wsl,
                "windows": windows}
    if info.get("system") == "wsl":
        return {"kind": "wsl", "label": "Linux (WSL) · Windows version unknown"}
    if os_name == "linux" and wsl == "0":
        return {"kind": "linux", "label": "Linux"}
    return {"kind": "unknown", "label": "unknown" if not os_name else f"{os_name.capitalize()} (WSL or not: unknown)"}

def request_host(request) -> str:
    return (request.url.hostname or '').rstrip('.').lower()


def through_edge(request) -> bool:
    return bool(getattr(request.state, 'edge_fingerprint', None))


def on_lan(place: dict, public_hostname: str) -> bool:
    """The private address, as pairing judges it: a private client address, never the public hostname or the edge."""
    try:
        ip = ipaddress.ip_address(place['client'].split('/')[0])
    except ValueError:
        return False
    return (not place['edge'] and any(ip in net for net in PRIVATE if net.version == ip.version)
            and not (public_hostname and place['host'] == public_hostname.rstrip('.').lower()))


def router(ctx: Context) -> APIRouter:
    api = APIRouter()
    db, settings = ctx.db, ctx.settings
    hub = ctx.broker.hub

    def out(node: Node, authority: str | None = None) -> dict:
        from ...backup_targets import node_max_size
        link = hub.links.get(node.name) if hub else None
        accounts = reported_accounts(_safe_report(link.accounts if link else (node.info or {}).get("accounts", [])))
        installer = release_links(settings.source_url).get("mac")
        update = node_update_state(node)
        raw = _safe_report(node.info or {})
        # Shown text is cleaned, also what was stored before SR6-11 (SR7-6). Recovery commands keep judging the raw
        # values: cleaning an unsafe distro name must never make it look safe enough to put in a command.
        info = node_info_text(raw)
        if (update or {}).get("installer_required") or raw.get("os") == "darwin":
            update = {**(update or {}), "recovery": update_recovery(raw, settings, installer)}
        return _safe_report({"id": node.id, "name": node.name, "status": node.status, "info": info,
                "backup_default_max_size_bytes": node_max_size(link),
                "connected": bool(link and not link.closed), "last_seen": node.last_seen, "accounts": accounts,
                "role": node.role, "offline_threshold_seconds": node.offline_threshold_seconds,
                "idle_sleep_minutes": node.idle_sleep_minutes,
                "downtime_policy": node.downtime_policy, "downtime_until_reconnect": node.downtime_until_reconnect,
                "policy_revision": policy_revision(node), "public_git_backup": bool(node.public_git_backup),
                "planned_downtime": planned_downtime(node),
                "sleep": sleep_summary(node),
                "offline_since": node.offline_since, "snoozed_until": node.snoozed_until,
                "last_downtime_seconds": node.last_downtime_seconds,
                "offline_alert": bool(node.status == 'active' and node.role == 'always_on' and node.offline_since
                    and (now() - node.offline_since).total_seconds() >= node.offline_threshold_seconds
                    and not planned_downtime(node)['active']
                    and not (node.snoozed_until and node.snoozed_until > now())),
                "created_at": node.created_at, "created_by": node.created_by,
                "update": update,
                "installer": installer,
                "follow": ctx.updates.follow_enabled(node.name),
                "update_available": (node.info or {}).get("build") != ctx.updates.build,
                "system": describe_system(info),
                "wsl_isolation": isolation_summary(node, accounts),
                # at a glance (Host): its agent CLIs as it last reported them, and each account's cached logins
                "software": {name: {"label": p.get("label"), "installed": p.get("installed")}
                             for name, p in ((link.packages or {}).get("packages") or {}).items()
                             if name not in BACKUP_TOOLS} if link else None,
                "logins": logins(node.name, accounts),
                "machine_identity": edge.summary(object_session(node), node.id, authority) if object_session(node) else None})

    def isolation_summary(node: Node, accounts: list[str]) -> dict | None:
        if describe_system(node.info or {})["kind"] != "wsl":
            return None
        state = clean_isolation((node.info or {}).get("wsl_isolation"), accounts)
        # Its own session, like logins(): reading through the route's session would reopen its write transaction.
        with db() as s:
            stored = s.get(Setting, f"node.wsl_isolation.{node.id}")
            raw = stored.value if stored else None
            saved = dict(raw or {})
        # Bound to the exact exposure accepted: any change (a new account, readable drives, unknown) raises it again.
        accepted = bool(saved.get("dismissed")) and saved.get("accepted") == exposure(state)
        return {**state, "warning": state["interop_enabled"] is True or state["windows_drive_readable"] is True,
                "dismissed": accepted, "dismissal_outdated": bool(saved.get("dismissed")) and not accepted,
                "revision": isolation_revision(node, raw)}

    def isolation_revision(node: Node, saved: dict | None) -> str:
        # Bind an opaque generation to this enrollment, including legacy saved choices.
        return hashlib.sha256(json.dumps([node.id, saved], sort_keys=True).encode()).hexdigest()

    def sleep_summary(node: Node) -> dict | None:
        if node.role != 'workstation':
            return None
        sessions = [s for s in object_session(node).scalars(select(Session).where(
            (Session.desired_state == 'running') | Session.sleep_paused_at.is_not(None))) if s.host == node.name]
        pending = lambda s: bool(s.sleep_paused_at and (s.last_error or '').startswith('stop not confirmed:'))
        running = [s for s in sessions if pending(s) or (s.desired_state == 'running'
                   and s.observed_state in ('starting', 'active', 'idle', 'waiting_input'))]
        summary = lambda s: {'id': s.id, 'name': s.name, **({'stop_pending': True} if pending(s) else {})}
        return {'running_sessions': len(running),
                'idle_sessions': [summary(s) for s in running if s.observed_state == 'idle'],
                'paused_sessions': [summary(s) for s in sessions if s.sleep_paused_at and s.desired_state == 'stopped']}

    def logins(machine: str, accounts: list[str]) -> dict:
        out = {}
        with db() as s:
            for account in accounts:
                health = s.get(Observation, f"auth.health.host.{machine}.{account}")
                if health is None or not isinstance(health.value, dict):
                    continue
                out[account] = {"checked_at": health.value.get("checked_at"),
                                **{k: bool(v.get("logged_in")) for k, v in health.value.items()
                                   if isinstance(v, dict) and "logged_in" in v}}
        return out

    @api.get("/api/nodes")
    def list_nodes(who=Depends(ctx.owner)):
        authority = ctx.edge.authority()  # refresh/audit before the listing's read transaction
        with db() as s:
            result = [out(n, authority) for n in s.scalars(select(Node).where(Node.status != "revoked").order_by(Node.name))]
            s.commit()
            return result

    @api.post("/api/nodes", status_code=201)
    async def add_node(body: NodeIn, who=Depends(ctx.owner)):
        """A pending node and its one-time code; the answer holds the command to run on that machine."""
        if body.name == "local":
            raise HTTPException(422, "The name local belongs to this machine. Choose another name.")
        primary = next((o for o in settings.allowed_origins if o.startswith("https://")), settings.allowed_origins[0])
        pin = ca_fingerprint(settings.node_ca_file) if settings.node_ca_file else None
        trusted = False if pin else await asyncio.to_thread(publicly_trusted, primary)
        code, hashed = new_code()
        with db() as s:
            existing = s.scalar(select(Node).where(Node.name == body.name))
            if existing is not None and existing.status != "revoked":
                raise HTTPException(409, f"A machine named {body.name} already exists. Choose another name.")
            if existing is not None:
                from ..models import GroupMachineAccount
                if s.scalar(select(GroupMachineAccount.id).where(GroupMachineAccount.node_id == existing.id).limit(1)):
                    # Its own-machine bindings stay as revocation history and keep the account reserved by this name.
                    raise HTTPException(409, f"{body.name} was used before for group members' own accounts. Add this machine "
                                             "under a new name.")
                s.delete(existing)
                s.flush()
            node = Node(id=secrets.token_hex(8), name=body.name, status="pending", code_hash=hashed,
                        code_expires=now() + CODE_TTL, info={"role_chosen": body.role is not None}, role=body.role, created_by=who.username)
            s.add(node)
            s.add(AuditLog(actor=who.username, action="node.add", target=body.name))
            s.commit()
            trust = f"--ca-fingerprint {pin}" if pin else "--trust system"
            command = f"rynhult-node enroll --primary {primary} {trust} --code {code} --name {body.name}"
            try:
                linux = linux_command(primary, pin, code, body.name)
            except ValueError:
                linux = None  # an origin with a path or odd characters: the enroll command alone still works
            ca = ca_der(settings.node_ca_file) if pin else None
            source = {}
            for system, key in (("wsl", "windows"), ("mac", "mac")):
                try:
                    source[key] = source_command(system, primary, ca, code, body.name) if (ca or not pin) else None
                except ValueError:
                    source[key] = None
            return {**out(node), "code": code, "expires_in": int(CODE_TTL.total_seconds()), "command": command,
                    "linux": linux, "from_source": {**source, "windows_keepalive": WSL_KEEPALIVE},
                    "installers": release_links(settings.source_url), "version": __version__, "prerelease": prerelease(),
                    "trust": "ca" if pin else "system", "verified": bool(pin) or trusted, "primary": primary}

    def write_policy(name: str, body: PolicyIn, who, session_hash: str | None, place: dict):
        with db() as s:
            node = s.scalar(select(Node).where(Node.name == name, Node.status != 'revoked').with_for_update())
            if node is None:
                raise HTTPException(404, 'That machine was not found. Reload Host → Machines and try again.')
            if 'public_git_backup' in body.model_fields_set and body.expected_revision is None:
                raise HTTPException(409, 'Reopen Machine settings before changing public git backup permission.')
            if body.expected_revision is not None and body.expected_revision != policy_revision(node):
                raise HTTPException(409, 'Machine settings changed. Cancel and reopen Machine settings to review the current policy before saving.')
            if 'public_git_backup' in body.model_fields_set:
                previous = bool(node.public_git_backup)
                if body.public_git_backup and not previous:
                    # Opting in widens what the public address accepts: an owner who just verified their sign-in,
                    # never the API token or a copied cookie. Opting out stays open to any owner (the safe way).
                    # It must also come from the private address, like remote-access Apply (owner decision).
                    node_id = node.id
                    reason, refusal = None, None
                    if not on_lan(place, devices.settings(s)['public_hostname']):
                        reason = 'private_address_required'
                        refusal = HTTPException(403, 'Turn public git backup on from the private LAN address.')
                    else:
                        try:
                            passkeys.recently_verified(s, session_hash, s.get(User, who.user_id) if who.user_id is not None else None)
                        except HTTPException as error:
                            reason, refusal = 'verification_required', error
                    if refusal is not None:
                        s.rollback()
                        s.add(AuditLog(actor=who.username, action='node.public_git_backup_refused', target=name,
                                       detail={'node_id': node_id, 'reason': reason}))
                        s.commit()
                        raise refusal
                node.public_git_backup = body.public_git_backup
                if previous != body.public_git_backup:
                    s.add(AuditLog(actor=who.username, action='node.public_git_backup', target=name,
                                   detail={'node_id': node.id, 'enabled': body.public_git_backup, 'previous': previous}))
            node.role = body.role
            node.info = {**(node.info or {}), "role_chosen": True}
            node.offline_threshold_seconds = body.offline_threshold_seconds
            if 'downtime_policy' in body.model_fields_set:
                node.downtime_policy = ({'windows': [], **body.downtime_policy.model_dump(exclude_unset=True)}
                                        if body.downtime_policy else None)
            if body.downtime_until_reconnect is not None:
                node.downtime_until_reconnect = body.downtime_until_reconnect
            if 'idle_sleep_minutes' in body.model_fields_set:
                node.idle_sleep_minutes = body.idle_sleep_minutes
            if body.snooze_minutes is not None:
                node.snoozed_until = now() + timedelta(minutes=body.snooze_minutes) if body.snooze_minutes else None
            evaluate(s, node)
            s.add(AuditLog(actor=who.username, action='node.policy', target=name,
                           detail=body.model_dump(exclude_unset=True, exclude={'expected_revision'})))
            s.commit()
            return out(node)

    @api.post('/api/nodes/{name}/policy')
    async def set_policy(name: str, body: PolicyIn, request: Request, who=Depends(ctx.owner)):
        place = {'client': ctx.client_key(request), 'host': request_host(request), 'edge': through_edge(request)}
        result = await asyncio.to_thread(write_policy, name, body, who, getattr(request.state, 'session_hash', None), place)
        if 'public_git_backup' in body.model_fields_set and not body.public_git_backup:
            ctx.supervisor.backup_grants.revoke_public_node(result['id'])
            if ctx.edge:
                await ctx.edge.close_node_git(result['id'])
        return result

    @api.put("/api/nodes/{name}/wsl-isolation")
    def wsl_isolation(name: str, body: WslIsolationIn, who=Depends(ctx.owner)):
        with db() as s:
            # SQLite's request write-intent gate and PostgreSQL's row lock both
            # serialize the revision check with the preference write.
            node = s.scalar(select(Node).where(Node.name == name, Node.status == "active").with_for_update())
            if node is None:
                raise HTTPException(404, "That machine was not found or hasn't finished joining. Reload Host → Machines and try again.")
            if describe_system(node.info or {})["kind"] != "wsl":
                raise HTTPException(409, "Only Windows (WSL) machines have a Windows access warning.")
            key = f"node.wsl_isolation.{node.id}"
            stored = s.get(Setting, key)
            if body.expected_revision != isolation_revision(node, stored.value if stored else None):
                raise HTTPException(409, "Windows access warning choice changed. Review the refreshed machine and try again.")
            value = {"dismissed": body.dismissed}
            if body.dismissed:
                link = hub.links.get(node.name) if hub else None
                accounts = reported_accounts(_safe_report(link.accounts if link else (node.info or {}).get("accounts", [])))
                current = exposure(clean_isolation((node.info or {}).get("wsl_isolation"), accounts))
                if body.accepted.model_dump() != current:
                    raise HTTPException(409, "Windows access on this machine changed. Review the current state before accepting it.")
                value["accepted"] = current
            s.merge(Setting(key=key, value={**value, "generation": secrets.token_hex(32)}, updated_by=who.username))
            s.add(AuditLog(actor=who.username, action="node.wsl_isolation", target=name, detail=value))
            s.commit()
            return out(node)

    check_after = [0.0]

    @api.post('/api/nodes/check')
    async def check_machines(who=Depends(ctx.owner)):
        if time.monotonic() < check_after[0]:
            raise HTTPException(429, 'Wait 30 seconds before checking machines again.', headers={'Retry-After': '30'})
        check_after[0] = time.monotonic() + 30
        with db() as s:
            nodes = [(n.name, (n.info or {}).get('accounts') or [], n.last_seen, n.offline_since)
                     for n in s.scalars(select(Node).where(Node.status != 'revoked'))]
            s.add(AuditLog(actor=who.username, action='node.check', target='all'))
            s.commit()

        async def check(name, accounts, last_seen=None, offline_since=None):
            result = {'name': name, 'checked_at': now().isoformat(), 'last_seen': last_seen,
                      'offline_since': offline_since}
            if not connected(ctx.broker, name):
                return {**result, 'state': 'offline'}
            started = time.monotonic()
            client = None
            try:
                client = ctx.broker.fresh(accounts[0] if accounts else settings.default_account, name, timeout=4)
                info = await asyncio.wait_for(client.call('ping', machine_snapshot=True), timeout=5)
                result.update(state='responding', latency_ms=round((time.monotonic() - started) * 1000, 1),
                              info=info.get('machine'), snapshot=info.get('snapshot'), snapshot_available='snapshot' in info)
                if name != 'local' and isinstance(info.get('machine'), dict):
                    with db() as s:
                        node = s.scalar(select(Node).where(Node.name == name))
                        if node:
                            node.info = {**node.info, **info['machine']}
                            node.last_seen = now()
                            s.commit()
            except Exception:
                result['state'] = 'unresponsive' if connected(ctx.broker, name) else 'offline'
            finally:
                if client:
                    await client.close()
            return result

        machines = await asyncio.gather(check('local', [settings.default_account]),
                                        *(check(*values) for values in nodes))
        await asyncio.to_thread(ctx.supervisor.refresh_host)
        # Store only JSON-safe public results; this cache is also shared between Host and Overview.
        from fastapi.encoders import jsonable_encoder
        result = jsonable_encoder({'machines': machines, 'checked_at': now()})
        with db() as s:
            s.add(AuditLog(actor=who.username, action='node.check_result', target='all',
                           detail={'machines': [{k: row[k] for k in ('name', 'state', 'latency_ms') if k in row} for row in result['machines']]}))
            s.merge(Observation(key='machines.check', value=result, source='owner check', observed_at=time.time()))
            s.commit()
        return result

    @api.get('/api/nodes/check')
    def last_check(who=Depends(ctx.owner)):
        with db() as s:
            observation = s.get(Observation, 'machines.check')
            return observation.value if observation else {'machines': []}

    def limited(request: Request) -> None:
        key = ctx.client_key(request)
        if ctx.downloads.blocked(key):
            raise HTTPException(429, "Too many downloads from this address. Wait 10 minutes and try again.")
        ctx.downloads.fail(key)  # every request counts, not only failures

    @api.get("/api/nodes/bundle")
    def bundle(request: Request):
        """This Rynhult's source at its running version (built by deploy/install.sh): what new machines install, and the
        source of this exact version for anyone using it. Public, like a release download."""
        limited(request)
        path = settings.bundle_dir / f"acc-{__version__}.tar.gz"
        if not path.is_file():
            raise HTTPException(404, "This Rynhult has no install bundle for its version yet. Run deploy/install.sh on the main machine to build it.")
        return FileResponse(path, media_type="application/gzip", filename=path.name)

    @api.get("/api/nodes/install.sh")
    def install_script(request: Request):
        """The Linux bootstrap for an additional machine; the enroll values are its arguments."""
        limited(request)
        return PlainTextResponse(bootstrap_script(), media_type="text/x-shellscript")

    @api.get("/api/nodes/ca")
    def node_ca():
        """The private root CA nodes pin (by fingerprint, from the enroll command). Public: it's a certificate."""
        if not settings.node_ca_file:
            raise HTTPException(404, 'This Rynhult uses a publicly trusted certificate, so there is no private certificate to download.')
        # Only ever one certificate, re-encoded: never the file's raw text (install and update review, IU-2).
        der = ca_der(settings.node_ca_file)
        if der is None:
            raise HTTPException(404, "Rynhult can't read a single valid private certificate. Check the certificate on the main machine.")
        return PlainTextResponse(ssl.DER_cert_to_PEM_cert(der), media_type="application/x-pem-file")

    @api.post("/api/nodes/{name}/remove")
    async def remove_node(name: str, who=Depends(ctx.owner)):  # async: the hub lives on the event loop
        with db() as s:
            node = s.scalar(select(Node).where(Node.name == name, Node.status != "revoked"))
            if node is None:
                raise HTTPException(404, "That machine was not found. Reload Host → Machines and try again.")
            used = s.scalar(select(Project.id).where(Project.host == name, Project.archived.is_(False)).limit(1))
            if used is not None:
                raise HTTPException(409, "Projects still use this machine. Archive them first, then remove the machine.")
            node.status, node.public_key, node.code_hash = "revoked", None, None
            identities = edge.revoke_node(s, node.id)
            from .. import group_remote
            from ..models import GroupMachineAccount, Session
            bindings, placements = [], []
            for binding in s.scalars(select(GroupMachineAccount).where(GroupMachineAccount.node_id == node.id)):
                if binding.status != 'revoked':
                    bindings.append(binding.id)
                binding.status = 'revoked'
                for sid in group_remote.revoke_placements(s, binding_id=binding.id, revoked=placements):
                    ctx.supervisor._set_state(s, s.get(Session, sid), 'stopped', 'api')
            evaluate(s, node)
            detail = {**({"machine_identities_revoked": identities} if identities else {}),
                      **({"group_bindings_revoked": bindings, "remote_placements_revoked": placements} if bindings else {})}
            s.add(AuditLog(actor=who.username, action="node.remove", target=name, detail=detail or None))
            s.commit()
        if hub:
            hub.disconnect(name)
            await hub.drain(name)
        await ctx.edge.sweep()  # public streams presenting its identity end now, not at the next sweep
        if identities:
            await reconcile_quietly()
        return {"removed": name}

    async def reconcile_quietly() -> None:
        try:
            await ctx.remote_access.reconcile()
        except HTTPException:
            pass  # durable status remains pending and the worker retries OFF

    async def offer_switch(link, presented) -> str | None:
        """Ask for a successor's CSR, or tell the node it may switch to an issued successor the edge admits."""
        action, fingerprint = await joined_thread(ctx.edge.step, link.node_id, presented)
        if action == "request":
            link.queue(edge.message("machine-csr-request"))
        elif action == "switch" and await joined_thread(edge.ready, ctx, fingerprint):
            link.queue(edge.message("machine-ready", fingerprint=fingerprint))
        return action

    async def machine_followup(link, presented) -> None:
        try:
            if await joined_thread(edge.switched, ctx, link.node_id, presented):
                await reconcile_quietly()
            while await offer_switch(link, presented) == "wait" and not link.closed:
                await asyncio.sleep(edge.ROLLOVER_RETRY)  # this hour's rollover share is used: ask again later
        except Exception:
            log.warning("node %s: machine identity follow-up failed", link.name)

    async def machine_issue(link, csr, presented) -> None:
        try:
            certificate = await joined_thread(edge.issue, ctx, link.node_id, csr, presented)
        except Exception:  # signer unavailable or refused, identity invalid, node removed: no detail to the node
            log.warning("node %s: machine identity not issued", link.name, exc_info=True)
            return
        link.queue(edge.message("machine-certificate", certificate=certificate))
        await reconcile_quietly()  # the policy must admit the successor before the node switches to it
        await offer_switch(link, presented)

    @api.post("/api/nodes/enroll")
    def enroll(body: EnrollIn):
        """Called by `acc-node enroll` on the new machine: spends the code, stores the node's public key."""
        try:
            raw = base64.b64decode(body.public_key, validate=True)
        except ValueError:
            raw = b""
        if len(raw) != 32:
            raise HTTPException(422, "The machine sent a key Rynhult can't use. Update Rynhult on that machine and run the command again.")
        info = {k: node_text(v) for k, v in (body.info or {}).items() if k in INFO_KEYS and k != "wsl_distro"}
        refused = HTTPException(403, "The code or machine name is wrong, or the code expired. Add the machine again in Host → Machines for a new code.")

        def matches(node) -> bool:
            return (node is not None and bool(node.code_hash) and node.code_expires is not None
                    and node.code_expires >= now() and secrets.compare_digest(node.code_hash, code_hash(body.code)))
        if not CODE.fullmatch(body.code.strip().lower()):
            raise refused
        token = WRITE_INTENT.set(False)  # a wrong code never takes the database's write lock
        try:
            with db() as s:
                found = matches(s.scalar(select(Node).where(Node.name == body.name, Node.status == "pending")))
                s.rollback()
        finally:
            WRITE_INTENT.reset(token)
        if not found:
            raise refused
        with db() as s:  # again under the write lock: one enrollment spends the code
            node = s.scalar(select(Node).where(Node.name == body.name, Node.status == "pending"))
            if not matches(node):
                raise refused
            node.status, node.public_key, node.code_hash, node.code_expires = "active", body.public_key, None, None
            node.info = {**info, "role_chosen": (node.info or {}).get("role_chosen", False)}
            if node.role is None:
                node.role = default_role(info)
            s.add(AuditLog(actor=f"node:{body.name}", action="node.enroll", target=body.name, detail=info))
            s.commit()
            return {"node_id": node.id, "name": node.name}

    handshakes = [0]  # sockets between accept and a verified hello
    handshakes_from: dict[str, int] = {}  # the same, by client address (Context.client_key)

    @api.websocket("/api/nodes/connect")
    async def connect(ws: WebSocket):
        """A node's link: challenge, signed hello, then channel frames until either side goes away."""
        if hub is None:
            await ws.close(code=4503)
            return
        address = ctx.client_key(ws)
        if handshakes[0] >= MAX_HANDSHAKES or handshakes_from.get(address, 0) >= MAX_HANDSHAKES_PER_ADDRESS:
            await ws.close(code=1013)  # unauthenticated sockets are cheap to open: only so many at once, and per address
            return
        handshakes[0] += 1
        handshakes_from[address] = handshakes_from.get(address, 0) + 1
        try:
            await ws.accept()
            nonce = secrets.token_bytes(32)
            await ws.send_text(json.dumps({"t": "challenge", "nonce": base64.b64encode(nonce).decode(),
                                           "primary": hub.primary_id}))
            try:
                hello = _safe_report(json.loads(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT)))
            except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, KeyError, RuntimeError):
                hello = None
            if not valid_hello(hello):
                await ws.close(code=4400)
                return
            presented = ws.scope.get("state", {}).get("edge_fingerprint")
            with db() as s:
                node = s.get(Node, hello["node_id"])
                key = node.public_key if node and node.status == "active" else None
                name = node.name if node else None
                if presented and s.scalar(select(Device.node_id).where(Device.fingerprint == presented)) != hello["node_id"]:
                    key = None  # a machine identity is bound to its enrolled node: never another node's link
            if not key or not verify(key, nonce, hub.primary_id, hello["signature"]):
                await ws.close(code=4401)
                return
        finally:
            handshakes[0] -= 1
            handshakes_from[address] -= 1
            if not handshakes_from[address]:
                del handshakes_from[address]
        accounts = reported_accounts(hello.get("accounts"))
        info = hello.get("info") if isinstance(hello.get("info"), dict) else {}
        offered = info.get("capabilities") if isinstance(info.get("capabilities"), list) else []
        capabilities = [c for c in KNOWN_CAPABILITIES if c in offered]
        isolation = clean_isolation(info.get("wsl_isolation"), accounts)
        info = {k: node_text(v) for k, v in info.items() if k in INFO_KEYS}
        if describe_system(info)["kind"] == "wsl":
            info["wsl_isolation"] = isolation
        if capabilities:
            info["capabilities"] = capabilities
        async def send(data: bytes | str) -> None:  # one sender task per link: frames never interleave
            await (ws.send_text(data) if isinstance(data, str) else ws.send_bytes(data))

        async def close_socket(code: int) -> None:
            try:
                await asyncio.wait_for(ws.close(code=code), timeout=1)
            except Exception:  # closed already
                pass
        if hub.closed:
            await ws.close(code=1012)
            return
        link = NodeLink(hello["node_id"], name, send, info, accounts)
        # Replaced, revoked, over a budget, silent: the link closes its WebSocket, which ends the loop below.
        link.on_close = lambda code: link.tasks.start(close_socket(code), "socket-close", cleanup=True)
        machine_tasks: set[asyncio.Task] = set()
        hub.attach(link)
        try:
            token = WRITE_INTENT.set(True)
            try:
                with db() as s:
                    node = s.get(Node, hello["node_id"])
                    if node is None or node.status != "active" or node.public_key != key:
                        link.close(4401)  # revoked (or re-enrolled) while this handshake was in flight
                        return
                    transition(s, node, True, connection_open=True)
                    previous_info = dict(node.info or {})
                    previous_info.pop("capabilities", None)  # a rollback/old reconnect must withdraw stale evidence
                    previous_info.pop("wsl_isolation", None)
                    previous_info.pop("wsl_distro", None)
                    node.last_seen, node.info = now(), {**previous_info, **info, "build": info.get("build"), "accounts": accounts}
                    s.commit()
            finally:
                WRITE_INTENT.reset(token)
            ctx.backup_jobs.wake.set()
            ctx.supervisor._host_backoff.pop(name, None)
            ctx.supervisor._wake.set()
            await ws.send_text(json.dumps({"t": "ready"}))
            ctx.updates.follow(link)
            claimed = hello.get("machine") if isinstance(hello.get("machine"), str) else None
            presented = presented or (claimed if claimed and edge.FINGERPRINT.fullmatch(claimed) else None)
            machine_tasks = {link.tasks.start(machine_followup(link, presented), "identity-followup")}
            last_csr = 0.0
            last_recorded = time.monotonic()
            while not link.closed:
                message = await link.tasks.start(ws.receive(), "receive", report=False)
                if time.monotonic() - last_recorded >= 30 and message['type'] != 'websocket.disconnect':
                    with db() as s:
                        node = s.get(Node, hello['node_id'])
                        if node and node.status == 'active':
                            node.last_seen = now()
                            s.commit()
                    last_recorded = time.monotonic()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    try:
                        link.receive(message["bytes"])
                    except LinkError as error:
                        log.warning("%s; closing its link", error)
                        link.close(1009)
                        break
                elif message.get("text"):
                    link.heard()
                    if len(message["text"]) > MAX_TEXT:
                        log.warning("node %s: ignoring a %d-character text frame", name, len(message["text"]))
                        continue
                    try:
                        text = _safe_report(json.loads(message["text"]))
                    except ValueError:
                        continue
                    if isinstance(text, dict) and text.get("t") == "ping":
                        link.queue(json.dumps({"t": "pong"}))
                    elif isinstance(text, dict) and text.get("t") == "update-state":
                        from ...updates import BUILD
                        reported = text.get("info") if isinstance(text.get("info"), dict) else {}
                        if "wsl_isolation" in reported and describe_system(link.info)["kind"] == "wsl":
                            link.info = {**link.info, "wsl_isolation": clean_isolation(reported["wsl_isolation"], link.accounts)}
                        build = reported.get("build")
                        if isinstance(build, str) and BUILD.fullmatch(build):
                            link.info = {**link.info, "build": build, "version": node_text(reported.get("version", ""), 80)}
                        result = text.get("result") if isinstance(text.get("result"), dict) else {}
                        refused = text.get('refused') if isinstance(text.get('refused'), dict) else None
                        clean = update_result(result, refused or None)
                        requested = getattr(link, 'update_requested_at', 0)
                        if requested and not refused and (result.get('at', 0) if isinstance(result.get('at'), (int,float)) else 0) < requested:
                            clean = {'running':True}
                        token = WRITE_INTENT.set(True)
                        try:
                            with db() as s:
                                node = s.get(Node, link.node_id)
                                if node:
                                    audited = (node.info or {}).get("update_audited_id")
                                    previous = (node.info or {}).get("update") or {}
                                    if previous.get("requested_at") and not refused and (result.get("at", 0) if isinstance(result.get("at"), (int, float)) else 0) < previous["requested_at"]:
                                        clean = previous
                                    elif clean.get("running"):
                                        clean = {**clean, "awaiting_result": False}
                                    node.info = {**(node.info or {}), **link.info, "update": clean}
                                    if clean.get("id") and clean.get("id") != audited and not clean.get("running"):
                                        node.info = {**node.info, "update_audited_id":clean["id"]}
                                        s.add(AuditLog(actor=f"node:{name}", action="update.result", target=name, detail=clean))
                                    s.commit()
                        finally:
                            WRITE_INTENT.reset(token)
                    elif isinstance(text, dict) and text.get("t") == "packages-state":
                        link.packages = clean_state(text)
                    elif isinstance(text, dict) and text.get("t") in ("host-state", "host-light"):
                        link.host_state(text)
                        snapshot = link.host_snapshot
                        if text.get("t") == "host-state" and snapshot and snapshot.get("system") == "wsl":
                            token = WRITE_INTENT.set(True)
                            try:
                                with db() as s:
                                    node = s.get(Node, link.node_id)
                                    if node:
                                        # Older policy choices have an audit record but no provenance flag.
                                        chosen = (node.info or {}).get("role_chosen") or s.scalar(
                                            select(AuditLog.id).where(AuditLog.action == "node.policy", AuditLog.target == name).limit(1)) is not None
                                        node.info = {**(node.info or {}), "os": "linux", "system": "wsl", "role_chosen": bool(chosen)}
                                        if not chosen:
                                            node.role = "workstation"
                                            evaluate(s, node)
                                        s.commit()
                            finally:
                                WRITE_INTENT.reset(token)
                    elif isinstance(text, dict) and text.get("t") == "git-backup-result":
                        link.git_backup_result(text)
                    elif isinstance(text, dict) and text.get("t") == "tool-result":
                        link.tool_result(text)
                    elif (isinstance(text, dict) and text.get("t") == "machine-csr" and isinstance(text.get("csr"), str)
                          and len(text["csr"]) <= 2048 and time.monotonic() - last_csr >= 60
                          and not any(not t.done() for t in machine_tasks)):
                        last_csr = time.monotonic()  # one at a time, at most one a minute per link
                        machine_tasks = {t for t in machine_tasks if not t.done()}
                        machine_tasks.add(link.tasks.start(machine_issue(link, text["csr"], presented), "identity-issue"))
        except asyncio.CancelledError:
            if not link.closed:
                raise  # server/request cancellation; closed links cancel their receive child normally
        except (WebSocketDisconnect, RuntimeError):  # RuntimeError: Starlette, once the socket is closed
            pass
        finally:
            was_current = hub.links.get(name) is link
            hub.detach(link)
            if was_current:
                with db() as s:
                    node = s.get(Node, hello['node_id'])
                    if node and node.status == 'active':
                        node.last_seen = now()
                        transition(s, node, False)
                        s.commit()
            await link.wait_closed()  # join workers, mutation threads and bounded socket-close cleanup

    return api
