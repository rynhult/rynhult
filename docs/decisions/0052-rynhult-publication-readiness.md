# Rynhult publication readiness, held for v1.0

Date: 2026-10-24. Status: accepted task direction from the owner.

## Publication timing follow-up (2026-10-27)

The owner now plans the first **public** release as `v0.15.0-rc.1`, after the
feature freeze (approximately November 9-13), alongside `rynhult/rynhult` and
`rynhult.dev`. This supersedes the v1-only publication timing and source-job
tag restriction recorded below, not the privacy, review or separate approval
gates. The source job also selects `v0.15.0-rc.*`; other 0.x releases remain
unselected. Keep `RYNHULT_PUBLICATION_APPROVED` unset until approval.
Registry publication still needs its own approval. Decision 0073 defines the
Linux-server/browser scope and supported nodes; native apps remain post-v1.

## Context

Stage 1 named the product but retained the old personal source repository. The
owner specified `rynhult/rynhult` as the intended public repository and requested
a local publication dry run. Nothing goes public until v1.0 is fully featured.

## Decision

Prepare clone links, package and installer metadata and both default update-source
consumers for `https://github.com/rynhult/rynhult`. Retain explicit root-owned source
and update repository overrides. Keep compatibility names, data and service paths.
Use `@rynhult` and `joshuafulton/rynhult` as planned registry identities without
creating packages or changing the private frontend package into a public npm CLI.
Continue using reviewed history-free snapshots and the shared exclusion policy.
Release jobs accept v1-or-later tags only in the canonical repository, and require
`RYNHULT_PUBLICATION_APPROVED=true`. The owner leaves this repository variable
unset until the fully featured v1.0 and publication review are complete.

## Consequences

A configured source URL does not prove a repository or artifacts exist. Existing
installations without explicit overrides will select the new update source after
upgrading, so administrators must review that authority before rollout. No running
installation is reconfigured by this task. The owner must verify private reporting,
approve publication identity, provide the authoritative private denylist and complete
v1.0 feature and platform qualification before separate publication approval.

## Alternatives considered

Keeping the old personal repository in current metadata would contradict the supplied
public destination. Renaming compatibility paths or publishing placeholder artifacts
would expand this task beyond publication preparation.
