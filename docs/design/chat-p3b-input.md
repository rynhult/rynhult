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

## Approved writer-only take-over amendment (2026-11-11)

For the relay's Codex single-writer mode, Josh replaced empty-draft retirement:
at idle explicit take-over, retain the previous view's draft visibly in its
permanently read-only client and create a distinct fresh input client on the same
app-server/thread. Never copy or replay drafts/keystrokes. Fence old byte/RPC
routes permanently before issuing the new grant; never reactivate a retired
client. Busy/unknown submissions still refuse, and retirement does not fabricate
processing. The native/fresh-client proofs remain required; this approval does
not enable the feature. The current Codex composition uses separately verified
owned TUI/Host generations: retire the old Host irreversibly, detach only its
input bridge, retain its native read subscription/draft, then verify the fresh
client's own peer and history bootstrap before issuing the new writer grant.
Retirement never advances processing. Retained clients are bounded per run;
capacity exhaustion refuses transfer rather than evicting an old draft. Late
Send replies also bind to their original Chat composer generation, so they cannot
clear identical text in a new composer. Qualification limits remain in the M3
release ledger. This amendment supersedes empty-editor requirements in
item4 below **for explicit relay writer take-over only**, not legacy typed
injection or Claude L1's separate verified process exit.


The gated browser Terminal keyboard binds each input generation to the person,
connection, attachment, run, holder and control revision captured on a deliberate
Connect click. Reads, focus, reconnect and restored preferences never claim input.
A changed authority or unreadable native screen permanently retires that browser
input; recovery requires an explicit new idle take-over and a fresh empty client.
Retain even locally buffered unsent characters in the old read-only draft. Never
flush them after retirement, retry unknown writes, reuse a claim or rebind delayed
replies to the new generation. At most16 retained browser generations are kept;
capacity refuses a new input instead of evicting drafts. Closing the page remains
subject to the existing dirty/uncertain working-view warning.

The initial desktop keyboard profile is ASCII append-at-end, Enter and Backspace;
no paste, IME, slash/bang/help commands or rich native controls. A4096-character
local draft,512 queued frames and one in-flight write bound buffering. Enter
waits600ms after prior typing delivery for the pinned stock client's native burst
boundary, then checks retirement again. A transport ACK proves only admission.
Only the causal native processed watermark may clear the submitted draft; another
submission additionally requires a fresh post-processing idle read. Public writer
qualification, feature default and terminalMediated remain false pending the full
release ledger, including the actual browser/native/new-session proof.

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
4. At an explicit idle take-over, pause the previous client's input and retain
   its unsent draft in that read-only client. The new writer gets a fresh empty
   input on the same session and conversation. Josh approved this rule on
   2026-11-11; it replaces the earlier requirement that the old draft be empty.
   Positively establish provider idle, no unresolved IME/preedit or input writes,
   and the old frame processing watermark. Fence the old writer epoch and prove
   its draft is retained read-only before granting the new writer. A nonempty
   draft is allowed with that proof; unknown preservation or processing blocks
   handoff. Never clear the old draft, invent editing keys, transfer it into the
   new input, submit it, or replay/type anything automatically. Returning to the
   previous view does not restore its retired input authority or replay its draft.
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

Josh's November12 R1 decision: crash recovery is an explicit **Recover into a
new session** action. It opens the normal reviewed new-session form with no
copied prompt, native input, approval or receipt. The person chooses and launches
that separate session. The old session's output, receipts and decisions remain
readable; querying original proofs may settle its original receipts, but cannot
restart the old provider or replay an operation. Same-public-session automatic
re-enrollment is later work, outside R1. Existing dormant same-session recovery
internals are not a qualified R1 recovery interface.


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


The owned input-only tmux bridge consumes its duplicate framed output with one
owned reader task, while the authorized screen observer supplies rendered output.
It cannot infer native processing or owner exit from those bytes. Unexpected
stream EOF fences further input with unknown execution state; detach cancels and
joins the reader before reclaiming the bridge. This prevents unread tty output
from applying backpressure while preserving the separate causal receipt barrier.


Claude empty-conversation target preflight: pinned native CLI initialization alone
does not create resumable history. Refuse a known-empty Terminal target before
stopping the current SDK owner, then recheck authorization after the awaited
preflight. Preserve its run/process/native ID and allow a real person Chat turn;
never seed history or submit an artificial prompt. This safety refusal is not
positive empty-session resume qualification. Whether first real Chat input may
be a product prerequisite for Terminal is pending Josh's decision (G1.27); no
release requirement is waived and Claude enabling remains blocked.


Public Codex enrollment reserves the initial Chat actor but grants no public
attachment input. Every public writer, including initial Chat, requires an
explicit person take bound to that attachment. A matching person alone cannot
make another tab/reloaded document writable. Observer reads and attachment
negotiation never acquire or restore writer authority; the person takes again.


An owned input bridge can outlive its native TUI peer. A successful tmux write
therefore cannot establish that the client is alive. On every owned Codex input
boundary, recheck the exact authenticated view bootstrap/process after awaited
grant validation and before frame write. Confirmed client exit refuses input and
fences uncertain stream state without advancing processing or replaying draft.

A person-initiated Terminal Connect focuses its active field only after the field
commits and is enabled by current authority/screen/readiness. A delayed claim or
pending receipt must not direct typing to document shortcuts; respect deliberate
focus moved elsewhere. This focus handling grants no input authority.

Claude L1 native background completion is not qualified on the pinned TUI.
Refuse explicit run_in_background before tool execution; an unexpected native
backgroundTaskId result latches a bounded unresolved background entry. Stop is
not completion, and unrelated task hooks cannot clear it. Idle handover remains
unavailable until qualified completion or explicit authorized Stop/recovery.

Claude L1 prepare/commit/cancel must consume the caller's registered operation
identity and full digest; no shadow commit registration. A bounded server-held
prepared plan preserves its source authority and authenticated attachment identity.
Commit settles only after verified old exit/new launch; pre-stop refusal or explicit
authorized cancellation restores the source without replay or a new provider.
Public switching remains unavailable until HTTP attachment/new-run scope and native
browser integration qualify. Registration alone never grants that capability.


