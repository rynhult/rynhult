# 0070: Container team merges stay inside the session boundary

Date: 2026-10-25. Status: implemented behind the default-off container gate;
owner runtime qualification pending.

## Context

Container helpers need standalone Git administration. The broker must not run
Git writes against mutable container checkout metadata. Existing native team
merges assume linked worktrees, while container launch inputs are frozen and
team replies arrive after launch.

## Decision

Local container leads launch every descendant through the existing container
grant and rootless launcher path. Seed helpers from bounded metadata snapshots.
Bind the account-owned private team reply directory read-only into the payload,
with its descriptor held by the launcher. Keep all launch inputs frozen.

The merge tool prepares a bounded bundle in that directory. It explicitly returns
`prepared=true, merged=false`; the lead imports and merges inside its container,
then explicitly finishes the helper after verification. Do not perform host Git
writes or automatic archival on preparation. Native teams keep existing behavior.

Decision 0043's outer-container sandbox policy remains. The image explicitly
disables Claude's inner sandbox too, with the same retained proc-mask/seccomp
boundary and an entrypoint that requires the verified launcher contract.

## Consequences

Container team agents receive instructions for the extra merge step. Remote
container helpers and automatic container merges remain unavailable. Rebuild and
review a new pinned image containing the stdio team helper; qualify it with real
providers and the trusted launcher before enabling containers in production.

## Alternatives

Host-side merges would require a new race-safe Git write boundary. Mounting the
entire mutable run folder would expose launch inputs to replacement. Relaxing
outer container restrictions for nested sandboxes contradicts decision 0043.
