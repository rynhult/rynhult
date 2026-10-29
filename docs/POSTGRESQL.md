# Optional PostgreSQL (HA phase 1)

The [approved HA design](research/high-availability.md#owner-decisions-2026-10-22) keeps SQLite for personal installations and accepts PostgreSQL for company installations. PostgreSQL remains single-main by default. Phase 2 explicitly adds [manual active-passive authority](HIGH-AVAILABILITY.md) behind `ACC_HA_NODE_ID`; follow its provisioning and independent fencing requirements before running a standby. ACC does not configure PostgreSQL replication, automatic promotion, edge routing or session migration.

## Configuration

SQLite remains the default: without `ACC_DATABASE_URL`, ACC uses `ACC_DATA_DIR/acc.db`, with the same WAL, foreign-key, busy-timeout and write-intent behavior. Existing installs need no configuration changes.

For an owner-managed PostgreSQL deployment, install ACC's `postgres` Python extra (for example `uv sync --extra postgres` in a deployment environment), provision an empty dedicated database and set a protected service environment variable. The standard `/etc/default/accd` file must belong to `root:root` with mode `0600`; the installer enforces this before reading it, including on upgrades. Systemd reads it before switching to the `acc` service account. Do not put database credentials in an agent-readable file:

```text
ACC_DATABASE_URL=postgresql+psycopg://acc:EXAMPLE_PASSWORD@db.example.lan/acc?sslmode=verify-full
```

Application connections, startup migrations and the transfer tool default to `sslmode=verify-full` when the URL omits it: TLS is required, the CA must be trusted and the certificate must match the hostname. Use the database operator's CA configuration for certificate verification. An explicit URL `sslmode` overrides the default; modes such as `disable`, `prefer` and `require` do not provide verified server identity. Use an insecure override only for an intentionally isolated test or a separately secured connection, never as a remedy for a certificate error. URL-encode credentials. Keep connection strings out of source control, shell history, screenshots and agent prompts. The database role needs schema migration privileges; PostgreSQL operations, access control, backups/PITR and restore drills remain the owner's responsibility. PostgreSQL 17 is the test-container baseline.

The standard installer selects the `postgres` extra when its protected environment file contains a PostgreSQL URL. For any explicit database URL, its key helper refuses to create a missing `vault.key` from an unrelated local SQLite file. Restore the matching key before an upgrade or transfer. For a genuinely new, verified-empty PostgreSQL installation, provision a private 32-byte base64 vault key as the ACC service account before installation. Never mint a replacement key for populated vault data. No installer is run as part of this change.

ACC runs the same Alembic history on either backend. PostgreSQL startup migrations take a dedicated session advisory lock, released after migration; it is **not** control-plane leadership. A startup that needs to migrate also takes the main leadership lock for the migration and refuses while another running main (or a manual-HA holder) has authority. API write-intent transactions take a transaction advisory lock to preserve serialized API read/modify/write operations. Background transactions use repeatable-read isolation to reject stale writes to rows changed concurrently; this is not SQLite's global snapshot-write exclusion and does not invalidate a snapshot when a different row changes. Launch and resume finish broker layout waits, end the old transaction and expire cached ORM objects before reading vault authorization and endpoint credentials in a fresh transaction; there is no broker wait between that refresh and the pre-launch commit. API transactions retain read-committed isolation so their first data read occurs after acquiring the advisory lock. Background transactions also use a short lock timeout so a contended synchronous database call cannot freeze the event loop waiting for an API transaction across broker I/O; failed supervisor stages retry on their next pass. Optional read refreshes retain cached data on a lock timeout or serialization conflict. Other database errors propagate. This does not provide multi-main command ownership or fencing.

History search uses FTS5/Porter on SQLite and an English PostgreSQL text-search GIN index. Both scope authorization before limiting, support a final prefix, and return up to three highlighted messages per session. Tokenization, stop words, stemming, snippets and ranking can differ between engines. The PostgreSQL table retains the transcript columns used by goals, team summaries and completion checks.

## Owner-run one-time transfer

1. Stop ACC and keep it stopped throughout copying and verification. Preserve a consistent SQLite backup and all existing files and configuration. Upgrade the source schema with the matching ACC release **before** this tool; the tool never migrates or writes the SQLite source. Use the same ACC release for source and target.
2. Provision an empty, dedicated PostgreSQL database. Install the `postgres` extra in the owner-run environment. Set its URL in `ACC_DATABASE_URL` through a protected environment file or secret mechanism. Never point the tool at a shared or production database holding other data.
3. From an owner-controlled root shell in the matching trusted source checkout, run:

   ```sh
   PYTHONPATH=src python -m acc.db_transfer --sqlite /path/to/offline/acc.db
   ```

   Installed distributions also expose `acc-db-transfer`. Both real and effective UID must be root; member and agent accounts are refused before opening either database. There is no API, broker or agent-request entrypoint. Load the protected target environment in that root shell without printing it; do not grant agents sudo access or execute an agent-writable checkout as root. `--target-env VARIABLE` chooses a different environment variable; the URL is never a command-line argument. An empty target schema is migrated first. An existing target must have the same tables, columns and Alembic revision and contain no application rows. There is no destructive overwrite flag. The copy checks counts and SHA-256 checksums for every logical table before committing; any mismatch rolls back the copied rows. Failed attempts may leave the empty migrated schema and advanced PostgreSQL sequence values. Rerunning an already completed import refuses to overwrite it.
4. Save the JSON report privately. Each table has a row count and checksum; the report contains no row values. Checksums hash canonical column/value rows, sort row digests (preserving duplicates), then hash the concatenation. JSON is normalized by key order and spacing; SQL NULL and JSON null remain distinct. FTS5 shadow tables are storage internals and excluded; `transcript_fts` messages and `alembic_version` are included. Memory use for verification grows with row count (one 32-byte digest plus Python overhead per row).
5. Independently repeat verification while both installations remain stopped:

   ```sh
   PYTHONPATH=src python -m acc.db_transfer --sqlite /path/to/offline/acc.db --verify-only
   ```

   Verification exits nonzero on mismatch. Integer sequences are reset during import so later inserts do not reuse imported IDs. Source reads use SQLite `mode=ro`, `query_only` and one read snapshot; no source checkpoints, DDL or updates are issued. Keep WAL/SHM alongside an uncheckpointed source; never copy just a live SQLite main file.
6. Keep the same secrets (especially `vault.key`), catalog overlay, git repositories, run folders and machine bindings. This tool copies **database rows only**; it does not move files, processes or machine identity. Configure the single stopped ACC main to use PostgreSQL, then start it and check login, authorized history search and normal operations. Rollback means stopping ACC and deliberately restoring the old configuration/data boundary; later PostgreSQL writes are not copied back to SQLite.

## Backups and unchanged boundaries

ACC's current server-backup capture/restore and legacy SQLite backup scripts do not back up PostgreSQL. Server capture explicitly refuses PostgreSQL rather than capturing a stale `acc.db`. Disable existing ACC backup schedules before transfer and arrange independent PostgreSQL backups and file/secret backups. The Host backup UI does not certify PostgreSQL protection. Future backup work must integrate PostgreSQL capture positions and file manifests.

The main updater's **Back up and update** workflow (including scheduled installation) also refuses PostgreSQL before downloading or installing: its backup and rollback implementation only protects the default SQLite database. Disable automatic installation before switching and use an owner-managed update procedure with independent PostgreSQL backup, schema compatibility and recovery checks. A leftover SQLite file must never count as a backup of the configured PostgreSQL database. Additional-machine updates are unaffected.

Sessions, brokers, tmux, workspaces, vault key files, local catalog files, TLS/front door, git stores and execution-machine ownership stay where they were. Database selection alone adds no availability or recovery-time guarantee. No live installation is switched by this code change.

## Tests

Run `PYTHONPATH=src python -m pytest -q tests/test_db.py tests/test_postgres.py tests/test_db_transfer.py tests/test_group_migration.py`. The matrix always runs SQLite and tries a test-owned PostgreSQL 17 container through Podman, then Docker. It uses a random container name/password confined to that instance, an ephemeral loopback port, no mounts and no database URL from the environment. Cleanup removes the container. Without an accessible runtime (or the driver extra), PostgreSQL tests skip with the reason. Container startup or readiness errors after an accessible runtime is found should be investigated. Run the full Python suite as well; CI installs the extra so its Docker-capable runner can run the matrix.

## Manual HA phase 2

The single-main restriction described for phase 1 can now be lifted explicitly with
`ACC_HA_NODE_ID` on matching phase-2 builds. Follow the separate
[manual active-passive runbook](HIGH-AVAILABILITY.md): shared PostgreSQL alone does
not enable failover, replicate files, or fence external work already dispatched.
