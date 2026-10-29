"""What a machine can say about itself without root: the live numbers (psutil) and the parts of acc-hoststate's
snapshot an unprivileged process can read (mounts, GPU, systemd unit states, Tailscale, uptime).

The main ACC's Host page combines `live()` with the root collector's file (deploy/hoststate/acc-hoststate). Another
machine's acc-node runs as its own unprivileged user and has no root collector, so it sends `snapshot()` over its link
on request and `light()` every minute (docs/MULTIHOST.md, "Host details"). The readers below keep acc-hoststate's
field names and shapes, so one page shows both. What a machine can't read is None and named in `unavailable`, never 0.

Everything here is read-only and bounded: a few short commands with timeouts, and capped lists and strings.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import plistlib
import re
import shutil
import subprocess
import time
from pathlib import Path
from xml.parsers.expat import ExpatError

import psutil

DISK_FILESYSTEMS = "ext2,ext3,ext4,xfs,btrfs,zfs,f2fs,bcachefs,jfs,reiserfs,vfat,exfat,ntfs3"
SKIPPED_MOUNTS = ("/snap/", "/boot/efi", "/var/lib/docker", "/run/", "/System/Volumes/", "/private/var/vm", "/mnt/wsl",
                  "/usr/lib/wsl", "/init")
# acc-hoststate's list, as far as a node has them, plus the node's own units
SERVICES = ["acc-node", "acc-packages.path", "tailscaled", "ssh", "smartmontools"]
MAX_DISKS, MAX_GPUS, MAX_UNITS, MAX_FAILED, MAX_TEXT = 24, 8, 48, 48, 200
TIMEOUT = 5
MAC_DATA_MOUNT = "/System/Volumes/Data"
MAC_DISK_DEVICE = re.compile(r"(/dev/disk\d+)(?:s\d+)*")
log = logging.getLogger(__name__)


def run(*argv: str, timeout: int = TIMEOUT) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, str(error)


def text(value, limit: int = MAX_TEXT) -> str:
    return str(value)[:limit]


def live(interval: float = 0.3) -> dict:
    memory = psutil.virtual_memory()
    net = psutil.net_io_counters()
    try:
        load = list(psutil.getloadavg())
    except (OSError, AttributeError):
        load = None
    return {
        "cpu_percent": psutil.cpu_percent(interval=interval), "cpu_count": psutil.cpu_count(),
        "load": load,
        "memory": {"total": memory.total, "used": memory.total - memory.available, "percent": memory.percent},
        "swap_percent": psutil.swap_memory().percent,
        "net": {"sent": net.bytes_sent, "received": net.bytes_recv} if net else None,
        "measured_at": time.time(),
    }


def _mac_mounts() -> list[tuple[str, str]]:
    image_disks = set()
    code, out = run("hdiutil", "info", "-plist")
    if code != 0:
        log.warning("Cannot identify macOS disk images: hdiutil exited %s", code)
    else:
        try:
            images = plistlib.loads(out.encode())
        except (plistlib.InvalidFileException, ValueError, ExpatError) as error:
            log.warning("Cannot identify macOS disk images: invalid hdiutil plist: %s", error)
        else:
            for image in images.get("images", []):
                for entity in image.get("system-entities", []):
                    device = MAC_DISK_DEVICE.fullmatch(entity.get("dev-entry", ""))
                    if device:
                        image_disks.add(device[1])

    found, sources = [], set()
    partitions = sorted(psutil.disk_partitions(all=False), key=lambda p: p.mountpoint != MAC_DATA_MOUNT)
    for partition in partitions:
        target, source = partition.mountpoint, partition.device
        device = MAC_DISK_DEVICE.fullmatch(source)
        if ("ro" in partition.opts.split(",") or (device and device[1] in image_disks)
                or partition.fstype in ("devfs", "autofs") or source.startswith("map ")
                or (target.startswith("/System/Volumes/") and target != MAC_DATA_MOUNT)
                or target.startswith(("/private/var/vm", "/private/var/run/com.apple.security.cryptexd/",
                                      "/var/run/com.apple.security.cryptexd/"))
                or not os.path.isdir(target)):
            continue
        # APFS volumes on a synthesized disk share their container's free space.
        identity = device[1] if device and partition.fstype == "apfs" else source
        if identity not in sources:
            found.append((target, source))
            sources.add(identity)
    return found[:MAX_DISKS]


def mounts() -> list[tuple[str, str]]:
    """(real mount, device) for writable Mac storage, or the existing findmnt/psutil list elsewhere."""
    if platform.system() == "Darwin":
        return _mac_mounts()
    found, sources = [], set()
    code, out = run("findmnt", "-rn", "-o", "TARGET,SOURCE", "-t", DISK_FILESYSTEMS) if shutil.which("findmnt") else (1, "")
    if code == 0:
        rows = []
        for line in out.splitlines():
            target, _, source = line.partition(" ")
            rows.append((target.replace("\\x20", " "), source))
    else:
        rows = [(p.mountpoint, p.device) for p in psutil.disk_partitions(all=False)]
    for target, source in rows:
        if (target.startswith(SKIPPED_MOUNTS) or source in sources or "[" in source or not os.path.isdir(target)):
            continue  # snaps, the EFI partition, WSL's own mounts, bind mounts of a filesystem already listed
        found.append((target, source))
        sources.add(source)
    return found[:MAX_DISKS] or [("/", "")]


def disks() -> list[dict]:
    rows = []
    for mount, device in mounts():
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        if not usage.total:
            continue
        rows.append({"mount": text(mount), "device": text(device), "total": usage.total, "used": usage.used,
                     "free": usage.free, "percent": round(usage.used / usage.total * 100, 1)})
    return rows


def gpu() -> list[dict] | None:
    """As acc-hoststate: NVIDIA cards through nvidia-smi. None when there's no nvidia-smi to ask."""
    if not shutil.which("nvidia-smi"):
        return None
    code, out = run("nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                    "--format=csv,noheader,nounits")
    if code != 0:
        return None
    number = lambda v: float(v) if v.replace(".", "", 1).isdigit() else None  # noqa: E731
    rows = []
    for line in out.strip().splitlines()[:MAX_GPUS]:
        parts = [x.strip() for x in line.split(",")]
        if len(parts) != 6:
            continue
        name, util, used, total, temp, power = parts
        rows.append({"name": text(name), "utilization": number(util), "memory_used_mb": number(used),
                     "memory_total_mb": number(total), "temperature": number(temp), "power_w": number(power)})
    return rows


def services() -> dict | None:
    """The watched units' states and every failed unit, as acc-hoststate; None without systemd (a Mac, older WSL)."""
    if not shutil.which("systemctl") or not Path("/run/systemd/system").is_dir():
        return None
    states = {}
    for unit in SERVICES:
        _, out = run("systemctl", "is-active", unit)
        state = out.strip() or "unknown"
        if state != "inactive" or unit.startswith("acc-"):  # a node lists what it runs, not what it never had
            states[unit] = text(state, 32)
    _, out = run("systemctl", "--failed", "--no-legend", "--plain")
    failed = [text(line.split()[0], 120) for line in out.strip().splitlines() if line.strip()][:MAX_FAILED]
    return {"units": states, "failed": failed}


def tailscale() -> dict | None:
    if not shutil.which("tailscale"):
        return None
    code, out = run("tailscale", "status", "--json", "--self=true", "--peers=false")
    if code != 0:
        return {"state": "unknown", "ips": [], "online": None}
    try:
        data = json.loads(out)
    except ValueError:
        return {"state": "unknown", "ips": [], "online": None}
    me = data.get("Self") or {}
    return {"state": text(data.get("BackendState"), 32), "ips": [text(ip, 64) for ip in (me.get("TailscaleIPs") or [])[:4]],
            "online": me.get("Online") if isinstance(me.get("Online"), bool) else None}


def uptime() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        try:
            return max(0.0, time.time() - psutil.boot_time())
        except OSError:
            return None


def system() -> str:
    name = platform.system().lower()
    if name == "linux" and "microsoft" in platform.release().lower():
        return "wsl"
    return {"darwin": "mac"}.get(name, name)


# Parts of the main's snapshot that need root or the services account (SMART, other users' Docker, broker limits)
ROOT_ONLY = ("smart", "containers", "accounts")


def snapshot() -> dict:
    """The full read-only snapshot a node sends when an owner opens its Host page."""
    found = {"disks": disks(), "gpu": gpu(), "services": services(), "tailscale": tailscale()}
    return {
        "collected_at": time.time(), "hostname": text(platform.node(), 120), "kernel": text(platform.release(), 120),
        "system": system(), "uptime_seconds": uptime(),
        "reboot_required": Path("/var/run/reboot-required").exists(),
        **found, **{key: None for key in ROOT_ONLY},
        "live": live(),
        "unavailable": [*ROOT_ONLY, *(key for key, value in found.items() if value is None)],
    }


def light() -> dict:
    """The periodic summary: what the status board needs, cheap enough to send every minute."""
    current = live(interval=0.1)
    rows = disks()
    fullest = max(rows, key=lambda d: d["percent"]) if rows else None
    return {"collected_at": time.time(), "cpu_percent": current["cpu_percent"], "memory_percent": current["memory"]["percent"],
            "load": current["load"], "cpu_count": current["cpu_count"], "uptime_seconds": uptime(),
            "disk": {"mount": fullest["mount"], "percent": fullest["percent"], "free": fullest["free"]} if fullest else None}
