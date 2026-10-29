# 0057: Shared-server browser isolation

## Context

Playwright workers share one temporary ACC installation per invocation.
Independent browser contexts do not isolate installation settings, the saved
owner login, authenticators, TOTP replay protection or per-person rate budgets.
File-local serial mode cannot exclude a different file's workers.

## Decision

Run setup once, then dependency-ordered desktop and mobile stateful projects,
each with one worker and its original device settings. Only genuinely global
writers, quota consumers and stable-inventory proofs belong there. Run the
remaining desktop/mobile cases together with four workers.

Use UUID-qualified project/checkouts and machine names, assertion scopes based
on owned resources, and finally cleanup for independent tests. Shared-login
revocation must persist a replacement valid login for subsequent observers.

The ephemeral browser server owns simulated signer/edge implementations and
captures the real pairing/terminal limiter instances. Serialized cases can reset
only their corresponding budget before starting. Limits remain unchanged
within each case; no production route, bypass, clock change or limiter change
is introduced.

## Consequences

Selecting desktop or mobile also runs the prerequisite stateful phases. Full
selection preserves every original case. Global phases reduce concurrency only
where sharing is intentional; ordinary tests still face parallel load.

New stateful specs must be classified explicitly. They must not rely on an
earlier file's incidental resource, verification status or configured default.

## Alternatives

- Serializing the entire suite hides resource-local races and wastes capacity.
- File-local serial mode does not isolate another file's shared-state mutation.
- One server per spec changes the gate's shared-server model and startup cost.
- Increasing timeouts does not repair revoked sessions, conflicting policies,
  occupied ports or exhausted rate budgets.
