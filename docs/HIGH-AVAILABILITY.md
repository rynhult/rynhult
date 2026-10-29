# Manual active-passive failover

The [owner runbook](wiki/Internal/Manual-Failover.md) covers standby setup,
health, explicit promotion/demotion, independent crash fencing, node reconnects
and returning to the original main. See the [Guide](wiki/User-Guide/High-Availability.md)
for the status screen. No automatic failover is implemented.

## Automatic failover

### Boundary and split-brain argument

The owner targets recovery within minutes and accepts losing approximately five
minutes of recent sessions. These are design targets, not measured guarantees.
The organizations plan's phase 5 remains separate: this work does not provide
concurrent dashboards, federated elections or autonomous organization sites.

Two mains cannot distinguish a dead primary from a network partition. Heartbeat
silence alone is never permission to promote. Availability must be sacrificed on
the side that cannot reach an independent authority. The minimal proposed authority
is one durable, linearizable witness in the PostgreSQL control plane, independent
of both main processes. Both mains must reach the **same** witness and database
history. A separately enrolled always-on machine could host that witness instead,
but ordinary enrollment/heartbeat does not make a machine a quorum service. It
would need authenticated role-limited RPC, durable CAS storage and recovery rules.
There is no peer-majority fallback when the witness is unreachable.

The witness serializes a singleton `(epoch, holder, boot incarnation, expiry)`.
Grant/renew operations compare the expected epoch; only the current incarnation
can renew an unexpired lease. On expiry every acquisition increments the epoch,
even for the same machine. A restarted main is always standby, with no recovered
lease. Witness restart must preserve epochs and deny new grants for a full maximum
lease interval unless its expiry clock continuity is proven. Restoring an old
witness snapshot requires independent fencing of every former holder and an epoch
recovery procedure; never reset the epoch. A PostgreSQL-backed witness is not a
solution for PostgreSQL split brain: database promotion must itself have quorum,
unique-writer fencing and no rollback of the witness/authority history. Treat the
witness and database as one failure domain, not two independent votes.

### Lease safety and old-primary fencing

Use monotonic elapsed time, not synchronized wall clocks. At request **send** the
client records a local monotonic start time. A grant carries an authenticated
duration, epoch, incarnation and request identity, not a remote wall-clock expiry.
The primary's usable deadline is conservatively:

`send_time + duration * (1 - drift) / (1 + drift) - heartbeat_interval`.

Both clocks' fractional rate error must be bounded by `drift` (model maximum 1%).
Clock offsets do not matter. Network and durable-commit delays consume this
budget; a response arriving after the deadline is unusable. Suspension, clock
regression or an unprovable clock-rate bound must invalidate authority. The model
assumes clocks advance during elapsed real time; it does not establish that a
platform's suspend behavior meets this assumption.

Before that deadline the old primary must stop admitting writes and dispatch,
close streams, drain admitted work and refuse further SQL. Witness expiry occurs
after its conservative local deadline; only then can the standby acquire a new
epoch. The real adapter would have to atomically coordinate that epoch with
`ha_authority`, retain the existing leadership/activity locks, and perform the
existing fresh-epoch checks on every write. A lease does not replace #225 fencing.
A paused process can resume after a check, and an external helper may already have
a command. Therefore a future automatic promotion also requires verified external
fencing: power isolation or receiver-enforced epoch/lease checks plus durable
command deduplication/drain. A boolean in the pure model is a **required input**,
not evidence of fencing. The existing manual API's `old_main_fenced` owner
attestation is not an automatic fencing adapter. Never map heartbeat silence to it.
Uncertain effects must block automatic takeover.

### Promotion gates and settings

All gates must hold together: authenticated, sequenced primary heartbeats missed
for **longer than** timeout; recent, known replay-lag measurement at or below the
limit; fresh candidate/incarnation/epoch grant; verified external fencing; no
uncertain effects; continuous readiness and elapsed cooldown. Any missing,
nonfinite, future-dated or stale observation fails closed. Slow disks, missing
witness replies and database errors reduce availability. Replay lag must use an
actual replicated position and freshness proof, including idle databases; a
missing timestamp or idle `last replay` time cannot be interpreted as zero lag.
Time lag alone cannot prove that security revocations or committed transactions
survived an asynchronous database failover. Require matching timeline/history and
reconcile security state before reopening access after data loss.

Pure `Policy` bounds (seconds except fractional drift):

| Field | Default | Bounds / constraint |
|---|---:|---|
| heartbeat | 5 | 1–30 |
| timeout | 120 | max(3 × heartbeat, lease + 2 × heartbeat) to 900 |
| lease | 60 | 30–300; at least 3 × heartbeat to leave renewal headroom |
| drift | 0.01 | 0–0.01 |
| max_lag | 300 | 0–300; lowering reduces accepted loss |
| sample_max_age | 10 | 1–30 |
| stabilization | 30 | 30–600 continuous seconds |
| cooldown | 300 | 300–3600 after transition |

