# 0043: The ACC session container is the sandbox (Option A)

Renumbered on 2026-10-24 from 0014 to 0043.

Date: 2026-10-23. Status: owner selected; implemented, pending security review.

## Context

Real CLI hook/trust dispatch qualifies inside the rootless session image, but
nested bubblewrap proc mounts conflict with retained OCI proc masks, and Chrome's
nested chroot conflicts with the default seccomp profile after dropping capabilities.
The owner selected the container itself as the session sandbox.

## Decision

Only in an ACC-launched container, use Codex danger-full-access and Chrome
--no-sandbox. Image-only wrappers at the pinned binary paths check a fixed
`/run/acc-container/contract.json`. The root launcher creates that file in its
private frozen input snapshot and mounts it read-only below an immutable image
parent. It rejects an agent-supplied contract. Environment variables, workspace
files and generic container detection do not select this policy. The reader also
requires a read-only rootfs/contract, NNP, seccomp and zero capability bounding set.

The contract is authoritative within the launcher's mount namespace: the payload
cannot mount or replace it. This is not cryptographic attestation against a host
administrator/runtime operator who controls the entire namespace. The local
user-manager harness emulates the contract to test image behavior; it never
claims registered-broker/root-launcher authority from doing so.

Native launch/compiler code and installed host binaries are unchanged. With no
verified contract the image wrappers preserve the original arguments too. Codex
approval settings are retained. Outer proc masks, seccomp, dropped capabilities,
network none, immutable inputs, limits and cleanup stay unchanged.

## Qualification and consequences

The bundled bubblewrap row passes the approved policy with reason
"bubblewrap: not applicable (container is the sandbox)"; it does not claim a
nested sandbox ran. Chrome must expose a live --no-sandbox renderer retaining
outer NNP/seccomp. Native renderer validation still requires Chrome's own sandbox.
A new mandatory container_boundary row verifies fresh writes under /etc and /usr
fail with EROFS. Every mandatory row, including trusted launcher, must pass for GO.

This deliberately gives tools the whole permitted session-container view rather
than additional per-command inner isolation. It does not grant host filesystem,
network or capabilities. Security review precedes merge; the global broker flag
stays off and no production rollout is qualified by the local prototype.

## Amendment, 2026-10-26 (qualification attempt 15)

In the trusted launcher's rootless container, the root-owned rootfs and contract show as the overflow uid (65534):
host root is never mapped there. The reader and wrappers accept that uid as root's only inside a user namespace and
never as the caller's own uid; every other check is unchanged. Nested bubblewrap would need `--security-opt
unmask=ALL` (the kernel mounts a new proc only where the existing one is fully visible), which exposes host-wide
`/proc/sched_debug` and `/proc/timer_list`; that stays not selected
(`docs/security/2026-10-26-review-phase1-in-container.md`).

## Amendment, 2026-10-26 (qualification attempt 16)

The container being the sandbox doesn't replace the guard. A provider CLI whose hooks the image can't verify doesn't
start in the container. Copilot is the first such case: its machine-policy hooks are skipped there (they aren't uid
0's), and a trusted repository can switch its user-level hooks off. So the image's Copilot wrapper checks its hooks
link, the image hooks file and every settings file Copilot would obey before each start, and exits 78 with the reason
otherwise (`docs/security/2026-10-26-review-phase1-hooks.md`).

## Alternatives

Relaxing proc masks/seccomp/capabilities to support nested sandboxes was not
selected. An environment flag or repository marker would be agent-controlled and
is rejected as a policy signal. Disabling native host sandboxes is not selected.
