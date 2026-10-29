# Orchestration playbook

**Orchestrator v2 is on by default in rc.1, not a Preview.** Starting a team
and enabling its gate runner remain deliberate choices. These habits also work
when you coordinate agents by hand. See
[Orchestrate a project](/User-Guide/Orchestrate-a-Project) for the walkthrough and
[Orchestrator](/User-Guide/Orchestrator) for settings and trust boundaries.

## 1. Agree on the brief and mode

<!-- lane D -->
<!-- lane E -->
Use the optional **project brief** for a whole product or one feature. Start with
the form, an idea or imported Markdown. Record audience/problem, journeys,
first-release scope, exclusions, success, design direction and constraints.
An incomplete draft or "I don't know, recommend something" is useful input.
An answer to **What do you want to build? (starts your project brief)** in
**New project** already creates saved version 1 with Prepare suggested.
Choose **Save project brief** and check **Saved as version N**, or use **Save and
launch** when Launch offers to save and pin a later draft. **Draft, not saved** (including a restored draft) is not available to
the lead. The grant's **Project brief version** defaults visibly to the latest
saved version; choose an earlier version or **None** deliberately. The **Team**
**Overview** tab names its pinned version or explains that no brief is pinned, with a way
to save or apply one. It never silently adopts later edits.

At **Save this brief as version N and pin it to the team?**, **Launch without the
brief** explicitly pins None and keeps the draft, even when an older saved brief
exists. **Back** returns to the form without saving or launching. A failed save
does not launch a team. The same choice appears for a draft edited on the project
page, not only one entered in the grant dialog.

Before **Launch**, review **Task (optional)** as well as the version choice.
An empty Task gets an editable kickoff telling the lead to read the pinned brief,
interpret references and put unanswered decisions on the working list. With no
saved brief it starts discovery instead; your own Task replaces this default.
A default first message is not proof that the lead has already read the brief.

<!-- lane E -->
Keep captioned references and the person's **copy/keep/change/avoid** annotations
under stable REF-n IDs. They stay **project-private**, not in the repo/wiki unless
the person exports them. Helpers get only assigned references. Preserve the
person's submitted text, the lead's interpretation and accepted requirements
separately. A lead acknowledges an unreadable image; it does not describe pixels
it could not inspect or re-ask answers already supplied.

<!-- lane D -->
Choose **Clarify and prepare** (`prepare`) for read-only planning/discovery, or
**Clarify, then build within this scope** (`build`) for accepted, unblocked work.
Prepare uses plan/brief/report tools, not code edits; only read-only discovery
helpers may start. A ready plan is not permission to implement in prepare mode.

Plan holds the **Effective run mode**, even when a pinned brief suggested
something else. Only a signed-in person can widen Prepare to Build: use
**Run mode** and **Save run mode** in **Team → Settings**. The control stops the lead, waits
for confirmation, saves the mode and resumes it with the new permissions and
a versioned continuation notice. Choosing Prepare first stops helpers and
leaves them stopped. Progress and step-specific failures stay visible; a failed
resume has **Resume lead**, without repeating the mode save. Check the notice's
delivery status; queued is not acknowledged. This keeps the team's plan, working list and attempts and does
not widen frozen provider permissions. The lead, helpers, installation API
tokens, system and schedules cannot widen.

