# Current-branch change baselines

Date: 2026-10-25. Status: accepted owner request.

Renumbered from 0053 on 2026-10-25 to preserve main's existing WSL isolation
record. The audit's provisional 0055 became 0057 when main's independently
allocated 0055 S3 retention-maintenance record landed later that day.
The subsequent reliability merge reserved 0057 for main's shared-server browser
isolation record, so this branch's record moved to 0058.

## Context

The session's immutable launch commit can belong to a branch it has left.
Comparing a new branch with that commit inflates the row badge and Changes tab
with unrelated additions and deletions. Git also discovers ancestor checkouts:
an untracked scratch folder can accidentally inherit the parent's whole diff.

## Decision

Choose the comparison on every collection, in the broker shared by cached
summaries and detailed diffs. Compare the current HEAD with its merge-base with
origin/main or local main first, then the locally recorded origin default branch,
configured upstream, origin/master or master, in that order. Main takes priority
over a feature branch's own upstream so pushing the feature does not erase its
comparison. Never fetch or change
the checkout. Report the selected reference and base in the response.

Without a shared upstream/default-branch ancestor, show only working changes
against HEAD. Before the first commit, use the empty tree. A plain folder, or
an untracked nested scratch folder without tracked files of its own, has no
Changes badge. Versioned subfolders remain supported, with relative paths and
diffs restricted to that subfolder.

## Consequences

Committed work on the comparison branch itself is not counted. The diff describes
the checkout, not authorship: agents sharing it see the same changes. Missing
references and unrelated histories have an explicit working-changes presentation;
unexpected Git failures remain errors, never clean summaries.

Keep the launch commit and operation argument for older node brokers. Their
legacy response is still labelled honestly; nodes need an update to adopt the
new comparison. No database migration or credentials are required.

## Alternatives considered

Resetting the launch commit only when a branch name changes misses history
rewrites and reused branch names. Keeping different badge and detail baselines
would make the visible counts disagree. Rejecting every nested folder would
break registered versioned subfolders.
