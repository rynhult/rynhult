# 0015 — Root-owned session image store

## Context

An image ID pins OCI content at import, but does not authenticate unpacked files
on every run. A native agent process can change files in its own rootless store,
including files owned by subordinate ids through `podman unshare` (CT-05).

## Decision

Only the owner imports a checksum-pinned reviewed OCI archive into
`/var/lib/acc/session-images`, using root's mappings and removing ACLs and all
non-root write access. Verify manifest/config and layer digests including diff
IDs before import; pin the expected config digest in root-owned launcher policy.
Rootless session Podman uses the protected additional image store read-only.
The launcher inspects resolution and rejects image layers outside that store.
The owner captures a separate host probe as the agent: no direct write opens,
no unshare write opens, and no keep-id copy into writable runtime lowerdirs.
A fresh root-protected proof is mandatory for qualification GO.

## Consequences

Image publication is an owner maintenance action, separate from unprivileged
builds. A rootless-store copy of the same ID is refused. A runtime that needs
writable copies of image layers fails qualification until its storage/keep-id
support is qualified; the account store is never a fallback. This implementation
session performs no root installation or real protected-store runtime test.

## Alternatives considered

Verifying only the image ID leaves unpacked files writable. Rehashing every
unpacked file at launch adds cost and still permits changes after verification.
Trusting a payload's immutability report cannot demonstrate host-side access.
