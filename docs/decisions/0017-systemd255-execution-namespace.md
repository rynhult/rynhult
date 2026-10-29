# 0017: Create the protected execution namespace in the root guard

## Context

Decision 0016 binds execution to a held root-owned rootfs and requires a
root-owned full identity parent user namespace. Systemd 255 on Ubuntu 24.04
cannot supply `PrivateUsers=full`; container launches therefore refuse startup.
An agent-owned rootless namespace is not an equivalent security boundary.

## Decision

Keep the held rootfs and private storage. Start the protected Python guard as
root in `/`, with `PrivateUsers=no` and command environment expansion disabled.
The launcher validates the guard file and every ancestor against unsafe
ownership, directory symlinks and write access. The guard creates a user namespace as root, uses a short-lived mapper in the
parent namespace to write full identity maps through a held proc descriptor,
and reaps it before dropping to the agent. It checks the namespace owner/maps,
clears supplementary groups, drops all saved/real/effective IDs and verifies
filesystem IDs and zero active/inheritable/ambient capabilities before exec.
All errors refuse execution; no account namespace/store fallback exists.

The root service needs explicit cgroup-v2 delegation to the agent. Validate the
exact slice/unit path, hold all directories and delegation controls without
symlinks, and change only the unit directory and `cgroup.procs`, `cgroup.threads`,
`cgroup.subtree_control`. Resource ceilings and ancestors stay root-owned.
This supersedes only 0016's systemd 258 dependency, preserving its trust boundary.

## Consequences

Kernel 6.8 supplies the required APIs; systemd 255 can provide the existing held
mount staging. The protected guard now includes a brief root bootstrap and
mapper. Owner deployment must update it with the launcher and rerun live
qualification. Non-root tests verify protocol, rejection and launch contracts;
they cannot establish live Podman/AppArmor/mount/cgroup interoperability.
Root must still keep published trees unchanged while sessions are active.

## Alternatives considered

Idmapping, a held overlay lowerdir, tree hashing and crun's state-directory
option do not independently isolate an agent-owned runtime namespace from the
same host UID. A dedicated per-session system UID is sound with the held tree,
but requires account/range provisioning and changes to workspace access,
identity, grants, cleanup and auditing. See the full candidate analysis and
primary sources in [the research note](../research/2026-10-23-ct05-systemd255.md).
