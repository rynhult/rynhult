# P3a structured Chat: adapter and qualification specification

> **Status: approved design (Josh, 2026-11-01).** Not yet implemented; implementation follows the redesign phases and their own reviews and qualification.

Original design status: **approved, 2026-11-01**. Base `main-cfb3` at
`cfb322ceb71c60e8f366570026389b21110ce0d3`. No product code, routes, schema,
launch mode, settings or permission changes are delivered by this document.
Read alongside [the reconciled approved design](chat-view.md).

## Server implementation status (2026-11-01)

The read-only broker/API slice is implemented on `chat-p3a-backend`. See
[the API contract and resource/qualification limits](chat-p3a-api.md). The
remaining UI, native-device/provider qualification and P3b/P3c gates below are
not implied complete by that server slice. Historical design statements about
“no product code” describe the design commit, not this later implementation.

## Scope, phase boundary and evidence

P3a reads the **existing native conversation**, presents bounded structured text
in a full Chat tab and an optional companion beside the same Terminal, and shows
only authorized Rynhult-managed workers linked to their own sessions. It does
not enable sending, uploads, approval answers, model changes or native-subagent
control. P3b owns input/attachments/receipts; P3c owns model/effort transitions
and deployment refinements. Shared pane lifetime/layout belongs to P0/P2.

Canonical planning inputs: supplied `claude-handoff.txt` → W4–W6, Technical
continuity contracts, Implementation phases; `state-contracts.csv` S08–S12;
`acceptance-criteria.csv` in the coordinator's
`ux-direction/astra-2026-10-31/rynhult-planning-handoff/` package. These sources
are not shipped product APIs and are not copied into the public Guide.

| Evidence | What was actually inspected | Limit |
|---|---|---|
| `deploy/packages/acc-packages:89–133` | Codex exact npm pin **0.163.0**; Claude native non-apt target **2.1.308** | apt Claude uses the signed apt repository/hold rather than an exact version in this catalog. Detect the installed version; do not claim all apt hosts have this format. |
| Disposable installed CLIs | `/usr/bin/claude --version` = 2.1.308; `/usr/bin/codex --version` = 0.163.0, each under an empty temporary home | Only version commands and fixture conversations; no existing credentials/sessions read. |
| Native fixture capture | Fresh temporary HOME/config/cache/provider roots, empty work directory, fake loopback model service, fixed dummy credentials, outbound proxy sink, nonessential Claude traffic disabled; one turn then explicit resume, both exit 0 | Native **print/exec**, not interactive TUI, writes actual on-disk records. No real inference, MCP, tool execution or managed Rynhult launch. This qualifies format observations, not live TUI, transport or latency. |
| `chat-spike/native-fixtures/` | Projected native records and provenance hashes from those runs | Context/usage/diagnostic records removed or projected; temporary paths renamed. Not byte-exact raw archives or full transcripts. |
| `chat-spike/replay.py` | Original `c3395e7a6` synthetic partial-write/repeated-text feasibility replay | Snapshot parser behavior only; not an incremental follower. |

The binaries were already installed; none were installed/upgraded for this task.
Only fresh disposable conversations were resumed. Never run these commands with
a real session ID, inherited provider home/config, project hooks or credentials.
Reproduction details and limitations are in [the fixture README](chat-spike/README.md).

## Existing integration points and identity

- `src/acc/server/models.py:Session` records Rynhult ID, provider, native ID,
  person, `run_as`, placement and launch specification. `SessionRun` records a
  process incarnation. `container_generation` is container-specific and is not a
  general native-session generation.
- Claude `src/acc/providers/claude.py:Claude.args` requires a preassigned native ID, uses `--session-id` on launch and
  `--resume` on restart. Codex `src/acc/providers/codex.py` uses native resume ID
  once discovered; never infer identity from the latest file in an account.
- Provider `messages_source` delegates to the adapter's `transcript(home,id)`;
  `src/acc/broker/ops.py:transcript_messages` selects under the account home and
  returns an mtime/size stamp and text snapshot. That stamp is not a Chat cursor.
  `src/acc/providers/transcripts.py` intentionally drops IDs/tools/context and
  may accept valid JSON at EOF without a newline: do not reuse it unchanged for
  a newline-committed incremental follower.
