# Multi-run

Owner approved, 2026-10-22 (competitor review, gap 4).

## Scope and launch

A person sends one project task to 2–5 agent/profile/model choices.
These are independent ordinary sessions, not an Orchestrator team: no
lead, helpers, automatic evaluation, merging, or additional launches.
Every run uses its own worktree and `acc/<name>-<agent>-<id>` branch,
from the project's current HEAD. Duplicate agents/models are allowed.

`POST /api/multiruns` accepts a shared name, project and first prompt,
plus launch choices. It validates the whole request and dangerous-mode
confirmations before starting anything, then attempts the choices together.
Each attempt goes through the ordinary session launch path, with its own
budget check and no budget override. A refused choice is recorded without
cancelling the others. The response is the durable group, even if all
choices refuse. There is no implicit retry.

## Persistence and authority

Migration 0037 adds a group (name, project, owner, winner, timestamp),
ordered attempts (choice, session id or refusal), and a session group id.
Session creation attaches its attempt in the same database transaction.
An interrupted launch can leave a visibly unfinished attempt; ACC never
starts another agent to repair it. Members can see and act only on their
own groups and sessions; owners can inspect any. Compare and keep check
each selected session as well as group ownership.

Audit events: `multirun.start` records choices, not the task text;
`multirun.keep` records the selected session and whether others are archived.
Normal launch, dangerous confirmation, stop and archive audits still apply.

## Review and keep

`GET /api/multiruns` lists scoped groups; `GET /api/multiruns/{id}`
includes ordinary session state, change summaries, observed usage/cost
with source and age, and branch-matched cached CI where available.
Unknown values remain unknown, never zero. GitHub failures/staleness
and launch refusals are explicit.

`GET /api/multiruns/{id}/compare?left=...&right=...` returns the two
existing Changes API results, with the same baseline, truncation and vault
redaction. It accepts only distinct runs belonging to this group. The UI
aligns the union of changed files and reuses the existing diff parser and
line styling. This compares each solution against its launch baseline,
not one branch against another. Explain remains an explicit per-session
action in Changes; opening the comparison never starts a paid helper.

`POST /api/multiruns/{id}/keep` marks the winner. Separately confirmed
stop-and-archive of the other runs uses ordinary supervisor lifecycle
actions, reporting per-run failures. It never deletes branches or merges
code. Multi-run archives retain worktrees for later comparison; people
can remove them through git after saving any work they want to keep.

## UI

Follow `docs/design/DESIGN.md`: existing Inter/JetBrains type, `bg`
#09090b, `panel` #111114, `line` #232329, `fg` #ededef,
`muted` #a0a0a8 and `accent` #8b9dff tokens, with no new visual theme.
New session adds **Run on several agents**, off by default and mutually
exclusive with Orchestrator. Each choice has agent, profile, model and
provider permission options, including explicit dangerous confirmation.
The shared prompt and project appear once.

The group page is a left-aligned comparison workbench:

```text
Group name                         project / launch time
[Run A: state, usage, changes] [Run B: ...] [Run C: ...]
Compare [left run] [right run]
file.py
[A's Changes diff           ] [B's Changes diff           ]
```

Cards stack on narrow screens; file panels stack without horizontal page
overflow. Use Page, Card, QueryState and the shared confirmation dialog.
Sessions adds one collapsible group row with a group-page link and member
SessionList; ordinary individual rows remain visible and independently
usable. Team trees are unchanged.

## Verification

Real broker/fake-CLI Python tests cover one-request launches, partial
budget refusals, naming, persistence, scope, confirmation, compare
redaction/membership, and keep/archive without deleting branches.
Playwright covers the person launching multiple choices, file-by-file
comparison, keep confirmation and Sessions grouping, with demo screenshots
for the Guide. Run the Python suite, web typecheck/build, e2e and wiki check.
Paid vendor CLIs and real pushed CI are not exercised by fake-CLI tests.

## Implementation notes

- All choices pass the existing preview compiler before a group is created;
  each actual launch still performs its own budget and project checks.
- The supervisor attaches an attempt to its session transaction, and excludes
  in-process initial launches from reconciliation until broker trust/start
  completes. This prevents a pending first launch from being mistaken for a
  missing conversation; restarts retain the existing recovery policy.
- Keep operations are serialized. A machine that cannot confirm a stop leaves
  durable `multirun_stop_pending` intent and an explicit per-run error; other
  archives continue. Reconciliation includes archived pending stops and clears
  the error only after confirming the process is gone. Stop/archive during an
  initial launch prevents that launch from starting.
- Ownership transfer on user removal includes groups. Every group read checks
  its linked sessions, so a transferred session cannot leak via its former group.
