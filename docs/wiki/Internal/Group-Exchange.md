# Group Exchange

Group exchange publishes explicitly selected committed history. It uses the existing
ACC authentication, HTTPS/device boundary and account broker peer checks. It does
not use the receive-only backup endpoint or put Git credentials in agent sessions.
The implementation decision is `docs/decisions/0036-group-pack-exchange.md`.

## Boundaries

| Store under the configured Git root | Contents | Authority |
|---|---|---|
| `exchange/PROJECT.git` | Published commit histories and refs | Current project group membership; viewers read |
| `group-backups/PROJECT/SNAPSHOT` | Immutable published recovery and safe group metadata | Maintainers create/restore; members download; viewers see status |
| `private-groups/PROJECT/PERSON/SNAPSHOT` | Explicit private committed recovery | Placement's person and trusted instance owners, independently of group membership |
| Existing `PROJECT.git` | Existing personal project backups | Existing personal backup authority |

A group role never opens another person's broker path, transcript, dirty file,
private recovery, terminal or legacy Files/Wiki view. Instance administration does
not automatically become a group code identity: exchange requires membership.
Placement provisioning remains an instance-owner action through the approved broker.

The broker creates 0700 account-owned group directories without extended ACLs or
symlinks. Every session checkout has independent `.git` metadata; config, hooks,
filters and alternates are not copied. Export reads only the selected commit's
objects through fresh metadata. Import validates in quarantine, then repacks only
that commit's closure. Rejected/unselected objects never enter shared storage.
Published code is untrusted input; no build, submodule initialization or agent
launch occurs during sync. People must review source before later execution.

## API contract

Use `/api/groups/GROUP/projects/PROJECT` as `BASE` below. Cookie requests use ACC's
normal CSRF header; bootstrap instance tokens do not substitute for membership.
All commit IDs are full SHA-1 hex. Checkout IDs are 12–32 lowercase hex characters;
checkout IDs are durable receipts. The group launch endpoint binds the same ID to one live session after source trust and quota checks.

| Method and path | Request / result |
|---|---|
| `POST BASE/placements/PLACEMENT/provision` | Owner provisions an existing reservation; no supplied path/account |
| `POST BASE/exchange/checkout` | `placement_id`, `session_id`, `commit`; own provisioned placement, published tip, fresh standalone checkout |
| `POST BASE/exchange/publish` | `placement_id`, `session_id`, `old_sha`, `new_sha`; broker exports selected committed history, service checks fast-forward and expected old ref |
| `GET BASE/exchange/refs` | Published ref → commit map; no private refs |
| `POST BASE/exchange/grants` | `operation` (`publish`/`fetch`), optional read `placement_id`, `session_id`, `old_sha`, `new_sha`; one-use token, derived ref and 60-second expiry |
| `GET BASE/exchange/git/info/refs?service=git-upload-pack` (or `git-receive-pack`) | Standard Git HTTP advertisement, scoped bearer grant; current membership rechecked |
| `POST BASE/exchange/git/git-upload-pack` | Read negotiation/pack from trusted staging containing only the selected published history |
| `POST BASE/exchange/git/git-receive-pack` | Exact granted ref transition in disposable staging, then selected-history promotion |
| `POST BASE/exchange/transfer` | Exchange token as bearer; base64 pack in `data` for publish, empty body for fetch; fetch returns selected published commit's full pack |
| `POST BASE/approvals` | Phase 1 `revision`, `branch`, `old_sha`, `new_sha`; currently named project owner approves exact integration result |
| `POST BASE/exchange/integrate` | `approval_id`; maintainer applies current named-owner receipt, consumes it, never overwrites changed integration |
| `POST BASE/exchange/tags` | Maintainer supplies `name`, published `commit`; new immutable tag only |
| `GET BASE/published/tree?commit=SHA` | Regular published file paths; symlinks/submodules omitted |
| `GET BASE/published/file?commit=SHA&path=PATH` | Bounded UTF-8 text from published history |
| `GET BASE/published/wiki?commit=SHA&path=Home.md` | Markdown text under `docs/wiki`; no tree traversal |
| `POST BASE/exchange/backup` | Maintainer creates published recovery snapshot and safe role/owner metadata |
| `GET BASE/exchange/backups` | Snapshot IDs, creation times and ref counts; local evidence only |
| `GET BASE/exchange/backup/SNAPSHOT` | Member/maintainer downloads safe metadata and base64 packs keyed by commit |
| `POST BASE/exchange/restore` | Maintainer supplies `snapshot`, backed-up `commit`; creates a new `restore/ID` branch |
| `POST BASE/placements/PLACEMENT/private-backups` | Placement person or trusted instance owner supplies `session_id`, private `commit`; returns snapshot ID |
| `GET BASE/placements/PLACEMENT/private-backups/SNAPSHOT` | That person or trusted instance owner downloads private pack; group roles confer no authority |

