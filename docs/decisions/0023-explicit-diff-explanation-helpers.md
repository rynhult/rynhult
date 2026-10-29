# 0023: Explicit diff explanation helpers

Renumbered on 2026-10-24 from 0004 to 0023.

Date: 2026-10-22

## Context

People need a reading order for large diffs, but ACC's supervisor must not
decide when to spend their usage. The Researcher profile can browse and leave
reports; those permissions are wider than this feature needs.

## Decision

Launch one native-CLI helper only after explicit agent/model confirmation,
under the requester's own account and existing launch budget. Reuse session
records, compiler hooks and the broker, but isolate its workspace and CLI home,
deny all tools, and never resume it. Capture a bounded JSON artifact and
validate references against the already-redacted displayed diff. Cache by
source session and revision. See [the design](../EXPLAIN.md).

## Consequences

Helpers are visible, attributable and stoppable. Reopening a cached revision
costs no additional usage. Failures need explicit regeneration. This is
CLI-level permission enforcement, not a new OS sandbox; CLI versions and
credential layouts require release qualification.

## Alternatives considered

- Automatic summaries: rejected because people choose what runs.
- Direct model API calls: rejected because they bypass the person's native
  CLI account and usage model.
- Launching Researcher in the source checkout: rejected because it inherits
  broader tools, project instructions and write access for reports.
