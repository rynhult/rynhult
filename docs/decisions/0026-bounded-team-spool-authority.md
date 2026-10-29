# Bounded team authority through session spools

Renumbered on 2026-10-24 from 0003 to 0026.

## Context

The optional Orchestrator needs to launch helpers without a dashboard token. Helpers may delegate further, and the person must retain control of permissions and limits.

## Decision

accd stores an immutable explicit grant and each member's ancestry. A request's caller is its registered spool, with a matching session claim required. Durable session-scoped request ids prevent replay; interrupted mutations require inspection rather than automatic repetition. One accd team mutation lock serializes admission and stopping, and the lifetime count is reserved before broker work. All depths share the grant and person's budget.

Brokers execute git operations and write replies as the agent account. Cross-machine work moves through bounded, verified bundles. See [Orchestrator design](../ORCHESTRATOR.md#decisions-while-building) for operational decisions and limits.

## Consequences

Ordinary session launch inputs remain unchanged. A failed start consumes a lifetime slot. Offline stop intent persists before a broker is contacted. Team permission prompts remain with the person. Sessions sharing a Unix account retain the existing spool trust boundary; this does not create OS isolation between arbitrary processes with that uid.

## Alternatives considered

Giving agents dashboard tokens would widen their authority. Trusting agent-maintained counters or ancestry would allow limit and subtree bypasses. Automatically replaying interrupted mutations could duplicate starts or merges. All were rejected.
