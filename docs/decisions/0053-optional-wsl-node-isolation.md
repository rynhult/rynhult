# Optional isolation for dedicated WSL nodes

Date: 2026-10-24. Status: implemented; owner approved.

## Context

WSL agent accounts can launch Windows programs as the distribution's Windows
owner and reach mounted Windows drives. Linux account separation alone does not
remove that bridge. Some owners deliberately need Windows development tools.

## Decision

The node installer offers an explicit WSL-only opt-in, default No. It disables
interop and appended Windows PATH and restricts drive automount to root; disabling
automount entirely needs a second choice. A stdlib-only isolated Python helper
validates, backs up and atomically merges the configuration without discarding
comments or unrelated settings. Updates never change isolation. Windows shutdown
and distribution restart are manual owner actions.

The node reports effective interop and each agent broker's metadata-only
Windows-drive access check, not a conclusion inferred from pending configuration.
Missing evidence is unknown. Host warns when either exposure is reported.
Owner dismissal is a separate per-enrollment Setting and audited action; the node
cannot dismiss its own warning.

## Consequences

Dedicated nodes can reduce their Windows exposure without silently breaking
development nodes. Windows tools and drive-backed projects may stop working.
This is not per-session containment and does not constrain trusted administrators
or network access. Actual Windows/WSL restart behavior still needs owner validation.

## Alternatives

- Enforce isolation everywhere: breaks intentional Windows workflows and updates.
- Infer safety from `wsl.conf` or the node service account: pending settings and
  different agent identities make this misleading.
- Read Windows profiles or run Windows probes: unnecessary privacy exposure;
  Linux mount/access metadata is sufficient for this advisory.
