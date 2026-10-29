# Planned machine downtime and reconnect captures

## Context

An Always-on machine can have an externally imposed shutdown schedule. Offline and
hourly backup failures during that schedule obscure unexpected outages. Backups
must still report their actual age, and recovering a link should restore coverage
without waiting for the next hourly retry or replaying every missed job.

## Decision

Store owner-managed weekly windows, an IANA timezone and a separate until-reconnect
flag on the enrolled machine (migration 0051, following 0050). Weekdays identify
window starts; ends at or before starts cross midnight (equal times are rejected).
Intervals include their start and exclude their end. Ambiguous DST starts use the
first occurrence and ends use the second. Nonexistent boundaries shift forward by
the DST gap. Adjacent and overlapping windows merge.

Use the same evaluator for machine status, offline alerts, backup reminders and
scheduled captures. Resolve active offline/failed/stale alerts during downtime;
normal evaluation resumes outside it. Do not alter capture or verification times,
staleness, receiver security or restore-drill reminders. Existing maintenance
snoozes retain their narrower offline-alert behavior.

A real observed offline-to-online transition persists its reconnect timestamp and
ends the one-shot policy. The websocket wakes the backup worker. Under the existing
worker lease, calculate missed hourly slots from the last pre-outage capture intent
(or observed outage start for a target without history), excluding successful
captures during the outage. Queue one capture with the reconnect timestamp and
missed count, coalescing a pending capture. Its durable intent consumes that
reconnect even after completion or restart. Disabled targets do not queue work.

A successful capture's history entry carries the catch-up note, bound to the exact
snapshot ID. The card retains the last successful off-machine capture age; a local
capture is labelled separately. A recurring window offers a conditional expected
capture time, while an indefinite shutdown promises no wall-clock deadline.

## Consequences

Planned downtime pauses automatic captures and related alerts, so the stored
backup can become stale and remains visibly stale. An owner may explicitly request
a capture. Catch-up resumes the hourly cadence from its own durable request time.
ACC cannot start an offline machine. Reconnect recovery also works for unplanned
outages. Policy edits use the existing owner-only audited machine-policy route;
no additional HTTP authority surface is introduced.

## Alternatives considered

- Treat the machine as a Workstation: changes its role and does not describe an
  expected recurring outage or recover missed backup coverage.
- Reuse duration snoozes: cannot express local weekly recurrence or an unknown
  return time, and do not cover backup reminders.
- Replay every missed job: creates a reconnect burst with no historical captures
  to recover; one current capture restores current coverage.
