# Manual active-passive failover

Phase 2 adds an explicit owner handover between two matching ACC mains sharing
PostgreSQL. It does not elect a leader, replicate PostgreSQL or files, move DNS,
configure Caddy, or migrate executing sessions. SQLite remains single-main.
Security review and a disposable PostgreSQL qualification run are required before deployment.

## Prepare the standby

1. Stop the original main before enabling this mode. Install the same trusted ACC
   build on both mains. Provision the PostgreSQL schema with that release's normal
   migration path. Back up PostgreSQL independently; ACC's SQLite captures and
   automatic main updater still do not support PostgreSQL recovery.
2. Configure the same protected `ACC_DATABASE_URL` on both, using verified TLS
   (`sslmode=verify-full`, a trusted root and the database's certificate hostname).
   Connect directly to PostgreSQL or use a session-affine pool. Transaction- or
   statement-pooling proxies are unsupported: authority depends on retained
   database-session locks.
   Set **different stable** `ACC_HA_NODE_ID` values, for example `main-a` and
   `main-b`. Names contain lowercase letters, digits and hyphens, up to 40 characters.
   Once initialized for HA, this database refuses a main without `ACC_HA_NODE_ID`.
   All mains must use this release; an older executable does not implement fencing.
3. Securely provision matching vault keys, API/bootstrap credentials, VAPID identity,
   catalog overlay, git repositories and the other non-database files listed in the
   approved state inventory in `docs/research/high-availability.md`. Compare their versions
   before every handover. File readiness is an operator check, not a replication
   service or an automatic readiness claim. Never merge live mutable directories
   bidirectionally or copy run folders containing session secrets.
4. Use dedicated enrolled execution machines. **HA mains refuse local broker calls**,
   including terminal streams. Preserve old `host=local` session records; do not
   change them to the replacement main. Deliberately move execution ownership to a
   stable enrolled machine before adopting those workloads. Failover does not
   relocate processes, native transcripts, dirty worktrees or project containers.
5. Provision a stable HTTPS front door (for example `https://acc.example.lan`) and
   a separate private owner path to each main (for example
   `https://main-a.example.lan` and `https://main-b.example.lan`). Preserve hostname
   verification, certificate trust, cookies and configured Origins. ACC does not
   create these routes or change Caddy on promotion. Published application routes
   must still point at the right execution host, not the standby's localhost.
6. Keep root helper automation on the passive host inactive. Inventory pending
   publishing/account/package/update requests on the original host: database
   ownership cannot revoke a file already handed to a root helper. Resolve or
   independently fence uncertain external operations before moving authority.
7. Start the applications. **Every start is standby**, even if the durable database
   holder has the same node name. Standby starts no supervisor, updater or backup
   workers, accepts no node/terminal sockets, performs no workload API operations
   and does not refresh login last-seen timestamps. A start whose build needs a
   schema migration **refuses while any main holds authority** (live or recorded
   after a crash) and holds the leadership lock while it migrates. To upgrade:
   demote the active main, start the new build (it migrates), then promote a main
   running that build. After a crash, finish fenced recovery with the old build
   first. A database initialized for HA refuses a main without `ACC_HA_NODE_ID`
   before migrating anything.

Open either main to see the **High availability** status screen. An active dashboard
links to it through **Active main · epoch …**. Standby serves observation only, so
workload pages are deliberately unavailable rather than shown as live cached data.
Existing shared login sessions can read owner status; sign-in/setup are active-only.
Use the owner API token for initial promotion and private direct-host administration.

## Observe health and authority

`GET /api/health` is a minimal unauthenticated probe. In HA mode `ha` contains
`role`, `epoch`, `database`, `uncertain` and `automatic_failover: false`. A healthy standby is
**not ready for control**: route workload traffic only to `role=active` with
`database=ok`. A failed database is reported as unavailable, never as ready.
The browser refreshes this status every five seconds; the server checks authority
for actions independently of that display.

`GET /api/ha/status` requires an owner (normal cookie/CSRF rules, or the owner bearer
token). It also reports this main's node name and `active_node`, the durable holder.
Owner status and public health include the current holder's local uncertainty latch even if the database could
not persist it. A failed uncertainty write cannot make that process report a clean handover as safe; independently
fenced recovery is still required. A later epoch does not inherit the old process's local latch.

The `uncertain` flag records a mutating broker command without a trusted reply,
or a canceled publish/tool execution. It survives process restart. The active
main keeps serving (one node's lost reply must not stop every machine), but clean
demotion refuses while this flag is set; only independently fenced recovery
promotion can clear it. To hand over, resolve the external work, stop and fence
the current main, then crash-promote the other. A holder can also remain recorded after a crash while both processes
show standby.
That is evidence to investigate, not permission to auto-promote.

For these examples, use a protected shell with `ACC_OWNER_TOKEN` supplied from the
owner's secret store; do not paste tokens into a URL, command history, screenshots
or support output. Responses contain no database URL or database credentials.

```sh
curl --fail-with-body https://main-a.example.lan/api/ha/status \
  -H "Authorization: Bearer $ACC_OWNER_TOKEN"
```

## Promote the first main

Read the current epoch; an unused database begins at `0`. Then explicitly promote:

```sh
curl --fail-with-body https://main-a.example.lan/api/ha/promote \
  -H "Authorization: Bearer $ACC_OWNER_TOKEN" -H 'Content-Type: application/json' \
  --data '{"expected_epoch":0}'
```

The new epoch is `1`. If the main cannot start its workers after the epoch change,
the request returns `503` and hands authority back by clean demotion (epoch `2`,
no holder); read status and retry. Confirm `role=active`, check enrolled machines and only then
route the stable front door to this main. No automatic promotion occurs after a
restart, a missed heartbeat, a database disconnect or a node reconnect.

## Planned handover to the standby

1. Pause new owner actions. Check that both builds, secrets and versioned files
   match, and that no unresolved external helper operation remains.
2. Read the active main's epoch. Demote **that process**:

   ```sh
   curl --fail-with-body https://main-a.example.lan/api/ha/demote \
     -H "Authorization: Bearer $ACC_OWNER_TOKEN" -H 'Content-Type: application/json' \
     --data '{"expected_epoch":1}'
   ```

   It stops admitting actions, closes node/terminal sockets, cancels and drains
   workers, then clears the durable holder and advances the epoch to `2`.
   HTTP `409` with draining means an admitted action/transaction is still finishing;
   inspect it, then retry demotion with the same epoch. A `409` saying the command
   outcome is uncertain is different: resolve the external operation and use the
   crash-fencing procedure below. Canceling a task is not evidence that its broker
   command or helper stopped. Do not bypass either refusal.
3. Read both statuses. Promote `main-b` with `{"expected_epoch":2}`. It becomes
   epoch `3`. A stale expected epoch returns `409`; read status before deciding
   whether to retry. Promotion refuses a running main holding leadership and
   refuses outstanding activity even when the leadership lock is free.
4. Move the stable front door only after confirming the new main is active. Inspect
   reconnected machines and existing sessions before resuming owner actions.

Promotion and demotion record the acting owner, main and resulting epoch in Audit.
No force option overrides another main's leadership or in-flight work locks.

## Crash recovery and fencing

If the original process cannot demote, independently stop or isolate **the entire
old control host and its privileged helpers** from execution nodes, DNS/publishing,
files and PostgreSQL. Resolve or independently fence any affected execution-node
command whose outcome is unknown as well. Confirm that no old control or command
owner can resume the uncertain work. Resolve any operation
whose outcome was uncertain; do not blindly replay it. Loss of a PostgreSQL socket
alone does not prove that an already dispatched external action has stopped.

After that independent fencing, inspect status and send promotion to the standby
with the observed epoch and `"old_main_fenced":true`. This is an explicit owner
attestation, not an ACC power-fencing mechanism. It does not bypass a live database
leadership lock or activity lock. Never set it just because a heartbeat is late.
If database locks remain, keep the old host fenced and investigate its connections;
do not terminate arbitrary database sessions to make promotion pass. HA connections
set TCP keepalives and `tcp_user_timeout`, so PostgreSQL drops a partitioned
main's sessions (and their locks) in about half a minute. A pooler or proxy in
between must not hold them open on its own behalf.

The database epoch/incarnation and activity barriers reject stale SQL writers and
new broker dispatch. Loss of either retained authority connection fails closed;
a one-second monitor also stops workers and sockets. Recovery still depends on
independent fencing of external work already beyond the database boundary. This
phase makes no automatic or partition-tolerant failover promise.

## Node reconnection

The preferred arrangement is the stable verified HTTPS front door. Nodes reconnect
with their existing enrollment key and ordinary backoff after a handover; sockets
and terminal channels are not replicated. Remote native processes keep running.

Alternatively, an owner can pre-provision approved alternate main URLs in each
node's protected environment file (the same CA trust must verify every hostname):

```sh
ACC_NODE_PRIMARY=https://main-a.example.lan
ACC_NODE_ALTERNATE_PRIMARIES="https://main-b.example.lan"
```

Restart the node using the normal owner-managed service procedure after editing.
It tries these URLs in order on disconnect and cycles until the active main accepts
its signed hello. A standby rejects the connection. Enrollment does not discover
alternates, and nodes never trust redirects, an advertised CA or an unapproved URL.
Bundle downloads, git pushes and root-owned update configuration still need the
stable endpoint; rotating node control URLs does not rewrite those settings.

## Return to the original

Repair/reinstall the original with the matching trusted build, provision current
files/secrets and start it as standby. Do not restore an older shared database or
let old helper requests run. Confirm the current active main's status and workload
health. Repeat the planned handover in the other direction: demote `main-b` at its
observed epoch, then promote `main-a` at the next epoch and move the front door.
Never decrement or reset the epoch, and never copy an old authority row over it.

## Qualification

Run `PYTHONPATH=src python -m pytest -q tests/test_ha.py tests/test_node.py` with the
project's PostgreSQL extra and an authorized disposable Docker/Podman runner. The
fixture uses only randomly named, test-created PostgreSQL containers and removes
them afterward; it never accepts an existing database URL. SQLite-backed app wiring
tests are useful but do not qualify PostgreSQL lock, connection-loss or contention
behavior. Run the split-brain and reconnection matrix without skips before deployment,
then perform an isolated operational drill with real edge routing and independently
fenced helper processes. Keep the planned security review as a release gate.


## Automatic evaluation (phase 3 adapters)

`ACC_HA_AUTO_EVALUATE=1` remains a **dry-run** setting, default off. There is no
automatic promotion. The installed status route still shows **Would promote now:
No** and missing telemetry: its runtime connection to the new adapters is not
part of this adapter layer. Setting placement variables alone does not start any
collector or change the existing HTTP projection.

The new internal `PostgresDryRunAdapter.status()` API reads actual adapter inputs
and feeds the existing fail-closed evaluator. Its response includes heartbeat age,
conservative replay-lag seconds and sample age, witness grant epoch and external
fence verification. A fully ready model reports `gates_ready: true` and
`reason: promotion_unwired`, while `would_promote` and `promotion_wired` remain
false. The adapter never calls the manual promotion API or executes a fence.

### Isolated lab configuration

Prepare only disposable infrastructure; never use agent tools to run this against
a live instance. These are protected service settings consumed by the adapter
factory, not browser settings (the equivalent `RYNHULT_` names take precedence):

| Setting | Choice |
|---|---|
| `ACC_HA_WITNESS_PLACEMENT` | Default `refuse`; explicitly choose `local-file` or an injected `external` transport |
| `ACC_HA_WITNESS_PATH` | For `local-file`, an absolute file in a pre-created private service-owned directory; no default |
| `ACC_HA_LAG_STANDBY_DATABASE_URL` | Separate read-only PostgreSQL replica connection, verified TLS; missing means unknown lag |

A local witness is a persistence test, not independent quorum. Do not configure
two unrelated witness files as if they were one authority or put SQLite on a
network share. Where an independent witness runs remains the owner's decision.
External placement requires a bounded, authenticated transport implementation;
there is no deployed network service in this slice.

The witness commits epoch CAS before granting a lease. Restart preserves the epoch
and refuses grants for the full 300-second maximum old lease. Corruption, lock
contention, symlinks and unsafe file permissions refuse storage. Never recover by
resetting the epoch or substituting an older file.

Explicitly provision the lab's adapter-owned heartbeat table using
`PostgresHeartbeatChannel.provision(primary_engine)` before collecting. This is
not a schema migration or an automatic startup action. Give the active service a
least-privilege writer and observers SELECT access over verified TLS. Pulse writes
use the existing manual-authority activity barrier. Call the active and standby
adapters regularly: this slice adds no scheduler. A baseline and a subsequent
advancing sequence are required; reading an unchanged or old-incarnation row does
not refresh liveness.

The WAL reader compares PostgreSQL system/history and flushed/replayed positions.
Idle catch-up can be proven without relying on the last-replay timestamp. A behind
replica without an earlier replayed time anchor is unknown. Position/history
regression blocks the reader until investigated and recreated; database loss is
not treated as zero lag. After restart or a manual epoch change, wait a full
cooldown and fresh continuous-readiness period.

The external-fence interface only reads a challenge-bound, fresh proof from a
trusted independent controller. No default proof source exists. Silence, a
member's input or an owner's manual checkbox cannot replace it. Even a successful
injected lab proof leaves promotion unwired.

Retain one factory-created adapter per service lifetime and call `close()` after
stopping collection. Its status never includes connection URLs, credentials,
witness paths or raw errors. A future runtime bridge must preserve owner-only
diagnostics, avoid auditing changing sample ages on every poll, and join in-flight
collection before closing resources. Public health and manual operations retain
their existing behavior.

Run only the HA tests for this slice; use the existing throwaway PostgreSQL harness
when available and record skips. SQLite and simulated WAL probes do not qualify a
PostgreSQL replication pair, an external fence or recovery time. See repository
`docs/HIGH-AVAILABILITY.md`, **Phase 3 observation adapters**, for the interfaces,
clock assumptions, bounds and remaining runtime/qualification boundary.

### Observation refusal after independent review

The internal dry-run monitor evaluates freshness **after** reading transition history,
so a delayed history read cannot preserve an expired lease or stale lag/fence proof.
Invalid adapter numbers are omitted from a fixed JSON-safe refusal; exception bodies
and credentials never become status fields. A clock regression during a WAL query
keeps that reader unavailable until it is deliberately recreated after investigation.

A local witness needs a protected parent chain as well as private files. Every ancestor
must be owned by root or the witness service account and refuse group/other writes;
a root-owned sticky temporary ancestor is allowed. A writable shared ancestor is
refused because another account could replace the directory containing the lock.
These checks do not enable promotion or complete the installed runtime connection.
