# Finish PRs in ACC

Scope approved 2026-10-22. A session's
branch can become a pull request and be brought to a merge from the session page. ACC acts only when a person clicks.
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
