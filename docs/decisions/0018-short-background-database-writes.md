# Short background database writes

## Context

SQLite WAL readers cannot upgrade to writers while another writer holds the
reservation, or after that writer commits a newer snapshot. A busy timeout
cannot repair this upgrade. Transcript indexing read the session before deleting
its FTS rows; host-pressure polling, model-cache persistence and direct
automation cancellation had the same read/modify/write race. Waiting synchronously on the event loop can also
prevent the request holding the reservation from finishing its broker call.

## Decision

Run transcript persistence, model-cache persistence and automation cancellation
in joined worker threads, with write intent set before opening their transactions.
Host polling already uses a worker and now sets write intent too. Each transcript
commits separately, after its broker read; FTS replacement, completion marker, stamp and indexing time
remain atomic. Redaction uses the session and vault state read under that same
reservation. No transaction spans a broker await.

Reuse the existing backup worker's write-intent decorator from `server/db.py`.
Cancellation waits for the short worker transaction to finish before releasing
its caller's lifetime or supervisor lock, including when shutdown requests
cancellation repeatedly. If that draining worker fails, log its error and preserve
cancellation so the supervisor still shuts down. Otherwise errors propagate; no
operation is automatically replayed.

Application, authority-control and startup migration connections share SQLite
WAL and a 15-second busy timeout, installed on every new pooled connection before
requesting WAL. Migration connections retain foreign keys disabled for Alembic
batch table rebuilds; application and authority connections enable them.
Authority reads retain their independent transaction policy.

## Consequences

Competing writers serialize before reading mutable state, without blocking the
event loop. A writer exceeding the timeout still raises an error; history remains
due for a later supervisor pass rather than being marked indexed. Transactions
that await external effects remain deferred and are not automatically replayed.
PostgreSQL workers use the existing API write-intent/advisory-lock policy.

## Alternatives considered

- Increasing the timeout does not repair stale snapshots.
- Making every supervisor transaction immediate can deadlock broker-dependent
  requests on the event loop and holds reservations across external waits.
- Retrying only the FTS delete reuses a stale transaction. Retrying arbitrary
  stages can repeat external effects. Neither is safe.
- Ignoring lock errors can lose indexing progress and conceal operational errors.

## Follow-up: admit workers between application transactions

Review exposed the reverse race: an unmarked Node session read on the event loop
while the model-cache worker inserted an observation. The test's own update then
failed immediately with SQLite busy, despite the worker correctly taking
`BEGIN IMMEDIATE`. A controlled newer-commit reproduction reports extended code
517 (`SQLITE_BUSY_SNAPSHOT`). Reserving only the background writer does not
protect another session's deferred snapshot.

A per-database, process-local admission gate now excludes short worker transactions
from ordinary application transactions, across engines for the same resolved
file path. Ordinary transactions register automatically at engine BEGIN; callers
do not need a new marker. Worker decorators mark only synchronous bodies, and
workers wait on their threads until existing transactions finish. New transactions
may wait briefly behind an admitted database-only worker; no worker body may await
an event-loop action or perform broker I/O. Ordinary application transactions keep
their existing concurrent/deferred behavior and API write-intent policy.

Maintenance leases are acquired before admission. A worker waiting for capture
must not own admission: otherwise a new event-loop request can wait behind that
worker instead of immediately returning the existing retryable maintenance error.
A deterministic regression holds a real exclusive capture lease, queues a worker
and verifies that a new loop transaction still receives `MaintenanceBusy`.

The reservation lasts through DBAPI commit/rollback/close, rather than SQLAlchemy's
before-commit event. Failed BEGIN or maintenance entry releases it too. Admission
uses the connection's busy timeout and surfaces a SQLite-compatible busy error,
so optional GET refreshes keep using their cached response on contention.
No transaction or external effect is replayed. Independent processes and raw
SQLite connections remain outside this process-local gate and must use SQLite's
existing write-intent discipline. PostgreSQL keeps its existing policy.

Universal `BEGIN IMMEDIATE` was rejected because existing supervisor and request
transactions can span broker awaits. Retrying arbitrary transactions was rejected
because it can repeat external effects and cannot safely reconstruct arbitrary
ORM state. Marking only the failing fixture would conceal the production race.

## Follow-up: reconcile broker-list snapshot

Browser assertions passed while the captured server log exposed an ordinary
reconcile transaction failing at its last-output update. Unlike worker contention,
this was an ordinary API/external commit during the initial broker list await.
A deterministic test commits a competing session edit from the list response and
first reproduced the same failed UPDATE. Reconcile now closes the initial read
scope before broker listing and reloads each session in a separate scope after
listing. It preserves the concurrent edit and pane output without transaction
replay or holding a writer across broker IO. The worker gate remains necessary
for ordinary synchronous read/modify/write segments.

## Follow-up: synchronous API write threads

An additional browser-log failure at the post-broker changes update showed that
worker decorators alone did not cover FastAPI's synchronous write threads. These
already inherit middleware WRITE_INTENT but lack the decorator's marker. Engine
BEGIN now admits every off-loop write-intent transaction exclusively against
ordinary snapshots, including API threads and the existing backup status writer.
Read-only/default transactions retain ordinary admission. A plain thread with
only WRITE_INTENT first reproduced a commit across a live loop read snapshot;
the regression now verifies that it waits and both updates survive. Existing GET
cache/last-seen contention behavior remains covered without making all reads
exclusive.

## Follow-up: lazy broker parameters

A placement-backed team session still reopened a read snapshot after commit:
evaluating `session.cwd` (and automation's `session.host`) lazily loaded placement
while constructing the broker call arguments. A competing commit during that
await invalidated the later changes update. Placement-backed deterministic
workspace/automation cases both failed before the fix. Resolve broker parameters
before committing, then await using plain values; this closes the lazy-read
snapshot too. No operation is replayed.
