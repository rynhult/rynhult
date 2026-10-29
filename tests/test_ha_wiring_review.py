"""Independent review regressions; disposable storage and injected transport only."""
import json

import pytest
from test_ha_wiring import Clock

from acc.server import ha_automatic as ha
from acc.server.ha_observations import (
    ExternalFence,
    FenceProof,
    Heartbeat,
    HeartbeatCollector,
    PostgresLag,
    WalPosition,
)
from acc.server.ha_wiring import DryRunMonitor, WitnessPlacement
from acc.server.ha_witness import DurableWitness, LeaseReply, LocalWitnessTransport, WitnessClient, WitnessUnavailable


class Fence:
    def probe(self, request):
        return FenceProof(request.target, request.epoch, request.challenge, True)


def monitor_for(clock, witness, *, lag=None):
    collector = HeartbeatCollector('main-a/boot1', 4, clock=clock)
    collector.receive(Heartbeat('main-a/boot1', 4, 1), authenticated_as='main-a/boot1')
    if lag is None:
        lag = PostgresLag(lambda: WalPosition('cluster', 1, '0/100', False),
                          lambda: WalPosition('cluster', 1, '0/100', True), clock=clock)
    return DryRunMonitor(ha.Policy(enabled=True), WitnessPlacement('external'), candidate='main-b/boot1',
        heartbeat=collector, lag=lag, witness=witness, fence=ExternalFence(Fence(), clock=clock),
        last_transition_at=lambda: 0, clock=clock)


def ready_monitor(tmp_path, clock, store):
    witness = WitnessClient(LocalWitnessTransport(store, 'main-b/boot1'), 'main-b/boot1',
                            ha.Policy(enabled=True), clock=clock)
    monitor = monitor_for(clock, witness)
    for now in range(1400, 1431, 5):
        clock.now = now
        result = monitor.status()
    assert result['gates_ready'] and not result['would_promote']
    return monitor


def test_slow_transition_history_cannot_reuse_prequery_lease_or_lag(tmp_path):
    clock = Clock()
    with DurableWitness(tmp_path / 'witness.db', ha.Policy(enabled=True), clock=clock) as store:
        monitor = ready_monitor(tmp_path, clock, store)
        def delayed_history():
            clock.now += 61
            return 0
        monitor.last_transition_at = delayed_history
        result = monitor.status()
        assert result['gates_ready'] is False
        assert result['would_promote'] is False
        assert monitor.history.healthy_since is None


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf')])
def test_bad_lag_adapter_clock_is_a_json_safe_refusal(bad):
    clock = Clock()
    lag_clock = Clock(bad)
    lag = PostgresLag(lambda: WalPosition('cluster', 1, '0/100', False),
                      lambda: WalPosition('cluster', 1, '0/100', True), clock=lag_clock)
    monitor = monitor_for(clock, None, lag=lag)
    clock.now = 1400
    result = monitor.status()
    assert not result['gates_ready'] and not result['would_promote']
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('duration', ['SECRET_CANARY', None, True])
def test_malformed_witness_duration_returns_no_lease(duration):
    class Broken:
        def acquire(self, request):
            return LeaseReply(request.holder, 1, duration, request.request_id)
    client = WitnessClient(Broken(), 'main-b/boot1', ha.Policy(enabled=True), clock=Clock())
    assert client.acquire(0) is None


def test_untrusted_writable_ancestor_cannot_house_a_witness(tmp_path):
    shared = tmp_path / 'shared'
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    private = shared / 'private'
    private.mkdir(mode=0o700)
    with pytest.raises(WitnessUnavailable):
        with DurableWitness(private / 'witness.db', ha.Policy(enabled=True)):
            pass


def test_clock_regression_during_wal_read_stays_quarantined_after_clock_recovers():
    clock = Clock()
    regress = [True]
    def primary():
        if regress[0]:
            clock.now -= 1
        return WalPosition('cluster', 1, '0/100', False)
    lag = PostgresLag(primary, lambda: WalPosition('cluster', 1, '0/100', True), clock=clock)
    assert lag.sample().seconds is None
    regress[0] = False
    clock.now = 1100
    assert lag.sample().seconds is None


