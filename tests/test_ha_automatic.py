"""Safety properties of the dry-run protocol; no live clock, network or services."""
import random
from dataclasses import replace

import pytest

from acc.server import ha_automatic as ha


def ready(**changes):
    return replace(ha.Observation(now=200, heartbeat_at=0, lag_seconds=10,
        lag_sample_at=199, stable_since=0, last_transition_at=-200,
        grant=ha.Grant('b/boot1', 2, 190, 250), candidate='b/boot1', epoch=2,
        externally_fenced=True), **changes)


def test_crash_requires_all_gates_and_opt_in():
    assert ha.evaluate(ha.Policy(), ready()).reason == 'disabled'
    policy = ha.Policy(enabled=True)
    assert ha.evaluate(policy, ready()).would_promote
    for changes, reason in [
        ({'heartbeat_at': 190}, 'heartbeat_recent'),
        ({'lag_seconds': 301}, 'lag_exceeded'),
        ({'lag_seconds': None}, 'lag_unknown'),
        ({'lag_sample_at': 100}, 'lag_stale'),
        ({'grant': None}, 'witness_unavailable'),
        ({'candidate': 'a/boot1'}, 'grant_mismatch'),
        ({'epoch': 3}, 'grant_mismatch'),
        ({'externally_fenced': False}, 'external_fencing_unverified'),
        ({'uncertain': True}, 'uncertain_effects'),
        ({'stable_since': 199}, 'stabilizing'),
        ({'last_transition_at': 199}, 'cooldown'),
        ({'now': 251, 'lag_sample_at': 251}, 'lease_expired'),
        ({'heartbeat_at': 201}, 'invalid_observation'),
        ({'lag_seconds': float('nan')}, 'invalid_observation'),
    ]:
        assert ha.evaluate(policy, ready(**changes)).reason == reason


def test_missing_telemetry_never_means_dead_primary():
    assert ha.evaluate(ha.Policy(enabled=True), None).reason == 'telemetry_unavailable'


@pytest.mark.parametrize('changes', [dict(heartbeat=0), dict(timeout=10), dict(lease=10),
    dict(max_lag=301), dict(drift=0.5), dict(cooldown=1), dict(heartbeat=float('nan')),
    dict(heartbeat=30, lease=30, timeout=120)])
def test_unsafe_settings_rejected(changes):
    with pytest.raises(ValueError):
        ha.Policy(**changes)


def test_partition_witness_on_primary_side_and_slow_disk():
    p = ha.Policy(enabled=True)
    state, grant = ha.acquire(ha.Witness(), 'a/boot1', 0, 0, p)
    assert grant.epoch == 1
    # Standby sees silence but witness continues renewing the primary.
    for t in range(10, 200, 10):
        state, grant = ha.acquire(state, 'a/boot1', 1, t, p)
        unchanged, denied = ha.acquire(state, 'b/boot1', 1, t, p)
        assert denied is None and unchanged == state
    # A stalled commit/response consumes the request-start deadline.
    assert not ha.evaluate(p, ready(now=260, grant=ha.Grant('b/boot1', 2, 190, 250))).would_promote


def test_partition_witness_on_standby_side_waits_for_expiry():
    p = ha.Policy(enabled=True)
    state, old = ha.acquire(ha.Witness(), 'a/boot1', 0, 0, p)
    assert ha.acquire(state, 'b/boot1', 1, 59, p)[1] is None
    state, new = ha.acquire(state, 'b/boot1', 1, 60, p)
    assert new.epoch == 2
    assert ha.acquire(state, 'a/boot1', 1, 61, p)[1] is None
    assert not ha.lease_valid(old, 60, p)


def test_seeded_partition_state_machine_has_no_overlapping_authority():
    p = ha.Policy(enabled=True)
    for seed in range(40):
        rng = random.Random(seed)
        state = ha.Witness()
        grants = {}
        now = 0
        for _ in range(300):
            now += rng.uniform(0, 10)
            candidate = rng.choice(['a/boot1', 'b/boot1', 'a/boot2'])
            if rng.random() < .7:  # packet loss/partition
                request_start = now
                state, grant = ha.acquire(state, candidate, state.epoch, now, p)
                if grant:
                    grants[candidate] = replace(grant, requested_at=request_start)
            valid = [g for g in grants.values() if ha.lease_valid(g, now, p)]
            assert len(valid) <= 1


