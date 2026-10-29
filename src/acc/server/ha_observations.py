"""Authenticated heartbeat, PostgreSQL replay-position and read-only fence evidence.

Readers are injected, allowing isolated qualification without contacting deployment
services. Transport credentials and database URLs never appear in these samples.
"""
from __future__ import annotations

import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from math import isfinite
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .ha_witness import IDENTITY


@dataclass(frozen=True)
class Heartbeat:
    holder: str
    epoch: int
    sequence: int


class HeartbeatCollector:
    """Stamp only authenticated, increasing packets in our own monotonic domain.

    The transport must supply live authenticated messages, not an arbitrarily old
    saved packet on restart. PostgresHeartbeatChannel below requires a baseline
    and a subsequent sequence advance before forwarding its first heartbeat.
    """
    def __init__(self, holder: str, epoch: int, *, clock=time.monotonic):
        self.holder, self.epoch, self.clock = holder, epoch, clock
        self.sequence = -1
        self.last_seen = None
        self._observed = None

    def receive(self, packet: Heartbeat, *, authenticated_as: str) -> bool:
        now = self.clock()
        if not isfinite(now) or self._observed is not None and now < self._observed:
            self.last_seen = None
            return False
        self._observed = now
        if (not isinstance(packet, Heartbeat) or packet.holder != self.holder
                or authenticated_as != packet.holder or not IDENTITY.fullmatch(packet.holder)
                or type(packet.epoch) is not int or packet.epoch != self.epoch
                or type(packet.sequence) is not int or not self.sequence < packet.sequence < 2**63):
            return False
        self.sequence, self.last_seen = packet.sequence, now
        return True


@dataclass(frozen=True)
class WalPosition:
    system_id: str
    timeline: int
    lsn: str | None
    recovery: bool


class PostgresWalReader:
    """Read-only, least-privilege connections need permission for the control functions.

    Connection/statement deadlines and verified TLS belong to the configured
    engine. No query uses last_replay_timestamp as a lag estimate.
    """
    def __init__(self, engine):
        self.engine = engine

    def __call__(self):
        # PostgreSQL 17: functions-admin.html (WAL/recovery), functions-info.html (control data).
        with self.engine.connect() as db:
            row = db.execute(text('SELECT pg_is_in_recovery() AS recovery, '
                                  '(pg_control_system()).system_identifier::text AS system_id, '
                                  '(pg_control_checkpoint()).timeline_id AS timeline, '
                                  '(CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() '
                                  'ELSE pg_current_wal_flush_lsn() END)::text AS lsn')).mappings().one()
        return WalPosition(**row)


@dataclass(frozen=True)
class LagSample:
    seconds: float | None
    sampled_at: float
    history: tuple[str, int] | None = None


def _lsn(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}', value):
        raise ValueError('unknown WAL position')
    high, low = value.split('/')
    return (int(high, 16) << 32) + int(low, 16)


class PostgresLag:
    """Conservative time since a primary WAL sample proven replayed by the replica.

    Fresh equal positions prove idle catch-up. Behind with no replayed local time
    anchor is unknown. Retain bounded pending anchors; losing one reduces
    availability, never manufactures a smaller lag. Requests start before querying
    either server, so query/delivery delays consume freshness and the lag budget.
    """
    def __init__(self, primary, standby, *, clock=time.monotonic):
        self.primary, self.standby, self.clock = primary, standby, clock
        self._history = None
        self._anchors = deque(maxlen=128)
        self._confirmed = None
        self._positions = None
        self._observed = None
        self._invalidated = False

    def _reset(self):
        self._confirmed = None
        self._anchors.clear()

    def sample(self) -> LagSample:
        started = self.clock()
        try:
            if self._invalidated or not isfinite(started) or self._observed is not None and started < self._observed:
                self._invalidated = True
                raise ValueError('clock regression or invalidated history')
            self._observed = started
            p, s = self.primary(), self.standby()
            if (not isinstance(p, WalPosition) or not isinstance(s, WalPosition)
                    or p.recovery is not False or s.recovery is not True
                    or not isinstance(p.system_id, str) or not 1 <= len(p.system_id) <= 64
                    or type(p.timeline) is not int or p.timeline < 1
                    or (p.system_id, p.timeline) != (s.system_id, s.timeline)):
                raise ValueError('replication history unavailable')
            positions = _lsn(p.lsn), _lsn(s.lsn)
            history = p.system_id, p.timeline
            if self._history is not None and history != self._history:
                self._invalidated = True
                self._reset()
                raise ValueError('replication history changed')
            if self._positions and any(new < old for new, old in zip(positions, self._positions)):
                self._invalidated = True
                raise ValueError('WAL regression')
            self._history, self._positions = history, positions
            self._anchors.append((positions[0], started))
            while self._anchors and self._anchors[0][0] <= positions[1]:
                self._confirmed = self._anchors.popleft()[1]
            now = self.clock()
            if not isfinite(now) or now < started:
                self._invalidated = True
                raise ValueError('clock regression')
            return LagSample(None if self._confirmed is None else now - self._confirmed, started, history)
        except (OSError, SQLAlchemyError, ValueError):
            self._reset()
            return LagSample(None, started)