A future owner configuration API must validate these together; the pure model
accepts them today, but production exposes only the deployment-owner dry-run
switch `ACC_HA_AUTO_EVALUATE=1` (default off). Reset continuous readiness on a
heartbeat recovery, unhealthy lag, witness/DB loss or changed incarnation/epoch.
Persist transition/cooldown history across restarts; require a fresh stabilization
window after restart. The pure readiness state machine also resets on monitor
gaps longer than sample_max_age, so a stalled event loop cannot count unobserved
time as continuous readiness. Do not automatically fail back to the preferred machine.

### Phase 3 observation adapters (still dry-run)

`ha_automatic.py` remains the pure protocol/evaluator. The adjacent adapter modules
provide a durable witness, SQL heartbeat channel and collector, conservative WAL
replay measurements, and a read-only external-fence transport interface.
`PostgresDryRunAdapter.status()` feeds these inputs through the existing `advance`
state machine. It returns input ages, lag, witness epoch and fence-verification
state. Even when every model gate is ready, status says `would_promote: false`,
`promotion_wired: false`, `gates_ready: true`, and `reason: promotion_unwired`.
No adapter accepts an authority-promotion callback or runs a fencing command.
The monitor samples its decision clock after every potentially blocking reader,
including transition history, then rechecks lag, lease and fence freshness. Invalid
or nonfinite adapter numbers produce a fixed JSON-safe refusal without projecting
the invalid inputs. Malformed witness durations/epochs cannot escape as a status
exception. A clock regression during either WAL query invalidates that reader for
its lifetime, even if the clock later recovers.

The adapter status API is separate from the installed HTTP runtime. The existing
`Runtime.automatic_status()` still evaluates missing telemetry and the public health
projection remains unchanged; neither calls the new factory. Connecting the owner
status route and scheduling collection requires a runtime integration change.
Until that connection exists, setting the new placement variables alone does not
start an observer, publish pulses or change the screen's blocking reason. No
telemetry submission endpoint, daemon, timer or production network witness is added.

#### Durable witness and placement

`ha_witness.DurableWitness` owns one private local SQLite database and a lifetime
no-follow file lock. Transactions serialize expected-epoch acquisition/renewal and
commit with `synchronous=FULL` before returning a reply. The client records its own
monotonic request-start, checks holder, request identity, epoch and duration, and
rejects a reply that used up its conservative lease budget. Corrupt, locked or
unwritable storage cannot produce a grant. Closing the service joins an in-flight
transition before releasing its lock and refuses later requests.

On restart the stored epoch is retained, but grants are quarantined for **300
seconds**, the maximum lease permitted by any previous policy. Persisted monotonic
expiry values do not prove clock continuity. Two service instances cannot open
the same state simultaneously. Do not restore an old witness file, reset its epoch
or share it through NFS/SMB. Independent fencing and a recovery procedure are
required if witness history may have been lost or rolled back. The entire parent chain must be
owned by root or the witness service account and refuse group/other writes; a
root-owned sticky temporary ancestor is allowed. Protecting only the leaf would
let another account rename/replace it from a writable ancestor, bypassing the
path-based service lock.

`WitnessTransport` is the role-limited request/reply interface. The concrete
`LocalWitnessTransport` binds one holder/incarnation inside a trusted service;
it is not a network authentication service. A remote implementation must provide
bounded authenticated RPC, incarnation binding and the same durable commit/CAS
semantics. Ordinary node enrollment is not witness authority. Placement remains
an owner decision, with these factory choices (both `ACC_` and `RYNHULT_` spellings
are supported; the latter wins):

| Setting | Default / behavior |
|---|---|
| `ACC_HA_WITNESS_PLACEMENT` | `refuse`: no implicit witness or peer-majority fallback |
| placement `local-file` | Explicit local lab placement; needs `ACC_HA_WITNESS_PATH` |
| `ACC_HA_WITNESS_PATH` | No default; absolute file in a pre-created private service-owned directory |
| placement `external` | Requires an explicitly injected `WitnessTransport`; otherwise unavailable |
| `ACC_HA_LAG_STANDBY_DATABASE_URL` | Optional separate replica URL; omission gives unknown lag |

The local-file choice exercises persistence; it does **not** resolve the independent
witness placement question. Two separate local files are not one shared witness.
The factory refuses unsafe placement, invalid paths and replica URLs without
verified TLS (`postgresql+psycopg`, `sslmode=verify-full`). It never returns the URL,
credentials, path or raw exception text in status. Construct and retain one adapter
per service lifetime, then call `close()` after collection stops.

