# Backup phase one: local restic and offline quarantine

Renumbered on 2026-10-24 from 0009 to 0032.

Accepted implementation decision, 2026-10-22; follows the approved backup-server design and decision 0008.

## Context

Recovery must be testable before shipping a network receiver. The existing daily backup encrypts only secrets,
uses date directories and is also used by updater rollback; it remains a separate compatibility path.

## Decision

Use official restic 0.19.1 Linux amd64/arm64 binaries with SHA-256 pinned in `acc-packages`, downloaded over HTTPS
and installed atomically. Never install the distribution package. Upstream checksums are anchored in trusted
ACC code, rather than trusting a checksum supplied alongside an arbitrary download.

Phase one transports an opaque USTAR capture through restic's stdin backend. Restic still deduplicates and
authenticates its chunks, encrypting database, secrets, catalog, git and signed manifest together. Restore dumps
that one object to bounded private scratch, validates raw regular-file headers before parsing/extraction, verifies
an Ed25519 manifest anchored in the owner's kit, and validates every file before publishing a fresh tree.
This keeps untrusted filesystem extraction out of restic and never executes restored hooks/configuration.

Local filesystem leases coordinate DB transactions/migrations, vault creation, catalog edits, git receive/ref
updates/orphan moves and capture. Git uses a conservative tree-wide lease; manual git maintenance must use it too.
A separate whole-capture/updater lease prevents source/build replacement after the short DB barrier releases.
Already-current restarts skip the migration write lease; pending migrations still serialize.
These leases coordinate one main on one filesystem and provide no multi-main fencing. Updates hold a durable
marker across service restarts; an abandoned marker blocks capture until an operator reviews the update.

Directory targets are provisioned locally, never from an API-supplied destination. Owner API actions use durable
DB intents and one leased worker. Interrupted running work fails closed for owner review; it does not advance
capture verification or silently delete retained staging. A restored installation serves only quarantine HTTP 503,
before migrations or normal initialization. There is no automatic activation or promotion command.

## Consequences

Full restic data checking and isolated validation run after every capture, conservatively reading older
referenced chunks too. This is slower than incremental readback but provides a useful phase-one correctness gate.
Private staging has a 20 GiB cumulative payload ceiling and a 64 MiB free-space reserve; archive and verification
scratch need additional working space. Failed uploads keep staging for operator review. Plaintext unlinking is
not secure erasure: use encrypted scratch storage.

Local/mounted repositories are mutable by their administrator and the main's service account. They always show
**backed up, not protected**; enabling a schedule cannot assert immutability. Only owners can request prune, and
it requires a verified generation and a fresh repository check. Separate maintenance identities, immutable
retention and provider conformance belong to phase two. No NFS/SMB/S3 or enrolled receiver is qualified here.

## Alternatives

Native restic filesystem extraction would need another hostile-tree safety boundary. Custom encryption or an
archive-only encryption tool would make ACC own cryptographic format and deduplication. Neither is needed.

Upstream references: [restic 0.19.1 release and binary checksums](https://github.com/restic/restic/releases/tag/v0.19.1),
[official binary verification](https://restic.readthedocs.io/en/stable/020_installation.html).
