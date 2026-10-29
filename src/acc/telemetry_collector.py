"""Minimal reference collector; no admin/public data UI, no third-party calls or access logs.

Run behind an HTTPS proxy that does not log connection addresses. See docs/TELEMETRY-COLLECTOR.md.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import sqlite3
import stat
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from .telemetry_schema import Deletion, Report, Usage, encode, key_id, wire_schema

DAY = 86400
# Erasure ledger outlives the 30-day backup limit so any restorable snapshot can be sanitized.
LEDGER_DAYS = 45
MAX_BYTES = 1 << 30

SECRET = "«secret»"
CONTROL = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)
# Shape rules mirror the client scrubber (src/acc/server/telemetry.py), so client-scrubbed text is a
# fixpoint. The collector cannot know an installation's own secrets or names: this is the backstop
# for old, buggy or hostile clients, not a replacement for client-side scrubbing.
SHAPES = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*"), SECRET),
    (re.compile(r"\b(?:gh[oprsu]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|sk-[\w-]{20,}|A[KS]IA[0-9A-Z]{16}"
                r"|xox[abprs]-[\w-]{10,}|glpat-[\w-]{20,})"), SECRET),
    (re.compile(r"(?i)\b(bearer\s+)\S+"), r"\1" + SECRET),
    (re.compile(r"(?i)\b((?:authorization|cookie|password|passwd|secret|token|api[_-]?key|access[_-]?key)"
                r"\b\s*[=:]\s*)[^\s,;'\"]+"), r"\1" + SECRET),
    (re.compile(r"\beyJ[\w-]*\.[\w-]+\.[\w-]+"), SECRET),
    (re.compile(r"[A-Za-z0-9_+/=-]{32,}"), SECRET),
    (re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+"), "<url>"),
    (re.compile(r"[^\s<>@]+@[^\s<>@]+\.[A-Za-z]+"), "<email>"),
    (re.compile(r"(?:[A-Za-z]:[\\/]|(?<![\w<>])/(?!/)|\\\\|~[\w.-]*/)[^\r\n]*"), "<path>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<id>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
]
IPV6 = re.compile(r"\[?[0-9a-fA-F]*:[0-9a-fA-F:.]+(?:%[\w.-]+)?\]?")


def scrub_text(text: str, limit: int = 300) -> str:
    """Redact secret/identifier shapes in report text; never trust that a client already did."""
    if "\r" in text or "\n" in text:
        return SECRET[:limit]
    text = CONTROL.sub("", text)
    for pattern, replacement in SHAPES:
        text = pattern.sub(replacement, text)

    def address(match):
        try:
            ipaddress.ip_address(match[0].rstrip(".").strip("[]").split("%", 1)[0])
            return "<ip>"
        except ValueError:
            return match[0]

    return IPV6.sub(address, text)[:limit]


def address_bucket(host: str) -> str:
    """One rate-limit bucket per IPv4 address or IPv6 /64; one site holds 2^64 IPv6 addresses."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.version == 6 and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def record_erasure(db, install, report="", now=None):
    db.execute("INSERT INTO erasure VALUES (?,?,?)", (install, report, time.time() if now is None else now))


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise ValueError("Collector requires DELETE journal mode; WAL can retain deleted reports")
        with connection:
            yield connection
    finally:
        connection.close()


def retain(db, now):
    """Finalize complete UTC months once; totals have no installation ids or small-cell publication."""
    month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
    months = [
        r[0]
        for r in db.execute(
            "SELECT DISTINCT substr(day,1,7) FROM payload WHERE kind='usage' AND substr(day,1,7) < ?", (month,)
        )
    ]
    for completed in months:
        year, number = map(int, completed.split("-"))
        following = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=timezone.utc).timestamp()
        # Seven-day client collection window: finalize only when late arrivals are closed.
        # Never recompute a finalized unlinked total from a shrinking raw-data window.
        if now < following + 7 * DAY or db.execute("SELECT 1 FROM monthly WHERE month=?", (completed,)).fetchone():
            continue
        cells = {}
        for install, text in db.execute(
            "SELECT install,body FROM payload WHERE kind='usage' AND substr(day,1,7)=?", (completed,)
        ):
            data = json.loads(text)
            for field in ("os", "providers_enabled", "features_enabled"):
                for value in data[field]:
                    cells.setdefault(field + ":" + value, set()).add(install)
        totals = {cell: len(installs) for cell, installs in cells.items()}
        db.execute("INSERT OR REPLACE INTO monthly VALUES (?,?)", (completed, encode(totals).decode()))
    # Calendar months, not an approximation of a month as 30 days.
    current = datetime.fromtimestamp(now, timezone.utc)
    index = current.year * 12 + current.month - 1 - 13
    cutoff = f"{index // 12:04d}-{index % 12 + 1:02d}"
    db.execute("DELETE FROM monthly WHERE month < ?", (cutoff,))
    db.execute(
        "DELETE FROM payload WHERE (kind='usage' AND received<?) OR (kind='reports' AND received<?)",
        (now - 90 * DAY, now - 180 * DAY),
    )
    db.execute(
        "DELETE FROM signing_key WHERE touched<? AND install NOT IN (SELECT install FROM payload)", (now - 397 * DAY,)
    )
    db.execute("DELETE FROM erasure WHERE erased<?", (now - LEDGER_DAYS * DAY,))


