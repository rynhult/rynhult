# Orchestration playbook

**Status:** a practice guide, 2026-10-26. It describes how a **lead** runs a team of agent helpers for one person,
the **owner**. The process was worked out by a human coordinator running many parallel helpers on this project.
It is written so that it can become [Orchestrator v2](ORCHESTRATOR-V2.md)'s default lead instructions. v2 is a
preview, off by default.

Where v2 already has a feature for a step, the step names it:
- the **working list**
- the **tree**
- **selection**
- **delivery** and **reports**
- **reservations**
- the **gate queue**
- **restack**

Without v2, the same steps work by hand with a shared file, handoff notes and a test script.

The principles:
- The owner's attention is the scarce resource, so give it one place.
- Helpers do small, checked pieces of work.
- Nothing merges without a gate.
- Decisions that matter stay with the owner.

## 1. Start with a planning session, and keep the plan documented

A new run starts with a **planning session** with the owner, before any helper is started. It covers:
- **the idea:** the problem, who it is for, and what "done" looks like;
- **the scope:** what is in, what is explicitly out, and what is parked for later;
- **the design decisions:** each as its own decision card (section 2), with a recommendation;
- **the phases:** small, shippable steps in order, each with its dependencies and how it will be checked.

The lead keeps all of this **documented, diagrammed and versioned** in the repository, so it outlives any one
session:
- **A roadmap page** (for example `docs/ROADMAP.md`) lists the phases with their status and dependencies.
  - A flowchart (a Mermaid `flowchart` block, or a diagram file next to it) shows what blocks what.
  - Each phase links to its design notes, branches and handoffs.
- **The working list** holds the open needs and decisions (section 2).
- **Versioned** means committed:
  - Each change to the plan is a commit with a one-line reason.
  - Superseded decisions are marked superseded and kept, not deleted.
  - The owner can always see what changed since they last looked.

**The owner works through it asynchronously.** They read the roadmap and the list when they have time, and fill in
answers in any order. The lead never needs them present. Unanswered cards get a recommendation and a default that
applies until answered, and the default is labelled as such. A default only covers work the lead can undo inside the
agreed scope: it is never the owner's answer or approval, and scope, machines, anything public and money wait for the
real answer.

**The owner can talk to the lead directly at any time,** in the lead's own session. The lead treats what is said
there like a working-list answer:
- it updates the plan and the list to match;
- it records the decision on a card, so nothing agreed in conversation is lost.

**In v2 today:**
- **Covered:**
  - asynchronous answers through the working list, in any order, with your edits winning;
  - talking to the lead directly, in its terminal or with a message;
  - a planning session that Rynhult enforces before any helper starts;
  - a versioned roadmap with dependencies, drawn as a diagram on the Team tab;
  - each card's default until answered;
  - person edits to the plan, which the lead can't overwrite unread.

  The Guide's [Orchestrate a project](wiki/User-Guide/Orchestrate-a-Project.md) walks through all of it.
- **Not yet in v2:** a plan that outlives the team. The lead commits `docs/ROADMAP.md` so the next run starts from
  it.

## 2. One channel to the owner: the working list

Everything the lead needs from the owner goes to one list, never scattered across chats. In v2 this is the
**working list** on the lead's Team tab (`worklist_add`, `now`, `worklist_ingest`).

- **One decision per card.**
  - Each card gives 2–6 options, each with its consequence, and a recommendation with a one-line reason.
  - Never bundle decisions. A card holding three questions gets one answer and two guesses.
- **Tasks for the owner are steps.**
  - One action per line, each with the expected result: "3. Run `…` → it prints `ok: 4 checks`".
  - Say where to run it and what to do if the result differs. The owner shouldn't need to ask a follow-up question.
- **Ingest every answer.**
  - When the owner answers, act on it and mark the card ingested (the card then shows *Read by the lead*), with what
    changed: "pinned 16 in compose; branch X restarted".
  - An answer the lead never acknowledged looks lost.
- **Keep a NOW summary at the top.** Three to six lines:
  - what is running;
  - what is waiting on the owner;
  - what just shipped;
  - what's next.

  Refresh it on a cadence (hourly while work runs) and whenever something important changes. If the owner has
  edited NOW, start from their version.
- **Notify only for "needs you now".**
  - A blocking decision or an owner-only action raises one notification. In v2, each open item raises one Inbox
    item.
  - Progress, finished helpers and passing gates go to NOW, not to the owner's phone.

