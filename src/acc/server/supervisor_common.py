"""Small helpers shared by the supervisor and its mixins (polling, ingest, reconcile)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("acc.supervisor")
MAX_PAYLOAD_STRING = 2000


def now() -> datetime:
    return datetime.now(timezone.utc)


def _trim(value):
    if isinstance(value, str):
        return value if len(value) <= MAX_PAYLOAD_STRING else value[:MAX_PAYLOAD_STRING] + "…"
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim(v) for v in value]
    return value


class LaunchRefused(Exception):
    pass
