"""Another machine's host details, primary side (docs/MULTIHOST.md, "Host details").

A node that advertises HOST_CAPABILITY answers {"t": "host", "id"} with {"t": "host-state", "id", "snapshot"} and sends
{"t": "host-light", "light"} on connect and every minute (acc.hostinfo). What it sends is kept only as far as it fits
what the Host page and the status board show: known fields, numbers as numbers, short strings and capped lists. A field
the node couldn't read stays None (shown as unavailable), never 0.
"""
from __future__ import annotations

import time

from .host import DISK_CRITICAL, DISK_WARN, MEMORY_WARN
from .nodes import node_text

HOST_CAPABILITY = "host-v1"
MIN_REFRESH = 15.0  # seconds: a full snapshot younger than this is served from the link's cache
REQUEST_TIMEOUT = 8.0  # seconds to wait for the node's answer before showing what the link has
MAX_DISKS, MAX_GPUS, MAX_UNITS, MAX_FAILED, MAX_TEXT = 24, 8, 48, 48, 200
SYSTEMS = ("linux", "wsl", "mac", "windows")
UNAVAILABLE = ("smart", "containers", "accounts", "gpu", "services", "tailscale", "disks")


def _num(value, low: float = 0, high: float = 1e18) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:  # NaN
        return None
    return value if low <= value <= high else None


def _timestamp(value) -> float | None:
    # Collection clocks may drift a little, but cannot make old readings appear fresh forever
    # or send dates outside the browser's representable range.
    return _num(value, 0, time.time() + 300)


def _pct(value) -> float | None:
    return _num(value, 0, 100)


def _str(value, limit: int = MAX_TEXT) -> str | None:
    return node_text(value, limit) if isinstance(value, str) else None


def _live(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    memory = raw.get("memory") if isinstance(raw.get("memory"), dict) else {}
    load = raw.get("load")
    return {"cpu_percent": _pct(raw.get("cpu_percent")), "cpu_count": _num(raw.get("cpu_count"), 1, 4096),
            "load": [_num(x, 0, 1e6) for x in load[:3]] if isinstance(load, list) and len(load) == 3 else None,
            "memory": {"total": _num(memory.get("total")), "used": _num(memory.get("used")),
                       "percent": _pct(memory.get("percent"))},
            "swap_percent": _pct(raw.get("swap_percent")), "measured_at": _timestamp(raw.get("measured_at"))}


def _disk(raw) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("mount"), str):
        return None
    row = {"mount": _str(raw["mount"]), "device": _str(raw.get("device")) or "", "total": _num(raw.get("total")),
           "used": _num(raw.get("used")), "free": _num(raw.get("free")), "percent": _pct(raw.get("percent"))}
    return row if row["percent"] is not None and row["total"] is not None else None


def _gpu(raw) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        return None
    return {"name": _str(raw["name"]), "utilization": _pct(raw.get("utilization")),
            "memory_used_mb": _num(raw.get("memory_used_mb")), "memory_total_mb": _num(raw.get("memory_total_mb")),
            "temperature": _num(raw.get("temperature"), -50, 200), "power_w": _num(raw.get("power_w"), 0, 10000)}


def _rows(raw, clean, limit: int) -> list | None:
    if not isinstance(raw, list):
        return None
    return [row for row in (clean(x) for x in raw[:limit]) if row is not None]


def _services(raw) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("units"), dict):
        return None
    units = {_str(k, 120): _str(v, 32) or "unknown" for k, v in list(raw["units"].items())[:MAX_UNITS] if isinstance(k, str)}
    failed = [_str(x, 120) for x in (raw.get("failed") or [])[:MAX_FAILED] if isinstance(x, str)] \
        if isinstance(raw.get("failed"), list) else []
    return {"units": units, "failed": failed}


