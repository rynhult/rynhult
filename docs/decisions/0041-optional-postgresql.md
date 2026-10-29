# Optional PostgreSQL with a single ACC main

Renumbered on 2026-10-24 from 0013 to 0041.

## Context

The owner's approved [HA decisions](../research/high-availability.md#owner-decisions-2026-10-22) accept PostgreSQL for company installations and retain SQLite for personal installations. This task implements the database phase only. Failover, replication and control-plane fencing remain later phases.

## Decision

Select PostgreSQL explicitly through `ACC_DATABASE_URL` and the optional `postgres` dependency extra; otherwise retain the existing SQLite path and transaction behavior. Keep the existing Alembic revision chain, changing only dialect-specific statements. Use PostgreSQL English text search over transcript rows instead of FTS5. Serialize startup DDL with a dedicated PostgreSQL session advisory lock. Serialize API write-intent transactions with a separate transaction advisory lock, use repeatable-read snapshots to reject stale background writes to concurrently changed rows, and bound background lock waits so synchronous database calls cannot hold the event loop waiting for an API broker call. Unlike SQLite, unrelated-row changes do not invalidate a snapshot: launch/resume refreshes both transaction and cached ORM authorization after broker layout waits, before resolving secrets.

Supply an offline owner-run, root-only transfer that opens SQLite read-only, requires an empty matching target, copies in foreign-key order, verifies counts and canonical per-table checksums before committing, and resets integer sequences. Copy logical transcript messages, not FTS5 storage internals. Protect standard service configuration as `root:root`/`0600`; default PostgreSQL engine connections to verified TLS (`verify-full`) unless the owner explicitly overrides the URL mode.

## Consequences

One controlling main remains mandatory. Neither advisory lock is a leadership or broker-fencing protocol. Search tokenization/ranking may differ by database. Files, keys, tmux and machine bindings do not move with database rows. PostgreSQL requires independent backup/PITR; current ACC server capture and SQLite-only main update/rollback refuse it. The test matrix starts only disposable containers and skips with a reason when the runtime or driver is unavailable.

## Alternatives considered

A URL-only change leaves SQLite SQL, FTS5, migration and concurrency assumptions broken. A new migration branch complicates upcoming revisions unnecessarily. Running multiple mains now would skip the approved command-ownership and fencing gates.