@dataclass(frozen=True)
class FenceRequest:
    target: str
    epoch: int
    challenge: str


@dataclass(frozen=True)
class FenceProof:
    target: str
    epoch: int
    challenge: str
    fenced: bool


class FenceTransport(Protocol):
    """Independent authenticated controller; bounded probe only, never execute a fence here.

    A positive response must certify isolation of the whole old control host and
    its helpers for this epoch. A host's own claim or owner boolean is not proof.
    """
    def probe(self, request: FenceRequest) -> FenceProof: ...


@dataclass(frozen=True)
class FenceSample:
    verified: bool
    sampled_at: float


class ExternalFence:
    def __init__(self, transport: FenceTransport | None, *, max_age=10, clock=time.monotonic):
        self.transport, self.max_age, self.clock = transport, max_age, clock

    def sample(self, target: str, epoch: int) -> FenceSample:
        started = self.clock()
        if self.transport is None or not IDENTITY.fullmatch(target) or type(epoch) is not int or epoch < 0:
            return FenceSample(False, started)
        request = FenceRequest(target, epoch, secrets.token_hex(16))
        try:
            proof = self.transport.probe(request)
            now = self.clock()
            verified = (isinstance(proof, FenceProof) and proof.fenced is True
                        and proof.target == target and type(proof.epoch) is int and proof.epoch == epoch
                        and proof.challenge == request.challenge
                        and all(isfinite(t) for t in (started, now)) and 0 <= now - started <= self.max_age)
            return FenceSample(verified, started)
        except (OSError, TimeoutError, ValueError):
            return FenceSample(False, started)


class PostgresHeartbeatChannel:
    """Adapter-owned SQL telemetry, explicitly provisioned by the operator.

    Writers publish only under existing manual authority. Verified-TLS service
    credentials authenticate the SQL channel; reader permissions are SELECT only.
    A saved row cannot establish initial liveness: require a baseline then an
    advance. No sender timestamp is used. No application migration is added.
    """
    def __init__(self, engine):
        import threading
        self.engine = engine
        self._baseline = None
        self._lock = threading.Lock()

    @staticmethod
    def provision(engine):
        with engine.begin() as db:
            db.execute(text('CREATE TABLE IF NOT EXISTS acc_ha_dry_heartbeat ('
                            'node VARCHAR(40) PRIMARY KEY, incarnation VARCHAR(64) NOT NULL, '
                            'epoch BIGINT NOT NULL, sequence BIGINT NOT NULL)'))

    def send(self, packet: Heartbeat):
        if (not IDENTITY.fullmatch(packet.holder) or type(packet.epoch) is not int or packet.epoch < 0
                or type(packet.sequence) is not int or not 0 <= packet.sequence < 2**63):
            raise ValueError('invalid heartbeat')
        node, incarnation = packet.holder.split('/')
        with self.engine.begin() as db:
            result = db.execute(text('INSERT INTO acc_ha_dry_heartbeat (node, incarnation, epoch, sequence) '
                'VALUES (:node, :incarnation, :epoch, :sequence) ON CONFLICT (node) DO UPDATE SET '
                'incarnation=excluded.incarnation, epoch=excluded.epoch, sequence=excluded.sequence '
                'WHERE excluded.epoch > acc_ha_dry_heartbeat.epoch OR '
                '(excluded.epoch = acc_ha_dry_heartbeat.epoch AND '
                'excluded.incarnation = acc_ha_dry_heartbeat.incarnation AND '
                'excluded.sequence > acc_ha_dry_heartbeat.sequence)'),
                {'node': node, 'incarnation': incarnation, 'epoch': packet.epoch, 'sequence': packet.sequence})
            return result.rowcount == 1

    def receive(self, collector: HeartbeatCollector):
        node, incarnation = collector.holder.split('/')
        with self._lock, self.engine.connect() as db:
            row = db.execute(text('SELECT incarnation, epoch, sequence FROM acc_ha_dry_heartbeat '
                                  'WHERE node=:node'), {'node': node}).mappings().one_or_none()
            if row is None or row['incarnation'] != incarnation or row['epoch'] != collector.epoch:
                self._baseline = None
                return False
            identity = collector.holder, row['epoch']
            previous = self._baseline
            self._baseline = identity, row['sequence']
            if previous is None or previous[0] != identity or row['sequence'] <= previous[1]:
                return False
            return collector.receive(Heartbeat(collector.holder, row['epoch'], row['sequence']),
                                     authenticated_as=collector.holder)
