# 0030: Durable person-owned schedules

Renumbered on 2026-10-24 from 0005 to 0030.

Date: 2026-10-22

## Context

Recurring sessions must express a person's choice of work, timing and permissions. ACC currently has one supervisor, but a process restart must preserve due intent without duplicating a session. A broker operation can succeed before its caller receives a reply.

## Decision

Persist schedules and run intents separately. Consume a due timestamp and insert an intent atomically, conditionally claim it before dispatch, and reserve its ordinary session ID in advance. Recording that session attaches it to the run in the same database transaction. Reuse manual launch validation, budgets and worktrees; freeze resolved profile launch options when the person saves them. Recheck identity/account access after asynchronous setup and persist cancellation before stopping a process.

After restart, retain pending intent and reconcile recorded sessions. Fail ambiguous claims without a session into the person's Inbox rather than retrying broker effects. Recovery coalesces missed occurrences into one run or records a skip. Only authenticated people manage schedules; repository discovery is deferred.

## Consequences

Every due run has an attributable durable outcome. A crash during worktree setup can leave an orphan worktree requiring inspection before a person retries. Interactive sessions remain active, including idle prompts and permission waits, until stopped or exited. This is a single-supervisor feature, not distributed leadership or fencing. See [the schedule design](../SCHEDULES.md).

## Alternatives considered

- In-memory timers: lose intent across restarts.
- Unconditional retries after broker timeouts: risk repeating external effects.
- A backlog per missed occurrence: can surprise people with many launches on recovery.
- Repository-owned or agent-created schedules: exceed the person's explicit authority.
