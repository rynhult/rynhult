"""Host overview: the root collector's snapshot (acc-hoststate) plus live psutil numbers, and pressure alerts."""
from __future__ import annotations

import json
import time
from pathlib import Path

from sqlalchemy.orm import Session as DbSession

from .. import hostinfo
from . import attention

DISK_WARN, DISK_CRITICAL, MEMORY_WARN, GPU_TEMP_WARN, STALE_SECONDS = 85.0, 95.0, 90.0, 85.0, 300


def live() -> dict:
    return hostinfo.live()


def read_snapshot(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def evaluate(db: DbSession, snapshot: dict | None, current: dict) -> None:
    """Raise or resolve host pressure items. Only actionable conditions; nothing on routine load."""
    stale = snapshot is None or time.time() - snapshot.get("collected_at", 0) > STALE_SECONDS
    if stale:
        attention.raise_item(db, "host:collector", kind="host", severity="warn", source="acc-hoststate",
                             title="Host health readings are out of date",
                             detail="The host health check hasn't reported in over 5 minutes, so disk, memory and GPU alerts "
                                    "may be missing. If this continues, run `systemctl status acc-hoststate.timer` on the host.")
    else:
        attention.resolve(db, "host:collector")
        for disk in snapshot.get("disks", []):
            key = f"host:disk:{disk['mount']}"
            if disk["percent"] >= DISK_WARN:
                attention.raise_item(db, key, kind="host", source="acc-hoststate",
                                     severity="critical" if disk["percent"] >= DISK_CRITICAL else "warn",
                                     title=f"Disk {disk['mount']} is {disk['percent']:.0f}% full",
                                     detail=f"{disk['free'] / 1e9:.0f} GB free on {disk['device']}. Free up space (old projects, "
                                            "containers or backups), or add a larger disk.")
            else:
                attention.resolve(db, key)
        for device in snapshot.get("smart", []):
            key = f"host:smart:{device['device']}"
            if device.get("passed") is False or (device.get("media_errors") or 0) > 0:
                attention.raise_item(db, key, kind="host", severity="critical", source="smartctl",
                                     title=f"Disk {device['device']} ({device['model']}) may be failing",
                                     detail=f"Its health check {'failed' if device.get('passed') is False else 'passed'} and it reported "
                                            f"{device.get('media_errors') or 0} media error(s). Make sure your backups are current "
                                            "and plan to replace this disk.")
            else:
                attention.resolve(db, key)
        failed = snapshot.get("services", {}).get("failed", [])
        if failed:
            attention.raise_item(db, "host:failed-units", kind="host", severity="warn", source="systemd",
                                 title=f"{len(failed)} host service(s) failed",
                                 detail=f"Failed: {', '.join(failed[:10])}. Check each one with `systemctl status <name>` "
                                        "on the host, or restart the host.")
        else:
            attention.resolve(db, "host:failed-units")
        for index, card in enumerate(snapshot.get("gpu", [])):
            key = f"host:gpu:{index}"
            if (card.get("temperature") or 0) >= GPU_TEMP_WARN:
                attention.raise_item(db, key, kind="host", severity="warn", source="nvidia-smi",
                                     title=f"GPU at {card['temperature']:.0f}°C",
                                     detail=f"{card['name']} is running hot. Check its fans and airflow, or reduce GPU work.")
            else:
                attention.resolve(db, key)
    if current["memory"]["percent"] >= MEMORY_WARN:
        attention.raise_item(db, "host:memory", kind="host", severity="warn", source="psutil",
                             title=f"Memory {current['memory']['percent']:.0f}% used",
                             detail="Stop sessions or projects you no longer need to free memory.")
    else:
        attention.resolve(db, "host:memory")
