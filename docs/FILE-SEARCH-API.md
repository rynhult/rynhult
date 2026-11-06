# Project filename search API (AC-060, slice 2)

These read-only endpoints resolve filesystem paths at the assigned account broker.
The new shell's **Find project files** dialog uses the project endpoint; see the
[Guide](wiki/User-Guide/New-Layout.md#find-a-project-file). The session and workspace
endpoints remain available to callers but are not searched by that dialog.

## Requests and placement

- `GET /api/projects/{project_id}/files/search?q=manager&limit=20`
- `GET /api/sessions/{acc_id}/files/search?q=manager&limit=20`
- `GET /api/files/{kind}/{key}/search?q=manager&limit=20` (the Files convention:
  `project` + project ID, or `session`/`workspace` + session ID).

Authentication, ownership and placement use the same resolver as Files listing.
Members can search their own personal projects and sessions; the installation
owner can inspect those resources across people, as with Files. Group project
access is not widened. Resource authorization and the live-group-checkout refusal
run before query validation, including missing or malformed query parameters.
Foreign resources return 404; missing authentication
follows the existing principal dependency. The installation API token has the
same owner authority as listing. Node/tool credentials do not grant this API.

The main chooses the stored agent account and machine, and sends the stored
project workspace or session's actual execution checkout/worktree to the account
broker. Session searches do **not** fall back to the project's primary checkout.
No caller-selected root, account, host or shell command is accepted. Extra query
parameters cannot change placement. The stand-in node test exercises the real
outward authenticated transport and checks the selected account.

The query is 1–200 characters, trimmed, Unicode NFC/case-folded. Control bytes,
backslashes and `.`/`..` path components refuse with 422. `limit` is 1–100,
default 20; broker callers undergo independent validation. It matches a basename
substring first, then a relative-path substring, then an ordered subsequence of
the relative path. This is fuzzy **filename** matching, not file-content search.
A slash can narrow a relative-path query; it never selects a search root.

## Successful response

```json
{
  "results": [{"name": "SessionManager.py", "path": "src/SessionManager.py", "kind": "file"}],
  "visited": 83,
  "truncated": false,
  "reason": null
}
```

Paths are relative to the selected execution location. Only regular files are
results. File contents, sizes, Git status and absolute filesystem paths are not
returned. Files' existing vault-value redaction applies to names/paths too.
The typed response model is shared by all three routes.

On an offline machine or unavailable account broker, the API returns **503**:
`{"state":"unavailable","detail":"…"}`. This is never a successful empty
search and never searches the main's local filesystem instead. Policy/HA fences,
older brokers without this operation and other execution refusals use the same
generic 503 shape. Authorization and query validation retain their normal errors.
One search per person and resolved location may be in flight; overlapping queries
(including aliases of the same location) return 429 with `state: unavailable`.
Account-worker admission pressure returns 429 with `state: busy` and
`Retry-After: 1`; retry shortly rather than suggesting an account-service update.
Other execution failures retain the generic 503 response.
Query strings on all search routes are removed from access and error logs.

## Bounds, exclusions and confinement

The read-only broker method is `files_search(path, q, limit=20)`. It is explicitly
excluded from automatic transport retries. Each API request uses a fresh client
with a three-second timeout, separate from terminal controls. It executes under
the assigned account's authority, with a captured folder-policy snapshot and a
revision recheck before returning results.

- At most 10,000 entries visited (including ignored entries), 100 results, 32
  directory descents, and a one-second cooperative monotonic-clock budget.
- A result limit stops on the first *additional* matching file. Exactly `limit`
  results with a fully scanned tree are not falsely marked truncated.
- Every directory opens relative to an already-held descriptor with no-follow
  flags. No links, sockets, devices or FIFOs are traversed/read. Secret paths
  already protected by Files are excluded, including credential directories.
- Linux checks mount IDs, including same-device bind aliases; other platforms
  check filesystem devices. Crossings are skipped and flagged. Reviewed policies
  recheck actual held-directory locations against mandatory protections and
  allowed roots, even when the configurable Allowed folders filter is Off.
- `.git` and `node_modules` are always excluded, regardless of ignore negation.
- Bounded `.gitignore` metadata is read via no-follow, nonblocking regular-file
  descriptors (hardlinked metadata refuses): at most 8 KiB per file and 256 patterns across a request. Nested
  rules, comments, simple shell wildcards, anchored paths, directory exclusions
  and `!` negations are supported. Ignored directories are not walked, so their
  children cannot be resurrected by a later negation. No global Git configuration,
  `.git/info/exclude`, executable Git hooks or arbitrary Git commands run.

This is a conservative `.gitignore`-style subset, not Git's full matcher: no
escape syntax, and shell wildcard matching of slash-containing patterns may
exclude more paths than Git. `.gitignore` changes during a walk are not an atomic
snapshot. Search does not claim Git index/tracked-file semantics. Unreadable or
oversized ignore metadata skips only its subtree and flags partial results.
The global 256-pattern budget still stops the entire walk when exhausted.

`truncated: true` means **partial**, not an exhaustive negative search. `reason`
is one of `entry_limit`, `result_limit`, `time_limit`, `depth_limit`, `ignore_limit`,
`path_limit`, `unreadable`, `mount_boundary` or `unsupported_name`. Names that
cannot be encoded as UTF-8 are skipped and flagged. If several bounds occur, one
reason is returned; callers must rely on `truncated`, not assume it lists every
cause. Within the visited portion, results sort by match class then folded path;
a truncated walk does not promise a global best-match ranking.

The wall-clock budget is checked between filesystem operations and ignore rules.
A separate child process gives each broker search a hard two-second deadline,
including stalled filesystem calls. No broker-wide control lock is held during
the walk. A broker admits one search worker at a time and rejects more rather
than queuing them. Timeout/cancellation kills the worker; if the kernel cannot
reap it yet, that slot remains occupied, without blocking controls. There is no
background scan, persistent index or new input queue.

## Verification and review scope

`tests/test_file_search.py` covers matching without content search, normal and
ignored/protected paths, outside-root and symlink refusals, a directory-to-link
race, reviewed-root relocation (Allowed folders On and Off), mount boundaries,
entry/result/depth/ignore/clock limits, actual session worktree placement,
owner/member/foreign-project authorization, offline refusal and real stand-in
node transport with attempted query-parameter placement overrides.
The standard Files and ownership files are run alongside it. The API adds
no migration. Authorization inventory rows are explicit and regenerated
with `scripts/generate_organizations_authz.py`.
