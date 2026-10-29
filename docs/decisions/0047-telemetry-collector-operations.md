# Standalone collector operations

Date: 2026-10-24. Status: accepted implementation choice within the approved telemetry policy.

## Context

[0012](0012-opt-in-telemetry.md) approves an owner-hosted collector, separate rotating keys,
retention and a ten-installation publication threshold. It does not authorize deployment.

## Decision

Ship a collector-only systemd installation for a dedicated VM, root-owned source/venv and a
non-login service account. Caddy terminates TLS and forwards real peer addresses through a
protected Unix socket. Only one worker runs the bounded memory rate limiters. Disable access
and request-bearing error logs, constrain resources and make the service filesystem read-only
except state/runtime. No public administrative or statistics-reading endpoint is added.

Finalize unlinked monthly counts once the seven-day late-arrival window closes, rather than
recomputing from raw rows that may have expired or been erased. A local export omits cells below
ten and never emits grand totals or suppression counts. Key rotation remains unlinked; the
collector does not add a continuity/alias protocol. Signing keys are pseudonymous installations,
not verified people. Public publication remains a later owner decision.

Provide local-only deletion, SQLite online backup, retention and restore commands. Operators
must remove affected backups within 30 days and replay all erasures before restore; snapshots
with unknown erasure history must be discarded. The runbook explains this limitation explicitly.

## Consequences

Low dependency/operational overhead; no Docker prerequisite or full ACC install. Diagnostic
logging is deliberately sparse. Resource limits and single-worker maps bound local exposure,
but do not defeat distributed floods or key fabrication. Finalized anonymous counts cannot be
adjusted for individual erasure; deletion/downtime before finalization can undercount. Backups
require an operator-controlled erasure process or must be disabled.

## Alternatives considered

A container stack adds runtime requirements without improving this VM's trust boundary. TCP
loopback makes forwarding trust depend on every local process; the private socket is narrower.
A rotation-linking protocol weakens unlinkability and would change the approved client contract.
A public export API or dynamic cross-tabs increases abuse and inference risk. Recomputing monthly
totals after raw expiration silently lowers counts; retaining per-installation monthly membership
would contradict the approved unlinked aggregates.
