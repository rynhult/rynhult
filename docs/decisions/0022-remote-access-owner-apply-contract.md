# Remote access owner apply contract

Date: 2026-10-24. Status: implemented preparation; not installation or public-release approval.

## Context

The approved remote-access design requires a separately reviewed mTLS hostname,
a registry-derived leaf allow list, explicit owner enablement and reliable OFF.
The pairing slice deliberately had no edge writes. The existing DNS helper
establishes a narrow root-owned IPC and atomic-file pattern; giving accd a generic
Caddy reload/configuration privilege would exceed that boundary.

## Decision

Use a separate uninstalled root helper accepting only review/apply/off/status.
Root provisions the complete public-OFF native JSON base, public hostname allow
list, dedicated device CA public certificate and qualification flag (default false).
accd supplies the selected hostname and current active leaves, never paths, commands,
raw configuration, upstreams or an admin address. The helper independently renders
the exact configuration; Apply requires the reviewed canonical SHA-256 revision.

Serialize with the existing DNS/publication Caddy lock. Verify the prior loaded
configuration, validate the candidate, write atomically, reload, then verify the
complete loaded JSON before acknowledgement. A stale base or review is refused.
OFF removes public routing/TLS policy rather than merely emptying membership, and
depends on neither the CA nor hostname nor recent owner verification. Failure
attempts OFF instead of restoring revoked leaves. Pending/unknown is durable;
never infer success from a successful write or an RPC timeout.

Keep desired intent and last verified status separately in an existing Setting.
Reconcile lifecycle changes after commit and every 15 seconds; clear enable intent
on uncertainty/restart/drift. Do not automatically re-expose the application.
Keep HA enablement blocked until that lifecycle is independently qualified.

Require the owner to compare a six-digit winning-claim-bound code shown on both
devices before signing; derive it from the existing pairing/receipt hashes so
pending claims require no schema migration or new stored secret.

## Consequences

The owner gets reviewable, separately applied changes and an always offered OFF
action. A healthy helper meets the one-minute registry propagation target; failed
infrastructure remains pending and needs private recovery. Root base composition
with DNS/publishing, immutable installation/confinement, restart loading, actual
Caddy/modules and public app-origin configuration still need deployment review.
No installation/service unit is shipped. Qualification stays false until the
remaining per-request/stream, physical-device and outside-network gates pass.
The helper is not a drop-in replacement for the running Caddyfile configuration.

## Alternatives considered

- Generic accd-supplied Caddy JSON/commands: rejected; excessive root authority.
- HTTP fingerprint filtering or an empty deprecated leaf list: rejected; weakens
  the qualified pre-ACC TLS gate.
- Restore the previous policy on failure: rejected; can re-admit revoked leaves.
- Silently retry enablement after a restart: rejected; exposure needs fresh review
  when consistency is lost.
- Rewrite unrelated live site/DNS changes from an old base: rejected; refuse drift
  and require root composition reconciliation instead.