## 3. Delegation: small tasks with written briefs

Give each helper one focused task that fits in a single review. In v2, the lead asks for a **selection**
(`select`) and then starts the helper (`start`) with the brief as its first message (**delivery**).

Every brief states:
1. **The goal,** and why it matters.
2. **The scope:** the branch to start from and the branch to create, the files or area in play, and **what not to
   touch.** Name other helpers' areas and shared infrastructure.
3. **The tests to run:** related tests only; the gate runs full suites.
4. **The finish:** leaf workers commit without merging, pushing or opening PRs. Coordinating helpers merge only
   stopped descendants through `acc-team merge`; only the root opens the consolidated PR. Nobody merges main.
   Keep one versioned handoff per attempt (`docs/handoffs/<date>-<attempt8>-<slug>.md`), never overwrite an earlier
   attempt's file. Read-only reviewers persist findings through `report` instead of writing files.
5. **What to ask rather than pick:** migration and decision numbers, scope changes, anything public.

Helpers report what they **didn't** verify, as plainly as what passed. A helper that finishes without a report or a
handoff isn't done. v2 raises an item for it.

## 4. Independent review for anything security-relevant

Changes to these areas are reviewed by a **different helper, preferably on a different model**, before they merge:
- guardrails and command filters;
- root or privileged helpers;
- authentication, sessions and permissions;
- cryptography and key handling;
- installers and anything that runs as root.

In v2, selection refuses to let a session review its own work.

- **Give the reviewer the code, not the author's conclusions.** The brief names the branch, the threat to consider
  and the earlier reviews.
- **Severity levels:**
  - The read-only reviewer reports findings without fixing code. Remediation is a separate assignment with tests.
  - Sensitive fixes receive a second independent review of the fixed head.
  - **Lows** are listed with a recommendation for the owner.
- **What the review records:** each finding's class, impact and fix, at the level of finding classes. No exploit
  recipes in the repository.
- **Review is not qualification.** A reviewed change still states what it was never run against: real devices,
  root, live providers.

## 5. Gates: test every branch merged with current main

A branch merges only after a **gate**. A gate is the branch merged with current `main`, then:
- the full test suite;
- the build;
- the browser suite;
- the docs and wiki checks.

In v2, this is the **gate queue** (`gate`, `gate_status`), with merge jobs ahead of checks and a host test
budget.

- **Flakes:**
  - Rerun a failing test alone. If it passes alone and is a **known** flake, record it as such. v2 shows
    "passed on rerun".
  - Give every recurring flake **its own task** to find the cause. Don't loosen assertions or add sleeps; wait for
    an owned condition.
- **A long queue:** group small, related branches into one combined gate run. If it fails, split it to find the
  culprit.
- **A backlog beyond the machine:** borrow temporary compute and release it afterwards. Watch the load: an
  overloaded host fails tests that are fine, so cap concurrent full suites.
- **What a gate result covers:** a pass is for the exact commit tested. A branch that moved needs a new gate.

## 6. Shared bookkeeping

- **One registry for numbers.**
  - Migration and decision-record numbers come from one list, kept by the lead. Helpers **ask**; they never pick
    the "next free" number themselves.
  - In v2, `reserve(kind)` hands out numbers, and the gate refuses unreserved ones.
  - A skipped number is recorded as skipped, not left looking reserved.
- **Stacked branches are replayed after their base merges.** Squash merges break stacks, so after a base lands,
  replay its children onto `main` as `<name>-r`. In v2, use `restack`.
- **Conflicts are logged, never silently dropped.**
  - Resolve each conflict keeping every reviewed behaviour from both sides, and write down how.
  - Generated files are regenerated, not hand-merged. Inventories are merged by entry.

## 7. Shipping

- **Ship and deploy as one step** when a change alters the database schema. A merged migration that isn't deployed
  leaves the running instance and `main` disagreeing.
- **A deploy:**
  1. backs up the database first;
  2. dry-runs the installer;
  3. **refuses while a backup capture is running**;
  4. then deploys, checks health, runs smoke tests, and diffs the published sites against the expected list.
- **Release notes:** written for people who run the software, grouped by what changed for them. Each one says what
  is off by default or in preview.

## 8. Watching the team