### Dormant M3 Claude public binding —2026-11-12

Claude public negotiation/enrollment remains behind a separate false qualification
gate in addition to the feature flag. Explicit person new-session opt-in may use
the owned L1 launcher only with a vault API-key grant or existing approved
subscription consent. Preview constructs pinned SDK flags without connecting;
installation readiness is a read-only broker-local hint, never runtime capability
or Terminal mediation attestation. The account broker resolves/pins its configured
CLI path; main cannot assume a remote path or symlink layout. Enrollment grants
no input capability automatically.

Registered switch prepare/commit/cancel binds the original authenticated source
attachment through the verified owned checkpoint. The new execution owner keeps
the same revocable cookie/device authority without the retired source attachment;
all new input still requires a fresh attachment and separate input grant. Accepted
lost replies are observed from the original durable receipt without redispatch.
Post-stop failures stay sealed for explicit recovery with uncertain receipts and
no automatic resume; source/target cleanup requires whole-tree exit evidence.
Explicit authorized pre-stop cancel remains possible when availability is off.
Native public new-session/browser, honest selector facts, native decision
application and the remaining release gates require their recorded scoped proofs.
Josh resolved the first-message question on November12: Claude Terminal becomes
available only after the first real Chat message. Before that its control explains
that it is waiting, selects Chat and keeps the running Chat owner untouched. A
queued registration or unsent draft does not satisfy the prerequisite. A qualified
provider-admitted turn updates this presentation hint; idle handover and the
broker's materialized-history preflight remain mandatory. Never bootstrap native
history with a dummy prompt or silently replace the conversation.


### Dormant Claude decision review binding —2026-11-12

Public Claude decisions require an independent false-by-default qualification
gate. The existing hub reserves actor/run/digest/CAS and resolves choice effects
from server-held data; actual attachments/cookies/devices are rechecked at native
release. A pending decision for the caller-owned current-run active operation may
be reviewed while that exact turn awaits its final receipt. Other unresolved
operations or previous unknown answers still block review. The presentation's
active operation reference is actor-private and separate from native turn IDs.
Native/socket response alone never means the decision applied: unknown receipts
stay unknown and cannot be replayed. Scoped API-key desktop native card/application proofs are recorded in the G1
ledger; unproven profiles and release integration keep all rollout gates off.

### Claude Inbox review projection (G1, rollout still disabled)

Authenticated Claude native-hook and SDK requests publish bounded complete tool
input and the reviewed working directory for the existing Inbox approval card.
Main redacts before retaining the display facts and joins them to the exact
hub decision and run; an old or unauthorized session cannot supply a current
card. Missing, malformed or oversized complete input permits denial only.
The Inbox card is display evidence, never an input grant or a legacy permission
token. A person's answer creates a fresh relay attachment and one registered
`decision.reply`, with the same hub CAS and post-await checks as session review.
Transport delivery remains an unknown native outcome until conclusive correlated
application evidence exists. Rollback permits denial only; no answer is replayed.

The scoped native Claude Terminal denial observer may settle the original answer
only from its unique invocation marker, exact action/tool identity and completed
provider chain under the pinned owned transcript boundary. It retires only the
conclusively denied tool entry; Stop and permission socket closure do not clear
tool state. Missing/ambiguous proof stays unknown. SDK/Allow and bounded late
observation use the additional evidence rules below. No rollout or attestation.

Review facts must reproduce the exact native action digest before redaction;
changed or unbound complete facts cannot grant and are not presented as the
request's command. Claude's generic session review permits denial and directs
granting review to the detailed Inbox card. This tightens presentation without
changing hub CAS, person scope, native permission policy or rollout gates.


### Claude application and installed guard composition — G1.50–53

A one-time native Allow returns only the allow behavior. It neither echoes
unchanged tool input nor updates permission rules. The broker binds a unique
same-prompt authenticated PreToolUse action/tool ID before releasing the answer;
matching PostToolUse and a completed provider chain prove application. A typed
SDK query instead requires exact ToolUseBlock/input digest, matching typed tool
result and successful same-session ResultMessage; denial requires its private
invocation marker. Ambiguous, failed, incomplete or wrong-query evidence stays
unknown. Settlement always uses the original server-retained actor/run/action/
full digest and server-selected effect.

Chat and Terminal have separate frozen settings: exactly one reviewer per owner.
Chat uses the SDK callback, Terminal the native hook; the known legacy permission
socket sink is excluded. Unknown custom PermissionRequest composition refuses.
A read-only observer may reconcile the original receipt for30s while the same
owner/run/peer remains valid (at most16 tasks). It cannot answer again, claim
input, replay keystrokes or restart a provider. Close/replacement, ambiguous proof
or window exhaustion leaves the outcome unknown. Cross-restart recovery requires
separate G5/G6 integration.

The native PreToolUse relay invokes the configured installed acc-guard before
recording tool admission. Its executable and settings reader are pinned under
root-owned safe ancestry or a kernel read-only mount, with pre/post identity
checks, a secret-minimal environment and bounded execution. Refusal is an actual
native tool denial and creates no outstanding admitted-tool ledger entry. Remove
only the exact known parallel ordinary guard hook; preserve other frozen rules.
Legacy spool hooks retain enrolled-session event history but cannot change its
native ID, lifecycle, owner or decision authority: late source SessionEnd must
never end a replacement owner. These rules do not qualify live deployment or
bootstrap capabilities; all feature/public/Host/socket gates and terminalMediated
remain false until consolidated release evidence is complete.