- `src/acc/server/history.py` is a redacted search index. Its reindexed message
  sequence is not a stable conversation identity or continuation token.

Proposed binding: `(installation authority identity, Rynhult Session.id,
provider, machine/node identity, run_as, placement, native_id)` plus an active
process-generation reference derived from **verified `SessionRun.id`** and the
shared P0 execution contract. Confirm current broker incarnation before following
new live records. An unverified/ambiguous run remains `unknown`, never guessed
from a PID, timestamp or newest file. A native-ID change creates a new binding.
A view-only reconnect does not create a new run or change the binding.

There is no new general execution-generation field/API in this design commit.
P0 must finalize the attestation/epoch interface before implementation. Existing
terminal writers and delivery are reuse points, not proof they already implement
P3b's complete generation fencing. P3a must not add a competing writer registry.

Historical messages retain native identity. A run boundary is a separate event,
not permission to retag every old record as the resumed run. When no recorded
pre-resume high-water checkpoint exists, show historical generation as unknown;
do not attribute bytes to a run merely by wall clock. Helpers each have their
own binding and authorization; a lead relationship is not transcript access.

## Claude Code 2.1.308 adapter

**Location:** selected account's `.claude/projects/<encoded-workspace>/<native-id>.jsonl`.
The captured folder encoded the temporary workspace path. Do not reproduce that
encoding as an authority rule: discover the exact recorded native ID under the
fixed projects root, validate all path components/descriptors, and check record
`sessionId`. Zero matches = not yet found/unknown; multiple matches = ambiguous
source error, not `next()` choosing one. Never recursively scan a human home.
Custom CLI roots require an explicitly trusted launch-root mapping, not browser
paths; current adapter discovery assumes the default `.claude` root.

**Observed native records:** user and assistant entries have `uuid`,
`parentUuid`, `sessionId`, `timestamp`, `version`, `cwd` and `message`. User
content was a string; assistant content was an array of `text` blocks. Assistant
also had provider `message.id`, `apiBlockIndex`, model and effort. Other record
types included attachments, queue operations, API request bodies and cost state.
These are internal records, **not** permission to expose their bodies. The
captured `parentUuid` may point through omitted nonvisible records; retain a
nullable parent reference, not a fabricated visible parent.

**Identity:** use the record UUID scoped to binding/source epoch. The fake model
intentionally reused `message.id` while resume produced different assistant UUIDs;
therefore model-response ID or body text alone is insufficient. UUID replay with
identical visible payload is idempotent; changed safe payload uses an explicit
revision/upsert, preserving item identity. Duplicate UUIDs with incompatible
identity/role/session are an integrity notice, not a silent overwrite. Records
without a valid UUID use the epoch/byte-range fallback below and mark weaker
identity. Do not assume assistant SDK streaming deltas are saved in this file.

**Normalization:** accept allowlisted user/assistant text blocks; exclude
`isMeta`/sidechain material from the main conversation unless separately
qualified. Filter known injected context wrappers through a versioned rule with
omission notice/count, rather than exposing instructions. Block types other than
qualified text produce an attachment/tool/unsupported placeholder without raw
payload. Never show private thinking, system messages, raw API request blobs,
credential-bearing metadata or tool argument objects.

**Resume observed:** a second native print process with `--resume <fixture-id>`
appended to the same file with the same `sessionId`, distinct record UUIDs and
identical prompt/reply text. This is an offline fixture resume **after exit**,
not evidence of attaching to a simultaneously running TUI. Compaction, branch
switches, rotation, partial assistant updates and tool chains were not generated
by this capture; qualify independently. Recognize only fixture-proven compact
markers; otherwise reset/incomplete on changed source, never invent a complete
pre-compaction archive from a summary.

## Codex 0.163.0 adapter

**Location:** selected account's `.codex/sessions/YYYY/MM/DD/rollout-<local-time>-<native-id>.jsonl`.
The fixture's native/thread ID matches `session_meta.payload.id` and
`session_meta.payload.session_id`. Its `cli_version` is 0.163.0. Use the explicit
Rynhult native ID and verified metadata; dates, filename ordering and cwd are
hints, not identity. Changed `CODEX_HOME` must be explicitly mapped from trusted
launch configuration. Same ambiguity/no-follow rules as Claude apply.

