# 0014: Explicit, draining manual HA authority

Accepted for phase-2 implementation, pending its security review and PostgreSQL
qualification. Follows the owner's [approved HA design](../research/high-availability.md)
and 0008 (private decision record).

## Context

PostgreSQL transaction serialization in phase 1 does not establish control-plane
ownership. External effects and node/terminal streams also need an admission and
drain boundary. A lost database connection cannot revoke an already sent command.

## Decision

Opt in through a stable `ACC_HA_NODE_ID`; every start is standby. Store a monotonic
epoch, holder and fresh process incarnation in a singleton database row. Explicit
owner transitions use the expected epoch and audit their result. A dedicated
PostgreSQL session lock excludes running leaders. Shared activity barriers drain
requests, workers, sockets and write transactions before exclusive epoch changes.
Check SQL writes on the engine (including raw SQL), and check broker dispatch and
ASGI admission/stream sends against fresh authority. Connection loss fails closed.

A live leader or outstanding activity refuses takeover. After a crash, require
independent fencing of the former host/helpers plus an explicit owner attestation;
there is no force override of live database locks. Local broker execution is
refused on HA mains: workloads must have stable enrolled execution identities.
Nodes may cycle through pre-approved HTTPS endpoints with unchanged TLS validation.
A standby offers a minimal observation screen rather than stale workload pages.

## Consequences

Manual handover is deliberately conservative. Restart requires owner promotion;
busy work may delay demotion. Matching builds, files/secrets and edge routing remain
operator responsibilities. Epoch checks do not retract commands beyond the DB
boundary, so independent crash fencing remains mandatory. PostgreSQL, its backups
and its own HA are separate. This phase does not claim automatic/partition-tolerant
recovery or automatic replication readiness.

## Alternatives

A heartbeat timeout alone cannot fence a paused main. An advisory leader lock alone
cannot drain external effects. Concurrent active dashboards require durable effect
claims/outboxes, distributed security limits and routing beyond this phase. A force
promotion API would hide unresolved execution and helper ownership, so is omitted.

## Uncertain effects

Migration 0044 adds a durable uncertainty latch. Lost or canceled mutating broker
replies and canceled publish/tool executions block clean demotion instead of
pretending that local cancellation drained the effect. The latch survives restart
and clears only during explicit independently fenced recovery promotion. The
retained live leadership/activity locks still cannot be overridden.

Amended by the phase-2 security review: the latch blocks clean demotion but no
longer stops the holder. Otherwise one node's lost reply, or an agent stopping its
own broker, halted the entire control plane and forced the crash-attestation path.
The review also made startup migrations refuse while a main holds authority.
