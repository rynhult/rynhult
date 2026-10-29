# 0044: S3-compatible encrypted backup targets

Renumbered on 2026-10-24 from 0011 to 0044.

Date: 2026-10-24. Follows backup decisions 0008, 0032 and 0039.

## Context

Remote Linux receivers preserve append-only history but do not prove a timed immutable window. S3-compatible
stores differ in Object Lock and versioning semantics; a provider name or owner checkbox cannot qualify protection.

## Decision

Use pinned restic 0.19.1's native S3 backend with explicit region, path lookup and TLS verification. The pinned
minio-go client refuses HTTP redirects. Owner routes accept a fixed endpoint/bucket/region and write-only writer
fields; credentials are purpose-bound `s3` / `backup-writer` infrastructure ciphertext, never general vault keys.
Each installation/target gets a unique prefix, encryption key and bound repository ID. Job-scoped 0700/0600
credential scratch is removed on exit or startup reconciliation; subprocesses receive no secrets in argv and
inherit no ambient cloud credentials, proxy or home configuration. Only restic ciphertext reaches storage.

Reuse capture, hourly scheduling, durable jobs, readback, history and snapshot-ID restore validation. Object Lock
probes use disposable valid encrypted snapshots. Observe versioning, default COMPLIANCE lock, actual probe
retention and AccessDenied on ordinary/version/bypass deletion. After capture inspect all current repository
objects (paginated, bounded to 100,000), excluding transient locks. Claim protection only for a fresh verified
capture and a fresh complete retention check with at least 30 days remaining on every object. Never infer a
retention window from versioning or governance alone. Read-denied/unsupported APIs permit encrypted backups,
with no protection claim. The minimum deadline is reevaluated as time passes.

Separate maintenance identities remain outside ACC, consistent with the enrolled receiver boundary. All remote
prune requests are refused, even persisted requests at worker dispatch. Independent maintenance can extend
retention or expire/prune after the lock window. Kit export contains independent provider-account recovery and
version-aware ciphertext-export instructions, never the writer. Writer replacement disables scheduling and
invalidates setup/protection evidence before updating the sealed credential. DNS credential routes cannot
manage backup keys. Setup, replacement, checks, capture/restore requests/results and kit export are audited.

## Consequences

COMPLIANCE locks protect versions even against administrator deletion, but allow overwrites. Recovery may require
restoring original versions through independent provider tools. Least privilege remains an independently reviewed
provider policy; finite probes are not a proof of all IAM privileges. Old deduplicated chunks must retain the full
remaining window; default retention alone cannot guarantee that forever. Independent retention extension or
repository rotation is necessary. Retained probes and lock versions cost storage. Every capture already performs
full repository readback; version-bound retention inspection adds a HEAD and explicit-version retention read per
current object, both before and after authenticated readback/restore.

Security review correction (2026-10-24): current-object retention observed only after readback can belong to a
replacement rather than the authenticated ciphertext. Require identical complete key/version inventories around
readback, version-specific COMPLIANCE deadlines, at least 30 days remaining at completion, and new version-bound
result evidence before protection qualifies. This relies on the independently reviewed writer policy denying
non-lock version deletion and retention overrides, and on maintenance not running concurrently. Existing unbound
receipts deliberately lose protection until a new successful capture.

No real cloud accounts are used for qualification. Recorded protocol responses cover fail-closed observations;
optional disposable MinIO tests cover native ciphertext capture/recovery when rootless Podman can start. Provider
production conformance and the following security review remain separate from these tests.

## Alternatives considered

Versioning alone retains old versions but proves neither a nondeletable window nor least-privilege keys.
Accepting an owner attestation or provider brand as protection would overclaim. Keeping prune credentials on the
main would defeat independence after main compromise. A custom transport or encryption format adds unnecessary
security surface; the pinned native backend and existing authenticated archive/restore machinery suffice.

Primary references: [restic S3 setup](https://restic.readthedocs.io/en/v0.19.1/030_preparing_a_new_repo.html#amazon-s3),
[minio-go redirect refusal](https://github.com/minio/minio-go/blob/v7.1.0/api.go),
[AWS Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html),
[delete markers and Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html).

Owners can pause hourly scheduling before independent maintenance. Pausing is audited and leaves already queued
or running work visible; wait for that work to finish before maintenance. Re-enable through the existing kit and
verified-capture gate.


Amendment (2026-10-25): [decision 0053](0055-independent-s3-retention-maintenance.md) adds the off-by-default
independent-host retention-only CLI, with credentials outside accd and its ordinary vault. Its monotonic
exact-version extension may overlap captures; the pause/provider-revocation/quiescence requirement remains
for namespace-changing prune and version cleanup. It does not grant protection credit without a fresh capture.
