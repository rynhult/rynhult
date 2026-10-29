# Update identity and Mac installation

Approved scope: owner, 2026-10-22 (docs/UPDATES.md).

## Context
Version labels alone cannot distinguish stale node code from the main ACC. Linux nodes have a root helper;
Mac nodes run LaunchAgents as the signed-in person.

## Decision
Use a content SHA-256 over bundled paths, executable flags and bytes, excluding generated files and the stamp.
Embed `.acc-build` in source archives and publish a small `BUILD` release asset alongside SHA256SUMS. Compare
builds at equal stable versions, including on the main. Preserve the stamp through node installation, whose
installed tree omits the web UI. Mac owners follow an installer link rather than adding a privileged daemon.

## Consequences
Archives can be recompressed without changing identity. Nodes with old protocols remain usable with an unknown
build. A Mac needs a person's installer action; automatic following applies to Linux/WSL only.

## Alternatives
A git commit does not identify modified installed source, and an archive checksum changes with packaging metadata.
A Mac root daemon would change the existing privilege model and is unnecessary for this milestone.