For a first publication, `old_sha` is 40 zeros. Later updates must supply the current
published branch tip. The service derives `refs/heads/people/STABLE_PERSON_ID/SESSION_ID`;
callers cannot supply another person's namespace or an integration destination.
A stable person component hashes the never-reused username, never a recycled user ID.
There is no force, ref deletion, unscoped Git remote or arbitrary-object fetch. Standard Git clients use
`BASE/exchange/git` with the scoped grant in an Authorization bearer header supplied
only in process environment. The service initially uses protocol v0. Never run
credentialed transfer inside contributor-controlled config: broker helpers export
through fresh metadata and keep the credential out of the working repository.
The low-level fetch grant accepts no placement for viewers; if supplied, a placement
must belong to the caller. Grants are hashed in memory, capped at 64 outstanding per
person and 4,096 in total, and forgotten on restart. A grant serves one Git HTTP request
at a time (429 otherwise), and at most 8 Git HTTP requests are buffered at once (503).
Only the transfer and Git paths accept bodies over 4 MiB, and transfer checks its grant
before reading the body.
Git pushes must be uncompressed; push options and certificates are not advertised. Pack-helper requests and Git write POSTs spend the token even on refusal. Git
advertisements and intermediate stateless read negotiation can repeat; the read
capability is consumed before its first pack is returned. Every request, including
intermediate negotiation, rechecks current membership. A
completed transfer cannot be replayed. This permits ordinary Git negotiation while
keeping final pack delivery and all mutations one-use.

## Fencing, limits and recovery

Service-side Git operations take the existing Git capture lease **first**, then the
database: waiting behind a capture never holds SQLite's writer (503 after 60 s).
Writes (publish, receive-pack, integrate, tag, backup, restore) hold a current-membership
writer fence through their Git work. Reads recheck group, revision, role, placement and
grant without the writer; publications are permanent, so a read racing a removal shows
nothing the person could not see a moment earlier. Revision changes invalidate old
grants/approvals.
Integration also verifies the approver is still a named maintainer. Spent approval
records are retained so their IDs cannot be recycled into later receipts. Git and SQLite
cannot commit together: after an uncertain response, inspect refs first. If Git
advanced before a crash, expected-old CAS refuses approval replay even if the
receipt survived. Do not roll back or force a ref to make an old receipt apply.

Packs are complete (not thin), limited to 8 MiB. Before Git inflates anything, a scan of
declared sizes refuses objects or delta results over 128 MiB and packs expanding beyond
512 MiB. Only objects the store does not already reach from its refs are promoted, so
fast-forwards, tags and integrations add their new objects, not another history copy.
Object stores are limited to 128 MiB and published refs to 128. Each person may publish
32 branches; people's publications stop at 112 refs and 112 MiB, leaving the rest for
integration, tags and restores. The object-store admission budget includes pack indexes. Text views are limited to 1 MiB, trees to
5,000 files. Git children have 60-second wall/CPU limits, 512 MiB address space,
64 MiB file size and 128 file descriptors. HTTP bodies remain bounded before parsing.
Snapshots have a 64 MiB transfer budget, 128 local snapshots and a 512 MiB local
store budget with headroom reserved before capture. These fail closed rather than
silently pruning recovery. Ask an instance owner to review storage/retention.