def test_clock_offsets_and_bounded_drift_expire_before_witness():
    p = ha.Policy(enabled=True)
    # No wall clock timestamps cross the wire. Offset cancels in elapsed duration.
    for offset in [-100000, 0, 100000]:
        for rate in [.99, 1, 1.01]:
            grant = ha.Grant('a/boot1', 1, offset, offset + 60)
            assert not ha.lease_valid(grant, offset + 60 * rate, p)


def test_flapping_resets_stability_and_cooldown():
    p = ha.Policy(enabled=True)
    for t in range(200, 220):
        assert not ha.evaluate(p, ready(now=t, lag_sample_at=t, stable_since=t-1)).would_promote


def test_status_projection_is_fail_closed_and_audited_once(tmp_path):
    from types import SimpleNamespace

    from sqlalchemy import select

    from acc.server.db import make_engine, migrate
    from acc.server.ha import Authority
    from acc.server.ha_runtime import Runtime
    from acc.server.models import AuditLog

    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    authority = Authority(engine, 'b')
    ctx = SimpleNamespace(settings=SimpleNamespace(ha_auto_evaluate=True))
    runtime = Runtime(authority, ctx)
    try:
        for _ in range(2):
            result = runtime.automatic_status('alex')
            assert result == {'evaluation_enabled': True, 'would_promote': False,
                              'reason': 'telemetry_unavailable', 'promotion_wired': False}
        with engine.connect() as db:
            rows = db.execute(select(AuditLog.action, AuditLog.detail)).all()
        assert len(rows) == 1
        assert rows[0].action == 'ha.automatic_evaluation'
        assert rows[0].detail['would_promote'] is False
        assert authority.status()['role'] == 'standby'
        ctx.settings.ha_auto_evaluate = False
        assert runtime.automatic_status('alex')['reason'] == 'disabled'
    finally:
        authority.release()
        authority.control.dispose()
        engine.dispose()


def test_owner_evaluation_setting_defaults_off(monkeypatch):
    from acc.server.config import Settings
    monkeypatch.delenv('ACC_HA_AUTO_EVALUATE', raising=False)
    assert not Settings.from_env().ha_auto_evaluate
    monkeypatch.setenv('ACC_HA_AUTO_EVALUATE', '1')
    assert Settings.from_env().ha_auto_evaluate


def test_witness_stale_epoch_reboot_and_clock_regression_are_denied():
    p = ha.Policy(enabled=True)
    state, _ = ha.acquire(ha.Witness(), 'a/boot1', 0, 10, p)
    for holder, epoch, now in [('a/boot1', 0, 11), ('a/boot2', 1, 11),
                               ('a/boot1', 1, 9), ('b/boot1', 1, float('nan'))]:
        assert ha.acquire(state, holder, epoch, now, p) == (state, None)
    state, grant = ha.acquire(state, 'a/boot1', 1, 70, p)
    assert grant.epoch == 2
    assert ha.acquire(state, 'a/boot1', 1, 71, p)[1] is None


@pytest.mark.parametrize('changes,expected', [
    ({'heartbeat_at': 80}, False),  # equality to 120 is insufficient
    ({'heartbeat_at': 79.99}, True),
    ({'lag_seconds': 300}, True),
    ({'lag_seconds': 300.01}, False),
    ({'lag_sample_at': 190}, True),
    ({'lag_sample_at': 189.99}, False),
    ({'stable_since': 170}, True),
    ({'stable_since': 170.01}, False),
    ({'last_transition_at': -100}, True),
    ({'last_transition_at': -99.99}, False),
])
def test_promotion_gate_boundaries(changes, expected):
    assert ha.evaluate(ha.Policy(enabled=True), ready(**changes)).would_promote is expected