def test_ready_sql_pipeline_cannot_change_or_promote_manual_authority(tmp_path, monkeypatch):
    from sqlalchemy import text

    from acc.server.db import make_engine, migrate
    from acc.server.ha import Authority
    from acc.server.ha_observations import PostgresHeartbeatChannel
    from acc.server.ha_wiring import PostgresDryRunAdapter

    url = f'sqlite:///{tmp_path}/authority.db'
    migrate(url)
    engine = make_engine(url)
    PostgresHeartbeatChannel.provision(engine)
    primary, standby = Authority(engine, 'main-a'), Authority(engine, 'main-b')
    primary.promote(expected_epoch=0)  # only explicit fixture setup is allowed to promote
    def forbidden(*a, **kw):
        pytest.fail('dry-run called authority.promote')
    monkeypatch.setattr(primary, 'promote', forbidden)
    monkeypatch.setattr(standby, 'promote', forbidden)
    clock, policy = Clock(), ha.Policy(enabled=True)
    try:
        with engine.connect() as db:
            before = tuple(db.execute(text('SELECT * FROM ha_authority WHERE id=1')).one())
        with DurableWitness(tmp_path / 'witness.db', policy, clock=clock) as store:
            name = f'{standby.node_id}/{standby.incarnation}'
            witness = WitnessClient(LocalWitnessTransport(store, name), name, policy, clock=clock)
            active = PostgresDryRunAdapter(primary, policy, WitnessPlacement('external'), clock=clock)
            passive = PostgresDryRunAdapter(standby, policy, WitnessPlacement('external'), witness=witness,
                fence=ExternalFence(Fence(), clock=clock), clock=clock)
            passive.lag = PostgresLag(lambda: WalPosition('cluster', 1, '0/100', False),
                                     lambda: WalPosition('cluster', 1, '0/100', True), clock=clock)
            active.status()
            passive.status()  # SQL baseline is not evidence of liveness
            clock.now += 5
            active.status()
            assert passive.status()['reason'] == 'heartbeat_recent'
            # Stop publishing pulses; real SQL reads of the saved row must not freshen it.
            for now in range(1400, 1431, 5):
                clock.now = now
                result = passive.status()
            assert result['gates_ready'] is True
            assert result['reason'] == 'promotion_unwired'
            assert result['would_promote'] is result['promotion_wired'] is False
            with engine.connect() as db:
                assert tuple(db.execute(text('SELECT * FROM ha_authority WHERE id=1')).one()) == before
            assert primary.status()['role'] == 'active'
            assert standby.status()['role'] == 'standby'
            clock.now += 5
            active.status()
            recovered = passive.status()
            assert recovered['reason'] == 'heartbeat_recent' and not recovered['gates_ready']
    finally:
        primary.release()
        standby.release()
        primary.control.dispose()
        standby.control.dispose()
        engine.dispose()


@pytest.mark.parametrize('failure', ['witness', 'lag', 'fence', 'transition'])
def test_unavailable_adapters_clear_ready_history_without_secret_status(tmp_path, failure):
    from sqlalchemy.exc import SQLAlchemyError
    clock = Clock()
    with DurableWitness(tmp_path / 'witness.db', ha.Policy(enabled=True), clock=clock) as store:
        monitor = ready_monitor(tmp_path, clock, store)
        def lost(*a):
            raise OSError('SECRET_CANARY transport credential')
        if failure == 'witness':
            monitor.witness.transport.epoch = lost
        elif failure == 'lag':
            def bad_query():
                raise SQLAlchemyError('SECRET_CANARY database URL')
            monitor.lag.primary = bad_query
        elif failure == 'fence':
            monitor.fence.transport.probe = lost
        else:
            monitor.last_transition_at = lost
        result = monitor.status()
        assert not result['gates_ready'] and not result['would_promote']
        assert monitor.history.healthy_since is None
        assert 'SECRET_CANARY' not in json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('epoch', [-1, True, 'SECRET_CANARY', 2**63])
def test_invalid_witness_epoch_never_reaches_transport(epoch):
    class Transport:
        def acquire(self, request):
            pytest.fail('invalid epoch was sent to the witness')
    client = WitnessClient(Transport(), 'main-b/boot1', ha.Policy(enabled=True), clock=Clock())
    assert client.acquire(epoch) is None


def test_non_numeric_lag_cannot_escape_in_status_or_raise():
    from acc.server.ha_observations import LagSample
    class BrokenLag:
        def sample(self):
            return LagSample('SECRET_CANARY', 1400)
    clock = Clock()
    monitor = monitor_for(clock, None, lag=BrokenLag())
    clock.now = 1400
    result = monitor.status()
    assert result['reason'] == 'invalid_observation'
    assert not result['gates_ready'] and not result['would_promote']
    assert 'SECRET_CANARY' not in json.dumps(result, allow_nan=False)
