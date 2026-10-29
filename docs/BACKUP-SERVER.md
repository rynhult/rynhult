# Backup server role

**Status: approved by the owner, 2026-10-22, with the answers in decision 0008 (private decision record). Phase 1 capture/offline restore and phase 2 enrolled Linux targets implemented; S3-compatible targets are implemented with fail-closed retention observations; other platforms remain gated. Replacement activation is implemented off by default and remains gated on independent review and a fenced replacement drill.**
Scope: stage 1 of the approved [HA design](research/high-availability.md#owner-decisions-2026-10-22):
recover one main from encrypted off-host backups, not a second controlling main or automatic failover.
The approved design permits any enrolled machine or S3-compatible storage as a target.

## Implementation status (phase one)

Local/mounted directory repositories now use pinned restic 0.19.1 with committed SHA-256 verification in
`acc-packages`. `acc.backup_server` captures and signs complete generations; `acc-restore restore` validates
an explicit kit/backup ID into a fresh quarantined tree. See [the Guide](wiki/User-Guide/Backups.md),
[Operations](wiki/Internal/Operations.md) and [decision 0032](decisions/0032-backup-phase-one.md).
Owner routes support kit export, durable jobs, hourly schedules, reminders and separate prune. Full data readback
and scratch validation run on every upload. Mutable local copies never claim protection. This does not qualify
NFS/SMB, privileged installation or replacement-host readiness; those qualification gates remain ahead.
The remaining sections describe the full approved design, including those later capabilities.

## Phase two: enrolled Linux target (implemented)

The first remote backend is an **active enrolled Linux node**, selected by its immutable enrollment ID.
Enrollment alone grants no receiver access. **Host → Backups → Set up** creates a disabled target and
independent random 256-bit repository and transport keys. **Target setup** emits exact root commands for
its owner, then **Check setup** initializes/binds the repository and tests authenticated read/write, full
repository integrity, and refusal to overwrite/delete a disposable encrypted probe snapshot. ACC never
runs these root commands and never obtains a root shell, SSH identity, sudo grant or maintenance credential.
A failed check does not enable capture. Re-export the kit after the repository identity has been verified;
schedule enablement requires that exact repository identity in the exported kit receipt.

The existing Linux updater normally executes a bundle supplied by the main as root. This would defeat
append-only after main compromise. Owner setup first stops update/package request watchers and the package
update timer, refuses to proceed while their root workers are active, then adds root-owned systemd conditions
that block those services/watchers/timer while the receiver binding exists. Node code refuses new update and
system package requests; the root update helper independently refuses even pre-queued node updates.
These conditions also protect an older helper. Update this target only from independently trusted source
under owner control; never remove the binding just to accept a main-supplied update. Enrolled broker accounts
must remain unprivileged (no sudo, Docker or write access to receiver/root configuration). Privileged broker
accounts or other main-controlled root automation invalidate the history-preservation boundary.

The owner installs official **rest-server 0.14.0**, pinned by committed SHA-256 for Linux amd64/arm64.
Upstream: [release](https://github.com/restic/rest-server/releases/tag/v0.14.0),
[checksums](https://github.com/restic/rest-server/releases/download/v0.14.0/SHA256SUMS),
[receiver behavior](https://github.com/restic/rest-server/blob/v0.14.0/README.md).
The rendered script downloads only that release, verifies before extraction/installation and uses fixed
paths. It requires existing systemd/curl/tar/sha256sum/useradd; it installs no distribution packages.
The receiver runs as the dedicated non-login `acc-backup` user, with a root-owned systemd unit and auth
file, private `/var/lib/acc-backup` storage, `--append-only --private-repos` and a 100 GiB per-repository limit.
This limit is not a filesystem quota: a hostile writer can create subrepositories/fill the disk. Use a
separate filesystem/owner-administered quota when disk exhaustion could harm workloads. Setup deliberately
refuses an existing binding/account; review partial setup manually rather than overwriting another service.

A root-owned `/etc/acc/backup-target.json` authorizes exactly one installation/target pair and fixed
loopback receiver port (18762). A typed `backup-v1` channel on the existing authenticated machine link
carries REST bytes; requests cannot supply an account, shell, path, hostname or port. A missing binding,
wrong IDs, writable/symlinked binding, offline/revoked node or old node fails closed. One receiver channel
per node, existing bounded frame/channel/link queues, 64 KiB adapter chunks, restic concurrency one,
10 MiB/s and read/drain deadlines bound transfer pressure. Revocation closes the enrolled link. Bulk traffic
shares the existing link budgets; independent QoS/throughput guarantees under saturation are not claimed.

Restic runs on the main. A job-scoped adapter forwards to the bound receiver and closes its listener/channels
on exit. It listens on a Unix socket in a private 0700 folder (restic `rest:http+unix://`), not a loopback
port: other local accounts on the main, such as member agents, can neither hold its single receiver gate nor
reach the receiver through it. The receiver has only a repository-scoped random transport token;
its root-owned htpasswd stores the SHA-1 verifier of that **random 256-bit token**, never a human password.
SHA-1 password hashing is unsuitable for human passwords; this token has cryptographic entropy and cannot
be recovered by a dictionary attack. The raw transport token and restic repository key are separate private
0600 files on the main, never argv, API status, audit, setup text or the node channel configuration.
No repository password, signed plaintext manifest, DB or vault key goes to the node.

Append-only is enforced by the receiver, not the main's UI. A main compromise with both main-side keys
can read backups, forge new captures, exhaust capacity and delete transient locks, but cannot delete or
overwrite retained objects through the receiver. Locks remain mutable for restic operation. Target root,
receiver-account compromise and storage failure can destroy history; append-only is **not** a 30-day
immutable window. ACC therefore never sets the global protected flag from this check. Maintain independent
offline/immutable copies and an off-main record of trusted IDs, dates and signing fingerprints.

Backup IDs are restic tags, and tags are written by whoever holds the writer credential. A compromised main
can append a snapshot under an old backup ID (even with a validly signed manifest, since it holds the signing
key). Append-only keeps the genuine snapshot, so restore, the CLI (`--snapshot-id`) and **Test restore** select
a generation by its snapshot ID, a content hash the owner records off-main; an ambiguous backup ID without one
is refused. History flags duplicate IDs. Each capture first checks that the target still lists the last
verified snapshot and fails with *target is missing the last verified backup* if it does not, so a target
serving an older copy of the repository is noticed rather than silently extended.

Retention is administered separately by the owner. Remote prune requests are refused even if sent directly
to the API. Do not install a maintenance broker, give `acc-node` access to ciphertext files, or expose a
non-append-only endpoint to the main. Use a trusted independent maintenance host and administrator-managed
repository access for restic check/forget/prune; keep its repository key and administrative transfer credentials
outside ACC. Stop the receiver and coordinate maintenance with captures before filesystem maintenance.

**List backups** lists explicit backup IDs with their snapshot IDs. **Test restore** queues a durable full data check and authenticated
restore of the selected generation into private disposable main-side scratch. The outcome is audited without
secret details; selecting an old generation never replaces the latest capture age. Offline recovery exports
only ciphertext from `/var/lib/acc-backup/<target-id>` by the target administrator, then uses
`acc-restore restore --repository /secure/exported-repository ...`; it needs neither the old main's link nor
transport token. The kit anchors repository/installation/signing identity and contains the encryption key.

Qualification in this worktree uses temporary pinned restic/rest-server binaries and repositories, including
the production NodeLink/Relay/adapter, deletion refusal after successive captures, and historical offline
recovery. No cloud node, owner root command, real systemd receiver or network fault under production load
has been exercised. S3-compatible storage follows Linux review; see decision 0044 and the implementation notes below.
Provider production conformance and the subsequent security review remain outstanding.

The phase-one capture barrier now fails immediately for synchronous transactions on the asyncio loop,
returning retryable HTTP 503 with `Retry-After: 1` if capture wins the race after authentication. Worker-thread
transactions still wait with a bounded lease. Failed BEGIN releases its acquired lease. Supervisor stages retry
on later iterations, so capture cannot freeze the loop while waiting for a transaction on that same loop.

## Recommendation and current baseline

Use **restic**, pinned and checksum-verified by the installer, with one independent encrypted repository per
target and ACC installation. Keep the existing local daily backup and updater rollback backup; neither counts
as an off-host success. Start with manual, scripted cold recovery and measured drills.

Today [acc-backup](../deploy/backup/acc-backup) copies SQLite using its online backup API and checks integrity,
then copies secrets, catalog overlay and bare git repositories. It keeps 14 days in date-named directories;
same-day reruns replace data. Git copies hard-link unchanged files. Optional `ACC_BACKUP_MIRRORS` run as the
source account, use `rsync --delete`, and exclude `session.env`; they are mirrors, not retained generations.
`ACC_BACKUP_PASSPHRASE_FILE` encrypts **only secrets**, with OpenSSL AES-256-CBC/PBKDF2, not authenticated
whole-backup encryption. The database still contains plaintext endpoint keys and TOTP secrets; git contains code.
Do not upload these existing folders unencrypted or silently reinterpret that passphrase setting.

## Targets and trust boundary

Owner-only target creation, credential changes, recovery-kit export, retention changes and restore; all audited
without secrets. Test connectivity, write/readback and capacity before enabling a target. Multiple targets have
independent jobs, keys, age and retention; success on one cannot hide failure on another.

| Target | Receiver and channel | Permissions and limits |
|---|---|---|
| Enrolled machine | Explicit backup capability, not enabled by enrollment alone. New typed backup channel over its existing outbound authenticated TLS WebSocket; node proxies only to a fixed local rest-server receiver. A main-side loopback adapter exposes the REST transport to restic. No public listener, arbitrary socket, shell, path or broker account supplied by a request. | Linux/WSL receiver runs as a new non-login `acc-backup` account, with a private repository directory on the selected disk; `acc-node` gets only the fixed receiver connection. Root-owned setup binds target ID to an approved directory, quota and installation ID. rest-server append-only mode; no ordinary delete/overwrite except transient repository locks. macOS uses a LaunchAgent as its dedicated ACC user, never root: same-user sessions can destroy ciphertext, so show weaker isolation. Windows uses WSL. |
| S3-compatible store | Main restic worker uses HTTPS with chain and hostname verification, fixed endpoint/bucket/prefix, no redirects to arbitrary endpoints. Access/secret/session credentials live as backup-service vault references, never assigned to projects or sent to enrolled targets. | One prefix per installation/repository; writer may list/read/add objects, not change policy, retention or bucket configuration. Delete permission, if needed for transient locks, is limited to `locks/`. Separate offline maintenance identity expires generations. Detect and display versioning/Object Lock support rather than assuming AWS semantics. |

The existing signed-link protocol authenticates nodes with Ed25519 and authenticates the main through enrolled
TLS trust; frames are not individually signed and this is **not a persistent HA leader identity**
([MULTIHOST](MULTIHOST.md#connection-and-authentication)). Backup channels need explicit capability negotiation,
repository binding, revocation, bounded frames/queues, backpressure and timeouts. Reserve control/terminal capacity:
one bulk channel per target, at most 1 MiB per data frame and existing link budgets, resumable after disconnect.
Authenticate the local receiver/adapter with per-repository service credentials, not merely loopback reachability;
other local accounts get no access. These credentials authorize ciphertext transport, never decryption.
Do not disguise a receiver as an agent broker. Old nodes say "update required".

An enrolled target reads repository ciphertext and transport metadata, not database rows, filenames, git history,
vault keys or backup passwords. It learns sizes, timing and approximate change volume. A target already executing
sessions may separately know the keys those sessions were allowed; backup enrollment grants no additional keys.
Target root can delete or withhold its disk regardless of append-only mode: use an independent immutable/offline
copy for that threat.

Follow the [updater root-helper pattern](UPDATES.md#main-update-transaction): accd persists a bounded action/ID,
not commands, URLs or filesystem destinations. A root-owned helper revalidates trusted configuration, provisions
the receiver/service and performs privileged recovery steps. Normal capture, restic and untrusted input parsing
run unprivileged on the main as `acc`, with private staging, not as root. Target storage selection requires local
administrator confirmation of a safe non-symlink directory; remote ownership never grants arbitrary root writes.
No code, hook or configuration executable from a backup runs as root.

## Encryption and the recovery kit

| Choice | Tradeoff |
|---|---|
| **Restic (recommended)** | Existing authenticated encryption (AES-256-CTR with Poly1305 authentication; scrypt-protected repository keys), encrypted metadata, content-defined deduplication, checks and selective restore; native REST/S3 backends. Adds maintained binaries and repository/retention compatibility work. Pin a supported version and test its exact backend behavior. |
| age-encrypted archives | Authenticated streaming encryption, straightforward offline recipient key; full archives lose cross-generation deduplication and require ACC to build indexes, retention, partial restore and verification. Sensible export format, not the primary repository. |
| Small built-in AEAD format | Avoids a binary but makes ACC responsible for nonce/key discipline, chunking, crash recovery and cryptographic format evolution. Reject for stage 1. Existing CBC secrets archives are not the new format. |

Encrypt **all contents and the manifest on the main before transport**, including `acc.db` and `vault.key`.
Never give the repository password, decrypted manifest or vault key to the receiver. Restic transport credentials
are separate from encryption keys. Remote integrity checks run on the main, not by handing the target a key.

Generate a random 256-bit repository password per target. Store it outside the database/vault in an `acc`-owned
0600 backup-key file within a 0700 service directory; otherwise recovering the vault would depend on itself.
The main is already trusted with plaintext secrets. Deliver passwords/credentials to subprocesses through private
files/descriptors, never argv, logs or API responses. Vault S3 credentials use a service-only access path; a validated
job may materialize them briefly in a private credential file, removed on completion and restart recovery.

Require a **printable recovery kit**, exported once through an owner-confirmed, non-cached download (never a
session artifact). Include installation/repository IDs, target locations, passwords and key epochs, backup-signing
public-key fingerprint, trusted release/version retrieval instructions, manifest format and restore instructions.
Include separately scoped read-only S3 recovery credentials, or independent provider-account recovery instructions.
For a machine target, print the local administrator's ciphertext export procedure: export the repository from the
receiver disk to removable media or an administrator-authenticated transfer. Recovery must not need the dead main's
signed link or a login to its restored vault. Never print node private enrollment keys.

Keep two sealed copies outside the main and target failure domains (paper/offline password manager); confirm a kit
can unlock a test snapshot before enabling protection. Treat it as all ACC secrets, not ordinary documentation.
Retain historic key epochs until their generations expire. Changing a restic password wraps the same repository
master key; actual compromise needs a new repository/master key and a new kit. Vault-key changes, if introduced,
must be serialized with capture; preserve the matching key for every retained DB snapshot
([VAULT](VAULT.md#storage)). Do not invent an independent vault rotation scheme here.

## Capture, consistency and manifest

Each capture gets a random immutable backup ID, installation ID and UTC start/end times, never a day directory
reused in place. Only one capture coordinator owns a main; uploads may retry independently.

1. Obtain a cross-process maintenance lease shared with updater/migrations and vault/catalog writers. Under a
   short application mutation barrier, take an online SQLite backup, matching secrets and the complete overlay
   into an `acc`-private immutable staging generation. Time out visibly rather than holding writes indefinitely.
   Use atomic file replacement/key epochs; a crash leaves an incomplete capture, never a publishable generation.
2. Coordinate **each repository** with receive, ref updates, orphan moves, repacking and GC; copy its complete
   stable state before releasing that repository's lock. Preserve refs, objects, reflogs, `acc.project` identities
   and `git/orphaned`, not just tip bundles. Never depend on size/mtime hard-link comparison for correctness.
   These captures have individual times: do not claim an atomic DB-plus-git transaction. Bind repositories to
   project identities in the DB snapshot; flag later/unmatched repositories as recoverable orphans.
3. Validate SQLite integrity/foreign keys and schema compatibility; validate overlay references and git object
   connectivity. Hash the staged regular files. Exclude links/devices and report excluded or unsupported entries,
   rather than following them. Run restic against staging, not the changing source.
4. Upload encrypted chunks/indexes and the encrypted manifest; publish the snapshot only after content upload.
   Read back and authenticate all newly written objects before recording upload verification. Partial jobs remain
   failed/retryable; no "last good" timestamp advances on mere receiver acknowledgement.

The versioned manifest records:

- Format, capture/job/installation IDs, source execution-machine ID, ACC version/build, required restore-tool
  version and per-component capture intervals.
- SQLite schema/migration head, file SHA-256, page count and durable capture checkpoint ID recorded before the
  online backup. SQLite has no PostgreSQL-style LSN: this ID is a capture boundary, not a replication position.
- Secret inventory and vault-key epoch/fingerprint (not plaintext key material); repository encryption-key epoch.
  Database and key are one restore unit, never selectable independently.
- Overlay content revision/digest, including profiles, custom files, restored generations and active pointer;
  per-repository project identity, refs/object IDs, capture time, size and latest successful upstream push time.
- File paths relative to allowlisted component roots, types, sizes and SHA-256; exclusions, configuration recovery
  inventory, target repository/snapshot identity and retention policy.

Sign the manifest content with a separate installation Ed25519 backup-signing key kept on the main; the encrypted
snapshot carries the signature. The recovery kit anchors the public key, not a key suggested by the restore source.
The signature binds installation, backup ID, inventory and component hashes. Keep last verified IDs/dates in the
off-main drill record to detect rollback; a valid old signature alone does not prove freshness.

Required payload: DB, all secrets, entire catalog overlay, all git backup repositories. Also include a restricted
configuration inventory and encrypted allowlisted main config, stable endpoint and enrolled trust information.
Root-owned TLS/CA private material requires a separate helper-produced encrypted recovery component or documented
independent PKI escrow; record its availability as a recovery prerequisite, not a silently missing file. Preserve
private-CA identity where nodes pin it. Regenerate source, assets and `data/bundle` from trusted matching releases.
Restore configuration as reviewed data, never by blindly sourcing backed-up environment files or installing units.

Exclude `/srv/acc/run*`, generated launch credentials/provider configs, node private keys, transient helper queues,
live WAL/SHM and process state. Do not copy `data/remote-spool` independently of ingestion checkpoints: stage 1
omits it and records history gaps, starting fresh mirrors without reusing mismatched offsets. Do not claim full
transcript recovery. Optional project dumps/mirrors need an explicit per-account capture adapter and consistency
statement, not root walking user-controlled trees. Main workspaces, dirty trees, provider transcripts/logins,
containers/volumes and published application data require separate execution-host/application backups
([HA state inventory](research/high-availability.md#state-inventory)).

## Schedule, retention and durability

Proposed defaults, subject to owner approval: hourly capture with small jitter; manual **Back up now**; retain
48 hourly, 30 daily and 12 monthly generations. Copy every capture to every enabled target. Upload one job per
target at 10 MiB/s by default, configurable rate/window, resumable bounded retries with exponential backoff.
Capture once, upload independently; bound staging by a configured disk budget, fail and notify rather than fill
the main. Retain unuploaded captures until budget requires operator action; never silently discard the only copy.
Report bytes/estimated initial-upload duration. Deduplication occurs within each repository, not across targets;
expect DB churn and large initial git transfers.

Expire only after a newer complete verified generation exists and the policy permits it. A separate maintenance
credential/process runs restic forget/prune; normal upload credentials cannot prune. On enrolled receivers,
maintenance is locally administered and cannot be requested through the ordinary backup channel.

For S3, enable versioning and a proposed **30-day immutable window**, with no uploader ability to delete versions,
shorten retention or bypass governance. Prefer compliance Object Lock where its irreversibility is acceptable.
Retain data/index/snapshot/key/config objects; exempt transient locks from retention. Bucket-wide default retention
that traps lock deletion is not assumed compatible. Prototype provider-specific prefix policies and restic
lock/prune behavior before claiming immutability. Versioning alone is not immutability.

Prune must respect the immutable window **and references from every retained snapshot**; conservatively keep
objects and report blocked maintenance rather than breaking a repository. Size lifecycle retention for monthly
snapshots; never apply blanket data-object expiry. Document reconstruction of a repository at a known timestamp
from versioned objects after malicious overwrites/delete markers. If a provider cannot enforce the policy, label
it mutable and require another protected/offline copy for ransomware resistance.

## Verification and visibility

After each upload, fetch and authenticate its manifest, indexes and all newly uploaded objects, checking component
hash inventory. Deduplicated old chunks remain covered by rolling checks, not an implied new full read.
Weekly, restore the latest snapshot **from each target** into an isolated private scratch directory on the main
or a designated trusted recovery host; check every restored file hash, SQLite integrity/foreign keys, sample vault
decryption without logging values, overlay loading and git fsck. No network, hooks, migrations, supervisor,
publishing or autostart. Monthly rotate through older generations and complete repository data checks.
Delete scratch plaintext afterwards; encrypted scratch storage is preferred, since unlinking is not secure erasure.
Quarterly perform a full fenced replacement-host drill from the kit and record measured RPO/RTO.

Proposed owner-only **Host → Backups** shows targets, coverage/exclusions, upload state/progress, last captured and
verified times, last scratch restore, bytes/capacity, immutable-until and exact failure. **Never** derive age from
upload time alone: show the age of the captured data and git upstream lag separately. Badge "uploaded", "verified"
and "restore-tested" distinctly. Initial default: stale after two hours without a verified capture; also warn on a
failed job immediately and a drill over eight days old. Persist one deduplicated owner Inbox item per target/cause,
resolve on recovery and audit results without sensitive paths/values. Thresholds track configured schedules.
ACC cannot notify while the main is dead: independent external age monitoring/off-main drill records are needed.
Host → Backups supplies enrolled Linux and S3 setup, listing and isolated restore tests. S3 COMPLIANCE retention is observed as described below; replacement activation is an off-by-default candidate described below.

Durable DB job intent records action, stable ID, target, owner, capture and attempt/result. One leased coordinator,
cross-process capture lock, idempotent snapshot IDs and restart reconciliation handle lost replies and abandoned
staging. Cache only derived progress; no correctness state solely in memory. This follows the
[HA rules to adopt now](research/high-availability.md#rules-to-adopt-now), without pretending local leases fence
two active mains.

## Restore contract

Implementation reserved 2026-10-25 on `backup-replacement-activation` (session `1ac80b89c34e`);
implementation is complete as a disabled candidate; independent review and a fenced replacement-host drill remain required.

The phase-one standalone `acc-restore restore` CLI and offline-readable runbook are available. Download trusted
matching source independently; never run a script supplied by the target. The contract below also describes
privileged installation and replacement activation, implemented as the off-by-default candidate below. Live qualification remains gated.

1. **Fence first.** Operator powers off/isolates the old main and prevents its services restarting or reaching
   nodes, DNS and root helpers. DNS movement alone is not fencing. Record evidence before replacement control
   starts. No reachability proof means remain in offline restore mode; stage 1 cannot safely auto-promote.
2. On a freshly installed replacement, keep accd/socket activation, timers, brokers, reconcile, publishing and
   autostart disabled. Use the kit to access the repository without the old main. Verify repository/installation
   identity, manifest signature, version/schema and complete hashes; select an explicit backup ID, not an
   untrusted "latest" pointer. Present data age, exclusions and missing prerequisites for confirmation.
3. Restore into private scratch **unprivileged**, under size/file-count limits and no-network validation.
   Reject path traversal, absolute paths, links/devices, unsupported formats and mismatched DB/key pairs.
   Install the matching version/build from a trusted release; validate before any deliberate migration.
4. A constrained local root helper installs only validated allowlisted data with fixed ownership (`acc`, 0600
   secrets/DB, 0700 private trees). While stopped, remove stale SQLite WAL/SHM; restore DB/key/secrets, overlay
   and git together. Regenerate git hooks/config from trusted installed code while preserving identities,
   kept rewrites and orphaned repos ([gitbackup.py](../src/acc/server/gitbackup.py)). Never execute restored hooks.
5. Review configuration, stable hostname/origin, DNS and TLS/CA escrow. Check account/path mappings and treat
   the old main's `host=local` sessions as unavailable on this replacement: do not relaunch them. Until stable
   execution identities ship, the restore tool must enforce this startup hold explicitly. Local workloads need
   their original transcripts/workspaces or explicit operator recovery, not invented replacement conversations.
6. While isolated, review post-snapshot enrollments, revocations, login sessions, approvals, grants and credentials
   using independent operator/audit records. Revoke restored login sessions, rotate exposed credentials and
   revalidate nodes as needed; do not resurrect old authority silently. Quarantine restored pending helper intents
   and backup jobs. Rebuild/reconcile spool checkpoints; disclose unavailable history.
7. Move the stable endpoint only after fencing and validation; allow nodes to reconnect under verified trust in
   recovery mode with effects still held. Inspect running remote sessions/brokers and desired state with a person.
   Explicitly release selected reconcile/publish/autostart work. Record outcome, new main identity and first new
   verified backup. The old main can return only after wiping/re-enrolling its control identity, never by DB merge.

**Single items:** use the same authenticated snapshot selection and scratch validation. For a project's git
backup, select by recorded project identity, fsck and export a bundle/clone into a new destination as that
project's account; never overwrite live refs by default. Preserve orphan recovery and kept rewrite refs
([Operations](wiki/Internal/Operations.md#git-backups)). For the overlay, preview the full generation against
current built-ins and vault references, then owner-confirm an atomic revision-checked switch under the catalog
mutation lock; retain the previous generation. Missing vault references block apply. Never restore the old DB
or secrets just to recover one catalog item.

## Security review of the proposal

| Threat | Mitigation and residual risk |
|---|---|
| Compromised target or stolen disk | Main-side encryption/authentication and signed manifests; target has no decryption key. Sizes/timing leak. It can withhold everything: independent second failure domain required. |
| Stolen S3/receiver credentials | Prefix/repository-scoped credentials, TLS pin/trust policy, vault storage, rotation and no retention-admin privilege. Ciphertext remains encrypted; write abuse can cost money/fill storage, so quotas and alerts matter. |
| Ransomware on main deleting backups | Append-only receiver plus separately controlled maintenance; S3 immutable versions, offline kit and independent copy. Main compromise exposes encryption/signing keys and can upload valid poisoned snapshots; retained historical generations and independent drill records are essential. Receiver root is not constrained by append-only. |
| Malicious restore source | Offline-pinned signing identity, authenticated repository data and hashes, known checkpoint comparison; distrust freshness claims. Parse with limits unprivileged, no links/traversal/hooks/config execution, fixed privileged destinations, isolated drills. A valid signature cannot prove an already compromised main captured honest data. |
| Stolen recovery kit or scratch plaintext | Physical/offline access controls, two independently secured copies, encrypted scratch and explicit export audit. Kit plus repository access exposes DB and vault together; losing every kit/key makes recovery impossible. |

Out of scope: automatic failover/streaming replication, active-active or PostgreSQL recovery, zero data loss,
survival of local execution state, application-consistent container backups, target-root availability guarantees,
recovery after loss of every key/copy, and protection of secrets on a compromised main. The HA goal of roughly
five-minute loss and failover within minutes belongs to later replication stages, not this hourly backup design.

## Effort, phases and approval questions

Planning estimate: **4-7 engineer-weeks**, plus owner-operated recovery drills; not a delivery commitment.

1. **Capture and offline restore (1-2 weeks):** manifest, coordinated barriers, authenticated encryption, recovery
   kit, quarantine/startup hold and restore CLI. Prove DB/key pairing, git concurrency and isolated replacement
   recovery before enabling remote targets.
2. **Targets and retention (1-2 weeks):** signed-link capability/receiver on Linux/WSL, Mac LaunchAgent parity,
   S3 vault credentials, quotas, retries and separate maintenance. Gate each platform/provider on conformance.
3. **Owner controls and qualification (2-3 weeks):** Host/Inbox, scheduled checks, lifecycle documentation and drills.
   Cover duplicate jobs/lost replies/restarts, disk-full and partitions, revocation, corrupted/replayed manifests,
   hostile paths, missing keys, version skew, malicious restore data, object-lock/prune and old-main fencing.
   Record full replacement-host RPO/RTO before describing the feature as recovery-ready.

Owner approval requested:

- Restic and maintenance dependencies, rather than a custom format; any provider/platform to qualify first?
- Hourly schedule/two-hour stale threshold, 48 hourly/30 daily/12 monthly retention, 10 MiB/s default; acceptable
  metadata loss and outage for stage 1? Initial uploads and weekly full restores may require larger windows.
- Require an independent immutable/offline copy for a "protected" status? Approve 30-day lock and storage cost;
  compliance retention cannot be shortened even by an administrator.
- Who holds/tests the kit, administers prune, fences/restores and owns TLS/CA escrow? Can the main also be a
  workload host with explicitly excluded local state, or must company workloads use execution nodes?

Owner approval is recorded in decision 0008; phase-one implementation choices are recorded in decision 0032.

## S3 implementation (decision 0044)

Owner setup in Host → Backups accepts an HTTPS endpoint, bucket, region and write-only infrastructure credentials.
The native pinned restic S3 backend stores only ciphertext under a unique installation/target prefix. Existing jobs
provide hourly capture, full readback, authenticated snapshot listing and isolated restore. Writer replacement
disables capture until setup is rechecked. Credential scratch is private and removed on exit/reconciliation.

Separate prune/retention identities remain outside accd and its ordinary vault; both API and worker refuse remote prune. Recovery kits
contain independent provider-account recovery and version-aware ciphertext-export instructions without writer keys.
Versioning-only or unsupported stores can back up but cannot claim protection. COMPLIANCE Object Lock observations
include deletion refusal on a disposable valid encrypted snapshot and explicit-version retention inspection of every
current object (excluding transient locks), bounded at 100,000. Matching complete key/version inventories bracket
full authenticated readback and isolated restore; protection never borrows a concurrent replacement's retention.
Receipts without version-bound evidence do not qualify after upgrade. Fresh checks must leave at least 30 days
at completion on all objects, including
older deduplicated chunks. Independent maintenance must extend retention or rotate repositories as that horizon
shrinks. Overwrites can hide current objects while original locked versions survive; independent version-aware
export is part of the recovery procedure. Finite probes do not prove an arbitrary IAM policy: the owner must review
least privilege and other provider identities. See [Guide](wiki/User-Guide/Backups.md#s3-compatible-storage) and
[decision 0044](decisions/0044-backup-s3-target.md).

The matching-inventory argument requires the independent policy to deny non-lock version deletion and retention
overrides, and namespace-changing maintenance to stay outside capture. Monotonic exact-version retention extension may overlap captures. Endpoint configuration is owner authority: HTTPS private
destinations are permitted, DNS is not pinned and no address-range egress sandbox is claimed.


## Independent S3 retention maintenance (decision 0053)

`acc-s3-maintain` is an off-by-default independent-host CLI, not an accd job. It takes an explicit private,
purpose-tagged maintenance identity outside the main, plus a target file listing all writer key IDs. Matching
writer IDs are refused even if misconfigured with retention rights. Owners must separately prove distinct IAM
principals and review effective permissions; a different key is not proof. The Guide includes the exact restricted
AWS IAM policy and owner commands.

A complete bounded inventory of all surviving restic object versions conservatively covers referenced old chunks
without a repository password, including hidden history and possible orphans. Dry-run reports exact extensions,
extra locked GiB-days, request counts and optional owner-priced cost estimates. Below 35 UTC days remaining,
apply extends to 60 days from inventory completion, never shortens/deletes, rechecks exact versions, verifies
read-back and fsyncs intent/result checkpoints. Plans bind identity/target, expire after 24 hours, use rate limits,
batch limits and an irreversible-storage budget. Failures/lost acknowledgements resume by re-reading retention.
Only retention extension may overlap capture; prune/version cleanup require pause, provider writer revocation
and quiescence. Independent completion does not update main-side capture receipts or protection status.

Host Backups projects retention runway from the last version-bound minimum deadline and warns at seven days;
it does not substitute for fresh two-hour capture/readback evidence. Owners can rotate via the new S3 target
flow: new prefix/key, new kit, verified capture/restore, then stop the old writer and maintenance. Preserve old
versions/kit until every actual lock and recovery obligation expires; no automatic cleanup is added. See
[owner runbook and policy](wiki/User-Guide/Backups.md#independent-retention-maintenance) and
[decision 0053](decisions/0055-independent-s3-retention-maintenance.md). Fake S3 tests do not qualify a live AWS
account, its IAM, KMS, billing or version-aware disaster recovery.

## Repository lock recovery

Restic exit 11 is reported as **repository locked**. Status and audit records contain
only bounded lock IDs, hostnames, PIDs, creation times, ages and lock types, read from
lock metadata; raw stderr and usernames are excluded. Unknown or malformed metadata
fails closed.

The main verifies the saved repository identity before recovery, and never runs `restic unlock`: that removes
whatever restic considers stale when it runs, including a lock created after Rynhult's check, and restic calls a
same-host lock under 30 minutes stale whenever it cannot signal the PID (a permission error included; Rynhult does not
run as root). Instead it removes exactly the lock IDs it judged removable, each re-read just before removal and removed
only if unchanged: a file delete for local targets, `DELETE locks/<id>` on the receiver (pinned rest-server
`--append-only` permits deleting locks only), and one object delete on S3. Lock files are written once (a refresh
writes a new ID), so an ID judged stale never becomes a live process's lock.

A lock is removable when it is older than four hours (automatic, twice the command timeout) or 30 minutes (owner,
restic's own stale timeout; live restic refreshes its lock every five minutes), or when it is provably this main's
own: a matching hostname is not proof, since a standby, restored clone or container can share it. The main records
each restic child it starts (PID, boot ID, start and reaped times, the last 64, beside the target key), and a recent
lock counts as its own only if the PID and lock time fall within a recorded child's lifetime and that child was
reaped, or belongs to an earlier boot or a crashed run whose PID is gone. On exit 11 the main removes such locks and
retries the command once only when every observed lock qualifies. Lock metadata reads are bounded: at most 16 lock
files of 4 KiB, 60 seconds per read and three minutes in all. History reads report locks but never remove them, and
a target busy with another command answers "in progress".

Otherwise an owner can confirm **Remove stale lock** in Host → Backups → Details.
That queues an audited stale-only check, reports removed and remaining locks, and
leaves retrying the backup to the owner. Recovery requires a bound repository identity
and readable metadata; incomplete setup may still require administrator review.

A process lease beside each target's private key serializes this main's repository
commands, including history and restore dumps. Commands retain a two-hour deadline;
worker cancellation and timeouts send SIGINT and allow 65 seconds for cleanup before
killing and reaping only the recorded child. Restore dumps retain their size and idle
bounds. A capture still runs `check --read-data` over the entire repository (not
`--read-data-subset`) and an authenticated restore of its selected snapshot. The
check itself has no stdout-idle timeout; metadata's short deadlines and zero
cleanup grace do not apply to it. The node adapter's 60-second idle budget measures
progress in either direction together, so an active long upload/download is not
cut off because the opposite half is silent; its 30-second write-drain bound
remains. Hard kills, host crashes or an unreachable receiver can still leave a lock;
stale-only recovery handles that on the next attempt. Append-only retention of backup
data is unchanged: transient lock deletion is already permitted by the pinned receiver.

Failures may now append fixed classifications for known restic stderr phrases:
repository lock refresh failed, operation cancelled inside restic, or cancellation
cause unavailable. Raw stderr, filenames, URLs and credentials are never copied.
Exit 130 alone does not prove the parent sent SIGINT: pinned restic also returns
130 for its own cancelled context. Such a failure never advances verification or
causes an automatic unchecked retry.

## October 26 operational fixes

Backup API lease contention is a typed LeaseBusy/BlockingIOError admission refusal
(409), including enable/disable/run/check and credential changes. Other timeouts
retain their own failure semantics. Receiver setup is a child Bash heredoc with
an ERR diagnostic; mount dedicated storage before setup. The standalone isolated
`backup_receiver_check.py` guard is copied into /usr/local/libexec, runs as the
receiver account in ExecStartPre, and checks device/fsid, 0700 ownership, filesystem
root mounts and recursive readability. The fixed node channel checks the same
binding identity before every receiver connection; owner Check setup preflights
that channel before restic. Missing legacy identity fails closed for owner review.

Revoked/missing enrolled machine targets have machine_removed=true, no stale
alert or enable/run authority, and a confirmed owner removal endpoint. Removal
archives target configuration and preserves results, key material and DB history.
Restic cancellation uses SIGINT, a bounded cleanup wait, then PID-specific kill;
repeated accd cancellation cannot detach its mutation thread. Installer dry-run
probes the existing capture-update lease read-only and exits 75 while busy.

## Replacement activation implementation contract

The implementation uses an offline, unprivileged preparation step followed by a local root-console controller;
there is no accd activation endpoint, sudoers grant, main-supplied command or automatic promotion. The controller
is off unless root explicitly enables `/etc/acc-replacement.json`, binds it to the exact reviewed preparation-plan
SHA-256 and records independent fencing evidence. DNS movement, a failed ping, a local lease and a checkbox alone
are not a fence. The operator records a power-off or independently enforced isolation reference, prevention of old
service restart, and isolation from nodes, DNS and privileged helpers. Evidence is bound to installation, backup,
old execution-machine identity and a fresh replacement identity, with a short expiry for transitions.

Preparation authenticates the selected generation with the kit using the existing offline validator. It produces
a fresh private tree and plan, never installs data or contacts a target. It invalidates restored browser/device and
login flows, drops the restored installation API token (owner access without sign-in; a fresh one is made on
first start) and the file-stored GitHub/Cloudflare tokens, disables schedules/autostart/backups/public routing, quarantines pending approval/helper/backup intents,
and archives restored sessions. Local sessions remain unavailable permanently on this replacement. An explicit
selection of remote sessions may be made resumable only with independently reviewed node identities; no session is
automatically launched. Restored grants and queued operations cannot silently regain authority. Independent review
of post-snapshot changes, stable endpoint/TLS escrow, account/path mappings and credential rotation is mandatory.

The fixed-source root controller copies only the plan's allowlisted regular data files to fresh fixed destinations;
it does not parse SQLite, run git, execute hooks, install configuration/units or start services. It pins open source
directory/file descriptors, checks hashes and size/count limits, sets acc ownership/private modes and journals the
installation before moving components. A partial install remains held and requires manual review. It never replaces
an existing installation or merges databases. WAL/SHM, runtime, spool and helper queues are never installed.

`installed → recovery → active` are separate durable, audited transitions. Recovery keeps ordinary HTTP/WebSockets
and all workers held. The operator may move the stable endpoint and review remote machines independently while
held; selected nodes reconnect only after explicit control release. Control release exposes normal control with
restored effects still disabled: selected remote sessions require a fresh Resume, Publishing requires a fresh owner
request, and autostart/schedules/backup targets require deliberate reapproval/reconfiguration. The old main must
remain fenced and can return only after wiping/re-enrolling its control identity. New verified backup and measured
replacement-host RPO/RTO remain release qualification, not an assertion made by activation.


The trusted-schema check compares SQLite schema objects/columns against a fresh database produced by matching
trusted migrations. It canonicalizes top-level table-clause ordering (Alembic constraint ordering is not
stable), retaining SQL clause bytes except the narrow numeric-default spelling exception below, and checking
column order separately. Extra triggers, views, virtual tables,
constraints or altered definitions refuse preparation, even when the backup signature is valid. The original
quarantine validator and this comparison run unprivileged. Root streams/copies bounded chunks, verifies exact
hashes and fsyncs files/directories before committing installation state; it never interprets the restored DB.

The comparison tolerates only the single-quote spelling of canonical signed 64-bit
integer DEFAULT literals on recognized numeric-affinity columns (`'0'` versus
`0`, for example). Both DDL and `table_xinfo` use that same narrow exception.
TEXT/BLOB defaults, expressions, constraints, column order, triggers, views,
indexes and extra objects retain strict comparison. This handles historical
migration default spelling without rewriting a restored database or executing
its DDL. No canonicalizing migration is needed.

Remote resume selections bind node enrollment ID/key fingerprint, account and original conversation ID, and
are consumed by the first successful manual Resume. Revocation, key/account/conversation changes or local
placement refuse the exception. Captured worker observations remain as inert recovery records, not live intents.
A reviewed node that reconnects still running a held session is reported to owners (`replacement_held` attention
item and a `session.replacement_held_running` audit entry). The supervisor neither adopts nor kills that pane,
input is refused and the browser terminal opens read-only. A Resume that uses the exception is audited with
`replacement_exception`. Restored approved telemetry is held (reports return to owner review, usage is discarded).
Nothing stops a held pane automatically. An owner whose sign-in was verified again within five minutes may use
**Stop on that machine** (`POST /api/sessions/{id}/stop-held`). It sends one `kill` for that held session only,
through the normal node channel. The session stays archived. Every attempt is audited as
`session.replacement_held_stop` (refused, failed or stopped), and the API token is refused.
Restored model endpoint keys, catalog MCP values and vault values are not cleared: the owner decided (2026-10-25)
that the operator's `credentials_rotated` attestation covers them, and the runbook says so explicitly.



Current limitations: no automatic power-fence adapter, no automatic credential/PKI rotation, no privilege grant
or systemd installer for this controller, no PostgreSQL activation, and no live RPO/RTO claim. The root operator
must independently enforce and document the fence and trusted deployment. The fixed controller only inspects
systemd; it never masks/unmasks, stops or starts units. Activation is not permission to remove the startup hold
manually or bypass root receipts. Normal control release allows reconnect/inspection with all restored workloads
held; subsequent explicit actions release selected work. See the Guide for the staged operator procedure.


## Busy-server catalog validation and deployment planning

`catalog_revision` records the effective catalog: built-in skill/agent contents, raw manifest/profile bytes, and
local/restored JSON. It does not hash sessions, the SQLite database, git refs or activity counters. Capture already
holds `catalog_lock(catalog_local_dir)` across database snapshot and overlay copy. Holding that lock through restic
upload would not stabilize the installed built-in tree and would unnecessarily block catalog edits.

New captures also sign `catalog_overlay_revision`, a SHA-256 of the `catalog/` subset of the authenticated file
inventory. Readback checks that payload-bound revision instead of comparing the descriptive full effective revision
against uncaptured installed built-ins. It still loads/validates the catalog, verifies vault references against the
captured DB, checks file hashes/signature and requires the trusted version/build. Legacy manifests without this
field retain their full revision equality check. No old capture is silently rewritten or relabelled successful.
A new reader accepts older matching manifests; an older reader can still validate new captures when its installed
catalog matches the recorded effective revision, since the original field is retained.

The `.worker` lease intentionally spans reconcile → schedule → work → remind. Reconcile treats outstanding running
jobs as interrupted; allowing another scheduler to reconcile while the first uploads would corrupt job state.
This lease is not an active-capture indicator: it can cover scheduling, DB or git-lock waits, restic uploads and
checks. Read-only deployment planning therefore inspects only `.capture-update.lock`, using `/proc/locks` without
flock/lockf or file creation (SR9-12). This is an observation, not a deploy/capture exclusion guarantee; real update
coordination remains authoritative. Supervisor maintenance skips inspect `.maintenance.lock`, not `.worker`.

Git backup non-fast-forward refusals preserve the old remote ref. The broker attempts the rejected source SHA under
`refs/heads/backup/<UTC second>/<branch>` (or the analogous tag ref). A successful fallback protects both histories.
“Could not be kept” means that fallback also failed: it preserves old history but does **not** establish a backup
of the new tip. The supervisor records the error without advancing its successful digest/time. Same-second name
collisions, prefix conflicts, concurrent pushes or other push errors can fail the fallback; the truncated error tail
is needed to distinguish them. The second-resolution name makes collisions possible under rewrite bursts. This
branch changes neither ref policy nor fallback naming.