def export_statistics(db):
    """Export only standalone, unlinked monthly cells with at least ten signing keys.

    Never export grand totals, suppressed-cell counts or cross-tabulations from which a
    small cell can be recovered. Keys are pseudonyms, not proof of distinct people.
    """
    result = {}
    for month, text in db.execute("SELECT month,totals FROM monthly ORDER BY month"):
        cells = {cell: count for cell, count in json.loads(text).items()
                 if type(count) is int and count >= 10}
        if cells:
            result[month] = cells
    return result


def create_collector(path: Path, max_bytes: int = MAX_BYTES) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        async def maintenance():
            while True:
                try:
                    with database(path) as db:
                        db.execute("PRAGMA secure_delete=ON")
                        retain(db, time.time())
                except sqlite3.Error:
                    logging.getLogger(__name__).error("Collector retention failed; retrying in 60 seconds")
                    delay = 60
                else:
                    delay = 3600
                await asyncio.sleep(delay)

        task = asyncio.create_task(maintenance())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = path.parent.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o077 or directory.st_uid != os.getuid():
        raise ValueError("Collector data directory must be private and owned by the service account")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = path.lstat()
        if (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_mode & 0o077
            or existing.st_uid != os.getuid()
            or existing.st_nlink != 1
        ):
            raise ValueError("Collector database must be a private regular file owned by the service account") from None
    else:
        os.close(fd)
    with database(path) as db:
        db.executescript("""
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS signing_key (install TEXT PRIMARY KEY, public TEXT NOT NULL,
                kind TEXT NOT NULL, touched REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS payload (id TEXT PRIMARY KEY, install TEXT NOT NULL, kind TEXT NOT NULL,
                day TEXT NOT NULL, body TEXT NOT NULL, received REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS payload_install ON payload(install);
            CREATE TABLE IF NOT EXISTS monthly (month TEXT PRIMARY KEY, totals TEXT NOT NULL);
            -- Key/report ids erased since recent backups were taken; replayed on restore.
            CREATE TABLE IF NOT EXISTS erasure (install TEXT NOT NULL, report TEXT NOT NULL, erased REAL NOT NULL);
        """)
    # Addresses are transient, bounded, and never enter SQLite or response/log text.
    windows = {}
    registrations = {}
    key_windows = {}
    # Erasure has maps of its own: a flood of usage or reports that fills the maps above must never refuse a person's
    # deletion request (telemetry privacy review, TP-6). Its per-address and per-key limits still apply.
    erase_windows = {}
    erase_keys = {}

    @app.middleware("http")
    async def limits(request: Request, call_next):
        from fastapi.responses import JSONResponse

        hour = int(time.time() // 3600)
        erasing = request.url.path == "/v1/delete"
        hourly = erase_windows if erasing else windows
        for old in [key for key in hourly if key[0] != hour]:
            del hourly[old]
        address = address_bucket(request.client.host) if request.client else "unknown"
        key = (hour, address)
        if key not in hourly and len(hourly) >= 10000:
            return JSONResponse({"detail": "Busy"}, status_code=429)
        entry = hourly.setdefault(key, Counter())
        entry["requests"] += 1
        if entry["requests"] > 60:
            return JSONResponse({"detail": "Retry later"}, status_code=429)
        request.state.rate = entry
        if erasing:  # registers no key, so needs no registration entry
            return await call_next(request)
        day = int(time.time() // DAY)
        for old in [key for key in registrations if key[0] != day]:
            del registrations[old]
        daily_key = (day, address)
        if daily_key not in registrations and len(registrations) >= 10000:
            return JSONResponse({"detail": "Busy"}, status_code=429)
        request.state.registrations = registrations.setdefault(daily_key, Counter())
        return await call_next(request)

    async def authenticated(request, model, cap):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > cap:
                raise HTTPException(413, "Payload too large")
        try:
            stamp = request.headers["x-acc-time"]
            if abs(time.time() - int(stamp)) > 300:
                raise ValueError()
            public = base64.b64decode(request.headers["x-acc-key"], validate=True)
            signature = base64.b64decode(request.headers["x-acc-signature"], validate=True)
            Ed25519PublicKey.from_public_bytes(public).verify(
                signature, stamp.encode() + b"\n" + request.url.path.encode() + b"\n" + body
            )
        except (KeyError, ValueError, InvalidSignature):
            raise HTTPException(401, "Invalid signature") from None
        try:
            value = model.model_validate_json(bytes(body))
            if encode(value) != bytes(body) or value.install != key_id(public):
                raise ValueError()
        except (ValidationError, ValueError):
            raise HTTPException(422, "Invalid payload") from None
        hour = int(time.time() // 3600)
        per_key = erase_keys if model is Deletion else key_windows
        for old in [key for key in per_key if key[0] != hour]:
            del per_key[old]
        rate_key = (hour, value.install)
        if rate_key not in per_key and len(per_key) >= 10000:
            raise HTTPException(429, "Busy")
        per_key[rate_key] = per_key.get(rate_key, 0) + 1
        if per_key[rate_key] > 60:
            raise HTTPException(429, "Retry later")
        return value, bytes(body), request.headers["x-acc-key"]

    async def collect(request, model, kind, cap):
        value, body, public = await authenticated(request, model, cap)
        now = time.time()
        with database(path) as db:
            db.execute("PRAGMA secure_delete=ON")
            retain(db, now)
            key = db.execute("SELECT public,kind FROM signing_key WHERE install=?", (value.install,)).fetchone()
            if key and key != (public, kind):
                raise HTTPException(409, "Use separate keys for usage and reports")
            if not key:
                request.state.registrations["keys"] += 1
                if request.state.registrations["keys"] > 20:
                    raise HTTPException(429, "Retry later")
            count = db.execute(
                "SELECT count(*) FROM payload WHERE install=? AND kind=? AND received>?",
                (value.install, kind, now - DAY),
            ).fetchone()[0]
            if count >= (2 if kind == "usage" else 10):
                raise HTTPException(429, "Retry tomorrow")
            pages, free, size = (db.execute(f"PRAGMA {name}").fetchone()[0]
                                 for name in ("page_count", "freelist_count", "page_size"))
            if (pages - free) * size >= max_bytes:
                # Global bound: many keys and addresses must not exhaust the disk. Deletion still works.
                raise HTTPException(429, "Retry tomorrow")
            if kind == "reports":
                value = value.model_copy(update={"message": scrub_text(value.message, 300)})
                body = encode(value)
            day = value.day.isoformat() if kind == "usage" else datetime.now(timezone.utc).date().isoformat()
            if kind == "usage" and abs((datetime.now(timezone.utc).date() - value.day).days) > 7:
                raise HTTPException(422, "Usage day is outside the collection window")
            identity = value.install + ":" + day if kind == "usage" else str(value.report)
            db.execute(
                "INSERT INTO signing_key VALUES (?,?,?,?) ON CONFLICT(install) DO UPDATE SET touched=excluded.touched",
                (value.install, public, kind, now),
            )
            db.execute(
                "INSERT OR IGNORE INTO payload VALUES (?,?,?,?,?,?)",
                (identity, value.install, kind, day, body.decode(), now),
            )
        from fastapi.responses import JSONResponse

        return JSONResponse({"accepted": True}, status_code=202)

    @app.post("/v1/usage")
    async def usage(request: Request):
        return await collect(request, Usage, "usage", 4096)

    @app.post("/v1/report")
    async def report(request: Request):
        return await collect(request, Report, "reports", 16384)

    @app.post("/v1/delete")
    async def erase(request: Request):
        value, _, public = await authenticated(request, Deletion, 16384)
        with database(path) as db:
            db.execute("PRAGMA secure_delete=ON")
            key = db.execute("SELECT public FROM signing_key WHERE install=?", (value.install,)).fetchone()
            if key and key[0] != public:
                raise HTTPException(401, "Invalid signature")
            if value.reports:
                for report in value.reports:
                    if db.execute("DELETE FROM payload WHERE install=? AND id=?",
                                  (value.install, str(report))).rowcount:
                        record_erasure(db, value.install, str(report))
            else:
                # Ledger only real erasures: fresh keys must not grow the ledger without bound.
                if db.execute("DELETE FROM payload WHERE install=?", (value.install,)).rowcount or key:
                    record_erasure(db, value.install)
                db.execute("DELETE FROM signing_key WHERE install=?", (value.install,))
            retain(db, time.time())
        return {"receipt": str(uuid.uuid4()), "status": "deleted"}

    @app.get("/v1/schemas")
    def schemas():
        return {"usage": wire_schema(Usage), "report": wire_schema(Report)}

    return app
