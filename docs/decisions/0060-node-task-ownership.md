# 0060 — Explicit ownership for main-side node tasks

Date: 2026-10-25. Status: implemented maintenance decision under the owner's node
task ownership request; no new execution authority or retry policy.

## Context

Connection workers and machine jobs had different cancellation paths. Dropping a
link could lose the owner of still-running work; cancellation without a join could
leave mutation threads or request children alive after teardown.

## Decision

Use a small task registry for each connection generation, with machine scopes for
jobs scheduled offline. Retain retired scopes until they drain. Node replacement,
removal and main shutdown cancel work and join cleanup. Request-scoped fan-out
joins children in finally blocks. Root workers use the same exception observer.

Unexpected background failures are logged once. Awaited jobs keep caller-owned
result handling. Failure of one job does not cancel unrelated jobs. Running
threads are joined because Python cannot forcibly stop them. Shutdown permits
reconnect; it does not revoke enrollment.

## Consequences and alternatives

Teardown waits for blocking operations and their existing deadlines; an OS call
that never returns can delay it indefinitely. Cancellation does not
roll back remote effects or authorize replay. Explicit registries preserve the
existing independent-job behavior; a bare asyncio.TaskGroup would cancel siblings
on failure. Detached tasks or cancel-without-join were rejected because they do
not establish a completed lifecycle boundary.

The creation/ownership table and test contracts are maintained in
`docs/wiki/Internal/Node-Task-Ownership.md`.