The backup server captures these directories under its existing Git lease and
validates group/project/person identities in its inventory. Its encrypted off-site
retention remains 48 hourly, 30 daily and 12 monthly copies, with owner-only pruning.
A local snapshot is not proof of off-site backup or independent protection. Use
[Backups](/User-Guide/Backups) to inspect host backup results and follow its recovery-kit
and held-restore procedure. Dirty files, credentials and native session state are
not included. Restore of published code always creates a new branch; integration
still needs fresh named-owner approval.

No new migration, machine configuration, account installation or service is needed.
The phase 3 candidate adds executable-session bindings, source trust and group coordination. Operator qualification still covers
transactional quotas, key delivery, and removal/downgrade stop/cleanup/reconnect
before production release. Cross-machine operation remains phase 4.

Broker checkout also reserves expanded content before creating a working tree:
128 MiB of blob payload across paths (duplicate blobs count for every path),
5,000 total tree entries, 1,024-byte paths and 64 components. This bounds disk
amplification from small compressed packs; no oversized worktree is created.


## Coordinated same-host sessions

Migration 0044 adds checkout receipts, explicit work rows, advisory claim leases,
operational notices and launch/resume reservations. Group policy includes concurrent
and UTC-day launch quotas, owner ceilings, a 15-minute audited override and a reported-USD warning.
Readings come from per-session provider observations; missing data is unknown.
The ledger projection is bounded to 100 recent sessions and reports partial coverage when truncated.

`POST BASE/sessions` accepts `checkout_id`, explicit `task`, private `prompt`, `name`,
`provider` (Claude or Codex) and `trust_source`. The approved clone must belong to the current person
and local account. The supervisor rechecks membership before reservation and dispatch, freezes a
session placement, and uses the checkout ID as session ID. A duplicate returns that ID without another charge.
Manual resumes reserve another launch; uncertain starts retain their concurrency slot. Group helper,
schedule, container and multi-run entry points remain refused.

Under `/api/groups/GROUP`, `board`, `usage` and `operations` are safe read projections;
`policy` is maintainer/owner-only. `work/SESSION/task`, `claim`, `release` and `context`
are own-developer-only (trusted instance owners retain administrative access).
Path validation is shared by server and broker. Viewer projections omit claims and observed paths,
including paths in conflict notices, following decision 0008. Claims serialize overlap checks with insertion,
require explicit shared-work confirmation, expire after 15 minutes, and renew only from broker liveness.
Tasks are bounded explicit text, never opening prompts.

A separate `group-context.json` attachment refreshes through a fixed broker operation using held
no-follow directory descriptors and atomic replacement. It never rewrites `session.env` or the frozen
launch spec. Claude hooks return the bounded local snapshot as untrusted additional context; other
agents have a visible explicit-refresh requirement. Broker observations copy bounded index data into fresh
Git metadata, disable filters/external diff/fsmonitor, omit protected paths, and never import source config.
The work spool derives identity from its originating session and records deduplicated bounded receipts.

`GET BASE/keys` lists eligible consent IDs; `POST BASE/keys/KEY/attach` requires group administration
and current key-owner consent. Vault delivery rechecks current membership, attachment, consent and local host.
Group role never enumerates private values or broadens grant scope. Removal/downgrade records stop intent
and releases claims in the membership transaction; broker stop/secret cleanup retries survive unavailability.
Terminal checks revalidate authority, and the membership route closes affected live streams.

Security review must cover the runtime changes separately from the phase 1 and 2 reviews.
Separate-UID deployment, production provider hooks and credentials remain operator qualification checks.


## Own-machine first slice

