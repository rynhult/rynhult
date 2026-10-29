# Scheduled sessions

Approved scope: owner request, 2026-10-22; PLAN and competitor gap 5.

A signed-in person creates a schedule for a project in their agent account, choosing the same provider, profile, launch options and prompt as a manual session. Creation explicitly chooses whether to enable it. Dangerous launch options require the manual launch confirmation once for that saved configuration; changing launch inputs requires confirmation again. Only the creator or an owner may view and manage it. Agent session tools and the bootstrap API token cannot manage schedules.

Schedules store an IANA timezone and either a one-time local datetime, daily local time, weekly weekday/time, or five-field numeric cron expression (lists, ranges and steps). UTC instants are persisted. Nonexistent DST times are skipped; repeated local times run once, at the first occurrence. Cron day-of-month and weekday use conventional OR matching when both are restricted. Recovery coalesces missed occurrences into at most one run, or skips occurrences at least one minute late when configured (a one-minute dispatch grace avoids treating normal polling delay as downtime). No backlog replay.

Two durable tables hold schedules and run intents with stable IDs. A transaction consumes a due timestamp, inserts its run intent and advances the schedule. A conditional database claim dispatches each intent once. Each run reserves its session ID before dispatch; recording the session atomically attaches it to the run. After restart, a claimed intent with a session is reconciled from that ordinary session; an ambiguous claim without a session fails visibly instead of automatically retrying an external effect. This favors no duplicate launches over automatic retry of ambiguous worktree creation. Current deployment remains single-supervisor; this does not claim distributed fencing or HA support.

Dispatch rechecks the person's account, project access and budget. Paused budgets and a still-active session from the same schedule produce audited skips, never overrides. Runs default to separate worktrees and share the ordinary launch/compiler/supervisor path. Durable pending and claimed runs also prevent overlap. Run now creates the same durable intent. Cancel cancels a pending intent or stops its session through the existing supervisor; it does not pause future occurrences. Deleting a schedule disables it and retains run/audit history.

The Schedules page shows timing, timezone, next run and recent results, links each ordinary session, and offers create/edit, enable/pause, Run now, Cancel and delete. Failures create personal Inbox items and use the existing push pipeline. Audit records cover create/update/enable/disable/run/skip and cancellation/deletion. Prompts are excluded from audit details.

Repository-defined schedules are deliberately not supported in this version: repository files cannot create or enable schedules. Discovery and digest-bound local approval can be added separately.

Validation covers timing/DST/cron, permissions and dangerous confirmation, budget and overlap skips, recovery and durable claims, audit/Inbox, and the browser create/Run now path. Guide, changelog, plan and handoff accompany implementation.