- **Notice quickly** when a helper is idle with nothing queued, stuck on a prompt, or failing because its provider
  is out of capacity.
  - In v2, the **tree** shows each helper's state.
  - An idle helper with nothing queued for 5 minutes raises an item.
  - Delivery states show whether a message really arrived.
- **On every report from a helper,** also check the gate queue and the machines (load, disk, stray processes), and
  update NOW.
- **Stop processes precisely:** by their recorded unit or exact process ID, never by pattern matching.
- **Provider capacity errors:** retry later or reselect another allowed provider. Don't let the helper spin.

## 9. Owner decisions stay with the owner

The lead brings **options and a recommendation**. The owner decides:
- **Scope:** what is in a release, and what is parked.
- **Machine changes:** root actions, system packages, services, firewall and DNS.
- **Anything public:** publishing, repositories, posts, and messages sent outside the team.
- **Money:** accounts, subscriptions, paid compute and certificates.

A guardrail refusal is an answer, not an obstacle: never work around it. Ask, or choose another approach.

## 10. Lessons from a long run with many helpers

Learned coordinating a dozen helpers through one night's release work. Each one cost real time once.

- **One browser gate at a time on a shared machine.** Three gates beside ten helpers' own tests overloaded the host,
  and two gates each reported dozens of browser "failures" that were only load. That was 90 minutes lost.
  - Combine compatible branches into one gate instead of gating each.
  - Have helpers run related tests only, with limited parallelism (for example 4 workers) and single browser specs.
- **Check that the queue actually runs.** A queue runner whose counting command printed two lines never started a
  gate, and nothing said so. Read the runner's log after starting it.
- **Read the numbers, not the word "done".** A report or gate is finished when its counts say so: passed, failed,
  skipped. Open the log when a count is missing.
- **Run a pre-ship check after the final merge,** on the merged tree:
  - repository-wide lint;
  - one migration head;
  - generated files up to date;
  - the docs checks.

  Branches that were each clean can still combine into a lint or numbering error.
- **Keep independent review before merge.** On the night of 2026-10-26 it caught four High and several Medium
  findings that every author's own tests had passed.
- **Dry-run merges of overlapping fixes** in a scratch worktree (`git merge --no-commit --no-ff`, then abort) before
  choosing their order. Note the conflicts in the plan, not after the fact.
- **Say "do the work now; the merge waits until X".** "Held until after the release" was read as "don't start", and
  the helper sat idle.
- **Suggested prompts in a helper's input box are not instructions.** CLIs show plausible ghost suggestions ("Re-merge
  …", "fix the flaky …"). Only the person's and the lead's messages count; say so in every brief.
- **Never route around model safeguards.** If a provider's safeguards stop a task, stop it and ask the person. Don't
  hand the same task to another model or provider, and don't accept a switch to an older model to get past it.
- **Keep a watcher on idle helpers, and relaunch it every time it ends.** Watch for capacity and rate-limit errors on
  screen too: a helper can stop on one while its state still says working.
- **Keep commits clean in shared worktrees.**
  - Never `git add -A` in a worktree where you linked or copied tooling, such as a dependency folder.
  - Read `git diff --cached --stat` before committing a merge.
  - Make ignore patterns match files and links, not only directories (`node_modules`, not `node_modules/`). A
    committed symlink once replaced a real dependency folder on every checkout that pulled it.
- **Tell helpers how to inspect tools.** List a team tool's actions through the test suite's fixtures, never by
  running the agent tools against a live instance.

## Default lead instructions (for Orchestrator v2)

This is the text Rynhult adds to a v2 lead's instructions at launch (`LEAD_INSTRUCTIONS` in
`src/acc/server/orchestrator/planning.py`, v2's planning slice). Helpers don't get it. The code is the source;
`tests/test_orchestration_playbook.py` fails if this copy differs.

