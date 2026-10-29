---
name: migration-recovery
description: Plan and verify engine-aware schema/data changes with backup and restore evidence and an explicit rollback or forward-fix path.
---

# Migration and recovery

Use the actual engine, versions and upgrade paths. A fresh schema passing tests
does not prove a long-lived database can upgrade or recover.

## Establish the change and authority

Inspect migrations, historical schema/defaults, data invariants, deployment
sequence and supported versions. Identify transaction/locking constraints and
whether old and new application versions overlap. Check the assignment's role,
grant and required approvals. A skill supplies no authority to touch live data,
start services, disable constraints or retrieve a production backup.

A reviewer stays read-only and returns findings. An assigned implementer uses
owned disposable fixtures with synthetic data; any live operation needs explicit
person approval and the project's existing operational procedure.

## Design an engine-aware change

- For SQLite, verify supported ALTER operations or safe rebuilds, preserving
  indexes, triggers, constraints, foreign-key checks and historical defaults.
  Account for busy handling, short write transactions and WAL where used.
- For PostgreSQL, check DDL locks, transaction restrictions, concurrent indexes,
  statement timeouts and the actual supported version. Use Supabase policy/auth
  advice only for projects that use Supabase.
- For other engines, consult their versioned primary documentation. Do not apply
  one engine's assumptions to another.

Define preconditions, invariants, measurable postconditions and abort points.
Record irreversible steps, capacity/time needs and compatibility windows.
Use expand/backfill/contract only where the real deployment needs that pattern.

## Prove recovery, not just migration

Identify a consistent backup and the keys/configuration required to restore it.
Keep sensitive recovery material out of repositories, artifacts and logs.
Test restore into a disposable isolated target; verify schema, data invariants
and application startup. Never let a restored copy contact production peers or
external integrations as an accidental second main.

Choose a tested **rollback** or **forward fix** with explicit triggers, commands
verified against the project, owner and expected data loss. A down migration
does not recover overwritten data. If reversal is unsafe, say so and demonstrate
the recovery path rather than inventing a reassuring rollback.

## Verify and report

Cover a fresh install and representative historical upgrades, including a
partially applied change, repeated execution where supported, busy/locked state
and recovery failure handling relevant to this change. Check constraints,
indexes, defaults and row invariants after both upgrade and restore.
Workers run related checks; use only the listed gate route for full verification.

Report the tested head, source/target engine versions, backup identity without
secrets, actual restore evidence, timings, abort results, recovery choice,
commands and omissions. Say "restore not tested" if only inspected or proposed.
Clean up owned fixtures by exact identity, never unrelated data or processes.
