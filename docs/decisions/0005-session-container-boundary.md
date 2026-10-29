# Session container boundary

Date: 2026-10-22. **Status: approved 2026-10-22 with the answers in 0008 (private decision record); not implemented.**

## Context

Per-person agent accounts isolate people, but sessions belonging to one person
share filesystem and process authority. The parsed-command guard is a safety
net, not a sandbox. PLAN calls for rootless containers for every agent, installed
by default, without giving agents a host Docker socket.

## Proposed decision

Use rootless Podman/crun per session, retaining rootless Docker as a qualified
fallback and as the separate owner-approved services runtime. Use Linux inside
WSL2 and a narrowly shared Linux VM on macOS. Keep trusted brokers/launchers
outside; isolate session homes, Git metadata, secrets, hooks, spools and networks.
Use gVisor for qualified higher-risk workloads, not as an assumed compatible
default. Full contracts, phases and approval questions: [CONTAINERS](../CONTAINERS.md).

## Consequences

Host tools and broad home access are intentionally lost. Private Git checkouts,
credential adapters, port relays, image builds and cross-platform qualification
are prerequisites, not follow-up hardening. Hook files become immutable without
claiming arbitrary code must invoke hooks. Native opt-outs weaken same-account
protection and require an explicit owner decision.

## Alternatives considered

Rootless Docker is viable but adds daemon management. Image-free namespaces
inherit host dependency drift. Kata and Firecracker provide separate kernels
but require substantially more provisioning and integration. None is authorized
for implementation until the owner approves the design.