G1.54 ownership observation: an operation lock acquired by same-owner native
registration during an idle read means busy, not lost authority. Return non-idle
facts with uncertain_operation while preserving the authenticated existing read
binding. Sample activity before idle awaits, then recheck it after authorization.
Reconcile Terminal activity again in main after native inspection: frame admission
can precede the native hook's durable turn registration. A changed input watermark,
in-flight frame lock or changed durable revision discards the old idle sample and
returns a non-idle snapshot. These activity facts never replace fresh actor/run/
policy/placement/fence checks or mint input authority.
Actual controller replacement/fence/closure, revoked actor/device, stale run,
policy/placement loss and failed transport still refuse; permanent input retirement
on lost authority is unchanged. No idle read can mint/renew input or replay drafts.

### Frozen Claude relay metadata boundary

The disabled exclusive Claude relay freezes a mandatory native shell sandbox,
with no unsandboxed fallback or excluded commands. Shell tools cannot modify
native conversation metadata or the run policy; native file tools receive Edit
deny rules for the same paths. The native owner can still append its own history.
Both SDK and TUI exclude mutable user/project/local settings scopes and use a
strict empty MCP configuration. Selected external MCP writers are unsupported
until their separate write boundary is qualified. Settings and empty MCP bytes
are bound to launch integrity and checked before admission; tampering refuses.
This policy does not itself qualify a transcript admission reader, public native
input, subscription consent, or any advertised capability.

### One permission decision authority per native owner

The verified live SDK owner answers permission requests through its SDK callback.
Its native PermissionRequest hook delegates to that callback after fresh owner
authority checks; it creates no second card and grants no tool permission itself.
A native TUI owner keeps the hook decision path. Missing, stale or revoked hook
authority explicitly denies. SDK questions retain their original absolute
five-minute deadline across publication and final authority waits; expiry denies
the native tool and expires only an unanswered durable card. A previously chosen
answer with unknown outcome stays unknown and is never replayed or accepted from
an expiration notification. Public native TUI/Chat races and conclusive chosen
answer reconciliation remain separate qualification requirements.

Retiring a Claude hook channel invalidates its token/listener and denies pending
questions first. In-flight observations may drain for at most five seconds so
cancelling an old SessionEnd RPC does not fence the replacement owner's shared
authority channel. Final token checks prevent a queued allow or SDK delegation
from escaping retirement. Stuck authority still cancels and fences the channel.

Native TUI permission hooks retain their original absolute15-second deadline.
An unanswered timeout explicitly denies and publishes same-run exact-question
expiry; chosen unknown answers are preserved. A registered late answer refused
before dispatch becomes a terminal failed/expired receipt after fresh exact
authority/registration checks, so it cannot leave an unclaimable queue blocker.
An already submitted/unknown answer is never failed from that refusal path.

For Claude's strict text profile, main and the owned Host use the same pure
frame projection. Main validates the current transient prefix before reserving
a sequence: known unsupported controls refuse without poisoning a healthy input
stream. The prefix clears only on conclusive processing or invalidation; it is
never persisted or replayed. Partial/unknown transport writes retain the existing
fence.

At an explicitly authorized idle L1 replacement, a fully delivered text-only
unsent prefix after a conclusive native processing boundary may instead remain
in an immutable read-only old client. Main and the owned Host independently
capture its exact person, attachment, run, native conversation, epoch and frame
watermarks, and compare a private digest before the destructive checkpoint.
The admitted/processed inequality remains unchanged; the old draft is never
called processed or empty. Pending Enter, first-use uncertainty, partial writes,
changed projections and unmatched proofs refuse before stopping the owner.
Old client data is ephemeral and bounded to16 retained clients; it has no send,
input-token renewal or replay method. A replacement SDK/TUI starts with fresh
input on the same conversation and receives no automatic query or typing. The
old execution owner still requires its genuine whole-tree exit receipt. Public
retained-client rendering and L1 mutation qualification remain separate from
this native candidate; no public capability is enabled by these observations.

### Pinned command answer application

The disabled Codex0.167.1 command profile can reconcile an admitted one-time
Accept or Cancel using the exact original native request/thread/turn/item,
request resolution, choice-specific command status and turn completion. Accept
requires completed command/completed turn; Cancel requires declined command/
interrupted turn. A write, request resolution, arbitrary tool output or a matching
prompt alone cannot confirm application. Missing/conflicting lifecycle evidence
stays unknown. Other families/choices and failed-command outcomes remain
unqualified. The broker publishes evidence, never a new execution grant; main
checks the original actor, winner, run/binding and complete registered digest.
Late genuine evidence can settle the old receipt after revocation without a
response replay. An unanswered question may become cancelled only after its
exact native closure and turn completion; cleanup never cancels a chosen unknown
answer or attributes it to a person. Bounded read-only outcome records retain the
original attribution in both panes. This does not qualify stock approval keys,
Claude application acknowledgement or public L1 mutation/recovery.

Retained Claude native client data also has a bounded public read-only projection.
Fresh session read authority and exact person/device actor, current runtime/
binding and native conversation checks precede normal vault redaction. Export
only source run, view, text and readOnly; never the whole model, input grant or
proof. The previous draft displays separately from fresh Chat input and has no
send handler. A revoked cookie cannot read a cached draft. Reads neither claim
input nor start a provider. Public L1 switching, old Chat-draft retirement across
public run changes and explicit recovery remain separately unqualified.

Pinned Claude SDK Bash acknowledgement now has a separate, default-off qualification
gate. A chosen answer settles only its original receipt using exact current wire
request/tool identity, the recorded response and explicit successful native result.
Denial requires the exact native permission-denial pair and typed error tool result;
allow requires its typed successful tool result plus authenticated PostToolUse.
A response write, generic tool text, absent denial or whole-turn success alone
cannot acknowledge an answer. Cancellation, duplicate IDs and failed native results
stay unknown. Claude TUI and other tool families remain separately unqualified.

Public exclusive-idle L1 switching has a separate default-off qualification gate.
Explicit prepare/commit/cancel use the original registered operation receipts,
opaque plans, exact initiating attachment and fresh authority checks. Private plan
metadata preserves source authority for pre-stop cancellation. Native positive
idle and whole-tree exit checks still precede replacement. After a verified owned
checkpoint, ongoing owner lifecycle uses fresh person authority; old attachment
input scopes remain stale. Public result delivery validates the same login, person
and initiating attachment without renewing old input authority.

