# P3a read-only Chat API

Implemented server slice, 2026-11-01, based on `main-475e` (`475e6063`).
This is backend/API documentation for [the adapter design](chat-p3a-adapters.md).
It adds no Chat UI, sending, upload, model/effort control, CLI launch or permission
change. Existing native sessions and terminals are untouched.

## Admission and execution boundary

All reads use the same authenticated `Context.session_for` authority as the
session view, including personal/group/remote-placement restrictions. The
installation API token retains its existing owner read authority. Browser/device
sign-in is revalidated after awaited reads and before each SSE emission; no new
stream chunk is emitted after detected revocation. Cross-origin browser requests
are refused. Missing/inaccessible sessions are 404, ended login is 401.

The server selects provider, native ID, node/account and current **SessionRun**
from the database. Only the account broker reads its own `.claude/projects` or
`.codex/sessions` root, through descriptor-relative no-follow opens. No browser
native ID/path, server-side account-home access, writable provider store,
credential access or new listener is involved. Unverified/missing runs produce
`unknown`; imported/external sessions and unsupported providers produce
`unsupported`. Existing history/search APIs are unchanged.

History items have unknown process-run attribution: a native file may predate
several resumes. Top-level `run_id` binds the **read observation and cursor** to
the currently verified process incarnation; it does not relabel every historical
message as belonging to that run. `historical_run_attribution` is `unknown`.

## Page

`GET /api/sessions/{acc_id}/chat?limit=100&before=<opaque cursor>`

- `limit`: 1–200 (default 100); invalid bounds return 422.
- Without `before`, return the newest bounded page in the scanned source range.
- `before_cursor` loads the next older page; null means no earlier page in the
  observed range. A gap/scan limit still means the archive may be incomplete.
- `cursor` is the follow checkpoint at this page's source watermark. A page cursor
  retains independent older-page and follow offsets; pagination must not change
  which live native session is followed.
- Responses use `Cache-Control: no-store`.

Response shape (example values, not a real cursor):

```json
{
  "session_id": "example-session",
  "run_id": 42,
  "state": "ready",
  "historical_run_attribution": "unknown",
  "observed_at": "2026-11-01T20:00:00+00:00",
  "items": [
    {"id": "scoped-item-digest", "revision": "safe-content-digest", "role": "assistant", "text": "Ready."}
  ],
  "omitted": 0,
  "reset": false,
  "more": false,
  "notices": {"tools": 0},
  "reason": null,
  "cursor": "opaque-follow-checkpoint",
  "before_cursor": null,
  "capabilities": {
    "read_history": true,
    "follow_messages": true,
    "rich_tools": false,
    "send_text": false,
    "send_multiline": false,
    "attach_image": false,
    "attach_document": false,
    "model_effort": false
  }
}
```

`id` is salted by the authorized session/account/node/caller binding. Native
identities preserve intentionally repeated messages and permit upsert of a
qualified text revision. `revision` is computed **after vault redaction**, not a
hash exposing unredacted message content. Render text as untrusted content; this
API does not supply trusted HTML or raw tool objects. Private reasoning, system
instructions, raw native metadata and account paths are not response fields.
Unknown/image/document structures and unqualified prompt/context blocks are
omitted with a gap count. Known Claude `isMeta`/`isSidechain` records stay private without counting as damaged visible history. Known context tags are removed while ordinary comparisons, generics and markup remain literal untrusted text. Qualified tool records increment `notices.tools` without
making normal tool use a corrupt-history gap; tool arguments remain private.
Rich tools and attachment capabilities remain false. `more` signals a bounded
continuation and `reason` is a fixed enum, never a path or exception.

`observed_at` describes a successful source observation, not a provider-message
timestamp; it is null when no source was observed (for example an offline node).
A retry that finds no source does not make a previously cached view fresh. On a failed/unavailable read, the caller's existing cursor is reissued unchanged, preserving both source/follow position and older-page position (including its original expiry). Pending prefix verification likewise keeps that cursor. No failure may replace a valid checkpoint with a source-less latest-page cursor. A round with no retained or observed position reports `reset=true`; actual source discontinuity still reports reset and requires a new snapshot.

