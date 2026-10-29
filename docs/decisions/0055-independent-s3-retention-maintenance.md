# 0055 — Independent S3 retention maintenance

Date: 2026-10-25. Status: accepted for implementation; real-provider qualification remains an owner task.

## Context

A default 60-day COMPLIANCE lock applies at object creation, not at each restic snapshot. Deduplication reuses
old chunks. Hourly captures therefore cannot maintain the requirement of 30 days remaining on every referenced
version. The write-only main must not gain retention-administration authority. COMPLIANCE extensions cannot
be undone, so retaining unbounded history creates an irreversible storage commitment.

## Decision

Provide an opt-in, independently invoked `acc-s3-maintain` CLI. No accd import, timer, HTTP route or credential
vault entry is added. A purpose-tagged owner-held 0600 JSON identity and 0700 job directory live on a trusted
independent host. The owner must use a separate IAM principal with only repository-scoped ListBucketVersions,
GetObjectVersion, GetObjectRetention and PutObjectRetention. The exact policy in the Guide limits mode to
COMPLIANCE and remaining retention to 30–61 days. A complete list of writer key IDs is required; any match is
refused before network access. This is a key-ID guard, not proof of principal separation or effective IAM policy.

Inventory all surviving versions of valid restic config/keys/index/snapshots/data objects. This conservative
superset includes every surviving referenced version, hidden historical versions and potentially orphans.
Exclude transient locks and delete markers. Refuse partial/malformed inventories; bound rows at 100,000 and
pages at 1,000. No restic password or content decryption is needed. New concurrent writer versions have the
owner-configured default 60-day lock; deletion of non-lock versions remains independently forbidden.

Dry-run is the default and saves a complete reviewed plan before mutations. At inventory completion, propose
60-day UTC deadlines (rounded up to whole seconds) for versions below 35 days remaining. Explicit apply re-reads exact-version retention,
skips sufficient locks, writes only monotonically extending COMPLIANCE retention, and verifies read-back.
Never delete, prune, upload ciphertext, change bucket policy or bypass governance. Because it changes no
namespace or object bytes, this retention-only operation may overlap capture; pruning and version cleanup still
require provider-side writer revocation, scheduling pause and quiescence.

Bind plans to target and maintenance key ID; expire after 24 hours and refuse clock anomalies. Rate-limit at
2 requests/second by default (0.1–10 configurable), apply in bounded batches, and use a private fsynced intent/result
journal identifying the maintenance key ID (not its secret), under a single local lease. Re-read pending versions
after failures/lost acknowledgements before retrying.
Planning and apply enforce an owner-selected extra GiB-days budget (default 1,000). Reports list exact proposed
versions, added locked storage and optional owner-priced estimates; unknown prices stay unknown. Independent
job reports never grant main-side protection: only fresh authenticated capture verification does that.

Project the last version-bound minimum deadline into a retention runway: minimum deadline minus now minus
30 days. Warn on enabled targets at seven days, including Inbox, while preserving separate two-hour capture
freshness rules. Unknown/future evidence gets no runway or protection credit. Offer the existing new-target
flow preselected for S3 as a fresh-repository rotation action; it creates an independent prefix/key and requires
a new kit. Retire old maintenance and writer authority after the replacement is verified, but retain old data,
kit and recovery records until all locks and history obligations expire. Cleanup remains independent and manual.

## Consequences

The main cannot spend a maintenance key to rewrite retention, but cannot guarantee an owner actually runs the
job. Owners must monitor its independent schedule, credentials, journal, costs and clock. Superset inventory
retains orphaned versions, and old locks/probes outside the maintenance set also consume storage. A compromised
maintenance principal can repeatedly extend locks within its ceiling; budgets in this tool do not constrain
another client. Provider/account budgets and independent IAM review remain necessary. Fake S3 proves protocol
and failure behavior, not deployed AWS policy, billing, KMS, account recovery or operational qualification.

## Alternatives considered

- Store an ordinary maintenance credential in the main vault: rejected; it collapses the writer/admin boundary.
- Exact decrypted restic reference traversal: narrower costs but requires sensitive repository credentials and a
  separately qualified graph parser. Conservative all-version coverage is simpler and transparent here.
- Reupload/rewrite deduplicated chunks or auto-prune: changes namespace/history and risks restic consistency;
  outside this extension-only job. Rotation gives an explicit bounded old repository without premature deletion.
- Implicit automatic apply in accd: rejected; scheduling, irreversible cost approval and authority belong outside
  the protected main. External owner automation can explicitly apply reviewed/budgeted plans.
