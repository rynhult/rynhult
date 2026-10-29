"""Pure, dry-run lease model. Not a durable witness or an election service.

Times are monotonic seconds in the caller's domain. Witness expiry uses only
witness time; grant.requested_at must be recorded by the client BEFORE sending.
Transport must authenticate holder/incarnation, CAS epoch, and commit durably
before returning a grant. No model result authorizes production side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    heartbeat: float = 5
    timeout: float = 120
    lease: float = 60
    drift: float = .01  # bound on each monotonic clock's fractional rate error
    max_lag: float = 300
    sample_max_age: float = 10
    stabilization: float = 30
    cooldown: float = 300

    def __post_init__(self):
        values = (self.heartbeat, self.timeout, self.lease, self.drift, self.max_lag,
                  self.sample_max_age, self.stabilization, self.cooldown)
        if (not all(isfinite(v) for v in values)
                or not 1 <= self.heartbeat <= 30 or not 30 <= self.lease <= 300
                or self.lease < 3 * self.heartbeat
                or not 0 <= self.drift <= .01 or not 0 <= self.max_lag <= 300
                or not 1 <= self.sample_max_age <= 30
                or not 30 <= self.stabilization <= 600 or not 300 <= self.cooldown <= 3600
                or not max(3 * self.heartbeat, self.lease + 2 * self.heartbeat) <= self.timeout <= 900):
            raise ValueError('Unsafe automatic failover bounds')


@dataclass(frozen=True)
class Grant:
    holder: str
    epoch: int
    requested_at: float
    expires_at: float  # client-local requested_at + duration, never witness wall time


@dataclass(frozen=True)
class Witness:
    epoch: int = 0
    holder: str | None = None
    expires_at: float = 0
    observed_at: float = 0


def acquire(state: Witness, holder: str, expected_epoch: int, now: float,
            policy: Policy) -> tuple[Witness, Grant | None]:
    """Atomic witness transition model; call only after authentication and durable CAS.

    Expired incarnations cannot renew an old epoch. Acquiring even the same
    identity after expiry increments epoch. Regressing witness clocks fail closed.
    The adapter replaces requested_at with the pre-request client clock value.
    """
    if not holder or not isfinite(now) or now < state.observed_at or expected_epoch != state.epoch:
        return state, None
    if now < state.expires_at and holder != state.holder:
        return state, None
    epoch = state.epoch + (now >= state.expires_at)
    next_state = Witness(epoch, holder, now + policy.lease, now)
    return next_state, Grant(holder, epoch, now, now + policy.lease)


def lease_valid(grant: Grant, now: float, policy: Policy) -> bool:
    # Slow client clock and fast witness clock are the worst combination.
    # Reserve another heartbeat for dispatch/drain; equality is already expired.
    duration = grant.expires_at - grant.requested_at
    deadline = grant.requested_at + duration * (1-policy.drift)/(1+policy.drift) - policy.heartbeat
    return (all(isfinite(v) for v in (now, grant.requested_at, grant.expires_at))
            and 0 < duration <= policy.lease and grant.requested_at <= now < deadline)


@dataclass(frozen=True)
class Observation:
    now: float
    heartbeat_at: float
    lag_seconds: float | None
    lag_sample_at: float
    stable_since: float
    last_transition_at: float
    grant: Grant | None
    candidate: str
    epoch: int
    externally_fenced: bool = False
    uncertain: bool = False


@dataclass(frozen=True)
class Decision:
    would_promote: bool
    reason: str


def evaluate(policy: Policy, observation: Observation | None) -> Decision:
    """Explain the first blocking gate. Caller maintains continuous stability history.

    Lag means measured replication replay delay, not time since the last write.
    Heartbeats must be authenticated and sequenced; replay must not refresh them.
    Missing telemetry is unknown, never a positive failure observation.
    """
    def blocked(reason):
        return Decision(False, reason)

    if not policy.enabled:
        return blocked('disabled')
    if observation is None:
        return blocked('telemetry_unavailable')
    o = observation
    timestamps = (o.heartbeat_at, o.lag_sample_at, o.stable_since, o.last_transition_at)
    if (not isfinite(o.now) or any(not isfinite(t) or t > o.now for t in timestamps)
            or (o.lag_seconds is not None and (not isfinite(o.lag_seconds) or o.lag_seconds < 0))):
        return blocked('invalid_observation')
    if o.now - o.heartbeat_at <= policy.timeout:
        return blocked('heartbeat_recent')
    if o.lag_seconds is None:
        return blocked('lag_unknown')
    if o.now - o.lag_sample_at > policy.sample_max_age:
        return blocked('lag_stale')
    if o.lag_seconds > policy.max_lag:
        return blocked('lag_exceeded')
    if o.grant is None:
        return blocked('witness_unavailable')
    if o.grant.holder != o.candidate or o.grant.epoch != o.epoch:
        return blocked('grant_mismatch')
    if not lease_valid(o.grant, o.now, policy):
        return blocked('lease_expired')
    if o.uncertain:
        return blocked('uncertain_effects')
    if not o.externally_fenced:
        return blocked('external_fencing_unverified')
    if o.now - o.stable_since < policy.stabilization:
        return blocked('stabilizing')
    if o.now - o.last_transition_at < policy.cooldown:
        return blocked('cooldown')
    return Decision(True, 'ready')


@dataclass(frozen=True)
class History:
    """Local continuous-readiness state; restart with empty history."""
    identity: tuple[str, int] | None = None
    healthy_since: float | None = None
    observed_at: float | None = None


def advance(policy: Policy, history: History,
            observation: Observation | None) -> tuple[History, Decision]:
    """One simulated monitor tick. Every unhealthy/unknown tick resets readiness.

    last_transition_at must come from durable transition history in a future
    adapter. Gaps beyond sample_max_age discard continuous readiness.
    """
    from dataclasses import replace

    if observation is None:
        return History(), evaluate(policy, None)
    o = observation
    if history.observed_at is not None and o.now < history.observed_at:
        return History(), Decision(False, 'invalid_observation')
    if history.observed_at is not None and o.now - history.observed_at > policy.sample_max_age:
        history = History()  # a stalled monitor cannot establish continuous readiness
    # Test the non-stability gates first; a caller-provided stable_since is never
    # trusted to preserve readiness across a partition or a changed incarnation.
    baseline = evaluate(policy, replace(o, stable_since=o.now-policy.stabilization))
    identity = (o.candidate, o.epoch)
    if not baseline.would_promote:
        return History(observed_at=o.now), baseline
    since = (history.healthy_since if history.identity == identity
             and history.healthy_since is not None else o.now)
    return History(identity, since, o.now), evaluate(policy, replace(o, stable_since=since))


def dry_run_status(policy: Policy, decision: Decision) -> dict:
    """A model-ready decision remains an explicit refusal at the adapter boundary."""
    return {'evaluation_enabled': policy.enabled, 'would_promote': False,
            'promotion_wired': False, 'gates_ready': decision.would_promote,
            'reason': 'promotion_unwired' if decision.would_promote else decision.reason}