`ACC_GROUPS_REMOTE=1` (or `RYNHULT_GROUPS_REMOTE=1`) on the main enables the
optional path; absent/false leaves it closed. Migration 0063 (after main’s OIDC follow-ups 0054) adds owner-reviewed
`group_machine_account` identities and nullable placement references. No existing
placement or personal project acquires group access. Enrollment stays owner-only.

A person requests `POST /api/group-machines` with `node_id` and `account`; only
an instance owner approves `POST /api/group-machines/ID/approve` against an active
node's currently advertised broker accounts. GET lists one's own records; owners
may review all. Self/owner revoke through `.../ID/revoke`. Unapproved cancellations
release their reservation. Approved/revoked account identity remains exclusive
while its private files may still exist. No reactivation route is provided.

For a group project BASE, its developer requests `POST BASE/remote-placements`
with `binding_id` and current group `revision`. Maintainer/owner review reads
`GET BASE/remote-placements`; `POST BASE/remote-placements/ID/approve` provisions
a derived private path below a broker-reported deployment root. Approval rechecks
membership and revision after IO. `.../ID/revoke` persists stop/cleanup in the
authorization transaction. Membership departure/viewer downgrade invalidates these
placements. Published exchange routes reuse sanitized, bounded commit packs and
carry the recorded host/account on every broker call. Session routing uses the
frozen placement rather than mutable project host fields.

The launcher's own remote-enabled keys are the only remote group delivery in this
slice; private teammate keys, including legacy group grants and MCP references,
are excluded. Provider OAuth material remains on its broker account. Partition
blocks new remote grants/checkout/launch, not already running offline editing.
On reconnect, authority is rechecked before adoption/start; revoked sessions are
killed and queued cleanup retries. Broker cleanup errors remain pending. A revoked
node key cannot reconnect; retain pending state and arrange owner-run local cleanup.
No automatic sync, key-grant expansion, container dependency or live migration.
Private recovery endpoints retain their same-host restriction; this slice does
not silently authorize remote private-checkout backups.

Qualify real separate OS users, machine administrators, production TLS and native
Linux/WSL/macOS before rollout; simulated nodes do not certify those boundaries.

### Local account-control proof

Migration 0064, chained after 0063, adds nullable `group_machine_account.proof_verified_at`. Earlier
approvals are preserved but unverified: no remote placement or session authority
is usable until that binding's person supplies proof. The historical 0063 table
DDL is frozen so model additions cannot leak into an earlier migration.

`rynhult-node group-proof --request ID --node-id NODE_ID --run-dir BROKER_RUN_DIR`
runs locally as the ordinary requested account. It generates a ten-character code
without reading enrollment keys or talking to the main. The account's broker run
directory holds `.group-proof.json` (0600, account-owned, no symlinks/hard links),
containing the digest, exact identity, expiry, counters and the last 32 local
receipts. Directory and file ownership/mode checks and a file lock gate access.
Only the local command displays the code; `group_machine_verify` is the only new
broker method and returns a boolean. Services brokers do not expose it.

The browser submits to `POST /api/group-machines/{binding_id}/verify`. Only the
requesting human can verify pending or older approved unverified bindings. The
main selects the active immutable node/account and rechecks authority after I/O.
Short database transactions run in workers; none span the broker call. Five-minute
expiry, one issuance per minute, five failed checks per challenge and ten API
attempts per person per minute survive restarts. Unrelated request IDs cannot
exhaust another request's challenge. Verification attempts/results are audited
without the code or digest. Owners see explicit verification state and cannot
approve unverified bindings. See decision 0068 and the Guide's own-machine steps.

Upgrade the main and each participating node before testing this flow. Older
brokers refuse the unknown method, leaving the request unverified. Keep
`ACC_GROUPS_REMOTE` off until native qualification; this feature adds no sudo or
socket permissions. Treat loss/corruption of the local proof file as unavailable
proof, never as successful verification.

The phase 4 replay chains directly after 0054; it does not wait for SCIM, which is parked until after v1. Migration upgrades leave own-machine groups off by default.
