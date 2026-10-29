"""Dry-run adapters operate only on disposable state and injected observations."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from acc.server import ha_automatic as ha


@pytest.fixture(autouse=True)
def isolated_wiring_environment(monkeypatch):
    for suffix in ('HA_WITNESS_PLACEMENT', 'HA_WITNESS_PATH', 'HA_LAG_STANDBY_DATABASE_URL'):
        for prefix in ('ACC_', 'RYNHULT_'):
            monkeypatch.delenv(prefix + suffix, raising=False)


class Clock:
    def __init__(self, now=1000):
        self.now = now

    def __call__(self):
        return self.now


def test_durable_witness_serializes_competitors_and_preserves_epoch_on_restart(tmp_path):
    from acc.server.ha_witness import DurableWitness, LocalWitnessTransport, WitnessClient

    clock = Clock()
    policy = ha.Policy(enabled=True)
    path = tmp_path / 'witness.db'
    with DurableWitness(path, policy, clock=clock) as store:
        clients = [WitnessClient(LocalWitnessTransport(store, name), name, policy, clock=clock)
                   for name in ('main-a/boot1', 'main-b/boot1')]
        with ThreadPoolExecutor(2) as pool:
            grants = list(pool.map(lambda client: client.acquire(0), clients))
        assert sum(grant is not None for grant in grants) == 1
        winner = next(grant for grant in grants if grant)
        assert winner.epoch == 1
        assert clients[0].acquire(0) is None
    # A new witness process cannot use old monotonic expiry timestamps as authority.
    clock.now = 5
    with DurableWitness(path, policy, clock=clock) as restarted:
        client = WitnessClient(LocalWitnessTransport(restarted, 'main-b/boot2'), 'main-b/boot2', policy, clock=clock)
        assert client.acquire(1) is None
        clock.now = 304.99
        assert client.acquire(1) is None
        clock.now = 305
        assert client.acquire(1).epoch == 2
        assert client.acquire(1) is None


def test_witness_partition_and_delayed_reply_cannot_extend_lease(tmp_path):
    from acc.server.ha_witness import DurableWitness, LocalWitnessTransport, WitnessClient

    policy, clock = ha.Policy(enabled=True), Clock()
    with DurableWitness(tmp_path / 'witness.db', policy, clock=clock) as store:
        local = LocalWitnessTransport(store, 'main-b/boot1')

        class Slow:
            def acquire(self, request):
                reply = local.acquire(request)
                clock.now += 59
                return reply

        assert WitnessClient(Slow(), 'main-b/boot1', policy, clock=clock).acquire(0) is None

        class Partition:
            def acquire(self, request):
                raise OSError('simulated unreachable witness')

        assert WitnessClient(Partition(), 'main-b/boot1', policy, clock=clock).acquire(1) is None


def test_witness_transport_binds_identity_and_reply_to_request(tmp_path):
    from dataclasses import replace

    from acc.server.ha_witness import DurableWitness, LocalWitnessTransport, WitnessClient

    policy, clock = ha.Policy(enabled=True), Clock()
    with DurableWitness(tmp_path / 'witness.db', policy, clock=clock) as store:
        bound = LocalWitnessTransport(store, 'main-a/boot1')
        assert WitnessClient(bound, 'main-b/boot1', policy, clock=clock).acquire(0) is None

        class Substituted:
            def acquire(self, request):
                return replace(bound.acquire(request), request_id='different-request')

        assert WitnessClient(Substituted(), 'main-a/boot1', policy, clock=clock).acquire(0) is None


def test_corrupt_witness_is_never_reinitialized(tmp_path):
    from acc.server.ha_witness import DurableWitness, WitnessUnavailable

    path = tmp_path / 'witness.db'
    path.write_bytes(b'broken-state')
    with pytest.raises(WitnessUnavailable):
        DurableWitness(path, ha.Policy())
    assert path.read_bytes() == b'broken-state'


def test_heartbeat_requires_authenticated_identity_and_advancing_sequence():
    from acc.server.ha_observations import Heartbeat, HeartbeatCollector

    clock = Clock()
    collector = HeartbeatCollector('main-a/boot1', epoch=4, clock=clock)
    packet = Heartbeat('main-a/boot1', 4, 10)
    assert not collector.receive(packet, authenticated_as='main-b/boot1')
    assert collector.last_seen is None
    assert collector.receive(packet, authenticated_as='main-a/boot1')
    clock.now += 121
    assert not collector.receive(packet, authenticated_as='main-a/boot1')
    assert collector.last_seen == 1000  # replay cannot freshen a lost heartbeat
    assert not collector.receive(Heartbeat('main-a/boot1', 3, 11), authenticated_as='main-a/boot1')
    assert not collector.receive(Heartbeat('main-a/boot2', 4, 11), authenticated_as='main-a/boot2')
    assert collector.receive(Heartbeat('main-a/boot1', 4, 11), authenticated_as='main-a/boot1')
    assert collector.last_seen == 1121
    clock.now = 10
    assert not collector.receive(Heartbeat('main-a/boot1', 4, 12), authenticated_as='main-a/boot1')
    assert collector.last_seen is None


def test_replication_lag_uses_replayed_wal_anchors_including_idle_database():
    from acc.server.ha_observations import PostgresLag, WalPosition

    clock = Clock()
    primary = [WalPosition('cluster-one', 1, '0/100', recovery=False)]
    standby = [WalPosition('cluster-one', 1, '0/100', recovery=True)]
    lag = PostgresLag(lambda: primary[0], lambda: standby[0], clock=clock)
    assert lag.sample().seconds == 0
    clock.now += 400  # idle last_replay_timestamp would look old, but fresh WAL comparison proves caught up
    assert lag.sample().seconds == 0
    primary[0] = WalPosition('cluster-one', 1, '0/200', recovery=False)
    clock.now += 5
    assert lag.sample().seconds == 5
    clock.now += 301
    assert lag.sample().seconds == 306
    standby[0] = WalPosition('cluster-one', 1, '0/200', recovery=True)
    assert lag.sample().seconds == 0


def test_replication_lag_unknown_on_start_behind_partition_or_wrong_history():
    from acc.server.ha_observations import PostgresLag, WalPosition

    clock = Clock()
    p = WalPosition('cluster-one', 1, '0/200', recovery=False)
    s = [WalPosition('cluster-one', 1, '0/100', recovery=True)]
    adapter = PostgresLag(lambda: p, lambda: s[0], clock=clock)
    assert adapter.sample().seconds is None  # position gap without a replayed time anchor is not zero lag
    s[0] = WalPosition('other-cluster', 1, '0/300', recovery=True)
    assert adapter.sample().seconds is None
    s[0] = WalPosition('cluster-one', 2, '0/300', recovery=True)
    assert adapter.sample().seconds is None

    def lost():
        raise OSError('simulated database partition')

    assert PostgresLag(lost, lambda: s[0], clock=clock).sample().seconds is None


def test_external_fence_requires_fresh_target_epoch_and_challenge_bound_proof():
    from dataclasses import replace

    from acc.server.ha_observations import ExternalFence, FenceProof

    clock = Clock()

    class Transport:
        proof_change = {}

        def probe(self, request):
            return replace(FenceProof(request.target, request.epoch, request.challenge, True), **self.proof_change)

    transport = Transport()
    adapter = ExternalFence(transport, clock=clock)
    assert adapter.sample('main-a/boot1', 4).verified
    for change in ({'target': 'main-b/boot1'}, {'epoch': 3}, {'challenge': 'old'}, {'fenced': False}):
        transport.proof_change = change
        assert not adapter.sample('main-a/boot1', 4).verified
    assert not ExternalFence(None, clock=clock).sample('main-a/boot1', 4).verified


def test_postgres_wal_reader_against_throwaway_harness(postgres_url):
    from acc.server.db import make_engine
    from acc.server.ha_observations import PostgresWalReader

    engine = make_engine(postgres_url)
    try:
        position = PostgresWalReader(engine)()
        assert position.system_id and position.timeline >= 1 and not position.recovery
        assert position.lsn and '/' in position.lsn
    finally:
        engine.dispose()


def test_sql_heartbeat_requires_baseline_and_rejects_old_incarnation(tmp_path):
    from acc.server.db import make_engine
    from acc.server.ha_observations import Heartbeat, HeartbeatCollector, PostgresHeartbeatChannel

    engine = make_engine(f'sqlite:///{tmp_path}/channel.db')
    try:
        PostgresHeartbeatChannel.provision(engine)
        channel = PostgresHeartbeatChannel(engine)
        clock = Clock()
        collector = HeartbeatCollector('main-a/boot1', 4, clock=clock)
        assert channel.send(Heartbeat('main-a/boot1', 4, 1))
        assert not channel.receive(collector) and collector.last_seen is None
        assert channel.send(Heartbeat('main-a/boot1', 4, 2))
        assert channel.receive(collector) and collector.last_seen == 1000
        clock.now += 200
        assert not channel.receive(collector) and collector.last_seen == 1000
        assert not channel.send(Heartbeat('main-a/boot1', 3, 100))
        assert not channel.send(Heartbeat('main-a/boot2', 4, 100))
        assert channel.send(Heartbeat('main-a/boot2', 5, 1))
        assert not channel.receive(collector)
        restarted = HeartbeatCollector('main-a/boot2', 5, clock=clock)
        assert not channel.receive(restarted)
        assert channel.send(Heartbeat('main-a/boot2', 5, 2))
        assert channel.receive(restarted)
    finally:
        engine.dispose()


def test_dry_run_pipeline_collects_real_adapters_but_never_authorizes_promotion(tmp_path):
    from acc.server.ha_observations import (
        ExternalFence,
        FenceProof,
        Heartbeat,
        HeartbeatCollector,
        PostgresLag,
        WalPosition,
    )
    from acc.server.ha_wiring import DryRunMonitor, WitnessPlacement
    from acc.server.ha_witness import DurableWitness, LocalWitnessTransport, WitnessClient

    clock, policy = Clock(), ha.Policy(enabled=True)
    heartbeat = HeartbeatCollector('main-a/boot1', 4, clock=clock)
    heartbeat.receive(Heartbeat('main-a/boot1', 4, 1), authenticated_as='main-a/boot1')
    lag = PostgresLag(lambda: WalPosition('cluster-one', 1, '0/100', False),
                      lambda: WalPosition('cluster-one', 1, '0/100', True), clock=clock)

    class Fence:
        def probe(self, request):
            return FenceProof(request.target, request.epoch, request.challenge, True)

    with DurableWitness(tmp_path / 'witness.db', policy, clock=clock) as store:
        witness = WitnessClient(LocalWitnessTransport(store, 'main-b/boot1'), 'main-b/boot1', policy, clock=clock)
        monitor = DryRunMonitor(policy, WitnessPlacement('external'), candidate='main-b/boot1',
            heartbeat=heartbeat, lag=lag, witness=witness, fence=ExternalFence(Fence(), clock=clock),
            last_transition_at=lambda: 0, clock=clock)
        clock.now = 1400
        for now in range(1400, 1431, 5):
            clock.now = now
            result = monitor.status()
            assert result['would_promote'] is False and result['promotion_wired'] is False
        assert result['gates_ready'] is True and result['reason'] == 'promotion_unwired'
        assert result['inputs']['heartbeat_age'] == 430
        assert result['inputs']['lag_seconds'] == 0
        assert result['inputs']['witness_epoch'] == 1
        assert result['inputs']['external_fence_verified'] is True
        monitor.fence = ExternalFence(None, clock=clock)
        assert monitor.status()['reason'] == 'external_fencing_unverified'
        assert not monitor.status()['gates_ready']
        monitor.fence = ExternalFence(Fence(), clock=clock)
        assert monitor.status()['reason'] == 'stabilizing'


@pytest.mark.parametrize('failure,reason', [('partition', 'witness_unavailable'), ('lag', 'lag_exceeded'),
                                          ('stale_lag', 'lag_stale'), ('heartbeat', 'telemetry_unavailable')])
def test_pipeline_failure_injection_never_ready(tmp_path, failure, reason):
    from acc.server.ha_observations import ExternalFence, Heartbeat, HeartbeatCollector, LagSample
    from acc.server.ha_wiring import DryRunMonitor, WitnessPlacement
    from acc.server.ha_witness import DurableWitness, LocalWitnessTransport, WitnessClient

    clock, policy = Clock(), ha.Policy(enabled=True)
    heartbeat = HeartbeatCollector('main-a/boot1', 4, clock=clock)
    if failure != 'heartbeat':
        heartbeat.receive(Heartbeat('main-a/boot1', 4, 1), authenticated_as='main-a/boot1')

    class Lag:
        def sample(self):
            return LagSample(301 if failure == 'lag' else 0, clock.now - (11 if failure == 'stale_lag' else 0))

    class Partition:
        def epoch(self):
            raise OSError('simulated witness partition')

    with DurableWitness(tmp_path / 'witness.db', policy, clock=clock) as store:
        transport = Partition() if failure == 'partition' else LocalWitnessTransport(store, 'main-b/boot1')
        witness = WitnessClient(transport, 'main-b/boot1', policy, clock=clock)
        monitor = DryRunMonitor(policy, WitnessPlacement('external'), candidate='main-b/boot1',
            heartbeat=heartbeat, lag=Lag(), witness=witness, fence=ExternalFence(None, clock=clock),
            last_transition_at=lambda: 0, clock=clock)
        clock.now = 1400
        result = monitor.status()
        assert result['reason'] == reason and not result['would_promote'] and not result['gates_ready']


def test_witness_placement_is_explicit_and_defaults_to_refusal(monkeypatch):
    from acc.server.ha_wiring import WitnessPlacement

    monkeypatch.delenv('ACC_HA_WITNESS_PLACEMENT', raising=False)
    assert WitnessPlacement.from_env().mode == 'refuse'
    monkeypatch.setenv('ACC_HA_WITNESS_PLACEMENT', 'local-file')
    monkeypatch.delenv('ACC_HA_WITNESS_PATH', raising=False)
    with pytest.raises(ValueError):
        WitnessPlacement.from_env()
    monkeypatch.setenv('ACC_HA_WITNESS_PATH', '/tmp/example-witness/witness.db')
    assert WitnessPlacement.from_env().path.name == 'witness.db'


def test_closed_witness_service_cannot_issue_a_grant(tmp_path):
    from acc.server.ha_witness import DurableWitness, LocalWitnessTransport, WitnessClient

    policy, clock = ha.Policy(enabled=True), Clock()
    store = DurableWitness(tmp_path / 'witness.db', policy, clock=clock)
    client = WitnessClient(LocalWitnessTransport(store, 'main-b/boot1'), 'main-b/boot1', policy, clock=clock)
    store.close()
    assert client.acquire(0) is None


def test_witness_read_only_or_contended_storage_returns_no_lease(tmp_path):
    import sqlite3

    from acc.server.ha_witness import DurableWitness, LocalWitnessTransport, WitnessClient

    policy, clock = ha.Policy(enabled=True), Clock()
    with DurableWitness(tmp_path / 'witness.db', policy, clock=clock) as store:
        client = WitnessClient(LocalWitnessTransport(store, 'main-b/boot1'), 'main-b/boot1', policy, clock=clock)
        with sqlite3.connect(store.path) as busy:
            busy.execute('BEGIN IMMEDIATE')
            assert client.acquire(0) is None
        assert client.acquire(0).epoch == 1


def test_fence_reply_that_outlives_freshness_is_not_proof():
    from acc.server.ha_observations import ExternalFence, FenceProof

    clock = Clock()

    class Slow:
        def probe(self, request):
            clock.now += 11
            return FenceProof(request.target, request.epoch, request.challenge, True)

    assert not ExternalFence(Slow(), clock=clock).sample('main-a/boot1', 4).verified


def test_replication_history_regression_remains_blocked_after_a_partition():
    from acc.server.ha_observations import PostgresLag, WalPosition

    values = ['0/200']
    disconnected = [False]

    def primary():
        if disconnected[0]:
            raise OSError('simulated partition')
        return WalPosition('cluster-one', 1, values[0], False)

    adapter = PostgresLag(primary, lambda: WalPosition('cluster-one', 1, values[0], True), clock=Clock())
    assert adapter.sample().seconds == 0
    disconnected[0] = True
    assert adapter.sample().seconds is None
    disconnected[0] = False
    values[0] = '0/100'
    assert adapter.sample().seconds is None
    assert adapter.sample().seconds is None


def test_sql_adapter_feeds_status_with_active_pulses_without_changing_authority(tmp_path):
    from acc.server.db import make_engine, migrate
    from acc.server.ha import Authority
    from acc.server.ha_observations import PostgresHeartbeatChannel
    from acc.server.ha_wiring import PostgresDryRunAdapter, WitnessPlacement
    from acc.server.ha_witness import DurableWitness, LocalWitnessTransport, WitnessClient

    url = f'sqlite:///{tmp_path}/authority.db'
    migrate(url)
    engine = make_engine(url)
    PostgresHeartbeatChannel.provision(engine)
    a, b = Authority(engine, 'main-a'), Authority(engine, 'main-b')
    clock, policy = Clock(), ha.Policy(enabled=True)
    a.promote(expected_epoch=0)
    try:
        with DurableWitness(tmp_path / 'witness.db', policy, clock=clock) as store:
            adapters = []
            for authority in (a, b):
                name = f'{authority.node_id}/{authority.incarnation}'
                client = WitnessClient(LocalWitnessTransport(store, name), name, policy, clock=clock)
                adapters.append(PostgresDryRunAdapter(authority, policy, WitnessPlacement('external'),
                                                     witness=client, clock=clock))
            active, passive = adapters
            assert not active.status()['would_promote']
            first = passive.status()
            assert first['inputs']['heartbeat_age'] is None
            clock.now += 5
            active.status()
            second = passive.status()
            assert second['inputs']['heartbeat_age'] == 0
            assert second['reason'] == 'heartbeat_recent'
            assert not second['promotion_wired'] and not second['would_promote']
            assert a.status()['role'] == 'active' and b.status()['role'] == 'standby'
            assert a.status()['epoch'] == b.status()['epoch'] == 1
    finally:
        a.release()
        b.release()
        a.control.dispose()
        b.control.dispose()
        engine.dispose()


def test_environment_factory_refuses_unplaced_or_insecure_adapters(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from acc.server.ha_wiring import configured_adapter

    for name in ('ACC_HA_WITNESS_PLACEMENT', 'RYNHULT_HA_WITNESS_PLACEMENT',
                 'ACC_HA_WITNESS_PATH', 'RYNHULT_HA_WITNESS_PATH'):
        monkeypatch.delenv(name, raising=False)
    assert configured_adapter(None, enabled=False) is None
    assert configured_adapter(None, enabled=True).status()['reason'] == 'witness_placement_unconfigured'
    monkeypatch.setenv('ACC_HA_WITNESS_PLACEMENT', 'local-file')
    monkeypatch.setenv('ACC_HA_WITNESS_PATH', str(tmp_path / 'witness.db'))
    monkeypatch.setenv('ACC_HA_LAG_STANDBY_DATABASE_URL',
                       'postgresql+psycopg://example:example-secret@db.example.lan/acc?sslmode=disable')
    result = configured_adapter(SimpleNamespace(node_id='main-b', incarnation='boot1'), enabled=True)
    output = result.status()
    assert output['reason'] == 'configuration_refused'
    assert 'example-secret' not in str(output) and 'db.example.lan' not in str(output)
    assert not (tmp_path / 'witness.db').exists()


def test_witness_close_waits_for_the_committed_transition(tmp_path):
    import threading

    from acc.server.ha_witness import DurableWitness, LocalWitnessTransport, WitnessClient

    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    blocking = [False]

    def server_clock():
        if blocking[0]:
            entered.set()
            assert release.wait(2)
        return 1000

    policy = ha.Policy(enabled=True)
    store = DurableWitness(tmp_path / 'witness.db', policy, clock=server_clock)
    client = WitnessClient(LocalWitnessTransport(store, 'main-b/boot1'), 'main-b/boot1', policy, clock=Clock())
    blocking[0] = True

    def close():
        store.close()
        closed.set()

    try:
        with ThreadPoolExecutor(2) as pool:
            pending = pool.submit(client.acquire, 0)
            assert entered.wait(2)
            ending = pool.submit(close)
            try:
                assert not closed.wait(0.05), 'service lock released before its transition completed'
            finally:
                release.set()
            assert pending.result(2).epoch == 1
            ending.result(2)
    finally:
        release.set()
        store.close()


def test_configured_adapter_close_stops_future_observation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from acc.server.db import make_engine, migrate
    from acc.server.ha_wiring import configured_adapter

    url = f'sqlite:///{tmp_path}/authority.db'
    migrate(url)
    engine = make_engine(url)
    monkeypatch.setenv('ACC_HA_WITNESS_PLACEMENT', 'local-file')
    monkeypatch.setenv('ACC_HA_WITNESS_PATH', str(tmp_path / 'witness.db'))
    monkeypatch.delenv('ACC_HA_LAG_STANDBY_DATABASE_URL', raising=False)
    authority = SimpleNamespace(node_id='main-b', incarnation='boot1', control=engine)
    adapter = configured_adapter(authority, enabled=True)
    try:
        adapter.close()
        assert adapter.status()['reason'] == 'adapter_closed'
        assert not adapter.status()['would_promote']
    finally:
        adapter.close()
        engine.dispose()


def test_invalid_collector_clock_is_a_json_safe_refusal():
    import json

    from acc.server.ha_observations import ExternalFence, Heartbeat, HeartbeatCollector, LagSample
    from acc.server.ha_wiring import DryRunMonitor, WitnessPlacement

    clock = Clock()
    collector = HeartbeatCollector('main-a/boot1', 1, clock=clock)
    collector.receive(Heartbeat('main-a/boot1', 1, 1), authenticated_as='main-a/boot1')

    class Lag:
        def sample(self):
            return LagSample(0, clock.now)

    monitor = DryRunMonitor(ha.Policy(enabled=True), WitnessPlacement('external'), candidate='main-b/boot1',
        heartbeat=collector, lag=Lag(), witness=None, fence=ExternalFence(None, clock=clock),
        last_transition_at=lambda: 0, clock=clock)
    clock.now = float('nan')
    result = monitor.status()
    assert result['reason'] == 'invalid_observation' and not result['would_promote']
    json.dumps(result, allow_nan=False)


def test_witness_never_replies_with_a_lease_before_commit(tmp_path):
    import sqlite3

    from acc.server.ha_witness import DurableWitness, LocalWitnessTransport, WitnessClient

    policy, clock = ha.Policy(enabled=True), Clock()
    with DurableWitness(tmp_path / 'witness.db', policy, clock=clock) as store:
        client = WitnessClient(LocalWitnessTransport(store, 'main-b/boot1'), 'main-b/boot1', policy, clock=clock)
        with sqlite3.connect(store.path) as db:
            db.execute("CREATE TRIGGER reject_write BEFORE UPDATE ON witness "
                       "BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END")
        assert client.acquire(0) is None and store.epoch() == 0
        with sqlite3.connect(store.path) as db:
            db.execute('DROP TRIGGER reject_write')
        assert client.acquire(0).epoch == 1


def test_second_witness_service_cannot_reset_a_live_services_expiry(tmp_path):
    from acc.server.ha_witness import DurableWitness, LocalWitnessTransport, WitnessClient, WitnessUnavailable

    policy, clock = ha.Policy(enabled=True), Clock()
    path = tmp_path / 'witness.db'
    with DurableWitness(path, policy, clock=clock) as store:
        client = WitnessClient(LocalWitnessTransport(store, 'main-a/boot1'), 'main-a/boot1', policy, clock=clock)
        assert client.acquire(0).epoch == 1
        with pytest.raises(WitnessUnavailable):
            DurableWitness(path, policy, clock=clock)
        assert client.acquire(1).epoch == 1
