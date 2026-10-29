# Organization migration rollback rehearsal

2026-10-24. Phase-0 runbook; no live migration, restore, process stop or service configuration was performed.
Existing tooling: [Backups Guide](wiki/User-Guide/Backups.md),
[offline replacement recovery](wiki/Internal/Operations.md#offline-replacement-recovery),
[`backup_server.py`](../src/acc/backup_server.py), `tests/test_backup_server.py` and
`tests/test_backup_phase2_security.py`. Do not use Git project backups alone: they omit uncommitted files,
ignored files, provider credentials and native transcripts. Recovery of every machine is a separate scope.

## Before a future org-mode migration

1. An installation owner schedules a write/launch hold and records the currently deployed source build,
   Alembic revision, DB engine, capture exclusions, active machine/session inventory and backup target ID.
   Fence other controlling dashboards and avoid concurrent update/capture/migration. Stopping accd alone
   does not terminate already running agents; arrange explicit broker/session holds without losing work.
2. Capture a verified backup with existing **Host → Backups → Run now**, record its backup ID **and snapshot ID**,
   signed manifest/build, hashes and verification result independently of ACC. Export the recovery kit after
   repository verification; keep it with two company-selected recovery owners on separate protected storage.
   A kit alone is not a backup. Preserve database + matching vault.key/secrets + catalog + Git bindings together.
3. Preserve the stable-ID namespace, forward/reverse mapping, migration checkpoints and the pre-migration
   permission inventory. Save post-backup revocations/deletion tombstones in independent storage; restoring
   old grants must not resurrect a suspended user. Inventory dirty files and external provider credentials
   excluded from the backup and explicitly protect them before proceeding.
4. Run the dry-run and migration against an isolated copy. Establish acceptance counts, FK checks,
   existing ownership/group-role decisions and a frozen-launch-file hash baseline before cutover.
   Record measured capture/restore times and acceptable RPO/RTO with the owner; phase 0 does not invent them.

## Rehearsal on isolated storage

Use trusted source of the **captured build** and independently obtained pinned restic. Export the ciphertext
repository from its receiver if the main is unavailable. The existing restore command validates before installing
and refuses to overwrite a destination. Example paths/IDs below are placeholders; use protected storage:

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python -m acc.backup_server restore \
  --kit /secure/example-kit.json --target-id TARGET_ID --backup-id BACKUP_ID \
  --snapshot-id SNAPSHOT_ID --repository /secure/example-repository \
  --destination /secure/example-restored
```

1. Check signature, manifest hashes, SQLite integrity, vault decryption, catalog validation and Git bindings.
   These are existing restore validations, not tests to bypass. A corrupt DB, wrong vault key, missing payload
   or ambiguous snapshot must fail without publishing a usable replacement. Keep test output free of secrets.
2. Verify `data/RESTORE-HOLD` exists. Existing restore revokes captured browser sessions/invites and writes a
   restore audit; compare the revocation results. Startup hold must quarantine before migrations, auth, node
   links, supervisor, publishing and autostart. There is no implemented activation/promotion command.
3. On the restored **copy**, compare table counts, stable-ID reverse maps, project/placement bindings and
   frozen launch digests against the saved pre-migration baseline. Run the ID report, ownership/group tests
   and backup round-trip tests with synthetic fixtures. Verify owner-only administration, personal terminals,
   private backups, viewer denial and key grants; test renamed/reused aliases cannot inherit authority.
4. Independently inspect retained code/history, dirty-work exclusions and required redaction keys. Confirm
   new DB rows, files and commits produced after cutover have been exported/quarantined before rollback.
   A passing automated restore is not proof that all external systems or all machines are recovered.
5. Record backup/snapshot/build IDs, source/target schema, times, verification outcomes and two recovery owners'
   review outside ACC. Use **Record restore drill** only after an actual drill; it is an owner attestation.
   This phase-0 note does not record a fictitious live drill.

## Rollback decision and return to authority

**Before activation and before new org writes:** keep the migration failed/held, preserve its diagnostic copy,
restore the complete pre-migration generation with the captured binary into fresh storage, and verify mappings
and personal access before an owner deliberately returns authority. Avoid in-place schema downgrade: a DB
snapshot with a different vault key, catalog or Git namespace is not a coherent rollback.

**After activation/new work:** stop new launches/writes, reconcile reachable machines and let leases expire on
unreachable company machines before any replacement execution. Preserve all new commits/dirty work, audit
outbox/cursors, identity changes, grants/revocations and provider-side changes. Replay suspensions, tombstones,
secret rotation and holds into the restored authority before reopening; do not restart work under another
person's credentials. Preserve new evidence as an immutable migration archive. Owner review is needed if an
older personal-only binary cannot enforce these restrictions: keep quarantine or roll forward with a repair.
Restoring a backup cannot undo already delivered secrets, published endpoints or provider-side permissions.

Keep the old controller fenced, review PKI/node enrollment and rotate/revoke exposed credentials. No automatic
RESTORE-HOLD removal is prescribed. The future migration must supply an activation checklist with verified
policy acknowledgments, recovery access, audit continuity and explicit owner approval as its final step.

## PostgreSQL limit and evidence

ACC's encrypted capture/restore command is SQLite-only and deliberately refuses PostgreSQL. A company pilot
requires separately qualified PostgreSQL/native backup, file/secrets backup and the approved HA manual-standby
fencing/restore procedure. Do not restore SQLite over PostgreSQL or claim this note qualifies that dependency.

This task exercises existing synthetic recovery tests and the read-only ID report; it does not have the captured
production repository, recovery kit or authority to perform a real replacement-host rehearsal. Handoff records
the exact automated commands/results. PostgreSQL rollback, live machine reconciliation, measured RPO/RTO and
owner-held hardware recovery remain qualification work before enabling company mode.
