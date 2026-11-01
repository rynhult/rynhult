# Architecture

Default Linux single-main installation (SQLite). Explicit PostgreSQL/HA setup and
the unreleased desktop spike have separate boundaries below and in
[Development](/Internal/Development#linux-desktop-spike).

```
 browser ──HTTPS──► Caddy ──► accd (user acc, /run/accd.sock)          root helpers (systemd)
  (private networks)          │  FastAPI API + built UI                  acc-publish   (Caddy + DNS)
                              │  supervisor loop                         acc-hoststate (host snapshot)
                              │  SQLite /srv/acc/data/acc.db              acc-backup    (daily backups)
                              │        ▲ hook spool files                   ▲ request/result files
                              ▼        │                                    │
                     broker per agent account (systemd socket, accd only) ──┘
                              │  tmux -L acc: launch, attach, kill, interrupt
                              ▼
                tmux session ─► claude … | codex …   (in the project or workspace folder)
                       hooks ─► /srv/acc/run/<session>/events.jsonl
```

## The pieces
- **accd** (user `acc`) serves the API and the dashboard, owns control state in SQLite or explicitly configured PostgreSQL, and runs the **supervisor**: it ingests hook events, compares desired and observed state, resumes crashed sessions with bounded retries, and polls GitHub, the host snapshot and provider usage.
- **Brokers**, one per agent account, are small, stateless executors, each on a socket systemd creates and only `acc` can open (accd checks who holds it). It launches and attaches tmux sessions, writes trust records, clones and pushes repositories, and reads docs and wikis. It does not own the main's vault, but handles session credentials for its account. It validates every request (session names, executables, folders under the workspace roots).
- **Agents** are the real Claude Code and Codex CLIs in tmux, running as their agent account (never the admin user). ACC gives each session generated settings: hooks that append to its spool file, a statusline that reports plan usage, and the chosen skills, agents and MCP servers.
- **Root helpers** do the few privileged jobs through fixed request files: publishing (Caddy routes), certificate credential delivery, the read-only host snapshot, and backups. Publishing DNS runs in accd through the fixed provider registry and ownership ledger.

## Key properties
- **Issue-source boundary.** `issue_sources.py` calls GitLab/Gitea/Forgejo REST or Linear GraphQL from accd only, resolving
  project-granted vault ids for the signed-in person before every cache lookup. Credentials partition a bounded
  30-second cache and 30-request/minute budget; provider backoff is honored. Connections never contain token values.
  Migration 0033 adds project `remote_url`/`issue_sources`, session `issue_source`, and vault-grant `server_only`.
  Preview/launch fetch authoritative issue details and append explicitly untrusted JSON. Server-only grants never
  enter agent environments or MCP configuration. Optional Linear PR comments persist a pre-send marker, report
  uncertain delivery, and never retry an ambiguous mutation automatically.
  Owner-only `issue_server.<id>` settings hold HTTPS base URLs, confirmed exact DNS sets and optional PEM CA trust.
  Project connections reference server ids; every source access reloads policy, and each outbound request resolves,
  checks the whole set and pins the numeric destination with the original Host/TLS identity. Private LAN/ULA access
  requires an entry. CA trust is confined to that entry's host/port/path. Deletion fails before cache access;
  changed policy partitions the cache. No machine trust changes or schema migration are needed.
- **Native sessions.** ACC never re-implements an agent. Resume uses the CLIs' own `--resume`, with the same generated settings.
- **Never a silent new conversation.** A session that can't be resumed becomes **Recovery needed**.
- **Least privilege.** The dashboard user can't run shells; agent accounts have no admin rights and can't read ACC's database or secrets; only root helpers touch Caddy, in fixed ways; accd changes Publishing DNS through the purpose-bound provider and ownership checks. Encrypted backup-server capture/restic runs unprivileged as `acc`; the legacy daily backup remains a root helper.
- **Observable.** Hooks and tmux process state are preferred evidence. Provider-specific terminal-screen heuristics fill gaps and are identified as inferred. Working hooks silent for ten minutes become **Unknown**, rather than keeping old activity current; see [stale status](/User-Guide/Sessions#when-a-working-status-is-too-old).

## Terminal message submission

`POST /api/sessions/{id}/message` routes the provider id through the supervisor to the broker's `send_text` operation. The broker inserts literal text once, waits 200 ms, and sends Enter. After 300 ms it captures only the cursor row, not transcript history. The provider checks that row for pending text or a paste placeholder; Enter is retried at most twice, without retyping the message.

The supervisor ingests existing hooks before sending and waits up to 15 checks, 200 ms apart, for a newer hook that normalizes to `UserPromptSubmit`. Local or mirrored remote spools are ingested directly during this check, including when a team operation is already running inside the normal ingestion loop. A quick turn that returns to idle still has a submission event. Pending input or missing confirmation returns **message typed but not submitted**, not success; only confirmed messages receive a `session.message` audit entry. Existing API error mapping returns 502 for a broker refusal and 409 for missing hook confirmation. No automatic resend follows an uncertain result.

## Optional teams

M17 adds `team` grants, session ancestry and durable `team_request` replies (migration 0032). `bin/acc-mcp-team` uses stdio MCP and appends `AccTeam` to its own session spool. accd derives the caller from that registered spool, rejects caller/team claims, checks subtree membership and the single shared grant, then invokes broker operations. No agent gets a dashboard token or broker socket access.

Replies are atomically written by the broker to `run/<caller>/team/<request-id>.json`, with no symlink traversal. Requests are deduplicated by caller and id. An interrupted mutation is not replayed. Team stop persists all desired states before broker calls. Expiry closes the grant, stops helpers and notifies the lead. Helper permission prompts follow the existing human Inbox path; team messages cannot approve them.

Each member's optional host/repository override routes its launch, spool, terminal and lifecycle to the selected machine. Ordinary project sessions default to their project's host but may explicitly select another enrolled machine, reusing an authorized checkout or confirming a copy. Both use the same bounded verified broker bundles and worktree operations; no network clone, new privileged operation or agent token is required. Placement keeps the session's routing independent of the project's default host. Team guard policy is frozen and hashed with the launch files. The process-level trust boundary remains the person's Unix account, as with existing agent spools.

The Plan run-mode control composes the existing stop, plan-update and resume
APIs; it waits for machine-confirmed stops, and stops helpers first for Prepare.
The plan API retains its revision and stop guards. In the mode-change transaction
it queues a versioned system continuation through the existing delivery outbox,
with vault redaction. Only these marked notices may wait for a stopped lead's
resume while its team remains active; ordinary stopped-session messages still
fail. A newer mode change expires the earlier queued notice. No mode or
permission change commits if the notice cannot be queued when delivery is on.

## Advisory permission labels

Hook ingestion classifies normalized structured actions with `risk_labels.py`.
It loads the trusted `acc-guard` beside the configured hook and uses its existing
shell parser, operand/wrapper handling and refusal rules with built-in rules on.
The session's frozen custom rules are included. A lock protects the standalone
module's run-directory context. No command is executed and no model is called.
Screen detection keeps its existing authority; screen prompts get Review rather
than parsing display text as executable commands.

Only label/reason metadata is stored on the attention item. Presentation checks
the caller's saved preference. `/api/attention?scope=pending&session_id=...`
includes unresolved acknowledged items under the normal ownership scope, so
Inbox acknowledgement cannot hide a session's pending prompt badge.
The classifier has no session-control or approval interface. See source design
`docs/RISK-LABELS.md` and decision `docs/decisions/0029-advisory-permission-risk-labels.md`.

## Diff explanations

**Explain these changes** is an explicit, budget-checked helper launch, not a
supervisor follow-up. The session API fetches the normal broker diff, applies
the source session's vault redactor, and hashes the bounded response. Cache
entries use `Observation` keys `explain:<source-session>:<revision-prefix>`;
they retain the full revision hash, helper id, provider, selected model,
time, status and validated steps. No migration is needed.

The helper is an ordinary `Session`/`LaunchSpec`/`SessionRun` under the requester's
local-workspaces project and agent account, even for a source on another machine.
The supervisor serializes reservations and reconciles completion, never recovery.
The broker permits only that helper's `<run>/<id>/workspace` as its special
working folder and artifact root, not the adjacent launch files or credentials.

The compiler supplies normal usage hooks and an `explanation_only` all-tools
deny policy. A bounded print-mode runner creates an isolated CLI home containing
only sign-in material and ACC hooks, with no inherited MCPs, skills, plugins,
project instructions, endpoint overrides or vault environment. It captures the
response in `.acc/artifacts/walkthrough.json`. The 180-second deadline,
256 KiB combined CLI-output cap and 64 KiB walkthrough cap are fixed. Stopping
the runner also terminates its native CLI process group. These are native
CLI permissions and hooks, not an OS-level egress firewall or a general sandbox.

ACC validates strict JSON types/counts/text lengths and drops references that
do not identify actual displayed old/new lines in the captured diff. The browser
renders plain text and creates its own local anchors. GET never launches a model.
POST requires source ownership (owners may use any), checks the requester's
budget and current local sign-ins, and refuses stale revisions. A failed run
is visible and requires an explicit regeneration.

## File browser and editor

The Files API (`/api/files/{kind}/{key}` plus read and download routes) authorizes the project or session before routing `files` operations to its host and agent account. Workspace keys are session ids, avoiding ambiguous folder names across accounts. accd never opens project files. The broker uses descriptor walks and bounded reads; atomic writes compare the opened revision before replacement. A private git index and git plumbing commit the editor bytes without including unrelated staged work or running repository filters and hooks. Personal git identities live in `setting` under `git.identity.<username>`.

See `docs/FILES.md` in the source for the design and [Files](/User-Guide/Files) for user-visible behavior.


### Remote preview transport

Project previews use the same origin and browser authorization boundary for local and remote apps. For a remote project, typed HTTP/WebSocket streams share its machine’s outbound authenticated link, below terminal/control priority. Credit controls each direction, with bounded frames, stream count and link queues. The node checks short-lived project reservation snapshots and literal loopback itself. Preview adds no node listener or general proxy. Roaming git backups separately use a temporary token-scoped loopback forwarder for one project; it is unreachable from other machines. Link replacement/disconnect cancels streams and clears the rebuildable reservation cache; requests are never replayed. Future multiple-main routing must select the live connection owner and fence stale owners before issuing snapshots. See `docs/PREVIEW.md` and `docs/MULTIHOST.md` in the repository for the wire contract and limits.

Broker reconnect retries are limited to explicitly listed read operations. A mutation
whose response is lost reports “delivery uncertain” and is never replayed automatically;
mixed read/write methods are conservative too. Inspect the session before trying again.

Broker JSON lines require an integer request id (0 through 2^53−1), a lowercase
method name up to 64 characters, and object params. Invalid envelopes receive a
protocol refusal and the stream remains usable. Terminal attach validates the same
request envelope before changing framing. Both ordinary and attach reply readers
require boolean status and typed success/error fields.

Codex app-server and Copilot ACP probes share a 256 KiB line reader. One monotonic
deadline covers the complete handshake and request sequence, including partial
lines and blocked writes. Cancelling a broker provider probe signals and joins its
worker. Probe teardown terminates, waits, then kills and reaps an unresponsive
child; pipe handles close before the worker returns.

Updater preferences remain in `data/updates-config.json`: the root update helper
must read the selected repository without opening accd's SQLite database. Only a
missing file uses first-run defaults. Unreadable JSON or invalid saved field types
block updater actions and report a configuration error, without logging file contents.
An explicit complete settings save repairs the file. Fetched release state uses
`Observation("updates.release")`; legacy `Setting("updates")` remains readable until
the next observation write moves it transactionally.

Advisory branch observations are cleared before and after each classification
under the same lock as guard context. Changing HEAD or switching workspaces cannot
reuse a branch observed by an earlier request.

### Backup recovery boundary

`backup_server.py` owns coordinated private capture, Ed25519 manifests, the local restic target adapter and bounded
offline restore. `backup_jobs.py` persists intent/result in observations and one leased worker handles scheduling.
`maintenance.py` supplies local process leases, shared by DB/migration, vault and git mutation paths; catalog uses
its existing restore lock. A separate capture/updater lease holds source identity stable through git staging and
manifest completion. Already-current restarts avoid the exclusive migration lease; pending migrations still use it.
These locks do not fence two mains. A restored tree's `RESTORE-HOLD` returns quarantine
HTTP 503 before ordinary application creation. See [Backups](/User-Guide/Backups) and [Operations](/Internal/Operations#offline-replacement-recovery).

The enrolled Linux backup backend uses a typed `backup-v1` channel on the existing machine link.
A root-owned binding permits one installation/target pair to reach only a fixed loopback append-only
rest-server receiver. Main-side restic encrypts everything before transport. Receiver credentials and
repository decryption keys are separate; only the former authorize ciphertext access. Owner-run root setup
and independent retention administration stay outside the link. Recovery can use administrator-exported
ciphertext with the offline kit, without the original main. See [Backups](/User-Guide/Backups).

### Machine availability

`machine_presence.py` persists node-link transitions and evaluates owner-selected Always-on/Workstation policy. WebSocket connect/disconnect writes presence; the supervisor reconciles persisted state after a restart and evaluates elapsed thresholds without probing offline links. Recorded outages survive a restart; otherwise a fresh observation window avoids counting the main ACC’s own downtime against nodes. Background remote work gates on `NodeHub` connection state. An Always-on outage creates an owner attention item, using the existing Inbox and push paths; reconnect resolves it with downtime. `POST /api/nodes/check` explicitly checks only live links over fresh, bounded broker channels, collecting versions and live readings through the optional `ping(machine_snapshot=True)` extension. Offline machines are never dialed. Checks and cached results are owner-only, audited, and limited to one request per 30 seconds per accd process.

### Manual active-passive authority

Migration 0044 adds the singleton `ha_authority` epoch, holder and process
incarnation. PostgreSQL advisory leadership and activity barriers serialize owner
handover. Application writes (including Core/raw SQL), API admission and broker
RPCs check current authority outside the caller's snapshot. A standby starts no
workers and rejects workload HTTP/WebSockets. Existing sockets are closed on
demotion and reauthenticate on the new main. HA mains refuse local brokers, so
persistent `host=local` records cannot execute on a replacement host. See the
[protocol and limits](/Internal/Manual-Failover), including mandatory independent
fencing for uncertain external effects and the absence of automatic failover.

Automatic evaluation is an off-by-default dry run without production telemetry
adapters or promotion wiring. It does not weaken the manual authority/fencing
boundary; see [the implemented evaluation slice](/Internal/Manual-Failover#automatic-evaluation-phase-3-adapters).

### Portable Publishing DNS

```text
Owner approval → accd → purpose-bound Publishing vault credential → fixed DNS adapter → provider API
                       ↕
                  ownership ledger (prior / desired / applied / request / pending or uncertain)

accd → fixed root Publishing helper → Caddy routes
accd → fixed root DNS helper → protected certificate generation → pinned Caddy DNS module
```

Provider release status is a single `qualification` value per provider (`qualified` or `preview`) in `acc.dns_credentials`. The owner-only `/api/dns/providers` response, credential metadata, saved-binding metadata and Publishing qualification gate use that value. Cloudflare is qualified; the other five adapters remain offered with preview badges. No binding migration or deletion occurs. Binary/per-consumer evidence and root-policy gates remain separate.

Only the selected Publishing credential is decrypted per job. DNS adapter errors are fixed categories; network
work occurs outside database transactions. Pending intent precedes writes. Azure and Route 53 candidate activation
requires live certificate and Publishing evidence; the known Azure TXT dependency blocker prevents qualification.
The deployment and trial procedure is in [Development](/Internal/Development#dns-provider-phase-2-qualification).

The S3 backup adapter runs native restic on the main, with fixed HTTPS endpoint/bucket/installation/target prefix,
explicit region and path lookup. It resolves only `s3` / `backup-writer` infrastructure credentials into private
job scratch. Storage sees ciphertext; repository keys stay main-side and in the owner’s kit. Bounded signed S3
requests observe versioning, COMPLIANCE Object Lock and object retention. A provider name is never evidence of
protection. Complete explicit key/version inventories and version-specific retention reads bracket full authenticated
readback and isolated restore. Changed inventories withhold protection; legacy unbound receipts do not qualify.
Only fresh verified captures with an exported repository-bound kit and at least 30 days remaining at completion
on every inventoried version qualify. This requires independently enforced denial of version deletion and retention
overrides, with no concurrent prune or version cleanup. Separate maintenance credentials and provider-account recovery
stay outside accd and its ordinary vault. The opt-in independent-host `acc-s3-maintain` CLI uses an explicit private
purpose-tagged identity, conservative all-version inventory, dry-run/explicit apply and monotonic exact-version
COMPLIANCE extension. It cannot delete or prune; retention-only extension may overlap capture. A bounded rate,
24-hour identity-bound plan, storage budget and fsynced intent/result journal support reviewed resumable work.
This job never changes the capture receipt. Host Backups projects runway from the last version-bound minimum
deadline and warns at seven days; fresh capture evidence remains mandatory. See the
[independent maintenance procedure](/User-Guide/Backups#independent-retention-maintenance).

### Node connection teardown

Before a node reconnects, its connection owner cancels and joins heartbeat, package and host reporters,
tool work, the priority sender, and live or draining broker-channel tasks. Relay task ownership lasts
through channel removal so asynchronous cancellation cleanup finishes before the connection returns.

The compiler's `session_environment` facade builds the complete provider environment (instructions,
configuration, conversation identity, MCP secrets and endpoint credentials). Launch and resume use the
same facade; provider option schemas remain the authority for accepted launch choices.
