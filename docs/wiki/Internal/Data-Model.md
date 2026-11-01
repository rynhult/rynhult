# Data model

SQLite at `/srv/acc/data/acc.db` (WAL mode), migrated with Alembic on start.

| Table | Holds |
|---|---|
| `project` | Name, slug, folder, repository, trust, import mode, autostart, archived, git backup on/off (`git_backup`, migration 0022; its status is the observation `backup.<project id>`), `private_workspace` (individual Development environment with mandatory local-only session policies), `backup_identity` (preserved when promotion changes its slug; migration 0031), `builtin` (`local`: the group of one account's local workspaces on this machine, found by account and host, never by slug; migration 0025) |
| `session` | One conversation: provider, names, native id, desired and observed state, retry state, work folder (workspaces) or git worktree and branch, management (`acc` or observe-only), what it's doing now (`activity`), its changes summary and legacy launch commit (`base_commit`, retained for older node brokers; current brokers recompute a branch merge-base), when its conversation was last indexed, the ids of the vault keys it was given (`vault_keys`, for redaction; never values) |
| `team`, `team_request` | Migration 0032: the person’s bounded Orchestrator grant, shared counters and expiry, durable session-scoped requests/results; session ancestry and host overrides route helpers without changing ordinary project sessions |
| `multirun`, `multirun_entry` | Migration 0037: a person's independent launch group, ordered choices and refusals, optional winner and per-attempt session link; `session.multirun_id` identifies ordinary sessions in the group. Session creation and its entry link commit together. `multirun_stop_pending` retains unconfirmed stop intent even after archive. No team grants or automatic launch retries |
| `launch_spec` | Immutable launch inputs and generated files; reused verbatim on every resume |
| `schedule` | Migration 0035: person and agent account, resolved launch choices, timezone/calendar, recovery policy, enabled/deleted state and next UTC occurrence |
| `schedule_run` | Migration 0035: stable due intent and reserved session id, launch snapshot, conditional dispatch claim, ordinary session reference, result and reason |
| `session_run` | One process: start, verification, end, exit status and reason |
| `event` | Append-only activity: hooks, state changes, launches |
| `attention_item` | Inbox items, deduplicated, with acknowledged and resolved times; migration 0034 adds nullable JSON `risk` (`label`, `reason`) for needs-input advice, independent of lifecycle fields |
| `observation` | Latest readings: plan usage, login health, models, GitHub snapshots |
| `publish_request` | Publishing requests and their outcomes |
| `tool_request` | Migration 0030: session-derived person, account, machine and project; manager/name/requested version, install/remove, reason, decision actor, status, verified version, completion and result |
| `installed_tool` | Migration 0030: verified name/version and source request, keyed by machine + account (empty for system) + manager + name |
| `audit_log` | Every change: actor, action, target, details (never secrets) |
| `push_subscription` | Devices that receive notifications: push endpoint and its keys, label, last delivery |
| `device` | Migration 0050 after 0049: person, label, browser kind, status, public leaf PEM, SHA-256 fingerprint, unique serial, expiry and last-seen placeholder; no private keys |
| `device_pairing` | Migration 0050: person-bound hashed one-use token and receipt, pending/claimed/ready/downloaded state, expiry and optional renewal device ID; bundles/passwords stay only in memory |
| `group_machine_account` | Migration 0063 (after 0054), proof fields in 0064: a person's own-machine agent account, pending until an owner approves it; approved and revoked rows keep that account reserved. The 0063 downgrade refuses while any are approved or revoked, because the reservations can't come back |
| `invite` | One-time join or reset links: hash of the token, role, agent account, expiry, use |
| `sso_invite`, `oidc_flow` | Company sign-in invitations and browser-bound pending flows; migration 0066 (after 0054) adds nullable `sso_invite.withdrawn_at`. Revocation retains the invitation so a pending callback reports why it was refused; downgrade expires revoked invitations before dropping the marker |
| `user`, `auth_session` | Accounts (password hash, TOTP secret) and sign-in sessions; migration 0041 adds an opaque passkey handle, recovery-code digest, and per-session reauthentication time/method |
| `passkey` | Migration 0041: user-bound public credential, RP hostname, name, signature counter and creation/use times; cascades on user removal |
| `passkey_challenge` | Migration 0041: bounded, expiring one-use ceremony; binds purpose, challenge, origin, client, browser cookie digest and optional user/session |
| `passkey_policy` | Migration 0041: singleton Allow/Require choices for owners and members, defaulting to Allow when absent |
| `retired_user` | Usernames of removed people, never given out again (ownership compares usernames): role, account, who removed them and whether their things were transferred or archived (migration 0024, which also retired names that owned rows without a user) |
| `setting` | Choices made in Settings: `guardrails` (instance-wide), `budget.<username>` (one person's; absent until they set one), `tools.person.<username>` / `tools.project.<id>` (`required`, default true; either false exempts new sessions from tool approval only) |
| `vault_key` | The key vault: name, variable, owner, sharing, other machines, note, times, and the value as AES-256-GCM ciphertext (`base64(nonce + ciphertext)`, associated data: key id and variable). The data key is `data/secrets/vault.key`, never in the database |
| `vault_grant` | A key given to a project (`key_id`, `project_id`, who, when); cascades when either is deleted |
| `transcript_fts` | FTS5 index of every session's conversation (prompts and replies only) for history search; rebuilt per session from the CLI's own file |

Session summary/detail responses derive `status_stale_since` for an `active`
claim whose `status_source` ends in `_hook` after 600 seconds without state or
activity evidence. The latest non-null `status_at`/`activity_at` is the evidence
time; `created_at` is a fallback only when both are absent. Generic `updated_at`
and terminal output do not renew a hook claim. The UI labels it Unknown, hides
old activity, and excludes it from Working counts. Fresh evidence clears the
derived field. Waiting-input and idle states are not expired. No migration or
stored lifecycle/automation change is involved.
Live stat counts inspect each participant independently: retaining a whole team
under its most urgent member does not hide its fresh Working or Unknown members.

Writes from API requests start with `BEGIN IMMEDIATE` so they wait for the write lock before reading mutable state. Ordinary read transactions retain deferred `BEGIN`.

Transcript persistence, model-cache persistence and automation cancellation use
short worker transactions with the same write intent. Host-pressure polling
reserves the writer too. A process-local admission gate shared by engines for the
same database file keeps these short worker transactions from overlapping ordinary
application transactions. Write-intent transactions on synchronous API threads
and other non-event-loop threads participate automatically, even without the
worker decorator. This protects read-then-write sessions even without a
write-intent marker, including GET last-seen updates and direct test sessions.
Ordinary transactions may still overlap each other; their existing write-intent
rules remain necessary. The gate releases only after the actual DBAPI commit,
rollback or close, and also releases if transaction setup fails. Maintenance
leases are acquired before admission so a worker queued behind capture cannot
make event-loop requests wait for that capture. It does not
coordinate independent processes or raw SQLite connections.
Workers can wait without blocking the event loop; broker reads happen outside
the indexing transaction. Transcript rows, the automation completion marker,
transcript stamp and indexing time commit atomically per session. Cancellation
joins an in-flight database worker before returning. Lock timeouts and other
database errors propagate; no external operation is replayed.

Reconciliation commits pending pane/startup observations before probing Claude
Remote Control through the broker. Otherwise a concurrent API commit can
invalidate its deferred SQLite snapshot before the later output-state write.
It neither reserves the writer across broker I/O nor retries external effects.

Every application, authority-control and startup migration SQLite connection
sets WAL and a 15-second busy timeout before beginning work. Application and
authority connections enable foreign keys; migrations keep them disabled for
Alembic batch table rebuilds.

Migration 0044 adds nullable `node.idle_sleep_minutes` (off by default), `session.idle_since` and `session.sleep_paused_at`. Idle time survives supervisor restarts and resets on leaving Idle; recent activity/output also defers a pause. Stop intent, the pause marker and `session.sleep_pause` audit (machine and threshold) commit together before the broker kill. `last_error` retains an unconfirmed stop; reconciliation clears it after confirming the process is gone. Ordinary Resume clears the pause marker and keeps the native conversation ID. The run ends with reason `machine_sleep`; no new session lifecycle state is introduced.

`setting` key `risk_labels.<username>` stores `{"enabled": true}` by default
(an absent row means enabled). This preference affects API presentation only;
the derived metadata persists without raw tool arguments or commands.

Tool decisions SQL-claim `pending_approval` before scheduling, then claim
`approved` before executing. Terminal states are `applied`, `removed`, `rejected`
and `failed`. Startup fails interrupted `approved`/`applying` work rather than
replaying it. Results and audit writes occur after the external operation; a
timeout can mean uncertain machine state and needs human inspection.

Migration 0033 adds `project.remote_url` and `project.issue_sources` (GitLab/Linear settings with vault key ids,
never token values), plus `session.issue_source` (the originating issue reference, launcher and optional comment
delivery status). `vault_grant.server_only` keeps source credentials out of agent environments and MCP files,
including after disconnecting a source. The Linear comment's pre-send marker prevents duplicate automatic sends
after an uncertain response or restart.

Projects, sessions, inbox items and publish requests carry an `owner` (a username). Members see rows they own; owners see all. Inbox items with no owner (host, ACC itself) are for owners. What the bootstrap API token creates is owned by `@api` (`models.API_PRINCIPAL`), which can't be a username (usernames start with a letter or digit); migration 0023 moved the token's older `api` rows there.

The supervisor consumes each due schedule timestamp and inserts its run intent in one transaction. Dispatch conditionally claims pending intent before any broker effect. Creating the ordinary session attaches it to the intent in the same transaction. After restart, pending intent survives, recorded sessions reconcile normally, and ambiguous claims without a recorded session fail into the personal Inbox without retrying. Cancellation persists the session stop intent before calling the broker. The current implementation assumes one supervisor; it does not provide distributed leadership or effect fencing.

Migration 0036 adds `session.automation` (nullable JSON): opt-in settings, goal start/caps/counter/status, assistant marker and baseline sequence, stable waiting/consumed reset IDs, cancellation, and the last durable continuation claim/result. Each claim commits before broker dispatch; an interrupted claim is visible and is not replayed. These fields are authoritative across accd restarts; process locks only serialize local work.

### Backup server control state

Phase one reuses versioned tables instead of adding correctness state only in memory:

- `setting` key `backup.capture_checkpoint`: random capture ID and UTC boundary time, committed under the
  maintenance barrier immediately before SQLite online backup; not a replication log position.
- `observation` keys `server-backup.job.<id>`: durable action, owner, target, request time, state, attempt,
  capture ID, result or fixed non-sensitive failure. Queued jobs survive restart; interrupted running jobs fail
  closed for operator review under the single local worker lease.
- `setting` key `backup.drill`: owner-attested target/backup ID, optional full selected snapshot ID, and test time. Keep independent off-main records.
- `attention_item` keys `backup:quarterly`, `backup:stale:<target>` and `backup:failed:<target>`: deduplicated,
  owner-only reminders, resolved on recovery. `audit_log` records requests, exports, target provisioning, results,
  drill attestations and offline restore without passwords or secret values.

Repository-password files and installation signing identity are outside the DB/vault in private
`data/backup-service`. Signed capture manifests bind DB checkpoint/schema, key fingerprint, catalog revision,
repository project identities, per-file hashes and target/repository IDs. Last verified result files advance only
after authenticated data readback and isolated restore validation. See [Backups](/User-Guide/Backups).
## Opt-in group authority (migration 0038)

`acc_group` holds a stable UUID, home site, archive/pause state and authorization
revision. `group_membership` has a unique group/person key, constrained role
(maintainer/member/viewer) and provenance. Creating a group is owner-only opt-in;
no default memberships or sharing are backfilled. The project authority constraint
refuses a group and personal owner simultaneously; legacy nullable owner rows stay
personal under their previous permissions.

`placement` stores project, person, machine, account, private path, trust, sync and
approver. Existing projects and sessions get routing snapshots, including helper
machine and worktree overrides. New personal launches also snapshot routing;
`session.placement_id` selects the immutable binding, including an explicit
New session machine choice. `session.team_repo` also retains an ordinary
cross-machine session's backing repository for worktree cleanup and later reuse;
the original project host/path are unchanged. Group approvals reserve
same-host paths without creating or trusting a directory. Reservation paths include a digest of the never-reused username so shared owner
accounts and recycled numeric user IDs cannot reuse another person’s clone path.
Phase 2 provisioning validates private ownership/modes/ACLs through the account
broker, then sets trust to approved and sync to ready. Session worktrees have
standalone Git metadata under that private placement.

`group_project_owner` names current maintainers who review integration merges.
`group_merge_approval` records exact branch/old/new commit IDs, actor username,
revision and time. The approval route records consent; the phase 2 exchange
rechecks membership, named ownership and current revision under the ref-update
fence before applying it. Spent rows are retained so SQLite cannot recycle their
IDs. Exact old-ref comparison prevents replay, including uncertain Git/DB outcomes.
No new migration is required. See [Group Exchange](/Internal/Group-Exchange).

`group_key_grant` records only a key owner's explicit consent, optionally limited
to a project or machine. It grants no metadata access to a nonmember and delivers
no key in this phase. Owner-only key deletion still cascades consent deletion.
Membership and ACC-user removal preserve a maintainer and named project owner;
removal does not transfer group projects. Group audit exposes an allowlist of
safe receipt fields, never generic private audit detail.

Migrations explicitly transact SQLite DDL and backfill so a failure leaves neither
new tables nor partially changed routing. Renumber 0038 at merge if another feature
uses that number. Group execution remains disabled until private exchange and
removal/stop/reconnect cleanup are implemented. See [Groups](/User-Guide/Groups).

Owner-allowed issue servers use `setting` keys `issue_server.<id>` with HTTPS base URL, confirmed DNS address set
and optional PEM CA. Project GitLab/Gitea connections reference `server_id`; policies reload on each source access.
No migration is needed. Add/re-confirm/remove audit details omit PEM contents.

Migration **0042** adds the node's owner-selected `role`, `offline_threshold_seconds` (600 by default), durable `offline_since`, maintenance `snoozed_until`, and `last_downtime_seconds`. Existing roles are backfilled from stored OS/release metadata once; pending new enrollments may defer their default until OS discovery. Attention items gain nullable `snoozed_until`; open Inbox and push queries suppress snoozed items. `machines.check` is the owner-only observation containing the last manual round-trip results, live readings and versions. Audit records preserve policy edits, transitions, alert creation, manual checks and downtime.

## PostgreSQL option (HA phase 1)

SQLite is still the default. The same migration chain supports PostgreSQL when selected through `ACC_DATABASE_URL`. PostgreSQL stores transcript messages in a normal `transcript_fts` table with an English text-search GIN index; SQLite retains FTS5. Search ranking and tokenization can differ. Startup DDL uses a PostgreSQL session advisory lock; API mutations use a transaction advisory lock and background transactions use repeatable-read isolation to reject stale writes to concurrently changed rows, with row-lock waits failing quickly for retry. Unlike SQLite, changes to unrelated rows do not invalidate a write snapshot. Launch/resume ends the pre-broker snapshot and expires cached authorization before planning secret delivery, with no further broker wait before commit. These locks do not elect or fence a main. The root-only owner-run transfer verifies counts/checksums per logical table and resets integer sequences. Files and processes are outside its scope. [Manual HA](Manual-Failover.md) adds a separate explicit authority boundary.

## Manual HA authority

Migration 0044 adds `ha_authority`, constrained to primary key `id=1`, with a
64-bit monotonic `epoch`, nullable stable `holder` name and nullable random process
`incarnation`. Migration 0045 adds a non-null `uncertain` flag, false by default,
which records mutating RPCs with lost replies and canceled external execution and
blocks clean demotion (the holder keeps serving).
The schema starts empty so offline SQLite transfer still accepts
an empty target; the first HA initialization inserts epoch 0 once. Promotion and
demotion use conditional updates and write the owner's Audit entry in the same
transaction. A restarted process never inherits the previous incarnation. Do not
restore or decrement this row independently of the shared database. See
[Manual failover](/Internal/Manual-Failover).

## DNS ownership ledger (migration 0043)

`dns_owned_record` has a unique provider/zone-ID/hostname key. Azure zone IDs include the subscription and resource
group; Google identities include project and managed zone. Cloudflare, Route 53 and Hetzner IDs identify
the provider account resource globally; DigitalOcean uses the globally unique domain name. Rows retain request ID,
state, exact prior, desired and applied record snapshots (ID/RRset, name, type, values, TTL, marker, ETag) and timestamp.
The committed pending intent precedes network mutation; a conditional claim and unique key serialize competing jobs
without holding a database transaction across network I/O. A timeout, crash or failed mutation leaves pending or
uncertain state and blocks retries until operator reconciliation. Successful deletion removes the claim.

Publishing verifies both the ledger and exact remote state before update/delete. Shared RRsets, aliases, unmanaged
conflicts and drift refuse. Azure uses ETag prerequisites; Route 53 and Google use atomic exact-delete/create batches.
Cloudflare, DigitalOcean and Hetzner re-read and retain a residual race without a write prerequisite.
An explicit Caddy refusal restores the exact prior owned record if it still matches, or deletes the new owned record.
An unanswered, unavailable or cancelled root job marks the ledger uncertain and retains DNS/address reservations;
that queued job may finish later. This also blocks retargeting after an uncertain unpublish.

The `dns.publishing_source` setting is an owner-password-confirmed Cloudflare rollback choice (`vault` or `legacy`).
Saving a DNS binding selects `vault`. No secret is stored in the setting. `dns.record.adopt` audits explicit adoption
of a Cloudflare record matching an applied historical publication, never a comment-only ownership inference.

## Organization foundations (disabled)

Phase 0 adds development-only authorization classifications, an opaque-ID migration report and a standalone
execution-lease prototype. There are no application-table migrations or automatic company-mode activation.
Current usernames remain ownership keys until a separately qualified migration; group IDs and session IDs
are already opaque. Run the copy-only report from trusted source:

```sh
PYTHONPATH=src python -m acc.organization_ids DATABASE_COPY --namespace INSTALLATION_UUID
```

Save one namespace per installation; report files contain private identity mappings. It never applies changes. Unresolved references block the proposed migration.

The implementation plans are in repository `docs/ORGANIZATIONS-STABLE-IDS.md`,
`docs/ORGANIZATIONS-AUTHZ.md` and `docs/ORGANIZATIONS-RECOVERY.md`.
The lease state machine is not connected to brokers or supervisors. SCIM and mandatory five-minute stopping are
unavailable until later implementation and qualification.

Phase 1 (migration 0053) adds company sign-in tables, unused unless `ACC_ORGANIZATIONS=1` and an owner turns
SSO on: `oidc_provider` (one row: issuer, client ID, sealed secret, enabled, revision), `external_identity`
(unique organization/issuer/subject → `user.person_id`), `sso_invite` (one-time, 7-day first sign-in permission bound to a directory account in `email`) and
`oidc_flow` (one-use sign-in state). `user.person_id` is filled only when an identity is bound, with the value
`acc.organization_ids.person_id(namespace, user.id)` that the phase 0 report proposes; the namespace is saved in
setting `organizations.namespace`. `auth_session.method` is `oidc` for company sign-ins, and `auth_session.step_up` marks one that must pass a passkey
check first. `oidc_provider.session_hours` (1-24, default 12) is the company sign-in lifetime. Migration 0054 adds
`oidc_provider.mfa_acr` (owner-named acr values that prove MFA), `oidc_provider.passkey_enrollment`,
`user.company_only` (made by company sign-in, back-filled from the audit log) and
`auth_session.passkey_enroll_until` (decision 0061). Ownership still uses
usernames.

The integrated phase 0 inventory covers 328 registered API/WebSocket method/path pairs, including group
coordination and launch, device pairing, remote-access preparation, telemetry and S3 backup targets.
The ownership gate checks human-only owner routes separately from installation-token owner routes.
Classification records describe current handlers and delegated audit writes; completeness does not prove
semantic authorization correctness or mandatory audit delivery. Regenerate the reference with
`scripts/generate_organizations_authz.py` after reviewing inventory changes.

### Planned machine downtime (0051)

The `node` row stores nullable JSON `downtime_policy` (IANA timezone and weekly
start-day/time windows), `downtime_until_reconnect` (false by default) and nullable
`last_reconnected_at` (UTC). A real offline-to-online transition clears the one-shot
flag and records its timestamp alongside the existing downtime duration. Policy
edits use the owner-only `node.policy` audit.

Existing `server-backup.job.*` observations carry `reconnect_at`, `missed_captures`
and a catch-up note. The durable reconnect identity deduplicates captures even after
a job completes or the worker restarts; snapshot history only adds the note to a
successful job's exact backup and snapshot IDs. Backup result files retain their
true capture timestamps. No new route is required.

Public git-backup consent is `node.public_git_backup` (migration 0068), a server-owned boolean defaulting to false for new and existing machines. Node reports cannot set it. It participates in the machine policy revision, with owner-only revision-checked writes and a dedicated consent audit.

## Native device lifecycle (migration 0067)

`device.kind=native` adds nullable installation ID, platform and public-key hash.
The person/kind/installation tuple is unique (browser/node NULL installations
remain independent). `device_pairing.kind` defaults browser for existing rows;
native claims additionally retain public CSR, installation/platform/key hash and
the expected prior fingerprint for guarded renewal. `auth_session` adds nullable
`device_fingerprint` and `device_bound_at`; ordinary browser sessions remain
unbound. No private device key is persisted. See [Mobile devices](/Internal/Mobile-Devices).