The public UI switches only on a person click. A run replacement retires unsent
Chat text into a bounded read-only local cache, separate from fresh input. It never
sends or replays that text. At capacity it requires explicit local discard before
more editing. Revocation clears cached private text. Native retained drafts remain
actor-private read-only server projections. Explicit failed-target recovery and
stock approval-key mediation remain unqualified; terminalMediated stays false.

Pinned Codex command Allow application also qualifies when the exact completed
command item reports failed with a typed nonzero exit code and its exact turn
completes, after exact native request resolution. This acknowledges permission
application, not successful command execution. Missing exit metadata, guard/
cleanup failure without that lifecycle, interruption or foreign IDs stay unknown.
Lost caller acknowledgement settles the original answer without replay.

Explicit failed-target recovery uses the additional gated `switch.recover`
operation: sessionId, expectedRunId, operationId, target, expectedRevision and
reason are required; its receipt needs HUB_COMMIT after verified new ownership.
The default-off recovery gate is independent of ordinary L1 direction gates.
Only a fresh authenticated person/attachment can register and claim this narrow
sealed exclusive-idle recovery. Fresh generation, placement, consent, auth mode,
budget and frozen binding checks precede every cleanup/start/owned checkpoint.
General sealed mutations and legacy resume remain refused.

Recovery first verifies complete old-factory cleanup, including every known
partial target's own tree receipt. An unknown fork cannot pass cleanup. Retired
factories/controllers never reopen: a fresh private factory incarnation and new
run resume exactly the same native conversation. No query, draft or answer is
replayed. The explicit person action selects Chat or Terminal and records the
complete review digest. Fresh input requires new-run negotiation and grants.
After positive cleanup, only the original failed never-owned switch/recovery
receipt may become conclusively refused; other unknown IO remains unknown.
A loss after durable owned commit seals that owner without downgrading its
accepted receipt. Another attempt requires a fresh reviewed operation, never an
automatic retry. Unknown cleanup leaves the session fenced.

The public recovery UI offers a target and bounded reason, then an explicit
Recover session click. It neither starts on render/tab changes nor retries an
uncertain receipt. The previous client draft remains read-only and the new input
is fresh. Recovery qualification and terminal mediation stay off by default.

Factory incarnation changes retain the same conversation-wide exclusive lock
and pinned directory. Recovery creates a fresh one-use recovered-run marker
under that lock; it never removes the original started marker. A competing
PrivateRuntime must still fail BUSY while the recovered owner is live. Stop/EOF
closes the retained current factory even if a failed recovery left the controller
pointing at the retired source. A completed close still requires every known
partial target's positive whole-tree receipt; unknown forks remain fenced.

Pinned SDK Read/Write/Edit normal one-off Allow and person Deny now use the
same exact native request/tool identity and original operation as Bash. Their
successful typed tool-result block can omit is_error; that is sufficient only
with actual exact block presence, authenticated successful PostToolUse and the
explicit successful native final report. Absence alone never acknowledges an
answer. Failed tool-body results stay unknown until separately qualified.
In the reviewed fixture profile Glob/Grep are neither advertised nor discovered
by native ToolSearch. Their unexpected SDK callbacks refuse before publishing a
card or granting input; no positive application support is implied. All global
qualification gates remain false.

SDK Read/Write/Edit Allow application also qualifies on a genuine tool IO
failure: require the exact typed error result, authenticated PostToolUseFailure
for the same live tool ID, explicit successful final permission report excluding
its denial pair, and exact current wire response. Conflicting success/failure
hooks, generic errors and missing metadata stay unknown. The original Allow
receipt means permission applied, never that file IO succeeded. Lost caller
acknowledgement resolves the original receipt without sending another response.

Pinned Codex file-change successful one-off Allow and Decline bind the exact
native request/thread/turn/item and an immutable bounded complete changes list
published before the question. The registered action digest includes both the
request and full patch; session grantRoot/amendments, missing or mutated review
snapshots and duplicates refuse. File Allow needs completed file item/completed
turn; Decline needs declined item/completed turn, both after exact request
resolution. Unanswered interruption closes only a winnerless question. Lost
pre-write response stays unknown; lost post-write acknowledgement reconciles
only the original receipt without replay. File Cancel remains unavailable:
resolution/interruption without a conclusive file item cannot prove that choice.
Failed apply outcomes also remain unqualified. No stock key or TUI acknowledgement
qualification is implied, and all production gates remain off.

File Allow application can also qualify on the exact native failed file item
and completed turn after request resolution. This acknowledges the chosen
permission, never successful file IO. Loss before response consumption stays
unknown. File Cancel remains unavailable: an invalid Cancel fails before native
response and leaves the same question pending for a fresh valid Decline review.

Pinned Claude TUI Deny application has a separate default-off qualification
slice. An exact chosen private hook invocation receives a fresh random denial
reference only after its original verified Hub CAS. Before returning that native
response, pin the generated/owned transcript inode and pre-answer prefix. Accept
only a new typed `hook_permission_decision` Deny attachment paired with the same
native tool-use ID and exact assistant tool block in a `permission-rule` tool result, the
exact prompt ID, exact hook rejection metadata and exact denial reference. A
protected native transcript is required; tool text, a hook/socket completion,
counts or a matching input are insufficient. Prefix mutation, replacement,
ambiguity, missing evidence and deadlines remain unknown. Publish application
only for the original chosen operation; no response replay. This qualifies no
Allow reference, stock key path or subscription account. Production defaults
remain false and terminalMediated remains false.


