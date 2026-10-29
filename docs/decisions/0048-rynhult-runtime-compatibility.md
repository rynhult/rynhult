# 0048 — Rynhult runtime compatibility

Date: 2026-10-24. Status: accepted by the owner.

## Context

Stage 1 introduced the Rynhult public name. Stage 2 must allow mixed-version
machines and preserve live sessions and enrollment. The owner approved these
choices on 2026-10-24 for the plan in `docs/RENAME-STAGE-2.md`.

## Decision

- The naming convention for **new installations** is `rynhultd`, `rynhult-*`,
  `RYNHULT_*`, `local.rynhult.*` and `/srv/rynhult`.
- Keep cookies and the CSRF header, `acc.db` (including backup members), service
  users/groups, and existing installations' `/srv/acc` paths permanently.
- Old and new names work side by side for at least two stable releases **and**
  until every enrolled machine reports the compatible version. Offline machines
  keep the compatibility window open. Use the `rename-compat-1` capability;
  a display version alone is insufficient evidence.
- A failed stage 2 update rolls back binaries/configuration only, keeping the
  database. Database recovery is a separate operation, never a rename side effect.
- Phase 1 adds compatibility only. New environment names win when both are
  present, including an explicitly empty value. Existing install defaults remain.
  Systemd aliases point at existing units; launchd labels wait for a drained node
  because launchd has no equivalent alias mechanism.

## Consequences

No paths, Python packages, accounts, cookies or databases move in phase 1.
Existing config files remain authoritative. Fresh-install path defaults and
canonical service/launchd identities need a separately qualified later phase.
The existing general updater's database rollback needs separate stage 2
integration before any path or label cutover; this phase does not enable one.

## Alternatives considered

A global replacement, duplicate services, account renames, and live data moves
would split state or interrupt sessions. A time-only expiry would strand offline
machines. A database restore on rename failure would discard accepted writes.
