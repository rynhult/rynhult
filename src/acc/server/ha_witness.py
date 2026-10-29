"""Private, durable single-writer witness and a role-bound transport boundary.

This database is independent of ha_authority. Its leases are dry-run evidence only.
A remote implementation of WitnessTransport must authenticate each incarnation,
provide bounded calls, and preserve request/reply identity; enrollment is not auth.
"""
from __future__ import annotations

import fcntl
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Protocol

from .ha_automatic import Grant, Policy, Witness, acquire, lease_valid

MAX_LEASE = 300  # maximum admitted by Policy, including a previous process's policy
IDENTITY = re.compile(r'[a-z0-9][a-z0-9-]{0,39}/[A-Za-z0-9_-]{1,64}')


class WitnessUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseRequest:
    holder: str
    expected_epoch: int
    request_id: str


@dataclass(frozen=True)
class LeaseReply:
    holder: str
    epoch: int
    duration: float
    request_id: str


class WitnessTransport(Protocol):
    def acquire(self, request: LeaseRequest) -> LeaseReply | None: ...

    def epoch(self) -> int: ...


class DurableWitness:
    """One service process, private local SQLite, FULL commit before reply.

    Restart preserves the epoch and quarantines grants for the maximum possible
    prior lease. Persisted monotonic times never prove clock continuity. Backups
    of this file must not be restored into use without independent recovery/fencing.
    A lifetime flock prevents a second service from resetting the recovery window.
    Network/shared filesystems and writable-by-others directories are unsupported.
    """
    def __init__(self, path: Path, policy: Policy, *, clock=time.monotonic):
        self.path, self.policy, self.clock = Path(path), policy, clock
        self._lock = threading.RLock()
        self._fd = None
        try:
            parent = self.path.parent.stat()
            if (not self.path.is_absolute() or self.path.parent.resolve() != self.path.parent
                    or parent.st_uid != os.geteuid() or parent.st_mode & 0o022):
                raise ValueError('private witness directory required')
            # A protected leaf is replaceable if an attacker can rename it from
            # a writable ancestor. Root-owned sticky temporary roots are safe:
            # their sticky rule protects our service-owned child directory.
            for directory in self.path.parent.parents:
                info = directory.stat()
                sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022 and not sticky_root:
                    raise ValueError('untrusted witness ancestor')
            self._fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            self._private(os.fstat(self._fd))
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            new = False
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                new = True
            except FileExistsError:
                fd = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                self._private(os.fstat(fd))
                if os.fstat(fd).st_size > 1024 * 1024:
                    raise ValueError('oversized witness')
            finally:
                os.close(fd)
            now = self.clock()
            if not isfinite(now):
                raise ValueError('invalid witness clock')
            with closing(self._connect()) as db:
                db.execute('BEGIN IMMEDIATE')
                if new:
                    db.execute('CREATE TABLE witness (id INTEGER PRIMARY KEY CHECK(id=1), '
                               'epoch INTEGER NOT NULL, holder TEXT, expires REAL NOT NULL, '
                               'observed REAL NOT NULL, recovery REAL NOT NULL)')
                    db.execute('INSERT INTO witness VALUES (1, 0, NULL, ?, ?, ?)', (now, now, now))
                else:
                    self._state(db)
                    db.execute('UPDATE witness SET holder=NULL, expires=?, observed=?, recovery=? WHERE id=1',
                               (now + MAX_LEASE, now, now + MAX_LEASE))
                db.commit()
        except (OSError, ValueError, sqlite3.Error) as error:
            self.close()
            raise WitnessUnavailable('Witness storage unavailable; no lease') from error

    @staticmethod
    def _private(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError('private regular witness file required')

    def _connect(self):
        if self._fd is None:
            raise WitnessUnavailable('Witness service closed; no lease')
        db = sqlite3.connect(self.path, timeout=1)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def _state(self, db):
        rows = db.execute('SELECT epoch, holder, expires, observed, recovery FROM witness WHERE id=1').fetchall()
        if len(rows) != 1:
            raise ValueError('missing witness state')
        epoch, holder, expiry, observed, recovery = rows[0]
        if (type(epoch) is not int or not 0 <= epoch < 2**63 - 1
                or holder is not None and not IDENTITY.fullmatch(holder)
                or not all(isinstance(v, (float, int)) and isfinite(v) for v in (expiry, observed, recovery))
                or not 0 <= expiry - observed <= MAX_LEASE):
            raise ValueError('invalid witness state')
        return Witness(epoch, holder, expiry, observed), recovery

    def epoch(self):
        with self._lock, closing(self._connect()) as db:
            return self._state(db)[0].epoch

    def acquire(self, request: LeaseRequest):
        if (not isinstance(request.holder, str) or not IDENTITY.fullmatch(request.holder)
                or type(request.expected_epoch) is not int or request.expected_epoch < 0
                or not isinstance(request.request_id, str) or not re.fullmatch('[0-9a-f]{32}', request.request_id)):
            return None
        with self._lock, closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            state, recovery = self._state(db)
            now = self.clock()
            if not isfinite(now) or now < recovery:
                return None
            updated, grant = acquire(state, request.holder, request.expected_epoch, now, self.policy)
            if grant is None:
                return None
            db.execute('UPDATE witness SET epoch=?, holder=?, expires=?, observed=? WHERE id=1',
                       (updated.epoch, updated.holder, updated.expires_at, updated.observed_at))
            db.commit()  # no reply (and no usable lease) if this fails or stalls past the client deadline
            return LeaseReply(grant.holder, grant.epoch, self.policy.lease, request.request_id)

    def close(self):
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class LocalWitnessTransport:
    """Explicit local placement, bound by trusted service code to one authenticated incarnation.

    This is not a network service or a permission to share SQLite over NFS/SMB.
    Remote transports implement the same interface with their own authenticated RPC.
    """
    def __init__(self, store: DurableWitness, holder: str):
        self.store, self.holder = store, holder

    def acquire(self, request):
        return self.store.acquire(request) if request.holder == self.holder else None

    def epoch(self):
        return self.store.epoch()


class WitnessClient:
    def __init__(self, transport: WitnessTransport, holder: str, policy: Policy, *, clock=time.monotonic):
        self.transport, self.holder, self.policy, self.clock = transport, holder, policy, clock
        self._last_start = float('-inf')

    def acquire(self, expected_epoch: int) -> Grant | None:
        if type(expected_epoch) is not int or not 0 <= expected_epoch < 2**63 - 1:
            return None
        started = self.clock()
        if not isfinite(started) or started < self._last_start:
            return None
        self._last_start = started
        request = LeaseRequest(self.holder, expected_epoch, secrets.token_hex(16))
        try:
            reply = self.transport.acquire(request)
            if (not isinstance(reply, LeaseReply) or reply.request_id != request.request_id
                    or reply.holder != self.holder or type(reply.epoch) is not int
                    or reply.epoch not in (expected_epoch, expected_epoch + 1)
                    or type(reply.duration) not in (int, float) or not 0 < reply.duration <= self.policy.lease):
                return None
            grant = Grant(reply.holder, reply.epoch, started, started + reply.duration)
            return grant if lease_valid(grant, self.clock(), self.policy) else None
        except (OSError, sqlite3.Error, WitnessUnavailable, ValueError):
            return None