def test_clock_rate_safety_for_all_partition_and_delivery_schedules():
    # Primary clock may run slow, witness fast; standby can acquire at earliest
    # witness expiry. Even a delayed primary reply never extends its deadline.
    p = ha.Policy(enabled=True)
    for witness_rate in [.99, 1, 1.01]:
        witness_real_expiry = 60 / witness_rate
        for client_rate in [.99, 1, 1.01]:
            for delivery_delay in [0, 1, 30, 59, 61, 90]:
                grant = ha.Grant('a/boot1', 1, 100000, 100060)
                at_takeover = 100000 + max(delivery_delay, witness_real_expiry) * client_rate
                assert not ha.lease_valid(grant, at_takeover, p)


def test_continuous_readiness_resets_on_lag_partition_and_incarnation_change():
    p = ha.Policy(enabled=True)
    history = ha.History()
    def tick(now, **changes):
        nonlocal history
        o = ready(now=now, heartbeat_at=0, lag_sample_at=now,
                  grant=ha.Grant('b/boot1', 2, now-1, now+59), **changes)
        history, decision = ha.advance(p, history, o)
        return decision
    assert not tick(200).would_promote
    for now in range(201, 230):
        assert not tick(now).would_promote
    assert tick(230).would_promote
    assert not tick(231, lag_seconds=400).would_promote
    assert not tick(232).would_promote
    for now in range(233, 262):
        assert not tick(now).would_promote
    assert tick(262).would_promote
    history, decision = ha.advance(p, history, None)  # unknown during partition
    assert not decision.would_promote
    assert not tick(263).would_promote
    assert not tick(300, candidate='b/boot2').would_promote
    assert not tick(301).would_promote
    # A local clock rollback invalidates continuous readiness too.
    for now in range(302, 331):
        assert not tick(now).would_promote
    assert tick(331).would_promote
    assert not tick(300).would_promote


def test_crash_simulation_promotes_only_after_timeout_grant_and_stabilization():
    p = ha.Policy(enabled=True)
    witness, old = ha.acquire(ha.Witness(), 'a/boot1', 0, 0, p)
    history = ha.History()
    promoted_at = []
    for now in range(1, 181):
        grant = None
        if now > 120:  # standby requests only after failure detector threshold
            witness, grant = ha.acquire(witness, 'b/boot1', witness.epoch, now, p)
        observation = ha.Observation(now, 0, 0, now, now, -400, grant,
                                     'b/boot1', witness.epoch, externally_fenced=True)
        history, decision = ha.advance(p, history, observation)
        if decision.would_promote:
            assert not ha.lease_valid(old, now, p)
            promoted_at.append(now)
    assert promoted_at[0] == 151


def test_stalled_monitor_cannot_count_unobserved_time_as_continuously_ready():
    p = ha.Policy(enabled=True)
    history, _ = ha.advance(p, ha.History(), ready())
    history, decision = ha.advance(p, history, ready(now=250, lag_sample_at=250,
                                   grant=ha.Grant('b/boot1', 2, 249, 309)))
    assert not decision.would_promote
    assert decision.reason == 'stabilizing'


def test_audit_failure_does_not_cache_success_or_change_authority(tmp_path):
    from types import SimpleNamespace

    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from acc.server.db import make_engine, migrate
    from acc.server.ha import Authority
    from acc.server.ha_runtime import Runtime

    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    authority = Authority(engine, 'b')
    runtime = Runtime(authority, SimpleNamespace(settings=SimpleNamespace(ha_auto_evaluate=True)))
    try:
        with authority.control.begin() as db:
            db.execute(text("CREATE TRIGGER reject_audit BEFORE INSERT ON audit_log "
                            "BEGIN SELECT RAISE(ABORT, 'test audit unavailable'); END"))
        with pytest.raises(SQLAlchemyError):
            runtime.automatic_status('alex')
        with authority.control.begin() as db:
            assert db.execute(text('SELECT count(*) FROM audit_log')).scalar() == 0
            db.execute(text('DROP TRIGGER reject_audit'))
        assert not runtime.automatic_status('alex')['would_promote']
        with authority.control.connect() as db:
            assert db.execute(text('SELECT count(*) FROM audit_log')).scalar() == 1
        assert authority.status()['epoch'] == 0
        assert authority.status()['role'] == 'standby'
    finally:
        authority.release()
        authority.control.dispose()
        engine.dispose()