**Observed records:** top-level `timestamp`, `ordinal`, `type`, `payload`, and
sometimes `metadata`. `session_meta`, `turn_context`, `response_item`,
`event_msg`, world-state and token/usage records coexist. Visible canonical
messages are `response_item` with `payload.type=message`, user/assistant role,
`payload.id`, and input_text/output_text blocks. The captured user environment
record is also role=user: role alone is insufficient to decide it is human text.
`internal_chat_message_metadata_passthrough.turn_id` and content-kind tags can
help classify known formats, but no unknown tag becomes authorization.

**Identity:** binding/source epoch plus `(turn_id, payload.id)` for qualified
message records. Read the explicit turn reference where present; do not assign
historical items to whichever turn happens to be current after resume. The fake
model reused its assistant item ID across distinct turns, so global item-ID-only
deduplication would lose a real reply. Top-level ordinal is a source-local order
hint, never a globally durable cursor. Retained-source revision/complete fields
were present in this capture; they are candidate revision evidence, not a stable
public API guarantee. Missing/contradictory IDs fall back to byte identity or an
integrity notice. Do not merge distinct deliberately repeated messages.

**Normalization:** choose `response_item` as the canonical visible family. Do
not append `event_msg.item_completed`, task-complete text, live deltas and final
messages as separate copies. Task start/complete can provide separately
qualified boundaries, not another assistant bubble. Exclude developer/system,
reasoning, environment instructions, world-state and raw usage/config metadata.
Function calls/results need call-ID fixtures before rich rendering; placeholders
are honest initial behavior.

**Resume observed:** `codex exec ... resume <fixture-id> <same-prompt>` appended
a second turn to the same rollout, retaining one session-meta ID and distinct
turn IDs. Final text repeated intentionally and the parser retained both turns.
Print/exec does not qualify TUI behavior, app-server ownership or compaction.
A compacted/new rollout, changed ID or unsupported record schema produces the
explicit source reset/gap behavior below. Do not assume all rollout changes keep
ordinals or that event timestamps strictly increase.

## Read/follow protocol: implementable proposed contract

1. **Resolve and authorize.** Main resolves the session binding server-side and
   invokes only its correct account/node broker. Browser supplies a Rynhult
   session ID, page size and opaque cursor, not native IDs, paths or executables.
   Broker validates root, descriptors, regular-file type and native metadata.
   Recheck main ownership/group/device/session authority after every awaited read
   and immediately before fan-out; discard an in-flight result after revocation.
2. **Commit complete records only.** Read at most 256 KiB per I/O batch through
   pinned descriptors. Only newline-terminated UTF-8 JSON objects advance the
   committed high-water offset. Retain split UTF-8 and incomplete JSON bytes,
   bounded at 4 MiB per record. EOF during a partial record means `waiting`, not
   malformed/empty. At a sealed source, unfinished bytes become an explicit gap.
   Drain oversized lines incrementally, count them and show an omission notice.
   Invalid complete UTF-8/JSON/schema is counted, not converted into replacement
   text or logged raw. Valid JSON without its newline still waits.
3. **Opaque cursor.** Logical fields: cursor schema/adapter version, binding,
   active run reference, source epoch, committed byte offset, item revision and
   integrity checkpoint. Store server-side or MAC it with protected server
   authority; bind it to the requesting principal/resource scope and reauthorize
   on use. Expire it on identity/revision/epoch changes. Reject cross-session,
   cross-account/node, tampered, overlong or unsupported cursors. Do not reveal
   filesystem paths, raw transcript bytes or signing material in a token.
4. **Source epochs and resets.** Track root/leaf descriptor identity, source
   metadata, retained byte-range digests and native metadata. Inode replacement,
   truncation, missing/reappearing source, compaction marker, incompatible schema
   or inability to verify continuity changes epoch and emits `reset(reason)` plus
   a fresh bounded page and gap/completeness status. Mtime+size is not sufficient.
   For P3a, conservatively revalidate the previously accepted bounded prefix on
   change before claiming append continuity: batch reads with yields and stat
   checks before/after, no long-held DB transaction. Cap verification at the
   existing 64 MiB source budget; if continuity cannot be established within it,
   reset/incomplete rather than silently trusting a suffix. Optimization needs
   equivalent rewrite/compaction tests; do not claim malicious same-UID stores
   cannot change between checks. Cached pages are observations, not a provider
   archive or a tamper-proof record.
