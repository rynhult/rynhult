"""Phase-three adapter composition. Every outward status refuses promotion.

No authority mutator, helper command, fence execution or promotion callback is
accepted by this interface. A ready model is exposed only as gates_ready.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from ..env import environment
from . import ha_automatic as ha
from .ha_observations import ExternalFence, FenceSample, HeartbeatCollector, LagSample
from .ha_witness import WitnessClient, WitnessUnavailable


def _finite(value):
    return type(value) in (float, int) and isfinite(value)


@dataclass(frozen=True)
class WitnessPlacement:
    mode: str = 'refuse'
    path: Path | None = None

    def __post_init__(self):
        if self.mode not in ('refuse', 'local-file', 'external'):
            raise ValueError('Choose an explicit dry-run witness placement')
        if self.mode == 'local-file' and (self.path is None or not self.path.is_absolute()):
            raise ValueError('Local witness placement needs an absolute private path')
        if self.mode != 'local-file' and self.path is not None:
            raise ValueError('Only local-file placement accepts a path')

    @classmethod
    def from_env(cls):
        mode = environment.get('ACC_HA_WITNESS_PLACEMENT', 'refuse')
        path = environment.get('ACC_HA_WITNESS_PATH')
        return cls(mode, Path(path) if path else None)


class DryRunMonitor:
    def __init__(self, policy: ha.Policy, placement: WitnessPlacement, *, candidate: str,
                 heartbeat: HeartbeatCollector, lag, witness: WitnessClient | None,
                 fence: ExternalFence, last_transition_at, clock=time.monotonic):
        self.policy, self.placement, self.candidate = policy, placement, candidate
        self.heartbeat, self.lag, self.witness, self.fence = heartbeat, lag, witness, fence
        self.last_transition_at, self.clock = last_transition_at, clock
        self.started_at = clock()
        self.history = ha.History()
        self._lock = threading.RLock()
        self._lag_history = None

    def status(self, *, uncertain=False):
        with self._lock:
            base = {'evaluation_enabled': self.policy.enabled, 'would_promote': False,
                    'promotion_wired': False, 'gates_ready': False}
            if not self.policy.enabled:
                self.history = ha.History()
                return {**base, 'reason': 'disabled'}
            if self.placement.mode == 'refuse':
                self.history = ha.History()
                return {**base, 'reason': 'witness_placement_unconfigured'}
            now = self.clock()
            if not _finite(now) or not _finite(self.started_at) or now < self.started_at:
                self.history = ha.History()
                return {**base, 'reason': 'invalid_observation'}
            tick_started = now
            try:
                lag = self.lag.sample()
            except (OSError, SQLAlchemyError, ValueError):
                lag = LagSample(None, self.clock())
            if (not isinstance(lag, LagSample) or not _finite(lag.sampled_at)
                    or lag.seconds is not None and (not _finite(lag.seconds) or lag.seconds < 0)):
                self.history = ha.History()
                return {**base, 'reason': 'invalid_observation'}
            grant = None
            if self.witness is not None:
                try:
                    grant = self.witness.acquire(self.witness.transport.epoch())
                except (OSError, SQLAlchemyError, sqlite3.Error, WitnessUnavailable, ValueError):
                    pass
            proof = self.fence.sample(self.heartbeat.holder, self.heartbeat.epoch)
            try:
                transition = self.last_transition_at()
            except (OSError, SQLAlchemyError, ValueError):
                transition = None
            # Take the decision timestamp AFTER every potentially blocking reader.
            # Otherwise a history query can spend the lease/freshness budget while
            # evaluation still sees the pre-query clock.
            now = self.clock()
            last_seen = self.heartbeat.last_seen
            if (not _finite(now) or now < tick_started
                    or last_seen is not None and (not _finite(last_seen) or last_seen > now)
                    or lag.sampled_at > now or not isinstance(proof, FenceSample)
                    or not _finite(proof.sampled_at) or proof.sampled_at > now):
                self.history = ha.History()
                return {**base, 'reason': 'invalid_observation'}
            fenced = (proof.verified is True
                      and now - proof.sampled_at <= self.policy.sample_max_age)
            inputs = {'heartbeat_age': None if last_seen is None else now - last_seen,
                      'lag_seconds': lag.seconds, 'lag_age': now - lag.sampled_at,
                      'witness_epoch': grant.epoch if grant else None,
                      'external_fence_verified': fenced}
            if lag.history != self._lag_history:
                self.history = ha.History()
                self._lag_history = lag.history
            if not _finite(transition):
                self.history = ha.History()
                return {**base, 'reason': 'transition_history_unavailable', 'inputs': inputs}
            # A restart always waits a full cooldown. Durable transition readers
            # may require longer; never shorten this using restored wall time.
            transition = max(transition, self.started_at)
            observation = None if last_seen is None else ha.Observation(
                now=now, heartbeat_at=last_seen, lag_seconds=lag.seconds, lag_sample_at=lag.sampled_at,
                stable_since=now, last_transition_at=transition, grant=grant, candidate=self.candidate,
                epoch=grant.epoch if grant else 0, externally_fenced=fenced, uncertain=uncertain)
            self.history, decision = ha.advance(self.policy, self.history, observation)
            return {**ha.dry_run_status(self.policy, decision), 'inputs': inputs}


class PostgresDryRunAdapter:
    """Collect existing manual authority and adapter telemetry into owner status.

    Only heartbeat telemetry is written, inside the existing activity barrier.
    Witness epochs never update ha_authority. This object can be driven by a
    qualification loop or a host's monitor; polling controls sample frequency.
    """
    def __init__(self, authority, policy, placement, *, witness=None, standby_engine=None,
                 fence=None, clock=time.monotonic):
        from .ha_observations import PostgresHeartbeatChannel, PostgresLag, PostgresWalReader

        self.authority, self.policy, self.placement = authority, policy, placement
        self.witness, self.clock = witness, clock
        self.fence = fence or ExternalFence(None, max_age=policy.sample_max_age, clock=clock)
        self.channel = PostgresHeartbeatChannel(authority.control)
        self.lag = PostgresLag(PostgresWalReader(authority.control), PostgresWalReader(standby_engine), clock=clock) \
            if standby_engine is not None else _UnknownLag(clock)
        self._monitor = None
        self._identity = None
        self._sequence = 0
        self._last_sent = float('-inf')
        self._lock = threading.RLock()

    def status(self):
        from sqlalchemy import text

        from .ha import Fenced
        from .ha_observations import Heartbeat

        with self._lock:
            if not self.policy.enabled:
                return self._refuse('disabled')
            if self.placement.mode == 'refuse':
                return self._refuse('witness_placement_unconfigured')
            try:
                with self.authority.control.connect() as db:
                    row = db.execute(text('SELECT epoch, holder, incarnation, uncertain '
                                          'FROM ha_authority WHERE id=1')).mappings().one()
                if not row['holder'] or not row['incarnation']:
                    return self._refuse('primary_identity_unavailable')
                target = f"{row['holder']}/{row['incarnation']}"
                identity = target, row['epoch']
                if identity != self._identity:
                    self._identity = identity
                    since = self.clock()
                    self._monitor = DryRunMonitor(self.policy, self.placement,
                        candidate=f'{self.authority.node_id}/{self.authority.incarnation}',
                        heartbeat=HeartbeatCollector(target, row['epoch'], clock=self.clock),
                        lag=self.lag, witness=self.witness, fence=self.fence,
                        last_transition_at=lambda: since, clock=self.clock)
                now = self.clock()
                if (self.authority.accepting and self.authority.epoch == row['epoch']
                        and row['incarnation'] == self.authority.incarnation
                        and now - self._last_sent >= self.policy.heartbeat):
                    with self.authority.activity():
                        self._sequence += 1
                        self.channel.send(Heartbeat(target, row['epoch'], self._sequence))
                    self._last_sent = now
                self.channel.receive(self._monitor.heartbeat)
                return self._monitor.status(uncertain=bool(row['uncertain']))
            except (Fenced, OSError, SQLAlchemyError, ValueError):
                if self._monitor:
                    self._monitor.history = ha.History()
                return self._refuse('telemetry_unavailable')

    def _refuse(self, reason):
        return {'evaluation_enabled': self.policy.enabled, 'would_promote': False,
                'promotion_wired': False, 'gates_ready': False, 'reason': reason}


class _UnknownLag:
    def __init__(self, clock):
        self.clock = clock

    def sample(self):
        return LagSample(None, self.clock())


class _UnavailableAdapter:
    def __init__(self, reason):
        self.reason = reason

    def status(self):
        return {'evaluation_enabled': True, 'would_promote': False, 'promotion_wired': False,
                'gates_ready': False, 'reason': self.reason}

    def close(self):
        pass


class _ConfiguredAdapter:
    def __init__(self, adapter, store, standby):
        self.adapter, self.store, self.standby = adapter, store, standby
        self._lock = threading.RLock()
        self._closed = False

    def status(self):
        with self._lock:
            if self._closed:
                return _UnavailableAdapter('adapter_closed').status()
            return self.adapter.status()

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                if self.store:
                    self.store.close()
                if self.standby:
                    self.standby.dispose()


def configured_adapter(authority, *, enabled: bool, transport=None, fence_transport=None):
    """Build only an explicitly placed, deployment-owned dry-run observer.

    The separate replica URL requires verified TLS and bounded SQL/connect waits.
    External witness/fence RPC implementations are injected by trusted deployment
    code, never imported or executed from an environment value or browser request.
    No connection credentials or exception text are returned in status.
    """
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from .db import _postgres_engine
    from .ha_witness import DurableWitness, LocalWitnessTransport

    if not enabled:
        return None
    store, standby = None, None
    try:
        placement = WitnessPlacement.from_env()
        if placement.mode == 'refuse':
            return _UnavailableAdapter('witness_placement_unconfigured')
        url = environment.get('ACC_HA_LAG_STANDBY_DATABASE_URL')
        if url:
            parsed = make_url(url)
            if (parsed.drivername != 'postgresql+psycopg' or not parsed.host or not parsed.database
                    or parsed.query.get('sslmode', 'verify-full') != 'verify-full'
                    or 'service' in parsed.query or 'host' in parsed.query or 'hostaddr' in parsed.query):
                raise ValueError('Replica needs a verified PostgreSQL URL')
            standby = _postgres_engine(url, poolclass=NullPool, connect_args={
                'connect_timeout': 2, 'options': '-c statement_timeout=2000 -c lock_timeout=1000'})
        policy = ha.Policy(enabled=True)
        candidate = f'{authority.node_id}/{authority.incarnation}'
        if placement.mode == 'local-file':
            store = DurableWitness(placement.path, policy)
            transport = LocalWitnessTransport(store, candidate)
        elif transport is None:
            if standby:
                standby.dispose()
            return _UnavailableAdapter('witness_transport_unavailable')
        client = WitnessClient(transport, candidate, policy)
        adapter = PostgresDryRunAdapter(authority, policy, placement, witness=client, standby_engine=standby,
                                        fence=ExternalFence(fence_transport, max_age=policy.sample_max_age))
        return _ConfiguredAdapter(adapter, store, standby)
    except (OSError, ValueError, SQLAlchemyError, WitnessUnavailable):
        if store:
            store.close()
        if standby:
            standby.dispose()
        return _UnavailableAdapter('configuration_refused')
