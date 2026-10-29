# Telemetry implementation

The main ACC owns the telemetry queue. Nodes have no client, keys or direct collector transport.
Both owner choices start off. The owner runs the reference collector at `telemetry.rynhult.dev` as a pre-release staging service
(Frankfurt, since 2026-10-26). The client is not yet qualified end to end against it.

```text
Authenticated owner → Settings / preview / report review / deletion
                              ↓
Local counters + closed wire validators → immutable queue bytes
                              ↓ consent + schema + policy + test/CI gates
                       verified HTTPS, signed requests
                              ↓
Separate owner-hosted collector → SQLite raw data → anonymous monthly totals
```

## Client state and interfaces

`src/acc/server/telemetry.py` owns counters, redaction, key files, queue retention and the sender.
`src/acc/telemetry_schema.py` is the shared closed contract used by client and collector; its generated
schemas in `schemas/telemetry/` are checked against the validators in tests. No telemetry SDK is used.

Migration `0044` adds `telemetry_counter` (UTC day/event/provider aggregate counts, seven-day retention)
and `telemetry_item` (exact canonical UTF-8 payload, kind, key id, review/send state, retries, receipt and
send time). `Setting` records consent with actor/time/schema versions, collector address, key metadata,
one-time notice state and an optional force-off policy adapter. Private Ed25519 bytes are in 0600 files
under `data/secrets/telemetry/`; they rotate after 90 days. Usage and reports, and different collectors,
use unrelated keys. Old keys remain only for deletion and expire after at most 397 days.

Owner-only API:

| Endpoint | Purpose |
|---|---|
| `GET /api/telemetry?offset=0` | Consent, destination, 50 log/queue entries and total count |
| `PUT /api/telemetry` | Independent choices, collector URL, optional deletion on revocation |
| `GET /api/telemetry/preview` | Local exact next usage bytes and pending report bytes |
| `POST /api/telemetry/reports/{id}` | Send, discard, or delete the message while pending |
| `POST /api/telemetry/delete` | Queue signed erasure for every retained key at its collector |

Usage preview after opt-in and the minute-based supervisor sender use the same durable queue entry.
Report approval changes its state, not its bytes. The UI only allows removing the message before
approval. The local log retains successful payloads for 90 days after send, with paginated access.
Pending/failed entries expire after seven days. Audit events contain metadata, never payload content.
Collector changes record their old and new URLs in `telemetry.collector` audit events. Browser mutations
invalidate both state and payload-preview queries so a visible preview follows the new frozen queue.
`telemetry.effective_collector` tracks observed destinations; supervisor passes audit managed override
changes and removal as installation-configuration changes, including while sharing is off.
Migration `0045` discards all unsent pre-review reports and resolves their Inbox notices, without
modifying sent bytes or unrelated usage/deletion requests.

Counters bind fixed audit action names, selected successful route templates, scheduled launches,
team creation, guardrail events and actual backup attempts to schema enum members. They never retain
request bodies or filled-in paths. Inventory is bucketed and OS/provider/features are allowlisted.
Unimplemented product features are absent from the feature set. Checkouts report `dev`.

Captured report sources are unhandled server exceptions, isolated supervisor-stage exceptions,
failed main updates, broker launch failures and abnormal agent-process exits observed during reconciliation. The crash
path carries metadata only. This first implementation omits helper/log tails and does not invent
broker restart or installer signals where ACC has no reliable recorded trigger. Those broader
proposal sources require separate integration before being offered. Frames resolve inside the ACC
package only; locals and arguments are never read.

The scrubber inventories current/retained vault values, infrastructure credentials and their retained
values, TOTP and model endpoint keys, secret files (including JSON values), and known inventory names.
It strips controls, normalizes URL encodings, removes authorization/cookie headers, credential assignments and textual
base64 values, and redacts known values (including short credentials) and sensitive shapes before
trimming, then scrubs again. Multiline free text is omitted: neither removing nor inserting whitespace
at a wrap boundary can safely reconstruct arbitrary known secrets and names. Relative path text is
also omitted rather than guessing where a spaced/punctuated folder name begins.
Paths are removed conservatively through the end of the message, including
spaces and punctuation in home-directory names; IPv6 zone identifiers are removed with the address.
Failure to read/decrypt the inventory drops the report. Unknown names and unusual encodings remain a
best-effort limitation; the owner must approve every report. No transcripts or terminal content enter
this pipeline. No automatic-report mode is exposed.

## Configuration and transport

