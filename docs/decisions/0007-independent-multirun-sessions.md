# Independent Multi-run sessions

Date: 2026-10-22. Status: accepted (owner-approved feature).

## Context

A person wants alternative solutions from several agents or models, without
granting any agent authority to launch helpers or decide which solution wins.
Ordinary launches already implement profiles, permission confirmation, budget
checks, worktrees, vault delivery and audit.

## Decision

Persist a person-owned group and ordered attempts, each linked to an ordinary
session in the same transaction that creates it. Validate all choices and
confirmations before side effects, then schedule ordinary launches together.
A budget or broker refusal affects only its attempt and never retries silently.

Keep Multi-run separate from Orchestrator grants and team ancestry. Compare
ordinary, baseline-relative Changes results with their existing redaction and
limits. Keeping a winner is metadata; stopping and archiving the alternatives
requires separate explicit confirmation.

Retain Multi-run worktrees on archive, including clean worktrees, so both
committed and uncommitted alternatives remain comparable. Never delete branches
or merge code as part of keeping a run.

## Consequences

Groups survive restart, failed choices stay visible, and people can still use
each session's terminal, Changes and explanation flow independently. Members
must own both the group and the selected sessions; owners may manage any.
Retained worktrees consume disk until a person removes them. Interrupted
unlinked attempts require inspection, not automatic additional launches.

## Alternatives

Reusing team records would mix human comparison with delegated agent authority.
Rolling back accepted runs after one refusal would discard useful work.
Removing archived worktrees and reconstructing only committed branches would
lose uncommitted alternatives; snapshotting every full diff would add another
storage/redaction lifecycle and would not preserve the actual working copy.