For concurrent native tools, the hook-decision attachment parent is an append
position and can follow a sibling tool's result. It is not invocation identity.
Require its explicit typed native tool-use ID, an existing owned parent node,
and the exact source assistant tool block/name referenced by the nonce-bearing
native permission-rule rejection. Cache that source context from the pinned
pre-answer transcript prefix; never join questions by identical inputs or counts.
The two-second foreground timeout now retains a read-only observer for at most
30 seconds (at most16 per runtime). It captures the original run/operation and
retries only idempotent native-application publication, including when a reply
was lost before or after main accepted it. It never repeats a hook answer or
reuses current-person authority. Foreground cancellation does not cancel the
observer; runtime close reclaims all tasks/readers. Missing, invalid or expired
evidence stays unknown. Across-restart/durable replay of evidence is unqualified.

The stock approval alias component is default-off and grants no actor or input
capability. A trusted owned route must separately consent to an opaque alias;
only that exact peer/route may return a one-time accept/cancel response. Consume
before awaiting its callback; foreign/scoped/duplicate responses never forward a
provider request ID. Production Host/main approval components are implemented behind independent
default-off gates. No public capability is enabled. The limited decoder qualification below does
not qualify public key mediation or application; modal rendering is not evidence
that Enter was decoded or applied.

Pinned stock decoder qualification is restricted to an already interactive
Codex0.167.1 basic command approval with empty composer and untouched default
selection:Enter byte0d returns accept;Escape byte1b returns cancel. The fixture
first proves and clears its own synthetic local edit before offering requests;
this is offline test setup, never a product input action. Startup protected
screens may discard input after rendering, so rendering cannot authorize a key
or imply readiness. Letters/numbers/navigation, LF, distinct modified-key encodings, bundles,
scoped/session choices and file keys are outside this qualification. The private
JSON alias response still supplies no authenticated actor, input grant or main
CAS/application proof. Public approval input remains disabled.

Private approval withdrawal takes the exact original immutable offer object and
its owned route. It removes the alias before authority/transport awaits, retains
its lifetime tombstone, then sends only that alias's serverRequest/resolved to
the same owned peer. A foreign or reconstructed offer cannot withdraw another
question. Consent-in-flight withdrawal prevents later publication. Transport or
authority loss never restores the alias or authorizes a repeated notification.
Notification delivery and native modal removal are client lifecycle evidence,
not provider application acknowledgement. Key names designate exact byte encodings;
legacy physical keys emitting identical bytes are indistinguishable. The decoder proof alone does not qualify main decision expiry/revocation/Chat
winner wiring or authenticated key tickets. Their separately qualified public
scopes are recorded below; all gates remain default-off.

G1.56 reader continuity: an expired run-bound HTTP ownership attachment permits
only reader reattachment after central person/role and current session-access
revalidation. Keep old mounted input permanently read-only; never replay or mint
input on reset. Genuine access denial still clears private content. Repeated L1
Terminal activation may retire only the exact proven-exited dead tmux pane using
pinned server/session/pane/PID and command-server deadness checks. A live, replaced,
unknown or unproved view cannot be removed. Same native conversation and existing
native permission/guard authority remain; all rollout/attestation defaults off.


G1.57 late proof observation: expiry of the30s fast observer is not permission to
resend an answer. While the same owner/run/peer remains, retain at most128 original
identity/source records. Existing owner inspection and SDK completion may schedule
up to4 fair one-shot proof inspections within16 tasks. Recheck owner/run/guard
trust after awaits; observed trust loss retires proof, while only original evidence
publication may retry after transient/lost acknowledgement. No new answer/query/
input/claim/restart is permitted. Owner replacement, peer loss and restart never
inherit this cache. Durable recovery remains separately qualified on integration.

G1.58 reader reconnect: an independently authorized screen observer must not
inherit the previous input attachment's lifecycle grant. Claude screen capture
uses the main-issued opaque read grant before/after process identity and capture
awaits, with frozen guard/owner/view/link checks; main revalidates scope and masks
output again. Detaching the old attachment does not revive its writer lease or
create another process. Input and stream opening still require their original
writer authorization. This qualifies idle, fully processed reader reconnection,
not persistence of unsent local drafts across browser navigation.

G1.63: an authenticated Claude whole-enclosure exited:true may persist a bounded
per-run Stop fact. Repeated Stop and a reconstructed coordinator may confirm only
that exact sealed run/binding/enrollment/epoch/producer identity and physical
placement, with a verified ended SessionRun. Missing/changed evidence refuses;
no runtime is never sufficient. This fact grants no launch/input/answer authority.
Admission policy/budget withdrawal does not invalidate physical exit; current
node/account/leader identity still must match. No migration or flag promotion.

G1.64: persist the original server-held permission-answer identity before releasing
it. A cold cache may recover original params/actor/full digest/binding/producer
identity for evidence correlation only; never recover a permission grant or resend
an answer. The map is bounded by4096 operation registrations. Missing/mismatched
records leave explicit uncertainty. Actual process-restart/unpublished-proof
recovery remains separately required; cache loss is not equivalent to restart.

Public stock command key admission uses one explicitly armed immutable alias.
Before sending bytes, main authenticates the actual frame RequestResolver and
captures InputGrant, actor, epoch, frame sequence, alias and exact CR0d/ESC1b
choice. It registers the complete original decision payload under a server-only
stock-frame-* key. Only that original resolver may dispatch the operation;
a same-person other tab cannot rebind it through the ordinary decision API.
The Host reserves its immutable writer route and ticket, then revalidates the
queued original operation after connection/held awaits immediately before write.
Native alias decoding must match the same ticket. Existing main decision CAS,
final provider admission and exact native command lifecycle evidence establish
the outcome. Delivery/decoding alone leaves processing unknown and fences input.
Only the exact accepted original receipt/digest can advance the processing mark.
Late application after revocation settles the original receipt without restoring
input authority. A read-only watcher withdraws the explicit alias on expiry or
lost login; Chat CAS withdraws before its provider dispatch. Registration in
progress is not invalidation; withdrawal during registration tombstones only
that unsubmitted operation. There is no automatic answer, key retry or replay.
Concurrent public command questions, post-byte failure/recovery, file keys,
Claude Allow identity and durable across-restart reconciliation retain their
separate qualification requirements. Gates stay false and terminalMediated=false.

