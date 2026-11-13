# Finish PRs in ACC

Scope approved 2026-10-22. A session's
branch can become a pull request and be brought to a merge from the session page. GitHub mutations start with a person’s click; guided feedback may be sent automatically only to eligible sessions.
Agents get no new power, and nothing merges automatically.

## Interaction

A **Pull request** tab appears on a project session (not a local or private workspace) whose project names a GitHub
repository and whose folder is on a branch other than the default one.

- **No pull request yet:** a form with title and body, the base branch (the repository's default) and **Open as
  draft**. ACC prefills the form from the session's handoff note, or from its last summary if there is no note, and
  the person can edit it. **Create pull request** is disabled until the branch exists on GitHub; the tab says that
  the agent has to push it first.
- **Pull request open:** number, state (draft, open, merged, closed), link, and an editable title and body (**Save**).
  **Ready for review** or **Back to draft**. **Request reviewers** (GitHub logins).
- **Checks:** every check run and commit status on the head commit, with required ones marked.
  **Send failed checks to the agent** opens a preview.
- **Review comments:** the unresolved review threads. **Send review comments to the agent** opens a preview.
- **Merge:** the merge methods the repository and branch rules allow, and the reasons a merge is blocked (draft,
  required checks not passed, conflicts, branch behind, reviews required). **Merge** is enabled only when nothing
  blocks it, and asks for confirmation. GitHub remains the final authority: if it refuses, the refusal is shown and
  nothing else happens. ACC never uses admin bypass.

## Whose GitHub identity

The same rule as the existing GitHub views, extended to members:

- **Owners:** the dashboard's GitHub token (`data/secrets/github_token`). It is the owners' token and is never given to
  agents.
- **Members**, and owners when ACC has no dashboard token: the person's own agent account's `gh` login. The broker
  runs `gh api` as that account (`github_api`), as `clone_repo` already does. The token stays in that account; accd
  only sees responses. The broker op accepts only GET/POST/PATCH/PUT on `repos/<owner>/<repo>/…` or `graphql`, and
  caps the output.

A member's project is never read through the owners' token (security review 2026-10-22, #1).

## Send to the agent

The server builds the message, and the person previews exactly that text:

1. `POST …/pull/feedback {kind}` collects the items and builds the text. It stores the text for 10 minutes under a
   preview id, bound to the person and the session, and returns both.
2. `POST …/pull/feedback/{id}/send` types that stored text into the session's terminal (the existing `send_text`
   path), unchanged. A preview cannot be edited or replayed after sending.

Content:

- **Failed checks:** each failed check run (failure, timed out, startup failure, action required) with its name, its
  failing step (for GitHub Actions jobs) and that job's log tail. Timestamps and ANSI codes are stripped, and lines
  are trimmed. Other check runs contribute their output summary. Failed commit statuses contribute their description.
- **Review comments:** each unresolved, non-outdated thread: file, line, author and comment text.

Characters the preview can't show (zero-width, bidi and Unicode tag characters, variation selectors, private use) are removed from every item before redaction, so the person sees everything the agent will read.

The message has the same 4000-character limit as every message ACC types. When the items don't fit, the earlier ones
are trimmed and the message says how many were left out.

**Untrusted framing:** CI logs and review comments are written by others (contributors, dependencies, bots). The
message says so before the content, wraps it in `<untrusted-…>` markers, and neutralises those markers inside the
content, so the content can't close the frame. It asks the agent to evaluate the content, not to obey it.

**Redaction:** the session's vault keys (the vault redactor), then well-known secret shapes: GitHub, OpenAI-style,
AWS and Slack tokens, private-key headers, `Bearer …`, `password=`/`token=` values and credentials in URLs. The
preview is already redacted. The session's audit entry (`session.message`) records the redacted text.

## Guardrails

Agents cannot merge from this feature: merging happens only through this endpoint, which needs a signed-in person and
an explicit confirmation. ACC itself calls GitHub (the dashboard token, or the broker's `github_api` op), never the
agent's shell, so the agent's guardrails don't apply to the person's Merge.

Agents' own merges are refused by a guardrail rule, **No merging pull requests** (`merge_pr`, reviewer request
2026-10-22). It is on by default for every session, and owners can turn it off in Settings like the other rules. It
refuses:

- `gh pr merge` in any spelling: wrappers, `-R`/`--repo` anywhere, nested shells, and `gh` aliases (read from the
  agent's gh config, including `!` shell aliases), plus `gh alias set` that would create one and `gh alias import`;
- `gh api` non-GET calls to `repos/…/pulls/N/merge`;
- GraphQL `mergePullRequest` and `enablePullRequestAutoMerge`, inline, from a heredoc or from a readable file;
- forms it can't check (`gh pr $X`, `gh $SUB`, an unreadable GraphQL query), conservatively.

Like the other rules it is a safety net, not a sandbox: a program calling GitHub's API directly isn't seen.

## Permissions and audit

Owners can act on any session. A member can act only on their own session in their own project. Anyone else gets a
404. Audit entries: `pr.create`, `pr.update` (title/body or reviewers), `pr.ready` (ready or draft), `pr.merge`
(method, sha) and `pr.send_to_agent` (kind, number, item count, length).

## API

`GET /api/sessions/{id}/pull` · `GET …/pull/draft` · `POST …/pull` (create) · `PATCH …/pull` · `POST …/pull/ready` ·
`POST …/pull/reviewers` · `POST …/pull/merge` · `POST …/pull/feedback` · `POST …/pull/feedback/{id}/send`.

Code: `src/acc/server/pulls.py` (GitHub calls, merge checks, message building, redaction),
`src/acc/server/api/pulls.py` (routes), broker `github_api`, `web/src/components/PullRequest.tsx`.

## GitLab

ACC supports GitLab issue and merge-request browsing through project issue sources. GitLab merge-request actions
are a follow-up, to build on top of that source: the same panel, backed by `/projects/:id/merge_requests`, with pipelines
for checks and discussions for comments. The message builder, redaction and preview flow are provider-neutral.


## Guided make it mergeable (2026-11-11)

The readiness summary owns the **Make it mergeable** dialog; it reuses `pulls.status`, blockers,
checks, feedback framing and the existing exact-SHA merge confirmation. There is no new PR-state model.
`GET …/pull/mergeable` compares the pushed PR head with its base. It is a remote observation, not a claim
that unpushed local changes have passed CI.

Person-only POST routes under `…/pull/mergeable`:

- `/base`: require project access, a trusted writable worktree, readable branch protection, an unprotected topic
  branch and a clean worktree. Fetch the GitHub-observed base SHA through the isolated Git transport, then use
  hardened rebase or merge (`pull.rebase=false` in the project checkout). **Block force pushes** forces merge
  before any history rewrite; GET readiness reports `force_push_blocked` and the effective mode. POST requires
  `{mode}` from the person-visible button; the broker checks it against current config/Security before executing.
  `head=pull.head_sha` must equal the original local head or be a locally present ancestor of it. Otherwise refuse
  with **Pull the remote changes first**, without fetching or issuing a ticket. Stop on conflicts without
  aborting or discarding the work. The person can preview conflicted paths for the agent. No push occurs here.
- `/push/{ticket}`: a successful person-started rebase issues a ten-minute, person/session/branch-bound in-memory
  ticket. Only that ticket permits `--force-with-lease=refs/heads/<branch>:<old-remote-sha>`, and only while the
  local rebased head is unchanged. Recheck project access, branch protection, custom rules and the Security
  force-push switch immediately before pushing. Consume after success; restart loses tickets.
- `/rerun`: rerun failed GitHub Actions workflows identified by the current head’s check jobs (at most 50
  observed checks, deduplicated run IDs). External CI statuses retain their provider controls. This does not
  introduce arbitrary shell execution or run an unmanaged suite.
- `/feedback`: build the existing bounded, redacted failure preview. Automatically deliver only to Codex,
  OpenCode or API-key Claude with a fresh API-key observation. Supervisor and broker auth admission remain final.
  All other providers retain a preview. Subscription Claude requires manual copy/paste; even the existing
  preview/send endpoint cannot override its automation refusal.

The merge route now explicitly rejects installation principals in addition to requiring `confirm=true`.
There is no orchestrator/agent merge or auto-merge path. The existing route has no additional recent-verification
requirement; GitHub’s fresh rule/check lookup and exact-head comparison still run on every confirmed merge.

Audit actions: `pr.base` (mode, before/after SHA, conflicts or refusal), `pr.push_rebased` (head and lease),
`pr.rerun` (run IDs or refusal), `pr.feedback_preview`, and existing `pr.send_to_agent` / `pr.merge`.
The authenticated person is the actor. Broker paths: `session_update_base`, `session_git`; transport is a fresh
bare repository carrying only account credential configuration, never checkout hooks/helpers/URL rewrites.
No database migration is needed. A bounded stripe lock serializes a session’s guided base/push requests.


### Security review fixes (2026-11-11)

H1 binds admission as well as the eventual lease to the observed PR head: remote-only changes cannot be omitted
by a local rebase and then overwritten by a matching lease. Focused regressions cover missing objects, fetched
but unincorporated remote commits, and a local branch safely ahead of the remote. A rejected base update has no ticket.

M1 selects merge with the force-push Security switch on. When a permitted rebase stops on conflicts, its response
contains `finish_instructions`: resolve/stage paths, `git rebase --continue`, review the resulting history, then the
person may run the exact explicit lease/refspec command captured from the validated pre-rebase remote head.
No ticket is reconstructed from agent-written continuation state. The instructions require the Security switch
to remain off, review and start again on a lease refusal, and offer `git rebase --abort` plus Merge base in as the non-rewriting route.

Detached Changes Push refuses before querying GitHub. Conflict feedback has its own `untrusted-conflicted-paths`
intro rather than a review-comment frame. Git output/conflicted names are ANSI-cleaned and render hidden Unicode
and control characters as visible code-point escapes; file names cannot insert newlines. Message framing retains
its existing stricter cleaning and redaction. A failed automatic send returns `sent=false`, `refused` and the
original preview instead of losing it, records `pr.feedback_preview`, and leaves the preview available for retry/copy.


### R2-1: completed rebase push

`POST …/pull/mergeable/completed-rebase {sha, confirm:true}` is person-only. The SHA is the local head returned
by GET readiness and displayed in the click-time confirmation. With the same mutable-worktree and Security checks,
read current GitHub PR/base heads and run broker `session_completed_rebase`. That read-only check requires no Git
operation or index lock, a clean worktree, the unchanged branch and expected local head, and base ancestry plus a clean `git diff --check <base>..<reviewed-head>`. For a recorded person-started
conflicted rebase, the server requires the current PR head to equal its stored validated lease. Without such a
record, it retains the current PR head (H1) and current base ancestry rules. A remote update refuses before any ticket or push.

After admission the server mints an expiring actor/session/branch/head-bound ticket, audits `pr.completed_rebase`,
and calls the existing ticket push route. It rechecks Security/protection and pins the reviewed commit through
isolated hardened transport; the explicit lease refuses a remote update between admission and push. The stripe
lock is released before invoking the existing route, which reacquires it; the bound SHA prevents a local-head race.
No agent, orchestrator or installation principal can request this action. Missing person consent refuses too.

The dialog exposes Refresh checklist and Push completed rebase, with exact-SHA/force-with-lease confirmation.
Fallback instructions explicitly say this session's own terminal in Rynhult (session account), never the person's
own shell, and prefix the push with `git -c core.hooksPath=/dev/null`. That fallback does not claim to neutralize
all checkout configuration; the new hardened route is preferred.


### R3-1: server-held completion records for real conflicted rebases

When `/base` passes H1 and returns a rebase conflict, retain a bounded, server-private record keyed by session:
`{session, branch, actor, person_id, host, path, lease=observed PR head, base=observed base SHA, before, expires=+1 h}`.
These values come from the authenticated principal, database routing, server GitHub reads and the broker's result,
never agent-writable rebase files. A later base operation supersedes the old record. Records are process-local;
restart or bounded-record eviction loses authority safely.

At a confirmed completed-rebase click, require the record to match person/session/branch/host/worktree and remain
unexpired. Read the current PR head from GitHub and require exact equality to the stored lease. The broker keeps
all existing operation/branch/clean-tree/reviewed-head guards, checks the recorded base is an ancestor, and requires
`git diff --check --no-ext-diff --no-textconv <base>..<reviewed-head>` to succeed, including no leftover conflict
markers. A broker-only validated-lease parameter permits the rewritten head to differ in ancestry from the old PR
head only after the server's record checks. Mint the ticket using that stored lease and push through the unchanged
hardened transport. Successful push consumes the pending record; failures retain it only until its original expiry.

With no record, retain the existing current PR/base ancestry admission. Expired or another person's record refuses;
it never silently changes the actor or refreshes expiry. Completion refusals and fallback instructions say the
branch changed on GitHub since the rebase started (or the record is unavailable); review and start again, never
pull the pre-rebase commits back into the rebased history. Clean guided-rebase tickets remain unchanged.


### R4-1: marker-only checks and confirmation summary

Completion runs `git -c core.whitespace=-blank-at-eol,-blank-at-eof,-space-before-tab,-indent-with-non-tab,-tab-in-indent,-cr-at-eol diff --check --no-ext-diff --no-textconv <base>..<reviewed-head>`.
Whitespace-only changes, including Markdown two-space line breaks and CRLF files, pass; leftover conflict markers
still refuse. This is an accident check, not a security boundary: in-tree attributes can influence Git's diagnostics.

GET readiness now includes `summary`: before/after commit counts relative to the recorded rebase base (using the
stored original head), and at most 20 rebased subjects capped at 240 characters each, plus an omitted count. Without
a matching unexpired person-bound record, use current GitHub base/PR-head revisions; missing local objects produce
unavailable counts rather than implicit fetches or authority. The broker reads only explicit SHA ranges through
hardened Git. The API redacts and visibly escapes subjects. The confirmation presents this beside the SHA and pins
that same SHA across the person's confirmation, even if readiness refreshes while the dialog is open.