5. **Stable items.** Provider IDs within epoch support idempotent upsert of a
   qualified revision. Fallback ID uses `(epoch, record-start, record-end,
   block-index)`; include payload digest to detect changed bytes. Reset invalidates
   offset-based items; do not body-match or silently stitch them across epochs.
   A page/event includes stable item key, event revision, role, safe blocks,
   source/run provenance and omission/redaction markers. Upsert replaces an item
   version; final text must not append after already displayed deltas. Rich
   streaming updates remain disabled until their exact identity rules qualify.
6. **Pagination/reconnect.** Initial read returns the newest bounded page (default
   100 visible messages, max 200, max 256 KiB normalized bytes); older pages are
   explicit. Scan budgets/omitted ranges are visible even if fewer messages are
   returned. Keep page watermarks consistent with follow events. Reconnect with
   the last acknowledged cursor is idempotent within epoch; reset on expired or
   unverifiable continuity. Preserve reading anchor and offer New messages when
   not at bottom. A source/run transition is visible; reconnect never launches
   a CLI or resumes one. On main restart, unavailable cursor state causes reset,
   not a fabricated empty list or undetected loss.
7. **Lifespan/bounds.** One follower per active authorized binding shared by views;
   scoped emissions per viewer. Initial 500 ms local / 2 s node floor, idle
   backoff, hidden-view suspension unless another viewer needs it. Cancel on
   last viewer, sign-out/revocation, generation replacement and shutdown. Bound
   concurrent followers, scan work, event queues and total cache; per-view queue
   cap 1 MiB, close slow consumer with reconnect status. No per-view recursive
   home watcher or unbounded node polling. Numeric total-host limits need load
   measurement before release; these per-request limits are starting defaults.
8. **Privacy.** Redact bounded complete content blocks before cache/fan-out and
   reapply current grant/redaction state on cached reads. Never emit a known
   secret prefix while waiting to redact its suffix. No raw-provider-JSON escape
   hatch, executable Markdown/links/remote-image fetches, private reasoning or
   secret-bearing diagnostics. Audit stream open/close and bounded counts/IDs,
   not prompt text. Reuse existing session/edge admission; P3a introduces no new
   listener or authentication exception.

A run change invalidates **live cursors** but need not duplicate native items
when their verified source epoch/IDs still match. Preserve prior pages as
historical observations and mark the boundary. A source reset cannot revoke
bytes already displayed; clearing unauthorized cached content and stopping new
emissions remains mandatory.

## Source state: never overload an empty array

| State | Evidence and visible behavior |
|---|---|
| Loading | First bounded request pending; skeleton, no empty claim |
| Unknown / waiting | Native ID not reported, file not yet flushed, ambiguous run/source, incomplete last record or unrecognized schema; say why safely, offer Terminal |
| Empty (observed) | Verified binding, supported format, successful bounded read, zero visible messages **and no omitted/partial/unscanned range**; only then “No messages yet” |
| Partial / compacted / truncated | Known messages plus counts/gap and supported loading of earlier data; never “complete archive” |
| Unsupported | Provider/version/format capability not qualified; Terminal remains available; no silent parser fallback |
| Disconnected | Correct node unavailable; retain currently authorized last known page as stale with observed time; do not substitute main account |
| Error | Permission/integrity/read/limit failure; safe category and Retry read; distinguish from not-found/empty |
| Reset / reconnecting | Cursor/epoch invalid; explicit refresh with anchor recovery where identity is still valid, otherwise gap |
| Revoked | End stream, invalidate cursors and clear sensitive view/cache references; unavailable placeholder contains only currently authorized metadata |

## Per-provider qualification matrix

**O** = observed native fixture format, **S** = synthetic existing-parser replay,
**U** = unqualified/disabled Chat capability. These labels are not a shipped UI.
A qualification record must include CLI and adapter versions, transport/mode,
OS/account/node/container scope, exact tests and outcomes; provider name alone
never enables a control. Other four providers remain independently unqualified
for new Chat, even though existing history adapters remain available.