## Follow

`GET /api/sessions/{acc_id}/chat/events?after=<cursor>&wait=25`

Authenticated same-origin SSE, `Content-Type: text/event-stream`, `no-store`,
`X-Accel-Buffering: no`. `wait` is 0–25 seconds. The response is intentionally
bounded in lifetime; reconnect explicitly from the last consumed checkpoint.
An omitted cursor starts with the newest snapshot. `wait=0` performs one read
and closes, useful for deterministic clients/tests.

```text
id: <opaque checkpoint>
event: chat
data: <same response envelope as the page>

```

The `Last-Event-ID` header, when present, takes precedence over the initial
`after` query, so normal SSE reconnects can advance beyond the original URL's
checkpoint. Messages are **upserts by item ID/revision**, not append-every-frame
bubbles. The heartbeat envelope may have no items. Following a full page of
updates stops before the first deferred message. The cursor covers only committed
records (including explicit omissions), avoiding skipped intermediate messages. A reset yields no guessed suffix: fetch a new page without the cursor.

Each emission revalidates current login/device/resource/run authority. If it
ends after headers were sent, the stream closes without private error details;
refresh the authorized session/page before reconnecting. Before headers, invalid
cursor returns 400, run change returns 409, and authorization returns its normal
401/404/403. No HTTP/SSE result creates or resumes a process or claims a writer.
No send endpoint is registered (the existing API catch-all refuses POST).

## States and resets

| State | Meaning |
|---|---|
| `ready` | Supported complete records available; a follow heartbeat may contain zero new items |
| `empty` | Supported source successfully scanned with no visible text and no unfinished/unscanned/omitted range |
| `unknown` | Native ID/source/run/format not established, ambiguous source discovery, or an unfinished final record; never “No messages yet” by inference |
| `unsupported` | Provider/management mode or observed native version not qualified for this reader |
| `offline` | Enrolled node is unavailable; do not fall back to a local account |
| `gap` | Omitted/malformed/unknown/oversized records, source scan limit or failed continuity; retain explicit incompleteness |
| `error` | Safe account-service/read/permission refusal; raw exceptions/paths are not returned |
| `busy` | Bounded account-read queue is full or its brief wait expired; `reason=account_busy`, keep the cursor and retry later; not a source error |

A detected source replacement, prefix rewrite, truncation or disappearance
invalidates the checkpoint and reports `reset=true`. Identity and 4 KiB head/tail
anchors are checked on every poll. A bounded full-prefix check runs before delivering any appended suffix, on mtime/ctime changes, or after 30 seconds even without a metadata change. A middle rewrite combined with append therefore cannot bypass verification for 30 seconds. Verification may span bounded reads and emits no suffix until it completes; it is not an immutable archive or a guarantee against changes after a read. A pending
check returns `unknown`, `more=true`, `reason=prefix_verification`, without moving
the cursor. Cache eviction or broker restart also requires an explicit reset. Appended unknown/compaction records produce
an omission/gap; compacted rewrites additionally reset continuity. Compaction
summaries are not a reconstructed archive. Only complete newline-terminated JSON
advances a checkpoint; even otherwise-valid JSON at EOF waits for its newline.

Cursors carry a MAC, schema, caller/binding hash, SessionRun ID, source identity,
committed-prefix digest/offset, pagination offsets and one-hour expiry. They are
opaque protocol values, not grants and not encrypted content containers. No
filesystem path or native transcript is included. Tampered/cross-caller or
cross-binding/expired tokens return 400. A changed run returns 409; a changed
account/node/placement/launch identity also invalidates the binding. Main restart
replaces the in-memory cursor key: reload the page instead of trusting an old
cursor or displaying an empty archive. Native item identity can remain stable
across a verified same-native-ID resume; live cursors cannot.

