# Orchestrator v2: working list, live agent tree, explainable selection

<!-- lane F -->
**Target: rc.1, on by default, not a Preview (owner-approved 2026-10-27).
Original design: 2026-10-25.** It extends the M17 Orchestrator
([ORCHESTRATOR.md](ORCHESTRATOR.md)). Availability does not grant a team: a person
still grants fixed limits. Helpers use that person's agent account and separate
worktrees, the root lead opens one consolidated PR, and the person merges main.

Section 5 records the corrected selection contract. The original slice
schema/API sketches and dated implementation records below are historical, not
the rc.1 assignment/attempt or planning interfaces. For the target end-to-end
behavior, including private briefs, prepare/build modes, verification binding
and bounded expiry, see the [Guide](wiki/User-Guide/Orchestrator.md).
Lane comments identify implementation dependencies still pending on this base.

The owner asked for three things:

1. **A working list**, like the owner's own: an ordered "what I need from you" with complete steps, decisions with a
   recommendation, answers that are ingested and marked, a NOW summary, and done items. Persisted, visible and
   editable by the owner.
2. **A live agent tree** below the lead (managed helpers and their descendants), drawn while it is being created and
   updated as sessions start, change state, wait for input or finish.
3. **Explainable selection** of profile, skills, provider and model for every helper the orchestrator asks for:
   the reason recorded and shown, owner-overridable, and budget-aware.

The design also closes the eleven gaps a human coordinator hit running this job by hand on Oct 22–25 (section 7).
The process that coordinator followed, and the lead's proposed default instructions, are in the
[orchestration playbook](ORCHESTRATION-PLAYBOOK.md).

## 1. What stays the same, and the one new rule

<!-- lane F -->
- **On by default; teams optional; immutable grant.** A person grants the role at launch. The grant fixes the allowed agents,
  profiles and permission modes, the machines, the at-once and total limits, the depth and the time. Agents can't
  edit it.
- **No tokens in agents.** Helpers and the lead reach Rynhult only through their session's spool (`acc-mcp-team`), which
  accd reads with the identity of the session that owns that spool.
- **Guardrails apply to every member.** No merge to main/master, no `gh pr merge`, no publishing without approval.
- **New rule: nothing in v2 widens what a team may do.**
  - The working list, selection, reports, reservations and gate jobs are bookkeeping and scheduling *inside* the
    grant.
  - A selection can only pick from the grant's allowed choices. The owner may override within the grant, or stop
    the team and grant again.
  - An owner override is a person's action in the dashboard, never an agent's.

## 2. Data model

