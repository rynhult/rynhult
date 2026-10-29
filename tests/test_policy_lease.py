"""A partition or policy replay must never extend execution authority."""
import pytest


def prototype():
    from acc.policy_lease import ExecutionLease
    return ExecutionLease


def test_partition_stops_at_five_minutes_and_cannot_restart():
    clock = [100.0]
    stopped = []
    lease = prototype()(clock=lambda: clock[0], stop=stopped.append)
    assert not lease.running
    assert lease.renew(revision=4, digest='policy-a', valid_for=900, reconciled=True)
    clock[0] = 399.99
    assert lease.check()
    clock[0] = 400
    assert not lease.check()
    assert stopped == ['lease_expired']
    assert not lease.renew(revision=4, digest='policy-a', valid_for=900, reconciled=True)
    assert not lease.check()
    assert len(stopped) == 1


def test_renewal_and_policy_expiry_bound_execution():
    clock = [0.0]
    lease = prototype()(clock=lambda: clock[0], stop=lambda reason: None)
    assert lease.renew(revision=2, digest='a', valid_for=450, reconciled=True)
    clock[0] = 250
    assert lease.renew(revision=2, digest='a', valid_for=200, reconciled=True)
    clock[0] = 450
    assert not lease.check()


@pytest.mark.parametrize('revision,digest,reconciled', [(1, 'a', True), (2, 'b', True), (2, 'a', False)])
def test_stale_conflicting_or_unreconciled_policy_stops(revision, digest, reconciled):
    stopped = []
    lease = prototype()(clock=lambda: 0, stop=stopped.append)
    assert lease.renew(revision=2, digest='a', valid_for=300, reconciled=True)
    assert not lease.renew(revision=revision, digest=digest, valid_for=300, reconciled=reconciled)
    assert not lease.check()
    assert len(stopped) == 1


def test_new_revision_stops_frozen_launch_and_restart_starts_without_authority():
    stopped = []
    lease = prototype()(clock=lambda: 0, stop=stopped.append)
    assert lease.renew(revision=2, digest='a', valid_for=300, reconciled=True)
    assert not lease.renew(revision=3, digest='b', valid_for=300, reconciled=True)
    assert stopped == ['policy_changed']
    assert not prototype()(clock=lambda: 0, stop=stopped.append).check()


@pytest.mark.parametrize('duration', [0, -1, float('nan'), float('inf')])
def test_invalid_policy_lifetime_never_grants(duration):
    lease = prototype()(clock=lambda: 0, stop=lambda reason: None)
    assert not lease.renew(revision=1, digest='a', valid_for=duration, reconciled=True)


def test_clock_rollback_fails_closed_and_stop_failure_remains_latched():
    clock = [10.0]
    def stop(reason):
        raise RuntimeError('simulated process stop failure')
    lease = prototype()(clock=lambda: clock[0], stop=stop)
    assert lease.renew(revision=1, digest='a', valid_for=300, reconciled=True)
    clock[0] = 9
    with pytest.raises(RuntimeError):
        lease.check()
    assert not lease.running
    assert not lease.renew(revision=1, digest='a', valid_for=300, reconciled=True)
