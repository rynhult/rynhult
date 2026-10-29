# Trusted container launcher — owner approval note

**Implemented 2026-10-23** after the owner's answers (fixed contract approved; limits configurable per account,
owners unlimited unless set; audit kept with the main audit log). What was built, and the owner's qualification run,
are in [CONTAINERS](CONTAINERS.md#trusted-launcher-built-2026-10-23); the authorization mechanism is decision
[0038](decisions/0038-trusted-container-launcher.md). The note below is kept as approved.

Documents only, 2026-10-23. Supplements [CONTAINERS](CONTAINERS.md) and decisions
[0005](decisions/0005-session-container-boundary.md) / 0008 (private decision record);
it does not reopen the container design. The owner has approved a small root
launcher outside the broker, agent user managers with linger for qualification,
and a later scheduled AppArmor restriction change with rollback.

**Trigger:** the supplied Phase 0 result is rootless Podman's
`newuidmap: write to uid_map failed`: broker children inherit
`NoNewPrivileges=yes`. Keep that broker setting. This checkout lacks the Phase 0
results, `src/acc/broker/container_prototype.py`, `scripts/container_qualification/`
and `scripts/qualify_containers.py`; their implementation and CLI options could
not be reviewed. Restore those artifacts before executing the runbook below.

## Request and authority

Use a root-owned system service with a bounded, versioned Unix-socket protocol,
not sudo access or an arbitrary `systemd-run` wrapper. Requests have exactly
`version, request_id, operation, session_id, generation, policy_id`; operations
are launch, status and stop. A protected control-plane record supplies account,
image digest, mount identities and limits. The broker cannot supply argv, units,
environment, paths, registry URLs, runtime options or systemd properties.

Only the trusted ACC control plane may authorize a session record. The helper
checks socket peer credentials and the registered agents-role broker identity,
and binds that identity to the authorized record/account. **UID alone cannot
identify a broker:** today's broker and agent payload share an account. Ship a
separate broker control identity or an equivalent protected, session-bound
control-plane authorization channel before enabling this endpoint. Agent
processes, hooks, services-role brokers and other accounts cannot mint grants.
As with the packages/accounts helpers, structured input narrows root authority;
a label saying “approved” from a writable request is not proof of approval.

Validation is independent of the broker: strict schema/types/size and ID bounds;
existing registered `*-agent` account with agents-role root-owned configuration,
nonzero UID, no administrative groups and disjoint subordinate UID/GID ranges;
approved local image by digest/signature and policy version, never a mutable tag
or launch-time pull. Root-owned helper, policy, entrypoint and executable paths
and their ancestors must be protected from non-root writes; use fixed absolute
argv and a clean environment. Never import code from a worktree.

Derive mount sources from authorized session records. Open every ancestor and
source without following symlinks, hold descriptors through setup, and reject
substitution, unexpected ownership/types and overlap with forbidden roots.
Allow only the session-private checkout/home, immutable inputs and secret tmpfs;
never host homes, shared Git administration, runtime/broker sockets or device
wildcards. Enforce the existing namespace, network, capability and seccomp
contract. Select numeric CPU/memory/PID/disk limits from owner policy, reject
missing/out-of-range values, and reserve aggregate account capacity atomically.
Missing enforcement is a refusal. Return bounded structured errors with stable
reason codes; reject unknown fields, stale generations, conflicting retries and
unauthorized stop/status without creating a unit. Exact retries are idempotent.

## Processes, immutable inputs and recovery

Preferred production path: root asks PID 1 to create a **service**, with fixed
`systemd-run --uid=<validated-account> --property=Delegate=yes` arguments and a
helper-derived unit name under an account-budget slice. Podman, crun, conmon and
payload run as the agent, never root. This is a new unit, outside the broker's
process/cgroup ancestry; mapping helpers may establish subordinate mappings.
Apply no-new-privileges to the container payload after setup, not to the mapping
stage. Verify actual cgroup-v2 delegation and accounting, rather than assuming
`Delegate=yes` enforces limits. The parent slice enforces the person's aggregate
budget; each service enforces the session budget and whole-cgroup termination.

Qualification may instead use `systemd-run --user` through the lingering agent
user manager. Its units must sit beneath an enforced account budget too; user
managers are not a production authorization boundary and agents get no launcher
socket inside containers. Both paths must survive broker restart and retain
runtime monitor ownership. Linger alone does not remove inherited NNP from a
process launched directly by the broker.

Use pinned guard/hook/request binaries and dependencies in the read-only image;
bind effective provider hooks, policy and launch settings read-only with
read-only parents. Mutable CLI state and credentials live separately. Keep hash
checks, drop mount capabilities, and qualify attempted chmod/unlink/replacement,
real pre-tool denial and post-tool delivery for each required CLI. Immutable
files do not force arbitrary programs to invoke hooks; unsupported mandatory
hook configuration fails qualification, as the parent design requires.

Root-owned durable records bind request/session/generation, unit, container ID,
account, image/policy digest and exit state. Journal and ACC audit record caller,
validation refusal, launch/stop, OOM, reconciliation and cleanup outcomes with
correlation IDs; redact runtime diagnostics and never log keys, secret contents,
payload environment or provider transcripts. Bound request queues, logs and
retention. Container events remain untrusted input.

On partial start, timeout or crash, stop the recorded unit's entire cgroup,
remove only its verified container, release relays/leases and aggregate capacity,
and tear down credentials/tmpfs with retry. Reconcile records against units and
containers after helper/broker restart or reboot; quarantine unknown or mismatched
objects rather than deleting by a caller's name. Do not report stopped until
verified; failed cleanup remains visible and blocks conflicting relaunches.
Preserve private checkout/state and dirty work. Never fall back to host execution.

## AppArmor window and owner-run qualification

First stage root-owned, executable-specific user-namespace profiles for the
resolved Podman, crun, newuidmap **and newgidmap** paths, checking profile
transitions and required mapping-helper privileges. A `userns,` allowance in an
Ubuntu-style `flags=(unconfined)` profile grants namespace use, not general
confinement; the container payload needs its separate policy. No broad path
wildcards over writable trees, global unconfined runtime or sandbox-disabling flags.

Known consumers from repository deployment rules are Chrome for Testing,
Codex's bundled bubblewrap, and the owner's rootless Docker stack (RootlessKit,
plus its selected runtime/network helpers). Check existing Chrome/Codex profiles
and distro Docker/RootlessKit profiles, including resolved installed paths.
Inventory other installed browsers/Electron apps, standalone bubblewrap/Flatpak,
Podman networking helpers (pasta/slirp4netns), and any nested runtime actually
used. These are candidates, not a verified machine inventory. Owner must trace
representative starts and inspect AppArmor denials to determine which executable
creates namespaces and needs a narrow profile. Do not enable the sysctl until
that inventory, existing services' smoke tests and rollback access are ready.

Owner runs these steps in a maintenance window from a retained root console;
this documentation task runs none of them:

1. Restore/review the missing Phase 0 files. In the repository root, inspect
   `scripts/qualify_containers.py --help` using the project Python below and its
   README; resolve required options before proceeding. The final invocation
   below assumes its default qualification suite is supported.
2. Substitute the enrolled account for the fictional `alex-agent`; record
   `loginctl show-user alex-agent -p Linger` and the current sysctl value. Then:

   ```sh
   sudo loginctl enable-linger alex-agent
   loginctl show-user alex-agent -p Linger -p State
   sudo systemctl status user@$(id -u alex-agent).service
   PYTHONPATH=src /srv/acc/app/.venv/bin/python scripts/qualify_containers.py --help
   PYTHONPATH=src /srv/acc/app/.venv/bin/python scripts/qualify_containers.py
   ```

   Confirm the harness actually launches via the user manager/trusted launcher,
   not directly beneath the NNP broker; otherwise stop and adapt the harness.
   Save baseline evidence, delegated cgroup paths and UID maps. A missing script
   or unsupported invocation is a stop, not a passing qualification.
3. Record `sysctl -n kernel.apparmor_restrict_unprivileged_userns` and back up
   existing affected profiles and any persistent sysctl override. After the
   reviewed runtime profile has been delivered as `/etc/apparmor.d/acc-podman`,
   and all inventory profiles are loaded, run:

   ```sh
   sudo apparmor_parser -r /etc/apparmor.d/acc-podman
   sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=1
   PYTHONPATH=src /srv/acc/app/.venv/bin/python scripts/qualify_containers.py
   sudo journalctl -k --since '15 minutes ago'
   ```

   Require rootless mappings, enforced individual/aggregate limits, isolation,
   immutable hooks, the owner-selected container sandbox boundary, restart/stop cleanup and no
   relevant AppArmor denials. Smoke-test the owner's existing Docker services
   and every inventoried consumer. A prototype-only pass does not qualify the
   full product; retain its explicit skips and failures.
4. On success, owner persists `kernel.apparmor_restrict_unprivileged_userns=1`
   in a reviewed `/etc/sysctl.d/` file, checks precedence, then verifies after a
   planned reboot. On failure, stop new qualifications, restore the **recorded**
   sysctl value with `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=<previous>`,
   restore the prior persistent override and profile files, and reload those
   profiles. Unload only newly introduced profiles after stopping their test
   workloads. Recheck existing services. If linger was previously off, remove
   test units and use `sudo loginctl disable-linger alex-agent` only after
   confirming no required user workloads depend on it. Preserve failure logs.

## Effort and remaining approval

Budget 3–5 engineering days for the narrow helper/protocol and lifecycle tests,
plus 1–2 days for inventory, profiles and owner-assisted qualification; this is
part of the existing Linux phase, not a replacement for its integration gates.

Owner review: approve the protected broker authorization mechanism and fixed
launch contract above; select concrete account/session limits and audit
retention; name the maintenance window/operator and accept the rollback gate.
The helper, linger qualification and staged AppArmor approach are already
approved. No machine changes or runtime implementation are made by this note.


## Option A immutable launch contract

Owner decision 0043 makes the ACC-launched container the sandbox. After bounded
hash-checked input copying, the helper rejects an agent-supplied
`.acc-launch-contract.json`, creates its own root-private copy with session and
generation metadata, then mounts it read-only at `/run/acc-container/contract.json`.
The destination parent is immutable image content. Existing proc masks, seccomp,
dropped capabilities, network none, immutable run inputs and writable-output
split are unchanged; this adds only a fixed readonly metadata bind.

Image-only wrappers for the pinned Codex/Chrome executables verify that fixed
contract and the outer restrictions. They select Codex danger-full-access and
Chrome --no-sandbox only in that context. No environment flag, repository
configuration or generic container marker selects the policy. Native binaries
and compiler paths are unchanged. This namespace-local authority is not remote
attestation against a host administrator controlling the namespace.

The mandatory container_boundary qualification must refuse outside-mount writes
with EROFS. Bubblewrap is explicitly not applicable; Chrome's live renderer must
retain outer NNP/seccomp. All mandatory rows, including the actual registered
broker/trusted launcher, still gate GO. Local prototype emulation of the contract
does not prove root authority. Security review follows before merge.

Security review (docs/reviews/2026-10-23-security-review-containers-phase-1.md):
the wrappers also require loopback as the only interface (CT-04) and force Codex
hooks on (CT-01); `container.launch` records `sandbox=container` (CT-06). The
image must resolve exclusively to root-owned layers in the read-only
`/var/lib/acc/session-images` additional store (CT-05). An account-local copy of
the pinned ID is refused. The owner alone imports a checksum-pinned archive after
verifying its layer/config digests and records the expected image ID in root
policy. See [exact owner commands](../deploy/containers/ROOT-IMAGE-STORE.md).
To bind verification to execution, the owner also publishes a protected flattened
tree and image-ID marker under that store. The launcher holds its directory
descriptor, stages it read-only through PID 1, and uses `podman --rootfs`, not a
second image-ID lookup in the agent's writable store. Rootfs mode supplies the
reviewed environment and entrypoint explicitly and never uses `:O`. Missing,
unprotected or mismatched trees fail closed, without image-mode fallback.
Execution additionally requires systemd 258+'s root-owned `PrivateUsers=full`
namespace, checked by an installed guard, and fresh namespace-private Podman
storage/runtime tmpfs. No weaker retry is permitted. Keep-id maps the host agent
to the image's existing UID/GID 1000, avoiding writes to the protected passwd file.
The mandatory host qualification denies direct and `podman unshare` writes and
compares the actual process-root inode with the protected tree, rejecting
keep-id copies into writable storage and old image-lookup proofs; root-store runtime qualification
has not been performed in the implementation session.
