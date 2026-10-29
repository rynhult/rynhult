# Scheduled downtime until reconnect

## Context

A backup workstation may be powered on manually after an overnight shutdown.
A fixed planned-downtime end raises an incorrect outage when it has not yet
reconnected. Decision 0049 supports weekly windows and a separate immediate
until-reconnect flag; the scheduled policy needs the same honest return model.

## Decision

Allow each weekly window to use `until_reconnect: true` instead of `end`. Reject
an omitted end without that flag, or both the flag and an end (including null).
Use the most recent selected weekday's start, looking back through the previous
week. Compare UTC instants: repeated starts use the first occurrence and nonexistent
starts move forward by the clock-change gap, matching existing fixed windows.

A recorded reconnect at or after that start consumes the occurrence. Use
`last_reconnected_at` preferentially, falling back to `last_seen` only without
reconnect history; routine heartbeats must not replace a recorded reconnect.
The next scheduled start rearms the window. An active occurrence has no expected
end (`ends_at: null`). Fixed windows and the separate immediate flag retain their
existing behavior. A fixed window can still apply after an overlapping reconnect
window is consumed.

Reuse the existing owner-only audited policy route and policy revision. Store the
optional flag in the existing JSON policy; no migration or background timer is
needed. The shared evaluator continues to govern offline alerts and backup work.

## Consequences

There is deliberately no deadline for an offline machine's return. A machine that
never reconnects remains in planned downtime until its owner changes the policy.
After reconnect, an unexpected outage outside the next planned window alerts using
the usual threshold. Backup age remains honest and no return time is invented.
Rynhult does not power on the machine or apply this policy automatically.

Older releases expect every recurring window to have an end. Convert these windows
to fixed ends or remove them before downgrading. The immediate one-shot flag is
not a substitute for a scheduled start, and making the machine a Workstation
would suppress unrelated outages too.