| Capability | Claude 2.1.308 | Codex 0.163.0 | Evidence required before enablement |
|---|---|---|---|
| History | O: text, UUIDs, same-ID resume | O: response_item IDs/turns, same-ID resume | H1 below with actual TUI-generated fixtures; isolation/redaction/omission bounds, pagination, generation/metadata checks |
| Follow | S: incomplete append and repeated text; U incremental cursor | S: same; U incremental cursor | H2: real flush behavior plus deterministic partial/rotation/compaction/reconnect/reset and cursor checks |
| Rich tool rendering | U; only text native capture | U; only text native capture | H3: tool call/result correlation, retries/errors/parallel calls, missing IDs, bounded redacted safe output; no approval actions |
| Text send | U (P3b) | U (P3b) | I1: same running process/ID, idle recognition or owned protocol, actual correlated acceptance, two composers/terminal/team race, Stop/revocation and crash receipts |
| Multiline send | U (P3b) | U (P3b) | I2: literal UTF-8/newlines, bracketed-paste or protocol semantics, no accidental submit/shell execution; IME/large/control/prefix cases |
| Image/document attachments | U (P3b) | U (P3b) | A1: immutable staging IDs/digests, correct actual account/node/container path, MIME/size/quota, offline/unreadable/swapped file, native reference acceptance and separate model capability |
| Model/effort | Launch argv supported; U live transition (P3c) | Launch argv supported; U live transition (P3c) | M1: effective versus pending, live/next-turn/reviewed restart, busy/approval state, failures/cancel and worker independence; no silent process replacement |

### Acceptance cases assigned to implementation

- **H1:** two people/two sessions on two simulated machines, wrong native ID,
  wrong account, duplicate source matches, symlink/FIFO/device/swap at each path
  component, metadata mismatch and version drift. Missing versus empty versus
  unsupported; 4 MiB line/64 MiB scan and normalized-page limits; malicious text,
  links, context wrappers, split known secrets and unknown blocks. Permission
  revoked during awaited source/cache read emits zero further content.
- **H2:** byte-by-byte UTF-8/JSON, valid JSON without newline, two identical
  messages with distinct IDs, identical ID replay and changed revision,
  append during pagination, copied/rotated/truncated/same-inode rewritten prefix,
  compaction with/without proven markers, incomplete sealed tail, source missing
  then reappearing, ordinal reset and same-native-ID new run. Disconnect at every
  cursor checkpoint, duplicate events, cache loss, main restart, tampered/stale/
  cross-scope cursor, slow consumer and cancellation leave no duplicate item,
  silent gap, leaked task or invented run attribution. Deterministic barriers,
  not timing sleeps, control races.
- **H3:** versioned real tool records before enabling rich tools. Synthetic hostile
  fixtures supplement but do not establish provider schema compatibility.
- **I1/I2, A1, M1:** P3b/P3c only; see matrix and approved design. A terminal echo,
  HTTP accepted status, uploaded file or saved model preference does not establish
  provider acceptance/effective state. Never auto-replay an uncertain input.
- **UI:** full read-only Chat and optional companion share one state/subscription,
  no active composer in P3a, workers show authoritative state with source/freshness
  and open their own authorized session. 1440/390 px, keyboard/focus/IME, light/
  dark, zoom, anchor preservation, slow/offline/error/empty/unsupported states;
  switching geometry preserves Terminal instance, connection and writer.

| State contract | P3a obligation | Later gate |
|---|---|---|
| S08 | Viewing/layout never claims input, existing Terminal control unchanged | P3b shared arbiter, draft-safe explicit handoff, no input into shell/approval/login |
| S09 | Entire read/follow/state/cursor contract above | Provider-specific additional formats qualify independently |
| S10 | No optimistic “sent” bubble or input endpoint | P3b durable receipts and uncertain recovery, no replay |
| S11 | Render only qualified safe historical placeholders; no new upload/send promise | P3b per-file lifecycle and receipt distinction |
| S12 | Show only sourced effective historical configuration, never a live selector from transcript metadata | P3c pending/effective/apply-timing qualification |

### Planning-package acceptance mapping

- **AC-025:** both read layouts, same native session/process/run; no substitute
  process. P3a implements the read/layout portion; P3b enables qualified input.
