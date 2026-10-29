# Orchestrator (M17)

Approved by the owner, 2026-10-22. An **optional** role a person gives one session in a project. It works as a team lead
under that person: it plans the work, starts other agents to do parts of it (as Claude ran Codex and Copilot sessions
overnight), checks their results, and brings their work into its own branch, so the person reviews **one** pull
request. It acts freely within the limits the person set. Its agents may start their own helpers, within the same
limits. Nobody gets this role by default, and ACC never starts an Orchestrator by itself.

## Giving the role
New session → **Orchestrator** (off by default), with its limits. They're stored with the session, and only a person
can change them:
- **Agents and profiles** it may start (e.g. Codex with Backend, Copilot with Reviewer), with their permission
  modes. A mode that skips permissions needs the same confirmation as launching it by hand, given once here.
- **At once / in total:** the most helpers running at the same time (default 3) and started over the whole run
  (default 10).
- **Depth:** how many levels of helpers-of-helpers (default 2; 1 means only the Orchestrator starts agents).
- **Machines** its helpers may run on (default: the project's machine).
- **Time:** how long the team may run (default 8 h). At the end, every helper is stopped and the Orchestrator is
  told.
- Optional: the person's usage budget applies to the team as it does to their own launches.

The whole team counts against one set of limits, however deep it goes.

## What it can do (the `acc-team` tools)
An MCP server (`bin/acc-mcp-team`) is given only to sessions in a team. Its tools:
- `start(name, agent, profile, task, kind=temporary|persistent, machine?)`: a helper in its own git worktree,
  branched from the caller's current branch. `task` becomes its first prompt; ACC adds a short header naming the
  team, its lead and the branch to commit to.
- `list()`, `status(helper)`: state, what it's doing now, its branch and commits ahead.
- `read(helper, last=N)`: its last messages and its handoff note.
- `message(helper, text)`: typed into the helper's terminal, as the Changes tab's "send to agent" does.
- `stop(helper)`, `finish(helper)`: finish means stop, archive and remove the worktree once its branch is merged
  or abandoned. Temporary helpers are finished automatically once their work is merged.
- `merge(helper)`: merges the helper's branch into the caller's branch in the caller's worktree, by fast-forward or
  a merge commit. A conflict is reported back with the conflicting files, and nothing is left half-merged.

A helper with depth left gets the same tools for its own helpers. A lead can only act on its own helpers, and on
their helpers.

### How the tools reach ACC without a token
Agents never get a dashboard token. The MCP server writes each request into its own session's spool, like
`acc-request`. ACC reads it with the session it came from, checks it against the team's limits, does it, and writes
the answer into that session's run folder (`team/<request id>.json`, through the broker, as the agent account),
where the tool reads it. A request in any other session's spool is refused: requests count only from sessions that
are members of the team, and only for that member's own helpers.

## What it can't do
- Merge into main or master, push to them, or merge a pull request (`gh pr merge`): guardrails block these for
  every member of a team. The Orchestrator pushes its own branch and opens **one** pull request; the person merges.
- Exceed its limits, use an agent, profile or machine not on its list, or change its own limits.
- Get around guardrails, publish without approval, or see other people's work. Helpers run as the same person's
  agent account, with the same rules as any of their sessions.

## What the person sees
- **Sessions** and the sidebar show the team as a tree under its lead (Needs you / Active / Idle still apply to each
  member).
- The lead's page gets a **Team** tab: the tree, each helper's state, branch and commits; the limits with what's
  used (helpers running, started, time left); **Stop team**, which stops every member at once; and a log of the
  lead's decisions (each start, merge and stop, with the task it gave).
- Needs-you items from helpers reach the person's Inbox as usual, labelled with the team. The lead also sees them
  through `status`, and may answer a helper with `message`, but it can't approve a permission prompt on the person's
  behalf.
- Audit: `team.grant`, `team.start`, `team.message`, `team.merge`, `team.stop`, `team.finish`, `team.expired`.

## Decisions while building

- Migration 0032 follows tools (0030) and private workspaces (0031). Team launch files retain the person/project tool-approval policy and the private-workspace local-only flag. Tool requests and Files operations use each helper's actual machine; tool approval remains with the person.
- The lead must launch in its own worktree in a managed, trusted git project. Helpers always branch from the caller's current commit. Ordinary sessions retain their original defaults and launch files.
- Permission choices are frozen as full launch inputs when the person grants the role. Helper requests cannot choose a permission mode, skills, MCPs, paths, limits or an owner. An unconfirmed permission-skipping helper choice refuses the entire grant.
- A start reserves a lifetime slot before broker work. Failed starts consume that slot; this conservative accounting prevents retries from evading the total. Running means desired-running, including starting and recovery. A stop that the machine has not confirmed keeps its running slot, including archived helpers, until reconcile confirms the process is gone. Such helpers cannot merge or resume. Stopped helpers can resume only while the grant is live and a shared running slot is available.
- `list` includes the granted agent/profile/permission choices, machines, limits, shared counters, caller depth and expiry alongside its own descendants, so a lead or helper can discover its bounds without guessing.
- Requests have session-scoped ids and durable results. A request interrupted after acceptance is not automatically repeated; its reply asks the agent to inspect the team before retrying. Duplicate spool entries reuse the original result.
- Team guards also refuse switching to or rewriting main/master (including a switch followed by a merge in one shell command), and direct `git update-ref`; ordinary session guard policy is unchanged.
- A helper must be stopped before merging. The caller must still be on its original ACC branch, with a clean index and working tree and no git operation in progress. Conflicts abort the merge and report files; hooks and signing are disabled for ACC's merge operation. Explicit **finish** abandons any unmerged branch, retaining its commits and any dirty worktree. Temporary helpers are finished only after a successful merge.
- A team message is refused while a helper needs input or a screen prompt is detected. The person answers permission prompts in Inbox/Terminal. Expiry stops helpers, closes the grant and notifies the lead (terminal text only when idle with no screen prompt; otherwise Inbox); **Stop team** also stops the lead. Stop intent for all members is committed before contacting any broker, so offline helpers are stopped when their machine reconnects.
- Cross-machine helpers keep the same person's project agent account. Only enrolled machines on the grant are eligible. ACC transfers verified git bundles through brokers (no agent tokens, remote URLs or network clone). Each bundle is capped at 8 MiB; larger repositories must use the same machine. Imported branches and the remote backing repository remain for recovery and audit.
- Spool authority comes from the session whose registered spool accd reads, never a supplied caller/team id. Claimed session mismatches and authority fields are refused; symlinked spool paths and reply folders are refused. As in the existing spool design, sessions sharing one Unix account are not OS-isolated from each other's files; the guard protects run folders during tool calls. Hostile arbitrary code running as that same account is outside that boundary.
- Team grants are immutable for this run. Changing limits requires the person to stop the team and launch a new lead; agents have no grant/edit endpoint. The existing person's usage budget applies to helper launches at every depth, without an agent override.