New tables live in a new package, `src/acc/server/orchestrator/` (one module per slice; see section 9). They're
registered with the shared declarative `Base`, so `models.py` stays untouched. Every table is keyed by the team (the
lead's session id, `team.id`). Migrations are reserved per slice up front (section 9).

**`orchestration_run`** (one per team): the list's header.
- `team_id` (PK, FK `team.id`), `now` (Markdown, at most 4 KiB), `now_updated_at`, `now_updated_by` (`lead` or a
  username).
- `revision` (int, bumped on every change to the run or its items; optimistic concurrency).

**`work_item`**: the ordered list.
- `id`, `team_id`, `position` (float; reordering touches one row).
- `kind`: `need` (something the person must do), `decision` (a choice with options), `question` (needs an answer),
  `task` (the lead's own planned work), `note`.
- `title` (at most 200 characters), `steps` (Markdown, at most 16 KiB: the complete steps a person can follow
  without asking).
- `options` (decision only: `[{id, label, consequence}]`, at most 6), `recommendation` (option id plus a reason, at
  most 1 KiB).
- `state`: `open`, then `answered` (the person answered), then `ingested` (the lead read the answer and acted), then
  `done`; or `dismissed`.
- `answer` (text or the chosen option id, at most 8 KiB), `answered_by`, `answered_at`, `ingested_at`, `done_at`.
- `source`: `lead`, or the username that created or last edited it. `owner_edited` (bool: the lead may no longer
  rewrite the title, steps or options; it may only append a note).
- `links` (`[{kind: session|branch|report|delivery, id}]`): what it's about.
- `revision`.

**`selection`**: why a helper got what it got (section 5).
- `id`, `team_id`, `requested_by` (session id), `request_id` (team request), `need` (the lead's structured ask:
  `role`, `skills_wanted`, `languages`, `effort`, `risk`, `machine?`), `task_digest` (SHA-256 of the task text, not
  the text).
- `candidates` (`[{agent, profile, model, permission, machine, score, reasons[], est_cost}]`, at most 12), `chosen`
  (the index), `reason` (one sentence from the rule table), `budget` (a snapshot: remaining at decision time, the
  window and the expected spend).
- `state`: `proposed`, then `started`, or `overridden` (by a person) or `refused` (nothing allowed fits, or the budget
  is exhausted); `override_by`, `override_reason`.
- `session_id` (once started).

**`message_delivery`**: every `/message` and team `message` (gap 2).
- `id`, `session_id`, `actor` (a username or `session:<id>`), `preview` (the first 200 characters, after vault
  redaction), `text_sha256`, `bytes`.
- `state`: `queued`, `typing`, `submitted` (input sent with Enter), `acknowledged` (the provider confirmed),
  `failed`, `expired`.
- `ack` (`hook:UserPromptSubmit`, `screen:input-cleared` or `screen:echo`), `attempts`, `error` (a fixed category),
  `created_at`, `updated_at`, `deliver_after` (queued until the session is idle or waiting).

**`task_report`**: the structured final report per task (gap 3).
- `session_id`, `task_id` (the team start request), `status` (`done`, `blocked`, `failed`, `partial`), `summary`
  (at most 4 KiB), `changed` (`[{path, kind}]`, at most 200), `tests` (`[{command, passed, failed, skipped}]`),
  `branch`, `head`, `handoff_path`, `needs` (an array becoming `work_item`s), `received_at`.

**`reservation`**: numbers handed out by the orchestrator (gap 9).
- `kind` (`migration`, `decision`, `port`), `number`, `team_id`, `session_id`, `branch`.
- `state`: `reserved`, then `used` (seen on the integrated branch) or `released`.
- `created_at`, `expires_at`. Unique on (`kind`, `number`), with numbers checked against `main` and every open
  reservation.

**`gate_job`**: the merge-gate queue (gaps 5, 10, 11).
- `id`, `project_id`, `team_id?`, `branch`, `head`, `base`, `stack_parent?` (a gate job id).
- `suite`: `full`, `related` or `rerun:<test ids>`.
- `priority`: `merge` (blocks a merge) or `check`; merge-first ordering.
- `state`: `queued`, `running`, `passed`, `failed`, `flaky-passed` (a failure passed on a targeted rerun),
  `cancelled`.
- `slot`, `pid_record` (the machine and the unit name the gate started; no pattern matching), `log_path`,
  `requested_by`, `started_at`, `finished_at`, `results` (counts, failed test ids).

**Settings** (`Setting` rows; owner-only API): `orchestrator.host_budget` holds `{gate_slots, full_suites_max,
worker_tests: "related", load_ceiling}` (section 7, gaps 10 and 11). `orchestrator.selection_rules` holds owner
preferences that refine the built-in table (section 5).

**Existing tables reused:**
- `Session.parent_session_id`, `team_id` and `team_depth` already form the tree.
- `Event` (append-only, indexed by id) is the live feed.
- `AttentionItem` stays the Inbox: a `need` or `decision` item that blocks the team also raises an Inbox item
  linking to it, so the person is notified once.
- `AuditLog` records every change by a person or the lead.

## 3. APIs

**Person-facing** (cookie session + CSRF, the lead's owner or an instance owner; members see only their own teams):
- `GET  /api/teams/{lead}/worklist`: the run (`now`, `revision`) and its items in order.
- `POST /api/teams/{lead}/worklist/items`: add (`kind`, `title`, `steps`, `options?`, `recommendation?`).
- `PATCH /api/teams/{lead}/worklist/items/{id}`: edit, reorder (`position`), answer (`answer`), mark done or
  dismiss. Needs `revision`; a stale revision gets 409 and the current list.
- `PUT  /api/teams/{lead}/worklist/now`: the person's NOW edit; the lead's next update must start from it.
- `GET  /api/teams/{lead}/tree`: a snapshot of every member, with `cursor` (the highest Event id included).
- `GET  /api/teams/{lead}/events?after=<cursor>&wait=25`: a long poll of tree and list events (section 4). It
  returns at once when there are events, else after `wait` seconds with an empty list.
- `GET  /api/teams/{lead}/selections`, `POST /api/teams/{lead}/selections/{id}/override` (`choice` from the grant's
  candidates, `reason`).
- `POST /api/sessions/{id}/message` now returns `{delivery}` (202); `GET /api/deliveries/{id}`. The existing
  synchronous behaviour stays available as `?wait=8` for the Changes tab.
- `GET  /api/sessions/{id}/report`: the structured report, if any.
- `GET  /api/gates?project=` and `POST /api/gates` (`project`, `branch`, `suite`, `priority`);
  `POST /api/gates/{id}/cancel`.
- `GET/PUT /api/settings/orchestrator` (owner): the host budget and selection preferences.
- **Launch API fixes** (gap 8): `topic` together with `project_id` is a 422 ("topic is for local workspaces; did you
  mean task?"), never a silent switch. A new `task` field (at most 64 KiB) on launch delivers the first instruction
  through the delivery queue, so long instructions don't need a separate `/message`.

**Lead and helper tools** (`acc-mcp-team`, through the spool, as today; each is checked against the caller's
team and depth):

| Tool | Effect | Limits |
|---|---|---|
| `worklist()` | Read the list and NOW | — |
| `worklist_add(kind, title, steps, options?, recommendation?, links?)` | Add an item (`source=lead`) | at most 200 open items; size caps |
| `worklist_update(id, …)` | Edit its own items | refused if `owner_edited`; can't set `answer`, `answered`, `dismissed` |
| `worklist_ingest(id, note)` | Mark an answered item `ingested` with what it did | only from `answered` |
| `worklist_done(id, note)` | Mark its own `task`/`need` done | can't close a person's open decision |
| `now(text)` | Replace NOW | refused if the person edited NOW after the lead last read it (409 + current) |
| `select(need, task)` | Returns a selection with candidates and the reason (section 5) | within the grant |
| `start(selection, name, task, kind, machine?)` | Starts the chosen candidate (M17 `start` now takes a selection) | M17 limits, budget |
| `message(helper, text)` | Queued, acknowledged delivery (section 7, gap 2) | M17 rules |
| `report(status, summary, changed?, tests?, needs?)` | The helper's structured final report | once per task; `needs` become items |
| `reserve(kind)` / `release(id)` | Migration, decision or port numbers | per team at most 20 open |
| `gate(branch, suite, priority)` / `gate_status(id)` | Queue a gate job for its own branch or a descendant's | `full` only from the lead |
| `restack(helper, onto)` | Replay a helper's commits onto the lead's current branch (gap 6) | M17 merge preconditions |

A helper sees its own subtree only, as in M17. Only the lead (depth 0) writes the working list. Helpers send `needs`
in reports, and those become items the lead triages. That keeps one voice in the person's list.

These "lead only" rules (writing the list, `full` gates, restacking) are conventions between agents that share one
Unix account, not security boundaries: a helper running code as that account can write the lead's spool (threat
review, section 1). What bounds them is the grant, the per-team limits counted in accd whoever calls (one budget of
60 writes a minute per team across every write tool, and the gate counts), and the person-only actions. Every agent-written item is labelled with its author, so nothing a team writes appears in
Rynhult's or the person's voice.

## 4. Live events

Long polling over the existing `Event` table fits the dashboard's polling model (React Query) and needs no new
transport. Every change below appends one `Event` row (`source="orchestrator"`, `session_id` = the member, or the
lead for list events) with a small payload. The snapshot carries the highest event id, so a client never misses or
double-applies an event.

| Kind | Payload | Drives |
|---|---|---|
| `team.member.created` | `{id, parent, depth, name, agent, profile, model, machine, selection}` | a new node, drawn at once in a "starting" state |
| `team.member.state` | `{id, observed, needs_you, activity}` (`starting`, `active`, `waiting_input`, `idle`, `stopped`, `failed`) | node colour and badge |
| `team.member.report` | `{id, status}` | a node's "reported" mark; a link to the report |
| `team.member.finished` | `{id, merged, archived}` | the node greys out; it stays visible, collapsed |
| `team.delivery` | `{id, session, state}` | message ticks |
| `team.selection` | `{id, chosen, reason, state}` | the reason on the node; override badge |
| `worklist.item` | `{id, state, revision}` | list row refresh |
| `worklist.now` | `{revision}` | NOW refresh |
| `gate.job` | `{id, state, slot}` | gate column |

The supervisor emits `team.member.state` on every observed-state transition it already tracks, so the tree updates
within one supervisor pass (seconds). The long poll holds at most one waiting request per browser tab, with a
25-second cap and a per-person limit (4).

## 5. Explainable selection

**Selection is the owner's ordered allow-list per task type. The score only
breaks ties; neither is a quality claim.** The lead supplies the classification;
the selector makes no model call and does not evaluate answer quality.

For the same complete snapshot (grant, need, owner policy, catalog, sign-in,
budgets, load and relevant usage), ranking is deterministic. The eligible sort is
**allow-list position first, descending heuristic score second**, expressed in
the existing implementation as `(not eligible, preference_rank, -score)`.
The owner's provider/model order is not a +30 bonus that a high score can beat.
The score only breaks ties within one allow-list position: for example machines,
models under `*`, or equivalent grant entries. It is not grant order after a
weighted-score winner.

<!-- lane C -->
### Assignments, hard requirements and starts

Every assignment sets `task_type`, `need.role` (a catalog role/profile ID),
acceptance checks and requirement IDs. Choose the smallest sufficient granted
profile. Role/profile matching is a hard filter, not merely the old +40 role
bonus; a no-profile entry matches only `general`. If nothing fits, refuse and
name the closest profile the person could add in a new grant.

<!-- lane C -->
Candidates are frozen grant entries, granted machines and known models. Rejections
include task/profile owner allow-lists, role, permission, machine/provider
availability, sign-in, model pins and budget. `need.requires[]` adds hard checks
for qualified `image_input`, `tool:<catalog id>`, `read_only`/`write` and
`network:none`. Missing capability flags do not qualify. `skills_wanted` stays
soft; `languages` is removed; `risk` is recorded, not used for ranking.

<!-- lane B -->
The tie-break heuristic includes wanted-skill overlap, estimated price class,
current load, recent actual failures and optional quota headroom. Waiting on a
person or dependency (`blocked`/`waiting_input`) is not a provider failure penalty.
The heuristic is scheduling assistance, not proof of competence, comparable cost
or a permission boundary.

<!-- lane C -->
All v2 starts follow `assign` → `select` → `start`, including a manual
`select{assignment, pin}`. A pin goes through the same task-specific hard filters,
not the old plain-start check for any task type. Readiness is checked again
under team authority before launch; no path can widen the frozen grant.

<!-- lane C -->
New helpers default to `temporary`. Each launch/reassignment has an attributable
attempt and versioned reports. Replacements receive a continuation pack:
assignment, requirements/constraints, prior reports and handoffs, each branch/head
and integrated state. They start from the previous attempt's head unless the
lead explicitly requests fresh. An uncertain launch is adopted or refused for
the same attempt, not silently launched twice.

<!-- lane C -->
### Recorded explanation

Store candidates/rejections, chosen profile and reason, classification and its
reason, budget snapshot, policy hash, catalog revision and profile digest.
The person can override only to another eligible candidate; the action/reason
are audited and do not rewrite owner preferences. A changed policy, sign-in,
model or budget may require a new selection, never an exception to the grant.

### RC quota integration (off by default)

The quota slice adds an optional `quota` object to `orchestrator.selection_rules`:
`{"enabled": false, "max_used_percent": 90, "max_age_minutes": 10}`. Owner-only validation bounds threshold
at 1–100% and freshness at 1–60 minutes. Existing policy documents without this object remain valid and disabled.
When opted in, fresh scoped usage at or above the threshold is an additional hard filter; the most-used known
window supplies `floor(headroom / 5)` score points. Owner provider/model order still precedes all heuristic
scores. Unknown/stale/unscoped readings preserve the current behavior, not a new pause. Of several readings for one
machine and provider the highest fresh usage counts, never simply the newest: the agent account writes its
sessions' statusline files and their times, so a forged or future-dated reading can't hide a higher one. Usage is rechecked on
start and override; reasons and candidate snapshots are recorded and shown. No collector or migration is added.

<!-- lane C -->
Quota is rechecked through the assignment's task-specific selection path at start
and override. Legacy non-v2 teams retain their old plain-start path; it is not a
v2 bypass. Never relabel a task to evade its owner allow-list.

This does not qualify unsupported accounts, reserve quota, stop running work or establish a provider billing limit. Quota is part of
selection. Its opt-in is separate from default-on orchestration.

### Default-on availability (owner decision, 2026-10-27)

<!-- lane F -->
The `orchestrator` Admin feature defaults on, without a Preview label.
`ACC_ORCHESTRATOR*` and the `RYNHULT_` aliases are overrides only; the working
list, plan and delivery do not need three opt-in flags. Existing explicit owner/
operator choices must be reviewed on upgrade, not silently widened.

The host budget's **Run the gate queue** and its branch-code trust confirmation
remain separate. Container projects have no host-gate fallback. Gates,
reservations and restacking may be requested only when actually available.
Provider quota hints remain independently opt-in. Availability changes no grant,
permission, machine or approval boundary.

## 6. UI sketches (text)

**Team tab → Working list** (left column on desktop; first section on a phone):

```
NOW  (edited by you 10:42)                                          [Edit]
  Gate queue: 2 running (slots 2/2), 1 waiting. Waiting on you: decision #3.

WHAT I NEED FROM YOU                                                drag to reorder
  ▢ #3 Decision: stacked branches after squash merges               [Answer]
      Options: (A) restack each child onto main  (B) no stacking
      Recommended: A, keeps parallel work; the lead restacks (2 min each)
  ▢ #5 Need: approve 'containers' profile for 2 helpers              [Open steps]
      1. Settings → Profiles → ...   2. ...
  ◐ #2 Answered 10:15 · waiting for the lead to ingest              [View answer]
DONE (12)                                                            [Show]
  ✓ #1 Choose Postgres version — you: 16 · ingested 09:58: "pinned in compose"
```

**Team tab → Tree** (right column; on a phone below the list, collapsed to two levels):

```
● Lead · Claude/Architect · opus · active — "integrating gate results"
├─ ● api-auth · Codex/Backend · gpt-5 · active · 3 ahead  ⓘ "Backend profile for a backend task; cheaper"
│  └─ ◌ api-auth-tests · Codex/QA · starting
├─ ◆ ui-forms · Copilot/Frontend · waiting for you  [Answer in Inbox]
└─ ✓ docs · Claude/Researcher · reported done · merged
```

Nodes appear the moment `team.member.created` arrives, in a `starting` state. Symbols mean active ●, starting ◌,
waiting for input ◆, finished ✓ and failed ✕, always with a text label (never colour alone). The tree is a
`role="tree"` list with arrow-key navigation, and each node is a link to the session. Selecting a node opens its
selection reasons, its last delivery states and its report.

**Session page → message box:** each sent message shows ticks: queued ○, typed ◔, submitted ◑, acknowledged ●, or
failed ✕ with the reason ("not submitted: Enter not accepted; open the terminal").

**Gates** (project page and Team tab): slots, a running job per slot, a queue with merge jobs first, and the host
budget line ("full suites: 1/2 · load 9.1 of ceiling 16").

## 7. The coordinator's gaps, and how each is closed

1. **Watcher drops** (a one-shot idle poller).
   - The supervisor already observes every session's state. It adds a persistent rule: a team member (or any
     session the person marks "worker") that is `idle` with a reported or finished task and no queued delivery for
     more than N minutes (default 5) raises a `need` item for the lead and an Inbox item for the person.
   - It clears itself when a delivery or a stop follows. Nothing has to be relaunched; it lives in the supervisor
     loop.
2. **Lost submits** (Codex unsent pastes, Copilot dropping messages while busy, Claude ghost suggestions).
   - Messages go through `message_delivery`. A message is typed only when no permission or screen prompt is
     detected, rechecked immediately before clearing, typing and each Enter (threat review S4-2). The session must
     also be `idle` (or `waiting_input` for Copilot, whose prompt check is the screen's), so it never interrupts work.
   - Before typing, the input line is cleared: `C-u`, plus Esc for providers that leave suggestion text.
   - Submission is confirmed by the provider hook (`UserPromptSubmit`), else by a screen check: the input line
     emptied and the text echoed in the transcript.
   - One automatic Enter retry, then `failed` with a reason, never a silent success. The lead's `message` returns
     the delivery id, and `status` shows its state.
3. **Copilot's full-screen TUI hides reports.** Every helper's header asks for `report(...)` at the end. Rynhult also
   reads the handoff file the header already asks for, and attaches it to `task_report.handoff_path`. A helper that
   finishes without either raises an item: "ui-forms finished without a report".
4. **Remote launches in default permission mode.**
   - Permission is part of the selected candidate, from the grant, so a remote helper launches with the same frozen
     inputs as a local one.
   - The broker's screen-prompt detection on the remote machine raises `waiting_input` like any session, and the
     tree and Inbox show it.
5. **Gate queue** (pgrep self-match, fixed slots).
   - `gate_job` is a real queue in accd. The machine's broker starts each job as a transient systemd unit (the
     project's account) whose name accd records; it never matches process patterns.
   - The slot count comes from the host budget; merge jobs go first; a job's branch must be restacked (gap 6)
     before it runs.
6. **Squash merges break stacks.** `restack(helper, onto)` replays the helper's commits (`git rebase --onto` in its
   worktree, as the agent account) after its parent was squash-merged. Conflicts stop and report files, as `merge`
   does. The gate refuses a job whose `stack_parent` was merged but which wasn't restacked.
7. **Guard false positive** on `powershell.exe -NoProfile -Command "…"`.
   - A guard bug, fixed in the guard itself (slice 4), not by the orchestrator.
   - A dynamic first word is treated as a possible `rm`, and any dash option containing an `r` counts as recursive,
     so `-NoProfile` matches. The fix: an option cluster counts as rm options only if every letter is one of rm's
     (`fiIRrdv`).
   - Add the refused command from the Oct 25 audit record to the guard corpus as a must-allow case, and keep
     `rm -rf "$X"` outside the workspace as a must-refuse case.
   - Nothing lets an agent route around a refusal. The model-safeguard refusal of that request was correct.
8. **API ergonomics.** `topic` with a project is a 422 with a hint; launch accepts `task` (section 3).
9. **Number collisions.** `reserve(kind)` hands out migration, decision and port numbers that don't collide with
   `main` or any open reservation. The gate checks that a branch's new migrations and decision records match its
   reservations.
10. **Flaky browser tests under load.**
    <!-- lane B -->
    - Keep the original failure. A new failure passing its targeted rerun is
      `retry-passed`, not a clean integration pass until the person accepts the
      lead's flake task on the working list or a fresh gate passes clean.
    - `known-flake` is only for the owner-managed project list; it counts as a
      pass and is listed in the PR. No automatic quarantine.
11. **Host overload** (load 37 on 20 cores, 14 false failures).
    - The host budget sets gate slots and full-suite concurrency. Workers' headers say "run related tests only; the
      gate runs full suites".
    - The gate checks the 1-minute load average before starting a full suite and waits while it is over the ceiling
      (default: the number of cores × 0.8).

## 8. Security

- **No tokens for agents.**
  - Every lead or helper action enters through its own session's spool, read by accd with that session's identity.
  - No dashboard cookie, API token, CSRF token or long-poll endpoint is reachable from an agent. The events endpoint
    needs a person's session.
- **The owner's grants only.**
  <!-- lane C -->
  - Selection ranks only the grant's allowed choices for the assignment's role
    and task type; every v2 start takes its assignment and selection.
  - Gate jobs run branch code as the project's account, outside helper sandbox/
    permission modes. They require owner trust confirmation; container projects
    have no host-gate fallback.
  - Reservations are numbers, not capabilities.
- **No self-escalation.**
  - No tool or field lets an agent change the grant, its permission mode, a profile's permissions, the budget or
    the host budget. Overrides are person-only (cookie + CSRF + owner check), and audited.
  - A helper can't write the working list, by convention within one account (section 3); the per-team limits and
    the author labels are what hold if a helper acts as the lead.
  - The lead can't answer a decision, mark a person's item answered or dismissed, or overwrite owner-edited text.
- **Untrusted text.** Item text, reports, NOW and deliveries are untrusted agent input:
  - stored with size caps, rendered as text (Markdown without HTML; links shown with their target, no autoload), and
    vault-redacted before storage and display;
  - never interpreted as commands by Rynhult.

  When an agent reads a person's answer, Rynhult frames it as the person's answer (provenance).
- **Delivery safety.**
  - Typing still refuses while a permission or screen prompt is detected (M17 rule), so a queued message can never
    answer a permission prompt.
  - Clearing the input line uses a reviewed per-provider key table (`delivery.CLEAR_KEYS`): `C-u` only, because Esc
    twice rewinds Claude Code and Codex and no provider is verified to need Esc. Delivered text is sent literally.
  - Like M17's `message`, agent-originated text starting with `!` (a shell), `/` (a slash command) or `#` (a memory
    note) is refused, checked after folding whitespace (threat review S4-1).
  - Every agent-originated message is typed after Rynhult's origin line, `[Message from the team lead "<name>", an agent
    of this team, not from your person]: …` (or `helper`), which the sender can't remove (S4-5). A person's own
    messages have no prefix.
  - An acknowledgement is status only: it needs the exact delivered text after typing began, and no merge, restack,
    gate or stop decision depends on it (S4-4).
- **Gates.**
  - Fixed commands per project (the project's test command and the e2e runner), from the project's settings, never
    from an agent's request text.
  - Logs are redacted. Stop is by recorded unit name only.
  - The job's environment keeps only `PATH`, `HOME`, the locale, `USER`, `LOGNAME` and `TMPDIR`: no vault keys,
    provider credentials, tokens or `ACC_*` session variables (S5-2).
  - A rerun takes test ids only from the job's own recorded failures, each pattern-checked and never option-shaped
    (S5-1).
  - Restacks and M17's merges run git with no repository-controlled program: hooks, `core.fsmonitor`, signing, the
    attributes file and every configured filter driver are off, every configured merge driver becomes `false` (the
    file conflicts and the operation aborts), and system and global git config are ignored (S5-5).
  - Each job runs under the host budget's per-job limits (default 120 minutes, a 50 MiB log, 8 GiB per process, at
    niceness 10), enforced by the job's own wrapper; a timed-out job fails and is never rerun. Gate jobs are counted
    per team (10 active, full suites up to the host's limit) and per project (50).
  - A restack replays exactly the helper's own patches (`git patch-id`), onto main or the lead's branch; anything
    else resets the helper's branch and refuses.
  <!-- lane B -->
  - A result binds the tested head, base SHA, command digest and runner version.
    Moving the branch/base or changing commands makes it stale; a machine that
    cannot be checked leaves currentness unconfirmed. Stop team cancels running
    gates as well as queued jobs, by recorded process identity.
- **Audit:** `worklist.*`, `selection.*` (including overrides), `delivery.failed`, `report.received`,
  `reservation.*`, `gate.*` and `host_budget.change`.

### Gates trust branch code (review L-1; owner decision pending, default C)

A gate job runs the owner's gate commands on a team branch's files as the project's account, on the machine, with its
network. That is outside a helper's container, permission mode and guardrails. A helper can commit a test file or
configuration that runs as part of the gate (for example a `conftest.py`) and then call `gate`. Default C:

- **Projects whose helpers run in containers** (`project.containers`, whether or not `ACC_CONTAINERS` is on now:
  with it off their sessions are refused, never run natively): their branch code never runs in a host gate. `enqueue`
  refuses them, and a job is refused right before every start, its targeted rerun included, if the project switched
  meanwhile (`gates.sandboxed`, `CONTAINER_REFUSAL`).
- **Running the gate inside the helpers' container** is the remaining piece. It needs:
  - a `gate` operation in the `acc-containers` root helper, with a grant naming the job's checkout, the owner's argv,
    limits and the pinned image, network `none` and the in-container wrapper writing the log and exit file;
  - the containers Preview qualification;
  - an owner root qualification run.

  Until then, container projects simply have no gate.
- **Everywhere else**, turning the queue on needs the owner's explicit confirmation of `TRUST_WARNING`. `PUT
  /api/gates/budget` answers 409 with the warning unless the body has `confirm_branch_code: true`. That is asked on
  every turn-on, not on other budget changes, and the audit entry records `branch_code_trusted`. The Guide says
  "gates trust branch code".

## 9. Build plan: slices that can be built in parallel

**Slice 0: foundation** (one small PR first, about 0.5 day). It creates the `src/acc/server/orchestrator/` package
with an empty module per slice. It registers each module with the shared `Base`, adds empty `include_router` calls,
reserves the migration numbers below and adds this doc's file map.
After it lands, the slices touch disjoint files. Shared files get only the one-line registrations slice 0 already
made.

| Slice | Owns (files) | Builds | Est. |
|---|---|---|---|
| **1. Working list** | `orchestrator/worklist.py`, `api/worklist.py`, migration `0069`, `web/src/components/WorkList.tsx`, `web/e2e/7x-worklist.spec.ts`, Guide `Orchestrator.md` § Working list | Tables `orchestration_run`, `work_item`; person APIs; lead tools in `bin/acc-mcp-team` under a `worklist_` prefix (append-only section); `report.needs` → items; idle-worker alert (gap 1) | 3–4 days |
| **2. Live tree + events** | `orchestrator/live.py`, `api/team_live.py`, no migration (it needed no table), `web/src/components/TeamTree.tsx`, e2e spec | Event kinds (section 4), snapshot + long poll, tree UI with accessibility, supervisor hook for state events (a single call site added in slice 0) | 3 days |
| **3. Selection** | `orchestrator/selection.py`, `api/selection.py`, migration `0070`, `web/src/components/SelectionReasons.tsx`, tests | Rules, budget snapshot, `select` tool, `start(selection)`, overrides, preferences; migrates M17 `start` to accept a selection id (one function in `teams.py`, owned by this slice) | 3–4 days |
| **4. Delivery, reports, guard, launch API** | `orchestrator/delivery.py`, migration `0071`, `supervisor.send_text` (owned here), `api/sessions.py` message and launch (owned here), `bin/acc-guard` `check_rm`, guard corpus | Gaps 2, 3, 7, 8: delivery queue and ack, `task_report` and `report` tool, the guard fix with corpus cases, `topic`/`task` validation | 3–4 days |
| **5. Gates, reservations, restack** | `orchestrator/gates.py`, `orchestrator/reservations.py`, migration `0072`, `broker/gate_ops.py`, `api/gates.py`, `web/src/components/Gates.tsx` | Gaps 5, 6, 9, 10, 11: queue with slots, merge-first, host budget and load ceiling, targeted reruns, `restack`, `reserve` and the gate's reservation check | 4–5 days |
| **6. Planning and roadmap** (added 2026-10-26) | `orchestrator/planning.py`, `api/plan.py`, migration `0073`, `web/src/components/Plan.tsx`, `web/e2e/78-plan.spec.ts`, Guide `Orchestrator.md` § Planning session and roadmap | Tables `orchestration_plan`, `plan_phase`, `plan_version`; `plan*` tools; `start` refused while planning; default lead instructions in the lead's header | 1–2 days |

**Migrations** (coordinator, 2026-10-26, replay `orchestrator-v2-r`): `0069`–`0072` for slices 1, 3, 4 and 5 in
order, after main's `0066` → mobile `0067` → remote-access backup forwarder `0068`. Slice 6 (planning) is `0073`,
after slice 5's `0072`. The earlier 2026-10-25
reservations (`0058`–`0062`) were never merged under those numbers and are released.

**Shared-file rule.** `bin/acc-mcp-team` gains tools from slices 1, 3, 4 and 5. Slice 0 splits it into a tool table
with one section per slice, so each slice adds only its own section. `TeamPanel.tsx` gets two mount points in slice
0 (`<WorkList/>` and `<TeamTree/>`), so slices 1 and 2 never edit it. Each slice has its own Guide subsection under
a heading created in slice 0.

**Order and gates.** Slice 0, then 1–5 in parallel. Slice 3's selection id becomes mandatory for `start` only after
slice 2's tree shows reasons (a small follow-up), so slice 3 can't stall slice 2. Each slice:
- runs related tests only, and the gate queue runs the full suites;
- gets a security review before merge, for the spool tools (1, 3, 4, 5), the guard change (4) and the gate's process
  handling (5).

Total about 3 weeks of parallel work after a half-day foundation, or 16–20 engineer-days sequentially.

## 10. Owner decisions: defaults in effect until he answers

The four questions below are on the owner's list with these recommendations. The slices build the recommended
default, and an answer changes a setting or default, not the design.

| # | Question | Recommendation (in effect) |
|---|---|---|
| 1 | Idle-worker alert outside teams | Teams only, plus an opt-in "worker" mark on any session; no alert on ordinary sessions by default |
| 2 | Selection preferences | Owner-ordered provider/model allow-lists per task/profile; heuristic score only breaks ties, neither is a quality claim |
| 3 | Gate defaults for this host | 2 slots, 1 full suite at a time, load ceiling 16 (20 cores × 0.8) |
| 4 | Restacking | The lead may restack on its own, because a restack is a replay in its own helper's worktree that refuses on conflict, and the gate reruns it. Merging into main still needs a person. This holds only with the threat review's S5-3 to S5-7: results bound to the tested commit and shown stale after a restack, a replay of the helper's own commits only, git with no repository-controlled program, and accd's own "based on the current tip" check |

## 11. Questions for the owner (detail)

1. Should the idle-worker alert (gap 1) also cover sessions outside a team? It's proposed as an opt-in "worker"
   mark.
2. Selection preferences (section 5): resolved by owner-ordered allow-lists, not a
   weighted-score decision; future preference changes remain the owner's.
3. Gate defaults for this host: 2 slots, 1 full suite at a time, load ceiling 16 (20 cores × 0.8)?
4. Should the lead be allowed to `restack` on its own, or only after a person's "yes" in the working list?

## Slice 4 implementation and slice 1 interface (2026-10-25)

Slice 4 is stacked on slice 0 and deliberately does not include the guard correction on
`guard-dynamic-command-reason-review`. Its deployment
switch is `ACC_ORCHESTRATOR_V2_DELIVERY=1` (or `RYNHULT_ORCHESTRATOR_V2_DELIVERY=1`, which wins); absent means off.
Restart accd when changing it: team handler registration is fixed at import, and the router owns the worker's lifespan.
The optional report tool is discoverable even without this switch because agent environments do not receive deployment
settings. The server still refuses that tool while disabled; existing M17 message/status behavior remains unchanged.

Migration `0071` follows `0070`. No predecessor stub belongs in migrations/versions; until `0067` and `0068` land, test
the chain in a combination with those branches.

### Delivery and launch

- The sessions router owns the worker lifespan; no other slice edits the supervisor loop. `supervisor.send_text`
  enqueues only when opted in. Default-off execution remains the existing synchronous implementation.
- Enabled `/message` returns 202 with `{delivery}`; `?wait=8` returns 200 only when acknowledgement was observed,
  otherwise 202 with the current state. GET `/api/deliveries/{id}` uses the session's existing ownership checks.
- Text is bounded to 64 KiB UTF-8, refuses control characters and command prefixes, is vault-redacted before storage,
  and sent literally with tabs/newlines converted to spaces. Preview/hash/byte count describe the redacted text.
  The private queued body is erased on acknowledged/failed/expired states. There are at most 20 pending entries per session.
- Typing waits for idle (or Copilot's non-permission waiting-input state), current session/team/sender authority,
  a fresh permission-screen check and the terminal writer slot. Explicit terminal takeover stops further delivery keys.
  Fixed Esc/C-u clear suggestions/input; no supplied text becomes terminal control sequences.
- Existing framed broker streams avoid the old paste helper's 4,000-character and three-Enter policy. There is one
  Enter-only retry, and only with positive pending-input evidence; there is never an automatic second paste.
  Hook acknowledgement matches a post-baseline prompt; conservative screen acknowledgement requires new echo plus
  an empty recognized input row, never echo alone. Unknown provider UI fails visibly rather than implying success.
- State callbacks fail outstanding deliveries when the target or sender stops, so stop/resume cannot revive stale instructions.
- Queues retain FIFO per session and avoid busy-session starvation. A 15-minute queued TTL bounds waiting. A delivery's
  deadlines follow its progress (each key written and each terminal output frame read; `delivery.Progress`): it fails
  after 15 seconds with nothing moving, never runs past 2 minutes, and after Enter it waits for acknowledgement until
  4 seconds pass without terminal output, so a long task on a busy machine isn't failed while the provider is still
  echoing it (review 2026-10-26). Interrupted typing/submission fails as uncertain on restart; queued work can resume. Worker
  operations remain inside manual HA authority, so a standby cannot consume an active main's queue.
- Launch `task` accepts up to 64 KiB through the queue, outside argv and compiled launch files; it cannot combine
  with `initial_prompt`, issue-derived prompts or automation. When disabled it is refused. `topic` with a project is
  always a 422 with the task hint, including previews, before any local folder/session is created.
- **Waking the lead** (review 2026-10-26, L-2). When the person answers a card (or marks a *Needs you* card done),
  edits the plan or edits NOW, the same transaction queues one line to the lead from `supervisor`:
  `[From Rynhult, not from your person or an agent]: your person answered a card on the working list. Read the working
  list and the plan (worklist, plan) before you go on.` It carries none of the person's text. At most one unclaimed
  line is queued per lead: further changes join that line. Changes after delivery claims its text keep a separate
  coalesced follow-up, because the lead may already have read its tools before acknowledgement reaches Rynhult.
  Delivery waits until the lead is idle, so a lead gets at most one per turn. It waits up to the team's expiry instead
  of the 15-minute TTL, and nothing is queued for a stopped lead or team, or with this switch off.

### Reports and the transactional needs outbox

The caller's spool identity alone chooses the report session and immutable team. `task_id` is the matching parent
start-request ID (the lead uses its session ID). A helper cannot report for another member. One report per assigned
task is accepted; counts and relative paths have fixed schemas/caps. Report commands are untrusted evidence and
are **never executed**. Branch/head/handoff metadata comes from confined read-only broker operations on the stored
assignment. Missing metadata refuses the report with a fixed error. Reports are vault-redacted before persistence.
GET `/api/sessions/{id}/report` uses existing person/session ownership. Audit `report.received` and event
`team.member.report` contain identity/status only. Completed helpers with a committed matching handoff get partial
evidence, not an invented success report; helpers with neither raise a deduplicated Inbox item.

**Interface agreed with the coordinator for slice 1:**

1. Slice 4 persists `TaskReport.needs` as at most 20 objects `{title, steps}`, already redacted, and emits
   `Event(source="orchestrator", kind="team.member.report", session_id=reporter, payload={id, status})`.
2. Slice 1 calls `delivery.pending_needs(db, team_id)` in its own authorized per-team write transaction. This
   returns unconsumed TaskReport rows ordered by received_at; each has session_id, task_id, team_id and needs.
3. Slice 1 creates/triages its own work items with provenance from `(session_id, task_id, need_index)`, then calls
   `delivery.mark_needs_ingested(report_row)` **in that same transaction**. Commit both together; rollback leaves
   the needs available. The caller must serialize consumption per team. Empty needs may simply be marked consumed.
4. Slice 4 never writes slice 1's tables or owner answers. The module is importable before slice 1 exists, so this
   branch can be tested independently. Actual conversion to work items belongs to slice 1/integration.

The existing frontend is unchanged in this slice: delivery ticks/report presentation can consume the new APIs in
its owning UI work. Queue acceptance is not acknowledgement. The focused browser spec exercises the authenticated
API path and the existing session window; it does not claim to add those new UI components.

## Slice 6: the planning session, the roadmap and the lead's instructions (2026-10-26)

Owner's requirement: a new run starts with a planning session (idea, scope, design decisions, phases). The lead keeps
the plan documented, diagrammed and versioned, the person answers asynchronously with one decision per card, and the
person can still talk to the lead directly. This slice closes the three biggest gaps the orchestration playbook's
handoff found against v2: no planning step, no roadmap with dependencies, and default instructions not in the lead's
header. It is part of the preview, off unless `ACC_ORCHESTRATOR_V2=1`.

**Tables** (migration `0073`):
- `orchestration_plan` (one per team): `state` (`planning`, then `ready`), `idea`, `scope` (`{in, out, parked}`),
  `version`, `ready_at`.
- `plan_phase`: `key` (unique per team), `title`, `summary`, `check`, `status` (`planned`, `active`, `done`,
  `dropped`), `depends_on` (phase keys), `position`.
- `plan_version`: one row per saved version: `version`, `reason`, `actor`, and a `snapshot` of the whole plan.

**Lead tools** (lead only, depth 0, through the spool):
- `plan`: the plan, each phase's `blocked_by` (dependencies not yet done or dropped), the open questions (the
  working list's open decisions and questions), the last 20 versions, and the roadmap as a Mermaid flowchart and as
  markdown for the lead to commit as `docs/ROADMAP.md`. Plan text is escaped there, so an idea, scope line, title or
  check reads as text and can't add a heading, fence, list, link or table column.
- `plan_update` (`reason` required): changes the idea, the scope or phases. Phases are added or changed by key;
  a phase is never deleted, only marked `dropped`. Unknown dependencies, a phase depending on itself and dependency
  cycles are refused. Every accepted change saves a new version.
- `plan_ready` (`reason`): ends the planning session. It needs the idea, at least one in-scope line and one phase
  that isn't dropped. Open questions may stay open. It adds a working-list *note* (no Inbox item) naming the version,
  the phases and the open questions.
- `plan_version` (`version`): one earlier version.

**The gate.** While a team's plan is `planning`, Rynhult refuses `start` for every member of the team. A team that
already had helpers before slice 6 (no plan row) is exempt, so turning the preview on doesn't stop running teams.

**Person API** (read only, as for the working list): `GET /api/teams/{lead}/plan` and
`GET /api/teams/{lead}/plan/versions/{n}`. The Team tab shows a **Plan** card above the working list. The person
changes the plan by answering cards or by talking to the lead.

**Lead instructions.** `planning.LEAD_INSTRUCTIONS` is the playbook's default lead instructions, adapted to the v2
tool names. `teams.team_files(..., lead=True)` adds it to a v2 lead's context at launch. Helpers never get it, and
with v2 off the M17 header is unchanged.

**Follow-up, same day: diagrams, defaults and person edits.**
- **The roadmap diagram** is rendered in the browser (`web/src/components/MermaidDiagram.tsx`, `mermaid` 12.2.0
  loaded on first use). Three layers keep agent text from becoming markup or script:
  - the server strips Mermaid syntax and directive characters from each phase title (`planning.mermaid`), and
    the node ids are Rynhult's own;
  - the browser renders with `securityLevel: "strict"` and without HTML labels, so no `foreignObject`. Strict
    mode alone still applies a diagram's own init directive (`themeCSS`, fonts, theme variables, HTML labels), so
    those options are in Mermaid's `secure` list and a directive can't change them (review 2026-10-26);
  - the SVG is sanitized again with DOMPurify's SVG profile, which also removes links and `use`.

  Spec 78 renders a hostile source (an init directive, HTML and script in labels, `click` calls and a
  `javascript:` link) and checks that nothing runs and none of it reaches the page; another source's directive
  tries to restyle the diagram and the page, and doesn't.
- **A default until answered** (`work_item.default_answer`, migration `0073`): `{option}` for a decision, `{text}`
  for a question, set with `worklist_add` or `worklist_update`.
  - Items carry `in_effect`: the person's answer once given, otherwise the default.
  - The Inbox item and the card say what the lead goes ahead with. A default pointing at a removed option is
    dropped.
- **Person edits to the plan** (`PATCH /api/teams/{lead}/plan`, with the `version` it was made against; 409 with the
  current plan if stale). Each edit is a new version under the person's name and marks the fields it touched
  (`person_edits`: `idea`, `scope`, `phase:<key>`).
  - `plan_update` is refused for a field the person edited after the lead's last `plan()` read.
  - Once the lead has read the edit, its change is allowed. The working list then gets a note From Rynhult saying
    what it changed and why, and the person's version stays in the history.

**Still open:**
- The plan is still kept per team. The committed `docs/ROADMAP.md` carries it to the next run.
- Rynhult's wiki viewer doesn't render Mermaid. Only the Team tab does.