- **AC-026/027:** capability matrix plus H1/H2 and UI state/cursor tests above.
- **AC-028/029:** preserve input behavior in P3a; P3b I1/I2 prove arbitration and
  receipts. No P3a composer or optimistic send status.
- **AC-030:** historical placeholders only in P3a; A1 is a P3b gate.
- **AC-031/032:** historical sourced configuration only in P3a; M1 is P3c and
  must leave other workers unchanged.
- **AC-033/034:** P3a worker visibility uses authoritative assignment/attempt,
  machine/location and reported configuration, with unknown/stale distinct from
  empty. Links reauthorize the worker session. Do not infer opaque subagent
  visibility, controller rights or new deployment authority; refinements are P3c.

These are implementation acceptance assignments, not claims those UI or security
tests ran on this documentation branch.

## Native-machine interfaces: P3b recommendation, not a retrofit

Read-only external reference: [HKUDS/AutoSwarm](https://github.com/HKUDS/AutoSwarm),
MIT, inspected 2026-11-01 at commit
`03494ac03d51287298240b5962c88c07cfbeb260`. No source was copied or executed.

| Source | Observed interface design | Rynhult implication (our assessment) |
|---|---|---|
| [providers.py](https://github.com/HKUDS/AutoSwarm/blob/03494ac03d51287298240b5962c88c07cfbeb260/src/autoswarm/providers.py#L44) | Builds Codex app-server and Claude print/stream-json/stdio-permission invocations | These launch owned processes; they do not establish attachment to our existing TUI. Preserve Rynhult compile/guard/account policy. |
| [native.py](https://github.com/HKUDS/AutoSwarm/blob/03494ac03d51287298240b5962c88c07cfbeb260/src/autoswarm/native.py#L144) | Codex thread/turn calls, Claude user/control messages; approval replies and correlated requests | Better structured input/acceptance evidence for a process owned from launch. Approval relay needs Rynhult consent checks, expiry and scope validation; resumed ID alone is not shared ownership. |
| [acp.py](https://github.com/HKUDS/AutoSwarm/blob/03494ac03d51287298240b5962c88c07cfbeb260/src/autoswarm/acp.py#L22) | Negotiates session load/resume, prompt/update and permission messages; distinguishes one-time permission from persistent grants | Capability negotiation is useful; provider/version support is still specific. Never silently turn one-time consent into persistent permission. |

For **new protocol-owned sessions**, structured interfaces are the preferable
P3b investigation: typed content, request/item/turn IDs and explicit errors avoid
screen scraping, paste heuristics and shell/prompt ambiguity. Codex documents
thread/start, thread/resume, turn/start and streaming/approval methods; Claude
documents noninteractive structured execution. These are not evidence that an
independent process can control a live TUI. Sources: [official Codex App Server
documentation](https://developers.openai.com/codex/app-server/),
[official Claude programmatic CLI documentation](https://code.claude.com/docs/en/headless).
Documentation is rolling; pinned binaries must pass version-specific fixtures.

**Existing sessions remain read-only Chat until their original-process input
transport qualifies.** Do not start app-server, print --resume or ACP beside a
running TUI to imitate continuity. A separately reviewed launch-mode transition
must stop/fence the old process, preserve native conversation/history and frozen
hooks/guards/MCP/account/grants, identify the new process incarnation, and tell
the person what changed. Unsupported transitions stay in Terminal.

All protocol producers still use the **one** server arbiter shared with Terminal
and team delivery. Provider request IDs do not make a crash exactly-once; retain
durable queued/submitting/accepted/uncertain receipts. Reauthorize before writes
and approval replies; unknown approval kind/scope fails closed. Protocol cancel,
steer and permission methods are not blanket authority to interrupt, approve or
weaken policy. Native helpers/descendants remain separately bounded; an opaque
provider subagent must not be advertised as a Rynhult-managed worker.

## Decision and remaining qualification

GO for **design/format feasibility**, not product enablement. Both exact installed
versions wrote and resumed disposable native text records; stable-ID and cursor
rules are specified, not implemented. TUI flush/compaction/tool/attachment/input
behavior, real provider model access, OS/node/container permutations, live protocol
ownership and browser/device accessibility remain unqualified. Use those gaps as
release gates, not arguments for silently broadening a fallback. No UI, schema,
permission or runtime change belongs in this design branch.
