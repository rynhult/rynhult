"""Fixed "live" host numbers for the demo instance and the e2e server, in place of psutil.

Their screenshots go into the public wiki: they must never show the CPU, memory or load of the machine that runs them.
"""
from __future__ import annotations

import time


def live() -> dict:
    return {"cpu_percent": 12.0, "cpu_count": 16, "load": [0.84, 0.92, 1.05],
            "memory": {"total": 64e9, "used": 21.8e9, "percent": 34.1}, "swap_percent": 0.0,
            "net": {"sent": 18.2e9, "received": 42.7e9}, "measured_at": time.time()}
