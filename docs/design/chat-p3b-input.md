# P3b: input to the session you are already using

> **Status: approved design (Josh, 2026-11-01).** Not yet implemented; implementation follows the redesign phases and their own reviews and qualification.

Status: **proposed implementation design, 2026-11-01**. Based on `main-475e`
(`475e6063dc15a94d676eac468f6bcc2c20ac86a0`). This document and its handoff
are the entire change: no input capability, launch mode, schema or rollout is
implemented or qualified here. The approved product direction remains
[Chat beside the live terminal](chat-view.md), with
[P3a's adapter boundaries](chat-p3a-adapters.md) and
[the continuity contract](continuity-contract.md), especially S08–S11/P2.

## Recommendation and decision boundary

**For the first P3b slice, qualify literal input into the original live TUI,
through the shared input arbiter.** This preserves the native CLI, Terminal and
pending state people already use. Ship one provider/version's plain-text path
first; qualify multiline and each attachment kind separately. If a reliable
empty editor, safe submission or correlated acceptance cannot be established,
that capability stays unavailable and Chat remains read-only. Screen heuristics
alone must not turn this into a best-effort paste feature.

Protocol-owned sessions are the stronger long-term transport candidate, with
structured inputs and identifiable turns, but require a separate launch-mode
pilot. They cannot silently replace the promised native Terminal. An app-server
or stream-json process is not an attachment to an arbitrary running TUI; neither
resuming its native ID nor rendering its logs creates the same live process.
The pilot needs an explicit product decision about Terminal semantics and
provider/version guard qualification before being offered. ACP is an adapter
candidate within that pilot, not a universal compatibility shortcut.

| Choice | Benefit | Risk / cost | P3b disposition |
|---|---|---|---|
| Original TUI, existing PTY | Same live process, native terminal experience and existing hooks/permissions | Empty-editor recognition, literal paste/submit, acknowledgement and exit races need exact-version proof | Recommended first path, individually gated |
| Codex app-server owned from launch | Structured request/turn identity and permission messages | New launch contract; ordinary Codex TUI cannot be assumed to share that running thread | Separate pilot; no retrofit alongside a live TUI |
| Claude stream-json owned from launch | Structured input/output and bidirectional permission handling | Stream lifetime, acknowledgement meaning, hooks and permission transport differ from TUI | Separate pilot; keep control channel alive across turns |
| ACP owned from launch | Negotiated session, content and permission interfaces | Each actual provider bridge adds its own version, behavior and trust boundary | Consider only a pinned, qualified bridge; fail closed on missing capabilities |

This extends the PTY-first rule in the approved Chat design. It does not reopen
approval of Chat/full-tab/companion input, nor approve a second inference process,
a silent restart, an external relay, a new network listener or weaker guardrails.
Model/effort changes and busy-turn steering remain P3c or later.

## Evidence: source inspection, not transport qualification

Local anchors at this base:

- `src/acc/server/models.py`: immutable `LaunchSpec`, `Session` placement/account/
  native ID, and verified `SessionRun` process incarnations. A container generation
  alone is not a general session-run identity.
- `src/acc/server/tty.py:Writers` and `bridge`: current single writer is websocket-
  based, not a durable run-fenced arbiter. Input awaits `on_input` before writing;
  P3b needs a fresh final authorization/token check after such awaits.
- `src/acc/server/orchestrator/delivery.py`: durable pre-key claim and refusal/
  no-retype machinery are reuse points. `validate_text` flattens tabs/newlines;
  clearing keys and `screen:input-cleared` acknowledgements cannot be adopted as
  Chat safety or acceptance. Historical delivery evidence must retain its meaning.
- `src/acc/broker/ops.py:prepare_tmux`, `launch`: remain-on-exit and exec-based
  launches are useful foundations; they do not prove every wrapper/container
  can never fall back to a shell or respawn into a different generation.
- `src/acc/providers/claude.py:Claude.compile/args`: per-session settings enforce
  hooks, PreToolUse guard, appended instructions and strict selected MCP config.
  `src/acc/providers/codex.py:Codex.compile/args/bootstrap` supplies catalog/MCP/
  skill restrictions, trusted minimal hook groups and final `features.hooks=true`.
- `src/acc/server/api/files.py` and `src/acc/broker/files.py`: reuse scoped routing
  and file checks, not a generic arbitrary-path upload. Chat requires additional
  private staging, identity, retention and per-send verification.

The P3a native fixtures observed **Claude 2.1.308** and **Codex 0.163.0** in
print/exec conversations. They did not qualify interactive input, protocol-owned
launches, attachments or permission replies. Claude apt hosts need their actual
installed version checked; a catalog target is not proof of that host's version.
P3a backend security follow-ups are separate work and must land before building
on its read path; this branch intentionally starts from the requested base.

Official references inspected read-only on 2026-11-01:

- [Official Codex App Server documentation](https://developers.openai.com/codex/app-server/)
  documents initialization, thread start/resume, `turn/start` returning a turn,
  user-message items, local-image inputs and structured approval requests.
  These are protocol-owned capabilities, not a guarantee of idempotent replay or
  control of an independent TUI. Rolling documentation redirected to ChatGPT Learn;
  exact 0.163.0 wire support still needs captured schemas and fixtures.
- [Claude programmatic operation](https://code.claude.com/docs/en/headless)
  documents print/stream-json output. The
  [CLI reference](https://code.claude.com/docs/en/cli-reference) documents
  stream-json input, replayed-user acknowledgements, session resume and a
  permission-prompt tool. Its public description names an MCP permission tool;
  it does not establish the special `--permission-prompt-tool stdio` contract.
- Anthropic's official SDK
  [subprocess transport](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/transport/subprocess_cli.py)
  and [control dispatcher](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py)
  expose structured transport configuration and correlated control requests/
  responses, including tool permission callbacks. Their rolling source is
  corroborating evidence, not a schema pin for installed Claude 2.1.308.
- ACP v1 [initialization](https://agentclientprotocol.com/protocol/v1/initialization),
  [prompt turns](https://agentclientprotocol.com/protocol/v1/prompt-turn) and
  [content](https://agentclientprotocol.com/protocol/v1/content) specify capability
  negotiation, session prompts/updates, permission requests and typed content.
  Negotiating ACP is not evidence a particular Claude/Codex bridge preserves
  native hooks or attaches to the existing TUI. Do not mix v1 and draft v2 shapes.

No CLI, provider account, real session, inference request or SDK was executed.
Protocol signals below are qualification requirements, not observed test results.

## One identity, one execution owner

Every input is bound server-side to:

```text
installation authority epoch
  + Rynhult Session.id + verified SessionRun.id
  + machine/node enrollment identity + run_as + placement/worktree identity
  + provider + native conversation ID + immutable LaunchSpec digest
  + broker incarnation + transport instance + writer epoch
```

Only the server/broker resolve these references. Browser requests carry the
Rynhult session, expected run/control revision and opaque IDs, never executable
names, native IDs, host paths, account names or raw provider RPC. Same native ID
on two machines/accounts is not interchangeable. PIDs need incarnation evidence;
PID/name reuse, a new node enrollment, unknown run, changed launch specification
or changed worktree invalidates the input binding. Read-history authority alone
never grants control, file upload or permission-decision authority.

For the recommended path, Chat and Terminal reach **the same original provider
process and PTY**. Views, full/split layout changes, cursor reset, browser reload
and read reconnect never launch/resume a CLI. P2 view restoration is always an
observer; the P1 historical claim-if-free exception must not become a Chat take.
A single canonical terminal host owns resize authority. Stop and process exit
seal input before any new run is admitted.

For the protocol pilot, a broker-supervised runtime owns one protocol producer
for this session/run; a transport may have supervisor children but never a second
independent conversation writer. Chat and any terminal-style client must submit
through that runtime/arbiter. Raw JSON stdin must never be attached to a browser
terminal. An ordinary TUI started separately is not a terminal view of this
process, even if it loads the same conversation. Until a genuine shared-process
terminal frontend is qualified and its presentation is approved, label diagnostics
as read-only logs and do not advertise native Terminal parity. This unresolved
requirement is why the pilot is not the first P3b transport.

## Shared arbiter and explicit handoff

Extend the existing writer boundary rather than adding a Chat mutex next to it.
Terminal frames, Chat submissions, team delivery and any scheduled automation
must all use one run-bound arbitration authority. An attachment upload/local
composer draft holds no writer. A queued message also holds none.

1. Authenticate the person/device and reauthorize existing session-control rights,
   current run, desired state, stopped/expired team or assignment, resource limits,
   placement and frozen grant. Use existing authority rules; no new broad role.
2. Resolve capability for the actual installed CLI/adapter/transport and frozen
   launch policy. Unknown broker/schema/version means unavailable, not PTY fallback.
3. An explicit Send requests control. For another page/device/team holder, show
   who controls it and use the existing explicit Take control/reason flow. A lost
   CAS returns conflict and keeps the draft; no automatic take retry.
4. Same-browser Terminal handoff pauses client input and flushes sequenced prior
   input frames to the server/broker. Verify editor emptiness and no IME/preedit
   against the provider, not just a textarea. Drain/ack the old frame watermark,
   revoke its writer epoch, then admit Chat atomically. Unknown/nonempty editor
   blocks handoff: **Finish your terminal input before sending**. Never clear it,
   invent editing keys, copy buffers or submit the person's half-written text.
5. Persist the receipt claim, then reauthorize immediately before each bounded
   write and separately before submit. Any awaited safety/path/node lookup is
   followed by the final run/token/authority check. Serialize handoff with that
   last authorized write. The account broker must reject stale frames/operations
   too: a main-only lease cannot fence delayed bytes on a node connection.
6. During submission Terminal displays output as **Chat is sending — terminal
   read-only**. It cannot buffer keystrokes to replay after handoff. Release Chat's
   lease after its bounded submission/acceptance wait, retaining an uncertainty
   barrier if acceptance is unresolved. Returning to Terminal control is explicit;
   focusing it, resizing or opening an approval never claims a writer.

Remote admission uses a short-lived, single-operation authority ticket bound to
the active node link/broker incarnation, run, writer epoch and request digest;
initial expiry is 10 seconds, rechecked immediately before submission. Link loss
or an expired ticket cannot authorize new writes. Stop/take/revocation serializes
with admission and invalidates unsent tickets. Bytes already admitted to a kernel
write cannot be recalled: record that boundary and retain uncertainty instead of
promising instantaneous revocation of bytes in flight. P0's installation fencing
must prevent an old main from minting usable tickets after a leadership change.

The execution protocol needs sequenced frame acknowledgements and run/epoch
checks; these are additions to qualify, not properties of today's attach bridge.
Where the broker cannot fence the original process, refuse sending. Never rely
on a checked screen plus an unfenced later `tmux send-keys` to prevent exit races.
The qualified launch must keep provider exit from handing buffered input to a
shell or a replacement process; pin the run/PTY, prohibit fallback/reuse, and
prove wrapper/container behavior. A node disconnect cannot carry queued writes
into its new broker incarnation when it reconnects.

Initial queue policy: refuse busy/unknown/prompt states by default. A person may
explicitly choose **Queue until ready**, at most one queued Chat message per
session, expiring after 60 seconds. Queueing requires a chosen target run and
fresh checks at dispatch; authority loss, run/broker change or expiry ends it
without sending. Disconnect pauses dispatch until fresh person confirmation;
there is no automatic delivery after reconnect. Pending approvals/login/trust
must be resolved through their own surface, never by draining the Chat queue.

## Durable delivery receipts and recovery

Proposed metadata: opaque operation ID, actor/device/channel, binding/run,
canonical request digest, attachment IDs/digests, writer/authority epochs,
monotonic status revision, timestamps, safe reason, provider request/item/turn
references and the last proven submission boundary. Store the receipt before
writing anything. Transport operations use this same ID and bounded broker-side
deduplication/fencing; generic reconnect retry must not replay mutations.

Use a canonical schema-versioned digest of the exact normalized bytes and ordered
attachment descriptors plus binding. A repeated ID with the same request returns
the original receipt, never another write; another body/run/actor is refused.
Allocate/register an operation before sending, so an expired/deleted operation
returns gone/unknown rather than becoming a fresh request. Retain deduplication
tombstones for the retained session; after archival/purge require a fresh operation
registration and never resurrect an old queued send. Main/broker crash recovery
queries evidence; it does not infer exactly-once execution from JSON-RPC IDs.

| State | Proven boundary | Actions / visible wording |
|---|---|---|
| `queued` | Durable record; no provider bytes submitted | **Not sent — queued**; cancel, expiry or explicit dispatch after fresh checks |
| `submitting` | Durable claim; bytes may cross, acceptance not yet proven | **Sending**; disable duplicate Send; query receipt after reconnect |
| `accepted` | Qualified correlated provider acceptance for this operation/run | **Accepted by Claude/Codex**, with evidence kind in details; not a claim of task success or model comprehension |
| `failed` | Proven pre-write refusal, or definitive whole-input rejection with known boundary | **Not accepted**, safe reason; draft retained; deliberate new operation when safe |
| `uncertain` | Possible bytes/submit/acceptance, but no conclusive evidence | **May have been sent. Check the session before trying again.** No automatic replay or requeue |

Allowed normal transitions: queued → submitting → accepted/failed/uncertain;
queued → failed for cancellation/expiry/refusal. Reconnect finds unresolved
submitting → uncertain. Uncertain may later become accepted or failed only with
conclusive evidence for its **original** run; a person's acknowledgement of risk
is not proof of failure. No accepted/uncertain receipt transitions back to queued.
Late duplicate evidence is idempotent. Conflicting evidence produces an integrity
notice; it must not overwrite an established accepted receipt as not sent.

A transport timeout after a write is uncertain, including a partial write, lost
Enter acknowledgement, lost protocol response or main crash before durable
acceptance. A provider rejecting one tool after accepting a prompt is still an
accepted input; show task/tool failure separately. Never retry payload or Enter
on a heuristic. Existing team Enter-only retry is not broadened by this design.
Cancel applies only before the queued→submitting CAS and has a status revision;
a late cancel returns current state. Stop is a separate existing action and does
not retract accepted bytes. An explicit new send after reviewing uncertainty
gets a new ID and duplicate-risk confirmation, retaining the old receipt.

Historical team `typing`/`submitted` map to submitting boundaries; acknowledged
records preserve their original evidence label. An old screen-cleared
acknowledgement does not become a newly qualified Chat accepted receipt.

### Provider-specific acceptance qualification

| Transport | Candidate acceptance signal | Insufficient / refusal rule |
|---|---|---|
| Claude TUI 2.1.308 | After qualified prompt gates, a committed native user UUID and successful correlated submission hook, scoped to the pinned run and pre-send watermark | UserPromptSubmit hook invocation alone may precede a denial. Echo, cleared editor, working spinner or matching old prompt text is insufficient |
| Codex TUI 0.163.0 | Qualified post-submission user item/turn evidence plus allowed prompt-gate result, correlated to exclusive operation and current run | Generic event_msg, pre-submit hook alone or assistant text does not identify acceptance |
| Codex app-server pilot | Correlated successful `turn/start` result with expected thread and returned turn ID; verify emitted user item corresponds to the sent input | JSON-RPC request ID is not a durable provider idempotency key. Response loss is uncertain unless a qualified query proves that turn; turn completion is task outcome |
| Claude stream-json pilot | Version-qualified replayed user message carrying the supplied operation/user UUID and session, plus proven prompt-gate outcome | `--replay-user-messages` alone promises acknowledgement, not guard success or model execution; `result` without input correlation is not enough |
| ACP pilot | Version-qualified own user-message update/admission evidence with session/request correlation | `session/prompt` completion is not generic early admission; updates may omit correlation. If only a terminal response conclusively ties to the sole request, wait for it; missing evidence remains uncertain |

For TUI correlation, hold exclusive submission ownership, capture the run-scoped
pre-write watermark, and validate the exact submitted-byte digest plus unique
native item/hook reference. Deliberately identical prompts, delayed old hooks,
compaction gaps and a concurrent human send must not match by body alone. Do not
insert invisible correlation tokens into the person's text. If the provider
cannot supply sufficient evidence, do not enable that path. Same-UID agents can
forge hooks/store content: acceptance evidence is operational, not cryptographic
provider authorship or stronger account isolation.

## Literal text and multiline

Initial defaults from the approved design: **8 KiB UTF-8 text**, CRLF normalized
to LF, intentional LF/tab/Unicode preserved, no silent truncation. Reject NUL,
ESC, DEL, C0 except LF/tab, C1 and bracketed-paste escape delimiters; bound before
normalization. Bidi/formatting controls need escaped preview or explicit refusal,
not executable UI. Validate the exact normalized text whose digest is recorded.
Initial Chat refuses command-mode prefixes `!`, `/`, `#` at the first non-space
character, including each logical line where the provider can interpret them;
link to Terminal. Do not turn Chat text into slash commands, shell mode or a
permission response. Keep protocol input a typed user message; never accept
browser-selected control methods or provider configuration overrides.

The PTY adapter must prove its actual editor honors bracketed paste, delivers
literal multiline content as one input, and uses one separate qualified submit
key. Do not infer paste support from xterm. No per-line Enter simulation, string
shell interpolation, temporary global tmux paste buffers containing prompts, or
clearing shortcuts. If single-line and multiline need different evidence, expose
independent capabilities. Busy-turn steering and interrupt/cancel RPC are disabled.

Full and companion Chat share one composer/draft/receipt model per page; only one
is rendered. Drafts use P2's person-scoped unsaved-work registry, with acknowledged
persistence and honest volatile/quota states. A restored draft never submits.
Enter adds a newline; Ctrl/Cmd+Enter and the labelled Send button submit only
outside IME composition (including keyCode 229). Other terminal shortcuts remain
local. Preserve draft/attachments on conflict, error and layout changes; clear
unauthorized content on sign-out/revocation. P1's reload-only layout choice and
P2 observer rule remain unchanged.

## Attachments: availability before acceptance

Use the approved limits: **4 MiB/file, 16 MiB/message, four files, 16 megapixels/
image, 256 MiB retained per session**, with per-person/account concurrent-upload
and byte quotas. Permit validated PNG/JPEG/WebP, UTF-8 text/Markdown/CSV/JSON and
opaque PDF; raster diagram exports are images. Reject SVG/HTML, executables,
archives, directories, links and mismatched types. Stream count decoded bytes
before allocation, enforce image dimensions without unbounded decoding, and use
a bounded worker if decoding is needed. No main-side PDF conversion, OCR, active
preview, remote URL fetching or new hosted upload service.

The browser supplies file bytes plus an escaped display name, not a destination.
Main authorizes and resolves session → enrolled node → account → actual private
worktree → execution mount mapping. The account broker stages through pinned
no-follow descriptors, no-clobber, private directories/files (0700/0600), generated
ASCII names and opaque attachment IDs. Proposed location is the approved
`.rynhult-chat/<session-id>/<attachment-id>/file.ext` beneath the session's writable
worktree. Establish/verify a local Git exclusion without overwriting existing
rules; refuse staging if this cannot be done safely. Git ignore is not secrecy
against an agent, another authorized workspace reader or backups.

Attachment metadata binds actor, session, run, node enrollment, account, worktree/
container generation, generated path identity, size/type and digest. Files are
logically immutable after upload; a replacement requires another ID. Verify the
same approved regular inode, size/digest and readable execution path immediately
before submission. Refuse hard-linked/unexpectedly linked sources, symlink or
mount swaps and changed roots. Never accept an ID from another session/person as
a path capability. Shared workspaces retain existing same-account access limits.

| Execution placement | Correct mapping | Failure behavior |
|---|---|---|
| Local ordinary session | Agent-account staging path under its real worktree, not main's cwd | Permission/quota/root change refuses; retain browser draft |
| Remote node | Stage through the correct enrolled node/account; verify that node's receipt/digest | Offline is unavailable; no main-local temporary file marked ready or cross-node fallback |
| Container | Existing private worktree mount's in-container path, resolved from trusted generation/mount metadata | No host path leakage, owner-home mount, runtime socket mount or added broad mount; unsupported mapping disables attachment |
| Placement/run changes | Revalidate retained bytes and require explicit rebind/new attachment metadata for the new run | Never silently attach an old ID to the next run or another checkout |

For PTY, use only provider-version-qualified native image/file reference syntax
with the generated path. A generic “see this path” string is not native image
attachment acceptance. A text/PDF reference may instead be an explicitly labelled
**document the agent can read with tools**; enable only after its reference path
qualifies, retain existing tool permissions and distinguish it from native model
file input. Do not quietly replace an unsupported image/PDF with text or send it
to a different service. For protocol ownership, use a qualified `localImage` or
provider content block and its actual execution path/bytes. Codex local-image
documentation does not establish native PDF support; Claude/ACP content support
must be qualified for the chosen bridge/model separately.

Lifecycle: local draft → uploading → staged/verified → reserved by receipt →
reference submitted → provider reference acknowledged. Failure/expired/removed
are separate attachment states. **Uploaded**, **Reference sent**, **Accepted by
provider** and **Agent read file** are different observations; do not claim the
last without specific evidence, or ever claim comprehension. The operation digest
covers the ordered attachment set. File staging success never accepts the message.

Unsent unreserved uploads expire after 24 hours. Accepted and uncertain references
stay for the retained session; active provider reads and unresolved receipts pin
cleanup. Refuse quota exhaustion rather than deleting earlier sent files. Cleanup
is descriptor-confined and audited; backups may retain removed bytes. Preview/
download uses session-authorized same-origin endpoints with renewed person/device
admission, MIME allowlist, nosniff and private/no-store responses. Serve documents
as downloads; never execute agent-supplied SVG/HTML or expose filesystem URLs.
Same-UID agents can still replace or alter accessible files between checks and
provider read; these are integrity checks, not a hostile-agent filesystem sandbox.

## Approvals belong to people

A pending native permission request is a distinct control operation, never a
Chat message. For TUI sessions, open the existing Inbox/Terminal approval flow;
normal Send is blocked. Do not type “yes”, Enter, a suggested command or a password
from transcript content. Login/trust prompts and hook-trust review stay in their
existing person-driven flows. An AI lead/helper cannot answer or delegate a
pending permission prompt, regardless of what its output claims.

A protocol pilot maps an authenticated provider request to one durable pending
approval containing binding/run, provider request/tool/turn IDs, bounded redacted
action/path/scope, available choices, policy revision, expiry and status revision.
Only an authorized person or a **human delegate explicitly granted that exact
approval scope** may decide. Delegation uses existing permitted roles plus an
explicit bounded person grant; it does not give the lead or arbitrary group
members approval rights. If such delegation is unavailable, route to the person
with existing approval authority. Decisions are single-use CAS operations,
reauthorized after awaits and immediately before the provider reply.

The broker translates a decision only to the still-pending correlated request.
Unknown methods, changed tool inputs, stale IDs, expiry, Stop, run replacement,
revoked device/delegate or disconnect invalidate it. Lost acknowledgement makes
the decision unresolved/uncertain; query the request, never replay a granting
response blindly. Timeout/disconnect never means allow. If no qualified deny
response exists, keep input fenced and stop/pause under existing authority;
never unblock by granting. One-time consent cannot become session permission,
“always allow”, configuration updates, wider roots or network access. Initial
protocol approval support offers deny or one-time allow only within the frozen
grant; broader choices stay unavailable.

Codex command/file/permission request families and Claude `can_use_tool` control
messages require separate schema adapters; ACP option IDs are not generic allow
strings. Claude's proposed `--permission-prompt-tool stdio` must be proven on the
pinned CLI or provided through a pinned official SDK path with equivalent
semantics; do not guess the framing from a third-party example. Keep stdin and
control dispatch alive while hooks/permissions/background tasks need them. A
turn's final result is not permission to close that channel. No LLM callback,
transcript keyword or convenience auto-approver returns the human decision.

## Guardrails, resume and compaction

All modes reuse the frozen compiled launch specification: instruction sources,
selected skills/agents/MCP servers and their pins, trust policy, vault delivery,
permission/sandbox settings, workspace roots, network/container restrictions,
hook commands and guard-file integrity. Protocol fields for cwd, sandbox,
approval policy, extra tools or permission updates are server-derived and cannot
broaden those settings. Never use permissive flags or disable hooks to make
headless mode work. Existing explicitly approved native permission policy is
preserved; Chat adds no AI-generated answers to requests requiring a person.

Qualification must exercise an actual denied PreToolUse action in each transport,
not merely assert that a hook path exists. Repository settings, CLI upgrades,
resume, child helpers and protocol callbacks must not disable/replace the guard.
Missing expected hooks, failed guard verification, unsupported hook-trust format
or changed launch digest disables input, with a person-visible reason. Run no
real guard/request tools during qualification outside isolated test fixtures.

Resume is a distinct authorized lifecycle action after old-process death/fencing.
It creates a new verified SessionRun even when the native conversation ID stays
unchanged. Seal old queued/submitting operations (failed if no bytes; otherwise
uncertain); retain accepted receipts and historical attribution. A reconnect
alone never resumes or sends. No two producers own the same conversation at once;
if prior ownership cannot be disproved during a partition, refuse the new owner.
Existing idle auto-resume policy cannot replay an unresolved Chat operation.

Compaction may preserve native conversation identity while changing history/source
epochs. Preserve accepted receipts independently of the mutable transcript and
show a history gap/reset; do not infer rejection from a missing compacted user
item, or reconstruct a duplicate input from a summary. Correlate live pending
operations only with qualified post-watermark evidence; ambiguous compaction
leaves them uncertain. Protocol resume/load/compaction behavior needs its own
fixtures, not assumptions from P3a disk history.

Changing transport for an existing TUI is a separately reviewed, explicit action:
stop/fence old producer, prove exit across the node boundary, preserve native ID
and frozen policy, show the new process/terminal behavior and create a new run.
If preserving history or restrictions cannot be proven, refuse transition. Never
launch stream-json/app-server/ACP beside it to imitate an unchanged session.

## Proposed implementation interfaces and bounds

These are design interfaces, not registered endpoints or granted capabilities.
A schema review must choose a migration when implementation starts; none is
reserved here. Reuse the shared arbiter/receipt store rather than duplicate team
and Chat writers. Keep sensitive payloads out of ordinary events and logging.

| Interface | Contract |
|---|---|
| Capability query for a session/run | Per-operation flags plus safe unavailable reason; actual provider/CLI/adapter/transport, policy, placement and generation checks; never provider-name-only enablement |
| `POST /api/sessions/{id}/chat/messages` | Explicit operation registration/submission; expected run/control revision, text, ordered attachment IDs, explicit queue preference; no arbitrary RPC/config/path fields |
| `GET /api/sessions/{id}/chat/operations/{operation_id}` | Reauthorize receipt lookup; reconnect queries this, never resubmits blindly; expose safe state/version/evidence, not raw hooks or request bodies |
| Queued-operation cancellation | Expected receipt revision and queued-only CAS; response distinguishes cancelled-before-write from too late |
| Attachment upload/status/remove/preview | Scoped streaming upload, node/account verification, state/digest, pin-aware cleanup; no arbitrary main-side path reads |
| Person approval decision | Existing approval authority with expected approval revision, one-shot scoped decision and current run; never a general Chat input payload |
| Broker run-bound submission/query | Session/run/authority/writer epochs, immutable request digest, operation ID and bounded literal/typed payload; no generic retry; refuse stale/unsupported brokers |

Initial qualification defaults: 8 KiB text, approved file limits above, one queued
Chat input/session, at most 32 in-flight Chat submissions installation-wide and
one per session/run; start admission at 30 sends/person/minute, no unbounded
waiting tasks. Receipt-status reads are separately bounded so exhaustion does
not prevent recovery. Bound broker I/O, protocol record size (initial 1 MiB
control frame), pending approvals (initial 16/run), event queues and acknowledgement
wait (initial 10 seconds before uncertain). Provider computation can take longer:
only admission is timed out. Large image bytes must use a separately bounded
qualified payload path, not bypass the control-frame limit. Numeric defaults
require measurement; saturation refuses visibly without deleting drafts.

For queued text, stage the exact normalized bytes in a private, generated
run-relative broker-runtime file (0700 directory/0600 file), not main's database,
command argv, provider transcript or project Git tree. The broker returns an
opaque payload reference and digest; main stores only that reference and receipt
metadata. Reopen with descriptor-relative no-follow checks and verify size/digest
before dispatch. A changed or unavailable staged payload fails before write;
recovery must never reconstruct it from a preview or redacted history. Staging
has the same account/node/run binding and quotas as submission. This is private
account-local storage, not encryption or protection against the same UID.

Do not reuse MessageDelivery's vault-redacted body as the exact Chat payload,
because redaction changes the person's text. Compute deduplication over submitted
bytes privately; redact displays/diagnostics, never silently rewrite what is sent.
Erase transient text payload copies once dispatch no longer needs them; uncertainty
requires retained IDs/evidence and attachment pins, not a replayable text queue.
Audit lifecycle actor/session/run/operation, counts, evidence category and result;
never prompt/file bytes, names, raw errors, credentials or full tool arguments.

## Phased plan and qualification gates

All rows below are **future acceptance requirements**, not tests executed here.
Every enablement record must name exact CLI/adapter/protocol versions, launch
specification, OS/node/account/container scope, fixture provenance and outcomes.
Version drift disables the affected operation until requalified. Gate each of
text, multiline, image and document input independently; a model's modality
support and a transport's file acceptance are separate capabilities.

1. **Arbiter and receipts first.** Extend Terminal/team delivery with run/epoch
   fencing, explicit handoff, durable operation registration/query and crash
   recovery. Retain existing refusal/no-retype tests. Old brokers fail closed;
   no composer enables until this layer passes independent review.
2. **Claude TUI text candidate**, then **Codex TUI text candidate**. Capture native
   exact-version idle/editor/prompt/acceptance fixtures in disposable homes with
   fake providers. If empty-state or acceptance evidence fails, leave that path
   disabled rather than weakening the contract. This order is an engineering
   default based on existing preassigned Claude IDs, not an owner mandate.
3. **Multiline, then attachments**, per provider/version and placement. Add one
   bounded composer in both presentations, truthful receipts, draft protection,
   and the person-default Chat tab only when a qualified writable path exists.
   Preserve Terminal-first defaults until the person changes their preference.
4. **Separate protocol-owned pilot** only after the shared-process Terminal
   presentation decision. Compare pinned Codex app-server and Claude stream-json
   plus stdio permissions; consider ACP only with a named pinned implementation.
   Prove frozen-policy parity, consent and resume before exposing a launch choice.
   Rollback disables new submissions, preserves receipts/uncertainty/attachments,
   leaves original TUI sessions unchanged and never auto-converts a protocol run.

| Gate | Claude Code 2.1.308 | Codex 0.163.0 | Required common proof |
|---|---|---|---|
| Q1 text / acceptance | TUI editor, hook decision and committed user UUID; protocol replay UUID candidate separately | TUI user item/turn evidence; app-server turn/start candidate separately | Double-click, deliberate identical prompts, delayed prior hook, stale/wrong run; no echo-as-acceptance, exactly one submission |
| Q2 multiline | Actual bracketed paste/one-submit and literal content; protocol user content separately | Same for Codex editor and app-server typed text | UTF-8, tabs/LF/CRLF, IME, maximum/over-limit bytes, controls, paste delimiters and command prefixes; no split turns or shell commands |
| Q3 attachments | Native TUI references vs qualified structured image/document blocks, separately | TUI syntax vs app-server localImage; PDF stays tools-only/unavailable unless proven | Two people/two nodes, node offline/reconnect, account/worktree/container path, swap/link/mount, MIME/dimensions/quota/disk-full, unreadable/mutated bytes; never main-path fallback |
| Q4 permissions / guards | TUI prompt refusal; stream-json/stdio framing, pending cancellation and denied guard | Hook trust/forced hooks, command/file/permission requests with scoped one-shot decisions | Actual denied tool, project override, stale/revoked delegate/device, expiry, wrong request/run, malicious fake approval output; AI never grants |
| Q5 lifecycle | Resume same native ID after exit, stream lifetime/background hooks, compaction | Resume thread after producer exit, turn evidence/query loss, rollout compaction | Kill main/broker/provider and partition node before/after claim, byte, submit, acceptance and durable acknowledgement; no replay, no concurrent owner, late evidence safe |
| Q6 shared views | Both Chat layouts with original Claude Terminal | Both layouts with original Codex Terminal | Two browsers + terminal/team races, pending frames/IME, partial human input, take during awaited checks, stopped/expired helper, no stale input/resize; P2 observer on restore |

Use deterministic fake broker/provider barriers for crash/write/ack races and
versioned native fixtures for actual provider behavior. Fakes alone cannot
qualify a CLI protocol. Root-free disposable tests must deny outbound traffic
except their fake loopback model service and use empty homes/configs/credentials;
never run Rynhult tools against the live instance. Start no installation or
service on a real node for this design. Owner-run platform qualification follows
separately, including actual TUI/clipboard/IME, hooks, permission dialogs and
provider/model attachment access.

Browser acceptance belongs to implementation: focused specs one file at a time
at 1440/390 px, keyboard/IME, light/dark, zoom/reflow, live announcements, draft
persistence/quota failures, screen-reader follow-up, offline/error/uncertain/
permission states and no horizontal overflow. Count process/run/socket/writer
identities across full/split/tab/layout moves, not only screenshots. Map Q1/Q2/Q6
to S08/S10 and AC-025/028/029, Q3 to S11/AC-030, and history/compaction to P3a's
S09 gates. S12/model/effort and AC-031/032 stay P3c; worker read access never
becomes authority to send or approve in that worker's session.

## Review outcome and unresolved risks

Ready for design review; **NO-GO for enabling writable Chat from this document**.
The owner-approved feature remains the goal; no new owner answer is required to
implement shared arbitration or investigate the first PTY candidate safely.
Native TUI empty-editor and acceptance correlation are the principal release
risks; if they cannot be established, first-slice sending stays off. Protocol
ownership improves transport structure but leaves Terminal parity, special
Claude stdio framing, pinned guard behavior and per-provider attachment semantics
to prove. Do not solve those gaps with screen optimism, an AI permission callback,
a second conversation, a silent mode conversion or an uncertain retry.