def _tailscale(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    ips = raw.get("ips") if isinstance(raw.get("ips"), list) else []
    return {"state": _str(raw.get("state"), 32) or "unknown", "ips": [_str(ip, 64) for ip in ips[:4] if isinstance(ip, str)],
            "online": raw.get("online") if isinstance(raw.get("online"), bool) else None}


def clean_snapshot(raw) -> dict | None:
    """A node's full snapshot, in the shape of the main's (HostView.snapshot) plus `live`, `system`, `unavailable`."""
    if not isinstance(raw, dict) or _timestamp(raw.get("collected_at")) is None:
        return None
    snapshot = {
        "collected_at": raw["collected_at"], "hostname": _str(raw.get("hostname"), 120) or "",
        "kernel": _str(raw.get("kernel"), 120) or "", "system": raw.get("system") if raw.get("system") in SYSTEMS else None,
        "uptime_seconds": _num(raw.get("uptime_seconds")), "reboot_required": raw.get("reboot_required") is True,
        "disks": _rows(raw.get("disks"), _disk, MAX_DISKS), "gpu": _rows(raw.get("gpu"), _gpu, MAX_GPUS),
        "services": _services(raw.get("services")), "tailscale": _tailscale(raw.get("tailscale")),
        "smart": None, "containers": None, "accounts": None,  # need root or the services account: never from a node
        "live": _live(raw.get("live")),
    }
    snapshot["unavailable"] = [key for key in UNAVAILABLE if snapshot.get(key) is None]
    return snapshot


def clean_light(raw) -> dict | None:
    if not isinstance(raw, dict) or _timestamp(raw.get("collected_at")) is None:
        return None
    disk = raw.get("disk") if isinstance(raw.get("disk"), dict) else None
    load = raw.get("load")
    return {"collected_at": raw["collected_at"], "cpu_percent": _pct(raw.get("cpu_percent")),
            "memory_percent": _pct(raw.get("memory_percent")), "cpu_count": _num(raw.get("cpu_count"), 1, 4096),
            "load": [_num(x, 0, 1e6) for x in load[:3]] if isinstance(load, list) and len(load) == 3 else None,
            "uptime_seconds": _num(raw.get("uptime_seconds")),
            "disk": {"mount": _str(disk.get("mount")), "percent": _pct(disk.get("percent")), "free": _num(disk.get("free"))}
            if disk and isinstance(disk.get("mount"), str) and _pct(disk.get("percent")) is not None else None}


def light_from(snapshot: dict) -> dict:
    """The summary of a full snapshot (the main's own, or a node's newer full one)."""
    live = snapshot.get("live") or {}
    disks = snapshot.get("disks") or []
    fullest = max(disks, key=lambda d: d["percent"]) if disks else None
    return {"collected_at": snapshot.get("collected_at"), "cpu_percent": live.get("cpu_percent"),
            "memory_percent": (live.get("memory") or {}).get("percent"), "cpu_count": live.get("cpu_count"),
            "load": live.get("load"), "uptime_seconds": snapshot.get("uptime_seconds"),
            "disk": {k: fullest[k] for k in ("mount", "percent", "free")} if fullest else None}


def alerts(light: dict | None, snapshot: dict | None = None) -> list[dict]:
    """The same pressure rules as the main's (host.evaluate), from what a node reported; for the status board."""
    found = []
    for disk in (snapshot or {}).get("disks") or ([light["disk"]] if light and light.get("disk") else []):
        if (disk.get("percent") or 0) >= DISK_WARN:
            found.append({"severity": "critical" if disk["percent"] >= DISK_CRITICAL else "warn",
                          "title": f"Disk {disk['mount']} is {disk['percent']:.0f}% full"})
    memory = (light or {}).get("memory_percent")
    if memory is not None and memory >= MEMORY_WARN:
        found.append({"severity": "warn", "title": f"Memory {memory:.0f}% used"})
    failed = ((snapshot or {}).get("services") or {}).get("failed") or []
    if failed:
        found.append({"severity": "warn", "title": f"{len(failed)} service(s) failed"})
    if (snapshot or {}).get("reboot_required"):
        found.append({"severity": "warn", "title": "Reboot required"})
    return found


def fresh(at: float | None, now: float | None = None) -> bool:
    return at is not None and (now or time.time()) - at < MIN_REFRESH