#### Heartbeats and PostgreSQL replay measurements

`PostgresHeartbeatChannel.provision(primary_engine)` is an explicit isolated-lab
setup operation, not an application migration or startup repair. It creates the
adapter-owned `acc_ha_dry_heartbeat` telemetry table. The active main's adapter
publishes increasing sequences only inside the existing manual-authority activity
barrier, at most once per heartbeat interval. A standby adapter never publishes.
Provision separate least-privilege SQL writer/reader roles over verified TLS;
write authority to this table is a trusted service capability, not a member API.

The collector binds to the durable manual holder, incarnation and epoch. Re-reading
the same sequence never refreshes receipt time. A newly started SQL reader needs
a baseline and a subsequent advance before it can assert any heartbeat receipt.
Changed epochs/incarnations reset collection and readiness. Without an initial
valid heartbeat, missing telemetry remains unknown rather than a dead-primary claim.
Collection is call-driven: a lab must regularly call the active and passive
adapters' `status()` methods. No scheduler is installed by this slice.

`PostgresWalReader` queries system identifier, checkpoint timeline, recovery role
and flushed/replayed LSN on the primary and replica, using bounded connections and
statements. The SQL account needs permission for `pg_control_system()` and
`pg_control_checkpoint()`; see the PostgreSQL 17
[WAL/recovery functions](https://www.postgresql.org/docs/17/functions-admin.html)
and [control-data functions](https://www.postgresql.org/docs/17/functions-info.html).
`PostgresLag` records local monotonic time anchors for
primary WAL positions and advances the confirmed anchor only when the replica has
replayed that position. Fresh equal positions prove idle catch-up, avoiding the
incorrect interpretation of an old last-replay timestamp as lag. A replica behind
without a confirmed time anchor is **unknown**, never zero. Otherwise the reported
seconds conservatively bound age since a known-replayed primary sample. Query and
delivery time consume both age and lag budgets.

Pending anchors are bounded. Unknown reads discard freshness evidence but preserve
position high-water marks. History changes, WAL regression (including after a
partition) or local clock regression remain blocked for that reader lifetime;
recreate it only after investigating database history. A new monitor always waits
a full cooldown, then requires continuous readiness. Durable manual epoch changes
also restart that wait, so restart cannot shorten cooldown by forgetting a recent
transition. The witness epoch and manual-authority epoch remain separate and are
never written into each other.

#### Independent external fence interface

`ExternalFence` only probes an injected authenticated `FenceTransport`. A positive
response must bind the old control-host incarnation, manual epoch and a fresh
random challenge, arrive within the freshness budget, and certify isolation of
the entire host and its privileged/external helpers. It is not an owner's
`old_main_fenced` boolean or the old host's self-report. No transport means
unverified; unavailable, stale or substituted proof resets model readiness.
No power isolation, privileged helper, shell command or vendor fence API runs.
Even an injected successful lab proof cannot enable promotion.

For isolated qualification, create adapters with a disposable authority database,
an explicitly selected witness and injected probe transports, repeatedly collect
status, and close all resources. Do not point qualification at a live instance.
Tests exercise real SQLite persistence/channel transactions and deterministic
partition, stale/replayed heartbeat, lag, fence, clock and restart schedules.
The existing throwaway PostgreSQL harness is used when it can create its own
container; a skipped harness does not qualify a real replication pair or a fence.

### Installed status and audit boundary

The existing owner `/api/ha/status` reads still audit their model-only evaluation
state as `ha.automatic_evaluation`; public health reads do not write audit entries.
Runtime integration must keep input ages out of the audit deduplication key (they
change every poll), preserve fail-closed audit writes, and close adapter resources
without abandoning an in-flight collection. No automatic promotion, routing,
background alert delivery or measured RTO/RPO is added here. Real PostgreSQL
pair/fencing and failure-injection qualification remain release gates.

### Non-goals and qualification

This does not protect against a malicious/compromised main or witness, Byzantine
clocks, loss/rollback of witness state, two writable PostgreSQL timelines, shared
storage corruption, simultaneous site loss, lost secrets/files, or commands
already accepted by brokers/helpers. It does not replicate tmux processes,
workspaces, transcripts, file bundles, secrets or routes; move the HTTPS front door;
repair a database; implement session migration; or guarantee RPO/RTO. Lag gating
cannot turn the owner's approximate five-minute tolerance into a data-loss SLA.
A witness outage deliberately prevents failover. Qualify database contention,
crash/restart, suspension, fencing of external work, restore and real routing in
an isolated operational drill before wiring any promotion.