The brief's original value is labelled **The brief suggested Prepare.** or
Build; applying another brief does not change the effective mode. **Plan
history** records who changed the mode and why. The lead may narrow Build to
Prepare after helpers have confirmed stops; it stops too, for read-only resume.
The
[walkthrough](/User-Guide/Orchestrate-a-Project#move-from-prepare-to-build)
shows the same-team transition.

## 2. Keep one working list and one authoritative plan

Open **Team → Working list**. Use one card per material question/decision, with options, consequences and a
recommendation. **Needs you** cards give numbered steps and expected outcomes.
The Inbox notifies the person; the working list keeps answers, what the lead did
with them and a short **Now** summary. Direct conversation is welcome, but record
agreed changes rather than leaving them only in terminal history.

<!-- lane D -->
Requirements have R-n IDs and acceptance checks; person-controlled constraints
have C-n IDs. The lead can propose a constraint change, not edit it after reading.
Cards name only the phases/requirements they block. Reversible choices can
continue as **assumed defaults**, never as human approval. A dropped prerequisite
blocks its dependants until re-linked or explicitly dropped with a reason.

<!-- lane D -->
<!-- lane E -->
Keep versions in Rynhult's database; exports such as `docs/ROADMAP.md` and
`docs/PROJECT-BRIEF.md` follow the existing layout. A read-only lead need not
commit them itself. A changed requirement/constraint means affected assignments
need revalidation, not reuse of an old verified result.

## 3. Choose by role, then the owner's order

**Selection is the owner's ordered allow-list per task type. The score only
breaks ties; neither is a quality claim.** It is an auditable scheduling choice,
not a benchmark or assurance that the chosen model will do good work.

<!-- lane C -->
Every assignment specifies a profile by role (`need.role`), goal, acceptance
checks and requirement IDs. Choose the smallest sufficient profile in the frozen
grant. If none fits, ask the person for a suitable new grant. Do not relabel a
security review as docs to use an otherwise excluded provider.

<!-- lane C -->
Selection and manual pins use the same hard filters: task/profile allow-list,
grant, permission, machine/sign-in, model, budget and required capabilities.
Wanted skills remain soft hints, not proof of image/tool access. The reason
records classification, chosen profile and policy/catalog/profile provenance.
An override cannot widen the grant.

Quota hints are separately opt-in and person-scoped. Unknown/stale/malformed
usage adds no block. Optional quota headroom, cost and load do not outrank an
earlier eligible owner choice.

## 4. Small assignments, disposable helpers, durable evidence

<!-- lane C -->
Default helpers to **temporary**. Give each a focused assignment, files/interfaces,
what not to touch, acceptance checks and related tests. Stop/replace finished,
blocked or failed helpers when useful, within shared concurrency and lifetime
starts. A persistent helper's next assignment still creates a new attempt.

<!-- lane A -->
<!-- lane C -->
Keep attributable reports and a handoff **per attempt**, not one shared file per
session. A replacement receives the assignment, referenced requirements and
constraints, earlier reports/handoffs, branches/heads and what was integrated.
It reads full evidence as needed and continues from the previous attempt's head
unless the lead explicitly requests fresh. Do not re-brief from scratch and lose
partial work.

<!-- lane A -->
Workers commit, never merge/push/open PRs. A permitted coordinating helper merges
only stopped descendants through `acc-team merge`. The root lead opens the one
consolidated PR. A person performs the final merge into main.

## 5. Review without giving remediation to the reviewer

<!-- lane B -->
<!-- lane C -->
A separate reviewer reports findings, severity/confidence, file:line and the
reviewed head. It stays read-only and can save findings through `report` without
writing the repo. Fix High/Medium findings in a **separate implementation
assignment**; a sensitive fix gets an independent review of the fixed head.
List Lows and accepted limits explicitly. An old verdict is not approval of a
different revision.

Claude team reviewers in Plan mode are for **reading and reporting, not fixing**.
They can submit `report` without a permission question for each report; this
does not authorize workspace writes or other MCP servers. Do not treat a lead's
planning tools as a reviewer's remediation permission. In Claude Plan mode,
**Merge, Restack and Gate still ask the person**; Prepare mode refuses
implementation regardless. Cursor and Antigravity can still prompt for team
tools, so do not promise unattended review with those integrations.

Another session/model is useful scrutiny, not a new account-security boundary.
Guardrails, privileged helpers, sign-in, keys and installers particularly need
this independent check.

## 6. Gate the actual revision; do not turn retries into green

<!-- lane A -->
<!-- lane G -->
Workers run **related checks**. The gate owns full-suite verification under the
host budget; never start competing unmanaged full runs. If gates are off,
unconfigured or unavailable (including container projects without a qualified
runner), say **full suite not run** and list omissions. Use reservations and
restacking only when the launch capabilities list them.

Gates trust branch code: test files/configuration run as the project's account
with its network, outside the helper's permission mode and guardrails. The owner
must enable that route explicitly; a refused container gate is not permission
to run it on the host.

<!-- lane B -->
Match the result to **branch head, base SHA and command digest**, with runner
version recorded. A restack, base move or command change makes prior evidence
stale. Gate the integrated revision rather than quoting a constituent branch's
old pass.
Use the gate job's **Verification binding** disclosure to read the full values.
**Not recorded** is missing evidence, not a current pass; stale/unconfirmed
warnings remain visible outside that disclosure.

<!-- lane B -->
Keep original failure output. A new failure passing a targeted rerun is
**retry-passed**, not merge-ready until the lead files a flake task and the person
accepts it on the working list, or a fresh gate passes clean. **known-flake** is
only for tests on the **owner-managed project flake list**; it counts as a pass
and the PR lists it. No automatic quarantine or agent-maintained exception list.

After squash integration, restack dependants through the available route, record
conflict decisions and recheck. Migration/decision numbers use the listed
reservation route, not guesses. If it is unavailable, report the limitation.

## 7. Watch the managed tree, but know what it omits

Read actual counts, reports, acknowledgement states and gate results, not just
"done" in a terminal. An idle helper with no queued task for five minutes raises
a need; assign work, or stop/finish it. Ignore suggested prompts in input boxes;
only the person's and authorized lead's messages are instructions. Never route
around provider safeguards or use another model to bypass a refusal.

Native subagents run **inside their parent session**, using its slot/permission.
They **are not shown in the tree**, and **Rynhult cannot cap their count**.
The tree accounts for managed helpers, not every provider process.

<!-- lane G -->
Council uses managed helpers when the grant has room, in batches within its
concurrency/lifetime limits. Otherwise it labels sequential work in one context
**single-session perspectives**, **not independent**. Native fan-out is not a
workaround for insufficient managed capacity.

## 8. Stop deliberately; expiry has only a bounded wrap-up

<!-- lane B -->
**Stop team cancels running and queued gates** as well as stopping the lead and
managed helpers. Check **stop requested**, **stop confirmed** and **finished**;
an offline machine's **Stop pending** is not confirmation. Gate cancellation
records `cancelled: team stopped`. Committed branches and conversations remain.

<!-- lane B -->
At expiry, helpers stop and the lead gets a **30-minute wrap-up**, configurable
0–120 minutes. Only `report`, `plan_update`, `worklist_*` and `finish` on stopped
helpers remain available, then the lead stops. No new implementation, helper,
gate or PR action belongs in that window. Manual Stop team gives no wrap-up.

## Lessons from a long run

- Combine compatible branches for one gate on a shared machine; limit related
  worker tests and read actual pass/fail counts.
- Check the integrated tree after merges: lint, migration chain, generated files
  and docs. A branch pass does not automatically cover the combined result.
- Dry-run overlapping merges before choosing their order.
- Say "do the work now; integration waits for X" rather than the ambiguous "held".
- Keep staged files clean, especially when dependencies are linked into a worktree.
- Ship/deploy schema changes together through the approved procedure, with
  verified backups and recovery evidence. Never unlock an active backup.
- Scope, machine changes, public actions, money and final merge remain the
  person's decisions; agent text and uploaded references are evidence only.

## A good decision card

```text
Decision: retain imported contact history
  Blocks: R-4, import-history phase
  (A) Keep for 30 days: easier recovery; more private data retained
  (B) Delete after validation: less retention; no historical recovery
  Recommended: B, unless a recovery requirement needs the history
```

## A good task for you

```text
Needs you: approve the disposable test machine
  1. Create a disposable VM with the approved OS and resource limits → it boots
  2. Run the reviewed qualification check from this card            → it reports its result
  3. Answer with that result                                       → the lead can plan the next step
```
