# 0016: Bind session execution to a held protected root

## Context

CT-05's preflight inspection of a pinned image ID does not bind a later
`podman run IMAGE_ID` lookup. An agent can introduce a modified same-ID image
between inspection and execution. Another inspection cannot remove this race.

## Decision

Keep the protected additional image store and archive/layer verification. During
owner-only installation, materialize an independent flattened rootfs from that
verified image, protect its ownership/permissions, and publish a matching
image-ID completion marker. The launcher validates and holds that directory,
binds its descriptor read-only into systemd staging, and runs plain `--rootfs`
without `:O`. No writable-store image lookup selects the execution filesystem.
The transient service requires systemd 258+'s `PrivateUsers=full` plus fresh
namespace-private runtime/storage tmpfs. A root-owned guard verifies the actual
user namespace, full identity mappings and root ownership before Podman starts.
This excludes the hostile host UID from runtime proc aliases and prevents
re-entry into an account-controlled pause namespace.

## Consequences

Existing installations need the new tree and fresh host qualification; missing
or mismatched trees refuse launch. Rootfs mode requires explicit environment,
entrypoint and user settings. Qualification checks the actual process-root inode
and direct/unshare write denials. Real keep-id/rootfs operation still requires
owner qualification before production enablement. Older runtimes fail closed;
there is no downgrade to a boolean `PrivateUsers` mode or shared account state.

## Alternatives considered

Repeated preflight inspection leaves a race. A separate writable graphroot or
`--imagestore` alone does not establish source integrity against the same UID.
An explicit storage transport has not been demonstrated to avoid local import
or resolution in Podman 4.9.3.