- Default address: the reserved owner-hosted collector; not deployed yet. Owners can choose their own HTTPS URL.
- `ACC_TELEMETRY_URL`: overrides the owner setting; empty disables outbound collection. Keep environment
  configuration outside agent-writable data. Changing the address discards pending payloads, never redirects them.
- `ACC_TELEMETRY_USAGE=off` and `ACC_TELEMETRY_REPORTS=off`: administrator force-off controls.
- `telemetry.policy` Setting: reserved integration adapter for force-off policy (`usage`/`reports`: `off`).
- Presence of `ACC_TEST` or `CI`: refuses every network send, including deletion. Pytest and browser harnesses
  set `ACC_TEST`; network tests remove the flag only after injecting a local mock transport.

HTTPS certificate verification, no redirects, ten-second timeout, no environment proxies or cookies.
Caps are 4 KiB usage and 16 KiB report. Non-successes use generic local outcomes and exponential backoff;
429 waits a day. A shared async lock serializes the sender and consent changes. Deletion is an explicit
owner action, allowed while sharing is off, signed by each retained key for its original collector;
only uncompressed, bounded string UUID receipts are accepted from responses. Requests advertise
identity encoding and reject encoded receipts before invoking a decompressor.

## Collector and qualification

SR10-4 narrows the shared `report/1` wire contract: `build` and `log` are not
accepted fields, including placeholder hashes and empty log lists. Client captures
omit them and the collector rejects them as extra properties. Previously queued
payloads containing either field fail client validation and are discarded without
sending. The legacy `remove_log` review flag remains accepted as a no-op; no log
field is added to a report. Session crashes remain metadata-only. Privacy notice
0.4 is unchanged; operators must deploy matching client and collector contracts.


`deploy/telemetry/` contains the ASGI factory and operator instructions; reusable code is
`src/acc/telemetry_collector.py`. It validates canonical payloads and Ed25519 signatures, binds signatures
to endpoint and timestamp, separates key kinds, enforces memory-only address limits and per-key limits,
deduplicates retries, applies retention hourly and on writes, and deletes raw data immediately on request.
Storage must be a private, service-owned directory and regular database file; symlinks and hard-linked
database files are refused. Only DELETE journal mode is supported, because WAL can retain earlier
payload pages after a successful SQL deletion. Idle retention logs a generic SQLite failure and retries
after 60 seconds; successful maintenance resumes the hourly interval.
The factory defaults to a private data subdirectory, but refuses to ignore an existing legacy
`./telemetry.sqlite` when no explicit `ACC_COLLECTOR_DB` is set. Secure/move that file and configure
its path rather than silently abandoning its retention/deletion state.
No report-reading/admin UI or public statistics endpoint exists. Proxy logging, backup erasure, operator
second-factor sign-in, disk alerts and the final controller contact must be verified before deployment.
See `docs/TELEMETRY-COLLECTOR.md` for the explicit VM installation, systemd/Caddy trust boundary,
resource limits, upgrades, backup/restore, erasure and incident procedures. The installer never starts
services. Caddy is the sole public listener and reaches a protected Unix socket; it replaces forwarding
headers and discards request-related logs. Authenticated keys have an independent memory-only hourly
limit across IPs/deletions. Monthly counts finalize after late arrivals close and stay immutable; the
local export omits cells below ten with no grand totals. No service was deployed or started by this task.
The collector re-scrubs report text by shape (`scrub_text`, mirroring the client rules) before storage,
refuses new data beyond `ACC_COLLECTOR_MAX_BYTES`, buckets IPv6 addresses per /64, and keeps a 45-day
erasure ledger (key and report ids only) that `telemetry_collector_ops erasures`/`restore` replay into
restored backups. Dependencies install hash-locked as wheels.

The tests use in-process collectors, injected
transports and a real local worker over a Unix socket; they do not qualify a live HTTPS service or its proxy/backups.

## Resource ownership

The SQLAlchemy flush listener holds a weak reference to the telemetry client. Replacing an application
or session factory therefore releases the old client and SQLite pool, including database, WAL and
shared-memory descriptors. Send requests and responses, key files and collector connections use scoped
contexts. The Linux regression repeats preview, scrubbing and signed sends to the reference collector
across client lifetimes and checks that `/proc/self/fd` stays flat after collection.

The merged migration chain is `0042 → 0043` (DNS ownership) `→ 0044` (telemetry) `→ 0045`
(privacy cleanup), with one head. Telemetry revisions were renumbered before integration with main.