Unsupported Terminal approval keys are a visible refusal. Pending decision cards
in both panes explain that file changes and other key combinations must use the
review buttons. Main returns only closed, fixed refusal messages for unsupported
stock encodings and for ordinary input blocked by an unarmed pending Codex review;
these say no key was sent. Refusal happens before operation registration and
before bytes. The pending-review check only blocks input; it never supplies an
alias, actor or choice. The mediated Terminal route and all release gates stay off.
### M3 durable evidence and conclusive Stop implementation (G1.68–69)

Before publishing conclusive Claude application evidence, the account broker
fsyncs a bounded immutable fact tied to the original actor, operation, digest,
run, full binding and producer fence. A current-authorized main may read that
fact through a separate opaque read-only account stream after main/broker restart.
It must recheck the original scope and current physical authority after awaits
in the settlement transaction. The reader cannot enroll, launch, submit or retry
a provider answer. Missing or incomplete facts preserve the unknown receipt.

Codex producer exit now uses the existing per-run subreaper enclosure around the
unchanged app-server executable and argv. Stop must retire every retained native
view and Host as well as the producer, with exact empty-tree receipts, before
acknowledging enclosure-v1. A root-only legacy reply, lost acknowledgement or
missing supervisor receipt cannot create a durable Stop fact. Repeated Stop may
reuse only the exact sealed run/binding/enrollment/identity and physical-placement
fact; changing provider or placement invalidates it. All capability and release
flags, including terminalMediated, remain off pending the remaining release gates.

Persistence cancellation must run the same unqualified-fact retirement as a failed post-await guard. Deletion serializes behind the outstanding writer before teardown completes; merely having completed a file write is insufficient when the final qualification check did not run.

Public native command outcome qualification also covers a permission Allow whose
exact command exits7, and Allow/Cancel permission response loss before or after
the write. A write attempt or native turn cleanup is not acknowledgement. The
before-write cases stay unknown even after Main's operation publication drains;
the after-write cases reconcile only the original receipt from exact native
application. No permission response or input is retried.

Two identical concurrent command questions are qualified with one explicit native
key ticket and an independent authenticated Chat review for the other question,
in either order. Each retains its original request, actor, operation and digest.
Revoking the key session after delivery permits original receipt settlement from
late evidence while input remains fenced; the other reviewer cannot borrow that
grant. This does not qualify multiple simultaneously armed native aliases,
same-question post-delivery expiry/Chat races, interrupted-owner recovery, files,
Claude Allow or durable reconciliation across broker restart. Gates stay off.

Native Claude Stop may precede its protected transcript flush. The owned input
Host permits at most one second of read-only observation for missing records,
under the original cursor, inode and prefix. Only the existing exact native user
and completed provider chain can settle the original operation. Wrong prompts,
contradictory chains or integrity loss refuse immediately; absent evidence still
poisons the boundary after that bound. Final input validation also refuses closed
or ended Hosts. A late original fact may settle after revocation, while processing
and input authority remain fenced. There is no resend, cursor reset or acceptance
from Stop alone.
The SDK adapter must fence commands and provider evidence when its supervised
process has exited, including before and after authority awaits. Orphan native
children can retain pipes after supervisor death; a connected pipe is insufficient
execution identity. Without an exact empty-tree receipt, retain the conversation
quarantine and unknown outcome rather than launching a replacement or certifying
Stop. The disposable native fault harness alone owns its deliberate orphan cleanup.

The native transcript cursor must retain the full validated accepted-history
prefix across inputs. A later begin cannot legitimize truncation or rewriting
of earlier evidence. Physical source replacement or unsafe metadata refuses
new input; an authorized main history reset invalidates old connection scopes
and requires fresh reads, without transferring input authority. Unsupported
native compaction/source recovery remains unavailable rather than auto-relaunching.
Claude TUI Allow identity uses the production hook registrations, including
PreToolUse and PermissionRequest. The pinned CLI supplies tool_use_id on PreToolUse
and PostToolUse, not PermissionRequest. Join the private PermissionRequest to its
native PreToolUse ID through the canonical digest of session_id, prompt_id,
tool_name and tool_input. That join must be unique: identical pending digests,
reused IDs, missing metadata or a collision during either final verifier refuse
Allow. No actor or tool ID is inferred from arrival order or pending count.

Before native write, capture the protected transcript cursor and immutable
binding. Native evidence validates the original prompt_id and numeric
promptIndex/turnIndex, and the exact native ID/name/input in its source tool block.
Some successful file-tool result parents are omitted by the CLI; the explicit
native prompt sequence partitions source blocks without inventing a tool ID.
A genuine authenticated PostToolUse/Failure for the original sent Allow may
establish application only with the same native tool ID and digest. Automatic
prerequisite completion, a foreign prompt/tool/input, and interruption do not
acknowledge it. A typed protected native hook Allow/result pair is also supported.
Delivery, rendering and callback completion alone remain unknown. Only original
receipt publication can be retried; no answer/input replay. Observer closure
reclaims its native IDs and evidence records.

Limit:explicit ask-rule policies are not qualified for TUI Allow. Hook Allow does
not override those rules. Such cards expose Deny only and explain that Allow is
unavailable for the policy; final broker admission also refuses it. Allow returns
the reviewed original input without an updatedInput rewrite. Other permissions,
SDK consent gating and protected metadata rules retain their boundaries. All
Allow/public/stock/recovery gates remain off; durable restart reconciliation and
post-delivery failure/recovery remain separate release criteria.

Integration cleanup preserves an unproved old or partial Claude conversation
reservation while closing every releasable transport, staging/journal and proof
resource. A replacement registry entry is never overwritten or removed. A proved
exit with another cleanup fault releases the reservation; an unproved tree cannot
become a fresh owner merely because its connection closed.

