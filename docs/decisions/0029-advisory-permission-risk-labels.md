# Advisory permission labels reuse the guard parser

Renumbered on 2026-10-24 from 0005 to 0029.

Date: 2026-10-22

## Context

Inbox prompts need a quick risk signal without changing ACC's principle that
people decide. CLI permission hooks and screen prompts have different authority;
Copilot's PermissionRequest alone does not mean a person is needed.

## Decision

Use deterministic rules in `server/risk_labels.py`, importing the trusted
standalone guard beside the configured hook. Reuse its parser and refusal
checks without adding a deployment dependency or executing commands. Persist only
the derived label/reason on attention items. A per-person Setting hides labels
on every surface; it does not alter attention or permission state.

## Consequences

There is one shell grammar, no extra hook deployment dependency and no model
usage. A process-local lock protects the guard module's run-directory context.
Screen text and unknown actions remain Review. Labels are static advice, not a
sandbox; script behavior, symlink targets and runtime expansions remain uncertain.

## Alternatives considered

Duplicating shell parsing would drift from guardrails. Extracting the guard's
parser into another installed file would widen this change across all node
installers. AI classification adds privacy, cost, latency and failure modes and
is deferred. Automatic approval is outside the feature's contract.
