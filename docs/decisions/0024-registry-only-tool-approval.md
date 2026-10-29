# Registry-only tool approvals

Renumbered on 2026-10-24 from 0003 to 0024.

Date: 2026-10-22. Design: [TOOLS](../TOOLS.md).

## Context

Installed tools should work without permission prompts. Changes to account or
machine software need a person's approval, while project-local dependencies do
not. ACC's parsed-command guard is a safety net, not an isolation boundary.

## Decision

Requests contain validated registry coordinates, never executable text, URLs or
paths. A person's agent account and machine come from their session. System
managers use the machine's root helper, and only owners request them. Account
managers run through that account's broker, which refuses root. The existing
authenticated node link carries both paths. Decisions and resolved versions are
durable; interrupted work is not replayed.

## Consequences

Arbitrary installer scripts and downloaded package files cannot be approved
through this API. Managers must already exist, and unavailable versions fail
explicitly. Approved package code can execute with the install destination's
privileges; approval is meaningful consent, not a claim that registry code is
safe. Guard exemptions do not grant system privileges or inventory direct
installs.

## Alternatives

Approving raw commands would create a remote root-command interface. Blocking
all unfamiliar executables would break normal development. Running every
manager in the root helper would give user packages unnecessary privileges.