Codex history reports the vendor return status only from the same authenticated
empty-tree enclosure record. The supervisor's encoded Python return code and its
own death are not a vendor outcome. Absent/malformed exit evidence stays unknown;
physical Stop qualification remains separately checked. Native status is retained
before descriptor retirement for concurrent exit publication.

Public frame validation authenticates and validates the current caller/attachment
before checking the original writer. A revoked original login cannot incorrectly
sign out a different valid reviewer. Original writer, run, policy/budget, exact
binding and post-IO acknowledgement checks all remain mandatory.
Native Write overwrite modal handoff (Claude 2.1.308): an owned visible screen
with the qualified overwrite frame and no pending PermissionRequest is a needs-input
hint, never a remote decision. Chat and Inbox show “Claude is asking in the
terminal; answer it there” and an Open Terminal link. The link selects the view
without an L1 mutation. Remote Allow and new framed input refuse, including final
byte validation. The original submitted turn retains authority to settle after a
person answers in the native terminal. No product modal answer key is generated.
The notice is run-bound, retired on fencing/replacement, and resolves when the
visible modal clears; disappearance establishes neither idle nor tool application.
Screen text is never an actor, tool identity, decision receipt or native evidence.
Legacy Inbox permission metadata cannot turn this notice into an approval card.

The offline positive proof deliberately loses PermissionRequest transport with all
production hook events registered; generated tmp HOME/config only. It separately
simulates a person's native-terminal answer. Browser tests prove notice/link and
refusal presentation with stubs; they do not qualify writable browser Terminal.
terminalMediated and all rollout gates remain false. Evidence:
2026-11-12-relay-r1-native-modal-handoff-20.json in docs/handoffs.

G1.83 decision delivery: revalidate the exact reserved answer (including expiry,
revision, policy, epoch, stopped state, account and budget) after the final await
before native delivery. Claude SDK and installed PermissionRequest hooks apply
this to both explicit Allow and Deny. A safe fallback denial is not evidence that
a stale person answer applied. Hook reply admission precedes application-evidence
observation; queuing bytes alone is never provider evidence. Codex performs the
answer check inside the acquired app-server stdio write lock, then validates the
owner before sending bytes. Consumed attempts cannot be replayed on refusal.

G1.86: disconnect/Stop may seal an in-flight receipt before its dispatch await
returns unknown. Preserve an already-uncertain receipt's revision and events on
that duplicate unknown result. Conclusive late evidence still settles the exact
original operation; this does not authorize replay or restore execution authority.
Durable Claude Allow evidence (rollout still off): a private account journal saves
original operation/actor/payload/action digest, full reviewed binding, unique tool
join, and protected pre-answer transcript cursor. Preparation is not send or
application. The final hook records send intent before emitting Allow, and saves
an authenticated exact PostToolUse/Failure before acknowledging that observation.
Separately authenticated reconciliation after person revocation can only fill an already sent original
record; it cannot publish a card, authorize another answer or restore input.
Interruption and foreign identities remain unknown. The live observer still ends
after thirty seconds and closes its reader/tasks; unresolved journal entries stay.

Version5 is a separate private read-only evidence channel. Main enrolls the
original binding after checking installation, enrolled node/key, account,
workspace, native conversation, run directory, frozen launch/guard/auth/ownership
policy. It never resolves a current-person answer grant or constructs an execution
owner. Broker derives the native root from its account, validates readonly frozen
run guards, restores the SAVED transcript offset and root/project/file identities
and prefix, and persists a verified native reference before publication. Main
rechecks original scope in the settlement transaction and matches the original
actor/payload/winner/choice/action digest. A duplicate must preserve the native
reference. Only original evidence publication can retry; no native answer/input
can replay. Changed roots, files, prefixes or policy refuse and retain unknown.

The evidence sweep is bounded to four original session/run pairs, with a seeking
cursor and SQL limit, uses an existing manager only, and remains gated off. It
cannot launch, claim a writer or unseal/recover an old owner. Explicit reviewed
owner recovery remains separate. This journal currently covers TUI Allow only;
Deny and other adapters retain their previously recorded reconciliation limits.

Owner decisions, 2026-11-12: Claude Terminal becomes available after the first real
Chat message completes and creates resumable native history. Before then the
Terminal control explains that it is waiting; it must not stop, replace or submit
to the live Chat owner. This presentation hint grants nothing: native preflight
retains its protected-history refusal.
The wait applies only to the live session. After an exit or recovery-required
transition, retained Terminal remains selectable; an old first-message hint or
explanation cannot disable that read-only view or make polling select Chat again.

R1 crash recovery is an explicit **Recover into a new session**. It opens a fresh
person-reviewed New Session draft with an empty task; only the person's subsequent
Launch starts a new session/native conversation. The old session, receipts,
output, decisions and unsent drafts stay readable in place. No old input, answer,
receipt or conversation is copied or replayed. Automatic same-session enrollment
recovery is later scope. Dormant same-session recovery components and their isolated
proofs do not qualify an R1 user flow or enable rollout.

Read-only reconciliation may still settle only original receipts; it never
authorizes another native answer or execution enrollment.

The ordinary person-reviewed session launch route can select Claude exclusive
ownership only when both `CLAUDE_LAUNCH_QUALIFIED` and the existing
`PUBLIC_CLAUDE_QUALIFIED` gates are explicitly qualified (both default false). Its read-only broker probe
must name Claude and the pinned native CLI digest, and verify the pinned SDK and
required installed tools. A generic availability reply or Codex readiness cannot
admit Claude. Launch uses the existing exclusive reservation/enrollment path,
never a legacy fallback; initial task input is refused for this path. Component
cookie/CSRF tests use stubbed owners and do not qualify joined native recovery.

Revoking the original person cannot authorize a new hook answer. After exact
private-channel authentication, an original already-sent Allow record may still
receive matching completion evidence when main denies authority or its response
is lost. This does not publish owner idle or restore input. Stop observation
failure is non-blocking to Claude and cannot ask it to continue the turn;
PreToolUse and PermissionRequest remain fail-closed. A native owner interrupted
without completion evidence leaves the original receipt uncertain, including
through read-only broker restart reconciliation.