```text
Orchestrator v2: you lead a team for one person (your person). Their attention is the scarce resource.
- Read the current project brief before plan_ready, if the brief tool is listed. Separate the person's original
  material, your interpretation, and the requirements. Admit unreadable images and request a usable description.
  If an image cannot be read, offer Check image access, upload a usable reference, or an explicit skip. Never close
  a free-text need empty or repeatedly demand transcription when those alternatives are available.
  Ask only about material ambiguity or scope changes; don't repeat questions already answered in a complete brief.
- Start with a planning session, before any helper: idea; scope (in, out, parked); requirements with acceptance;
  the person's constraints; phases and dependencies. Use plan_update and plan_ready only when listed.
  The app exports docs/ROADMAP.md and docs/PROJECT-BRIEF.md; do not ask read-only agents to write them.
  Review and commit approved public exports on your writing task branch before final handoff; never discard them
  or commit unrelated files to clear a dirty-tree refusal. Overlapping export changes need explicit review.
- Respect the effective plan mode within the frozen provider grant. Prepare is read-only: only read-only discovery
  assignments, no implementation.
  The plan holds the effective mode; the brief's mode is a suggestion. Only a signed-in person can widen to Build
  using Team > Plan > Run mode. You may narrow to Prepare with plan_update after stopping helpers; this stops
  your lead so read-only permissions apply when the person resumes it.
  Reading a constraint does not authorize changing it. Propose changes for the person to decide; never rewrite
  person constraints yourself. Respect decision blocks on phases and requirements; dropped prerequisites do not
  satisfy dependencies. Revalidate assignments after material requirement/brief changes and notify affected helpers.
- Read plan() before plan_update; retain versions and give reasons, mark obsolete phases dropped. Your person
  may answer asynchronously, edit the plan or talk directly. Record agreements on the working list and in the plan.
- One decision per card, never bundled: options, consequences, a recommendation and its reason. Work on a default
  only when reversible and in scope, with no blocking dependency. A default is never your person's answer or approval.
  Scope, machine changes, public actions, money and owner approvals wait for the real answer. Never work around a refusal.
- Use working-list cards for what needs your person; progress belongs in NOW (3-6 lines: running, waiting,
  shipped, next), refreshed at least hourly. Ingest each answer with what changed; preserve the person's edits.
- For each assignment choose the smallest sufficient profile. Set need.role to its profile ID or catalog role.
  No-profile candidates are only for role: general. Explain the chosen profile and why. A missing matching profile
  means ask the person to add <profile> to the grant; name the closest choices, never widen the frozen grant.
- Use assign{goal,task_type,acceptance,requirement_ids,need}, then select{assignment,pin?}, then
  start{assignment,selection,name,kind}. A pin still goes through ranking and the task allow-list. No plain start in v2.
  Each brief includes scope, exclusions, related tests, acceptance, requirement IDs and role-specific Git rules.
  Default kind: temporary; helpers are disposable. Stop and replace completed, blocked or failed helpers within
  concurrency limits. Use give{helper,assignment} for a new assignment to an idle persistent helper.
- A new attempt on the same assignment must use its continuation pack: assignment and referenced requirements and
  constraints, latest report/handoff from every earlier attempt (paths and summaries; read full content as needed),
  earlier branch/head and what was merged. Continue from the previous head unless you explicitly choose fresh.
  Do not rebrief from scratch or lose earlier evidence. Keep both attempts attributable. If launch is uncertain,
  inspect/adopt the recorded attempt; do not duplicate it with a new selection.
- Read-only reviewers never fix. Persist findings through report{findings_md}; remediation is a new assignment.
  Security-sensitive fixes get a second independent review of the fixed head, preferably a different model.
  Bind review evidence to the reviewed revision; a review of an older head is not approval of the fix.
- Only use gates, reserve and restack when listed as available. Helpers run bounded related checks and list skips;
  the gate owns the full suite. Gate unavailability means limited verification, never an invented pass or an
  unmanaged full run. Retain original failures. A retry-passed rerun is not integration success until a person
  accepts a flake task or a fresh clean gate passes. Only the owner's project list defines known flakes; list them in the PR.
  Reserve numbers only when available; otherwise ask the person/lead to coordinate numbers. Restack only stopped
  helpers when available; record conflicts and verify the resulting head, base and gate commands.
- Watch idle, stuck and capacity-errored helpers. On reports check evidence and available gates, not confidence.
  Respect stops, expiry and assignment changes; report limits. Only the root opens one consolidated PR when a remote exists and publishing is authorized; otherwise offer a local merge/handoff. Never invent/add a remote or merge main.
- Native subagents share the parent's slot and permissions, are not shown in the tree, and their count cannot be capped.
  Council uses managed helpers within grant limits, or sequential single-session perspectives; these are not independent.
  Profiles, skills and native subagents confer no additional authority.
```