## Resource bounds and privacy

- Source discovery: Claude goes directly to the server-selected encoded workspace;
  qualified Codex UUIDv7 IDs first try timestamp-derived native paths. Legacy
  fallback discovery has bounded depth, 4,096 entries and one matching source;
  discovery exhaustion returns `unknown`, `more=true`, `reason=discovery_budget`.
  Unrelated links are ignored; selected links/FIFOs/devices refuse. Ancestors use
  traverse-only `O_PATH` where available. Ambiguity never selects another source.
- Reads reuse FS-1's same-account isolated `search_worker.Searches` child path:
  trusted installation imports under `-I`, two-second hard execution deadline,
  kill on cancellation/timeout, one worker per broker retained until reap, a FIFO of at most four waiters with a 500 ms admission deadline, and
  frozen-policy validation before/after execution outside the control lock. Queue saturation/admission timeout uses a typed `read_busy` broker reply; Chat maps it to `busy`, never `error`. Queue cancellation removes the waiter; cancellation at handoff releases exactly one reservation. An unreaped child keeps the active slot and cannot spawn replacements. The existing two-second execution deadline plus admission wait fits the fresh three-second client timeout. When merging file search, use this same queue and preserve typed busy handling in its API.
  API reads use fresh three-second broker clients, never the terminal-control
  FIFO, and are not transport-replayed or treated as uncertain HA writes.
- Cooperative work limits: one second per scan, 50 ms per normalized record,
  256 KiB I/O fragments, 4 MiB records, 128 KiB text, 64 MiB scanned per request,
  100,000 records. Limits report gaps/continuation and never advance over unread
  bytes. An incomplete final line waits for its newline. Mid-scan append is read
  to the initial size; the next poll receives the appended suffix without reset.
- Follows parse only the appended range. A serializable SHA-256 fragment chain
  and bounded (128 checkpoint / 128 path) metadata cache cross worker lifetimes;
  no transcript bodies, tool arguments or private reasoning are cached. Prefix
  verification uses the same fragment boundaries and can resume within its budget.
- Pages: at most 200 messages and 256 KiB normalized public item data. Oversized
  text is explicitly omitted. Initial/older pages retain bounded recent items;
  follows defer the first item exceeding the page bound without committing it.
- Matching reads share a cancellation-owned task; a per-binding lock serializes
  reads with 500 ms local / 2 s node floors. No detached result cache survives
  request consumers. At most 32 active read keys and 32 follows, four per caller;
  a new/less-represented caller can reclaim an excess follow from another person.
  An evicted view closes before emitting another chunk; reconnect from its last
  cursor. With 32 equally represented people admission remains bounded/refused.
  Opens are limited to 60 per caller/minute; disconnect releases read/follow
  ownership and closes audit. Slow SSE consumers have no unbounded event queue.
- Vault redaction includes retained material for retired session keys and occurs
  on complete bounded text before response fan-out. Known-value redaction is not
  universal DLP or protection against a malicious same-UID agent; existing native
  account/session isolation limits still apply.
- `chat.read`, `chat.open`, `chat.close` audit actor, session and run ID only;
  never prompt text, native bodies, credentials or per-token events. There is no
  audit claim of provider acceptance or a client receiving every SSE byte.

## Qualification and limits

Tests use the committed **native-generated Claude 2.1.308/Codex 0.163.0**
projections plus synthetic hostile files and two simulated account/machine
brokers. They do not read real sessions. The apt Claude package path is not an
exact catalog pin; other observed formats remain unsupported rather than guessed.

This server slice implements the read/cursor/authority contracts. Full-tab and
companion UI, managed-worker presentation, browser/device acceptance, TUI flush
latency and native compaction/tool qualification still belong to their respective
lanes/review gates. Sending, shared input receipts, new attachments and effective
model/effort transitions remain P3b/P3c. No new layout/settings/admin-feature files
are modified; deployment and the focused independent security review are with
the coordinator.
