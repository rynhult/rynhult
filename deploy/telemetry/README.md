# Standalone telemetry collector

Deployment and operations: [docs/TELEMETRY-COLLECTOR.md](../../docs/TELEMETRY-COLLECTOR.md).
The systemd installer, dependency pins, logging configuration and Caddyfile in this folder target
a dedicated VM; nothing is deployed by this change.

This is source for a small owner-hosted collector. **Nothing is deployed by this change.**
The default ACC client address is reserved, but is not running yet. No third-party service is involved.
The ASGI factory is here; its reusable implementation and closed wire contract are
`src/acc/telemetry_collector.py` and `src/acc/telemetry_schema.py`. Generated JSON schemas are in
`schemas/telemetry/`; tests require them to match the shared validators.

Use an ACC Python environment with the project's declared dependencies. From the repository root:

```sh
install -d -m 0700 ./telemetry-data
PYTHONPATH=src ACC_COLLECTOR_DB=./telemetry-data/telemetry.sqlite \
  uvicorn --factory deploy.telemetry.app:create_app --host 127.0.0.1 --port 8000 --no-access-log
```

The port is an example for a separate, self-hosted installation. On an ACC-managed machine, reserve the
port and arrange deployment through the owner. This task does not authorize starting it.

Put it behind verified HTTPS. Disable access logs at **both** the ASGI server and reverse proxy,
including error logs that include request addresses, bodies, or headers. Restrict database/filesystem
access to the collector service account, with a restrictive umask. Do not expose SQLite, a database
browser, or a reporting UI publicly. Administrative access belongs behind the operator's own sign-in
with a second factor; no admin interface is shipped here. Do not feed report text into an agent as
instructions or render it as HTML. It is untrusted input.

The data directory must already belong to the service account with no group/world access (0700);
an existing database must be a regular, singly linked, service-owned private file (0600).
Permissive files/directories and symlinks are refused rather than silently accepted or chmodded.
Use a dedicated directory, not the source checkout. If the former default `./telemetry.sqlite` exists
and `ACC_COLLECTOR_DB` is unset, startup refuses to
silently create a replacement database. Secure/move the existing data and explicitly set its path.
Only SQLite **DELETE** journal mode is supported; inherited WAL or other journal modes are refused,
because older WAL frames can retain deleted reports.
Stop all collectors and readers before migrating an existing database out of WAL, and account for
its old journal files and backups as sensitive data. Do not change journal mode while the collector runs.

Endpoints:

- `POST /v1/usage`: at most 4 KiB, the closed `usage/1` contract.
- `POST /v1/report`: at most 16 KiB, the closed `report/1` contract; session crashes prohibit text and frames.
- `POST /v1/delete`: erase a key's raw data, or named reports belonging to that key; returns a UUID receipt.
- `GET /v1/schemas`: accepted wire contracts. There is no public statistics or report-reading endpoint.

Requests carry an Ed25519 public key (`X-ACC-Key`, standard base64), Unix timestamp (`X-ACC-Time`),
and signature (`X-ACC-Signature`, standard base64). The signed bytes are:

```text
<timestamp>\n<endpoint path>\n<canonical UTF-8 JSON body>
```

The timestamp window is five minutes. The installation id is base64url of the first 16 bytes of
SHA-256(public key), without padding. Usage and reports must use different keys. Bodies must equal
`encode()` of the validated model, so duplicate keys, coercions and extra properties are rejected.
Signatures bind the endpoint as well as the body. They prove continuity, not a genuine ACC installation.
Retries are idempotent by installation/day for usage and report UUID for reports.
Return deletion receipts as uncompressed JSON with a string UUID in `receipt`, at most 1,024 bytes.
The client advertises identity encoding and rejects compressed or malformed receipts.

Per key: at most two retained usage summaries and ten reports in a rolling day. Per connection address:
60 requests/hour and 20 new keys/day. Connection addresses exist in bounded process memory only, for the current hour or registration day.
Authenticated keys also have a bounded 60-request/hour limit across all endpoints and IPs, independent
of database deletion. Multiple workers need a shared edge rate limiter; run exactly one worker.

Retention runs at startup, hourly while the ASGI lifespan is active, and on ingestion/deletion:
raw usage 90 days after receipt, reports 180 days, anonymous monthly totals 13 calendar months.
Complete months are aggregated into distinct installation counts per OS, provider and feature;
no installation ids survive in the monthly table. Monthly totals finalize after the seven-day late-arrival
window, then remain frozen. The local export command omits every cell below ten installations and supplies
no grand totals. Anonymous totals cannot be traced back for individual deletion. SQLite uses
secure deletion on write connections. Operate encrypted storage if filesystem recovery is a concern.
Transient SQLite maintenance failures emit a generic error without database details and retry after
60 seconds; monitor that error. Run with ASGI lifespan enabled. SQL deletion does not erase backups,
snapshots, or recoverable filesystem blocks, so secure deletion is not a physical-media erasure guarantee.

Backups are an operator responsibility: keep them for at most 30 days and ensure a deletion is reflected
in restored backups before serving again. Do not add request logging through backup or monitoring tools.
Before a public deployment, publish the privacy note with the controller's contact, configure disk alerts,
verify the HTTPS boundary and proxy logging, and complete security/privacy review. No public figures may
be released unless each counts at least ten installations; this service publishes none.