Native qualification now includes simultaneous Read permission hooks. Distinct
canonical inputs bind to distinct actual PreToolUse IDs; concurrent public replies
retain those identities through application. Identical input digests with no
unique native PermissionRequest ID offer no remote Allow, and attempted Allow
refuses before native dispatch. Explicit Deny remains available. The paired Bash
diagnostic reached one pending hook only; the Read proof is not an all-tool native
concurrency assertion.

For normal and failed Read/Edit, an accepted decision receipt means the chosen
Allow was applied to the exact native invocation. A genuine PostToolUseFailure
can conclusively acknowledge that approval while the file operation itself fails.
Loss before hook consumption stays uncertain; authenticated completion can settle
the original decision after real execution-broker/main restart without replay.
These observations never grant current input or replace the old session.

The exited-old-owner recovery path now joins real broker/main restart, ordinary
person-cookie/CSRF public Launch, real exclusive enrollment and a new native SDK
owner. The new session/native IDs are distinct, blank Launch makes no model call,
and only a separate explicit new Chat message reaches the model. The old public
snapshot/events and original DB receipts/decisions stay unchanged and readable.
The new-session review click is separately browser-qualified. Recovery while an
old native enclosure survives broker loss remains a separate proof boundary.
All public/launch/Allow qualification flags remain false in production.

Advisory security-policy revision changes revoke the old input/answer authority
without altering native frozen settings. Authenticated completion for an already
sent original Allow can still reconcile through the read-only original binding;
this qualifies an epoch change, not tightened file-access/custom-rule policy.
When the native consumer has positively exited before the hook answer attempt,
a journal send intent cannot prove delivery. Without native completion, the
original receipt stays uncertain through restart. No per-tool result is invented.

Original read-only evidence validation now separates execution eligibility from
past receipt settlement. Tightened custom rules, paused budgets, holds or changed
execution sources still refuse new execution. The read-only original channel
retains structural provider/mode and active node/account/link checks, then the
full saved binding, installation/workspace/native ID, immutable launch/guard
digests and exact original operation. Retargeting the account or workspace refuses
settlement under the changed binding and leaves the receipt uncertain.

Actual surviving-enclosure recovery now joins broker/main restart, original
read-only settlement, ordinary explicit public fresh Launch and one separate new
Chat message. Old records remain unchanged/readable, new IDs and input are fresh,
and the still-alive old enclosure is never adopted or granted input. General
old-enclosure cleanup remains separate; same-session automatic re-enrollment is
outside R1. Production qualification flags remain false.

Native Codex decision replies recheck their original authority inside the actual
stdio response write lock, after any queueing and before response bytes. The
internal callback revalidates the same grant, operation deadline, review expiry
and exact file review without claiming the operation a second time. Transport
closure is checked again after that awaited validation. A queued response denied
there remains an unknown original outcome; it is never retried or acknowledged
as provider application. Only exact native lifecycle evidence can settle it.

Live current-run owner EOF exposes R1's explicit new-session recovery even when
native exit publication already sealed the session. Ownership changes notify
read observers; repeated EOF and delayed EOF for a replacement binding do not
change its authority. Old receipts/output/decisions remain readable. No implicit
same-session re-enrollment, launch or input replay is introduced. Native response
write/drain followed by owner or transport loss before application publication
keeps the original outcome unknown and does not acknowledge the original key.

Same-provider fresh Codex recovery is now qualified on the recorded integration
production tree. It uses an ordinary reviewed blank Launch, an explicit public
Chat writer take for the new attachment, then a separate new Chat message. Launch
and writer take cause no model call or typing. A browser attachment cannot skip
writer binding merely because the launch already names that person as controller.
Old unknown or accepted receipts, decisions and output stay readable and unchanged;
new session/native IDs and positive native-owner cleanup prevent reuse or replay.
The broker pins the reviewed enclosure supervisor with a Linux pidfd and observes
kernel exit independently of inherited pipe EOF. Root death removes mutation
authority promptly; final native writes recheck that lifetime under serialization.
Read-only integrity checks and old receipts/output/decisions remain available.
Supervisor death proves neither vendor death nor an empty native tree. Without the
trusted exit receipt, native exit/cleanup and in-flight application remain unknown.
Recovery requires a separately reviewed new session and explicit writer binding;
there is no adoption of the old tree, same-session re-enrollment, or replay.
The handoff records the exact integration-source qualification and its limits.


Cancellation remains pre-claim only. For an already claimed or terminal operation,
main reauthenticates and refuses the receipt CAS without waiting behind the same
operation's provider dispatch. That refusal changes neither receipt nor reserved
decision and has no broker/resource effect. Queued cancellation still holds the
submit lock through its authenticated CAS and post-commit stage disposal. A later
BUSY after ownership has entered recovery must not be mistaken for qualification
of the earlier owned/pre-loss cancellation boundary. Source-specific native
qualification and unchanged-winner assertions are recorded in the frozen-cancel
handoff evidence.


The RC broker security boundary does not trust agent-UID-readable receipt tokens.
Supervisor keys remain in memory and are provisioned with kernel sender credentials;
receipts authenticate their filename, full content and pinned supervisor incarnation.
Missing key/MAC or supervisor death preserves unknown cleanup. Hook sockets require
current pinned native-tree ancestry as well as token and main authority. A denied
or revoked hook cannot append journal evidence, including revocation during an
awaited observation; separately authenticated durable reconciliation is distinct.
Claude TUI and SDK settings both require the frozen run-directory sandbox block
and broker-derived input paths. Codex may read metadata; its supported effective
write policy must exclude relay records, including implicit temp roots. Unsafe
full-access/unknown policies or writable-root overlap refuse launch. CLI auth-mode
probes are cached thirty seconds per owner; main authority never is. Global factory
reservation locks end before SDK IO, and native before_write admits only True.
Source-specific proofs and rollout limits are in the RC broker security handoff.
