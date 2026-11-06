# Continuity contract

> **Status: approved design (Josh, 2026-11-01).** Not yet implemented; implementation follows the redesign phases and their own reviews and qualification.

Status: design only, 2026-11-01. Source baseline: `main-cfb3`, `cfb322ceb71c60e8f366570026389b21110ce0d3`. This document specifies prerequisites for P2 persistent views and future Chat sending; it does not claim those features exist. No product runtime or shell changes accompany it; the isolated prototype below is not wired into the app.

## Authority and current boundary

The reconciliation's C2 finding separates P1 sidebar browsing from P2 resource lifetime. The package's Technical continuity contracts require stable registry identities, versioned presentation-only layouts, a shared input arbiter, bounded hidden work and measured performance. Decision 04 explicitly removes **Switch now**: the layout is latched for the page load. A preference change takes effect only on the person's next reload/sign-in; an owner disabling the new layout does not force navigation or offer a reload action. This contract does not relax that decision.

Design inputs (handover artifacts, outside the public source tree):

- `ux-direction/reconciliation/01-reconciliation-report.md`, finding C2; `ux-direction/reconciliation/04-coordinator-decision-switch.md`, the decision and draft coverage list.
- `ux-direction/astra-2026-10-31/rynhult-planning-handoff/claude-handoff.txt:120` (registry), `:122` (layout scope/CAS), `:124` (input), `:126` (stream states), `:128` (accessibility/performance).
- The same package's `state-contracts.csv:3` (S02 unavailable references), `:4` (S03 offline/layout conflicts), `:5` (S04 dirty/conflicting files), `:9` (S08 input ownership).

Current implementation boundaries:

| Resource | Evidence at the baseline | What that evidence does not establish |
| --- | --- | --- |
| Terminal | `web/src/components/Terminal.tsx:188` cleans up listeners, closes the socket and disposes xterm; `:210` includes session/generation/mode in effect dependencies. `web/src/pages/SessionPage.tsx:511` conditionally mounts the terminal by tab. | Surviving a route change, tab switch or a move to another parent. |
| Expansion | `web/src/components/terminalExpansion.ts:8` describes geometry-only expansion; `:29` changes the mounted view's surrounding layout. | A persistent host outside the routed page. |
| Files | `web/src/components/Files.tsx:87` derives dirty state; `:91` blocks leaving; `:105` preserves dirty content across reads; `:123` saves against a revision. `web/src/components/FileEditor.tsx:54` destroys its CodeMirror view on unmount. | Keeping a buffer or editor selection after its owner unmounts. |
| Draft storage | `web/src/lib/drafts.ts:7` cannot distinguish an absent draft from failed reads; `:16` writes best-effort sessionStorage and silently catches failure. | Durable storage, acknowledged persistence, or safe switching of layouts. |
| Writer | `src/acc/server/tty.py:27` claims when free or on take; `:48` invokes that for a writable bridge. `:89` checks the current owner before input. | A shared durable receipt protocol for all input surfaces. |
| Automated delivery | `src/acc/server/orchestrator/delivery.py:380` durably claims a queued delivery before typing; `:423` checks its writer token before bytes. | Permission to bypass human input ownership, or replay an uncertain send. |

## Persistent view registry

### Identity and ownership

Create one browser-tab `ViewRegistry` above routed pages. It owns resource handles; routes, tabs and columns own only **placements** referring to those handles. A placement has a stable `viewId`, resource reference, rectangle, focus/scroll preferences and visibility. A move updates that record, not the resource key, native process or writer. Server authorization remains authoritative; a registry reference grants nothing.

Use canonical server identities, never titles or display paths:

- A terminal key is installation + person + session ID + **process incarnation**, bound to its node/account/placement and native session identity. `src/acc/server/models.py:196` already models process incarnations as `SessionRun`; its ID is suitable only if the server verifies and exposes the current run consistently at attach/input time. The container-specific `container_generation` (`src/acc/server/models.py:156`) is not a general run identifier. Add a current-run protocol field before promising cross-resume continuity; do not infer it from a timestamp.
- A file document key includes installation/person, authorized node/account, workspace/worktree identity and canonical relative file identity. Identically named files in different checkouts are distinct. Replacing a checkout, changing access or resolving a different symlink target requires revalidation. A buffer retains its base revision and source identity through a rename/conflict; it must not silently attach to a similar path.
- Multiple file views share one document buffer and revision, with separate cursor/selection/scroll state. Duplicate views cannot overwrite each other's edits with stale snapshots. Multiple terminal placements share one canonical interactive terminal host; a secondary placement is a read-only presentation or a link to focus the host, not another writer.

The registry API should be small: `acquire(reference)`, `place(viewId, slot)`, `setVisibility(viewId, visible)`, `closeView(viewId)`, `inspect(resourceKey)` and explicit `dispose(resourceKey, reason)`. Reference counts describe views, not permission. Drafts, active writer/preedit state and unresolved sends pin the resource independently of its view count. A failed acquisition leaves a retryable placeholder, not a newly started session.

### DOM and lifetime

A terminal entry owns xterm, its DOM host, websocket, bounded scrollback, selection, input subscriptions, composition state and its writer token. An editor entry owns document text, revision, dirty state and view states; CodeMirror view destruction must not destroy the document. A route renderer subscribes to these objects instead of constructing them as a page effect. Today those resources are coupled to component cleanup (`web/src/components/Terminal.tsx:188`, `web/src/components/FileEditor.tsx:54`).

Prefer a stable host layer positioned over registered layout slots. Changing a React key or rendering a portal under a different parent is **not proof** that the DOM, IME composition, focus and selection survive. Before choosing actual DOM reparenting, qualify it in a throwaway browser prototype with socket identity, xterm identity, input-handler count, focus, selection and composition assertions. Defer a move during active IME composition until commit/cancel; never synthesize a second commit. The throwaway prototype below compares both strategies in Chromium; it does not qualify native OS IME or React portal target changes.

| Transition | Contract |
| --- | --- |
| Route/tab/column move | Keep the entry and its native process, socket, writer and buffer. Transfer placement/focus only. Merely browsing the P1 rail must not navigate the active route. |
| Hidden | Continue authorized terminal output draining and connection/auth handling. Disable hidden keyboard targets. Retain writer ownership; visibility is not a take/release request. |
| Close a view | Remove the placement; ask the draft guard before closing the last dirty view. Do not Stop the session. A retained background writer is visible in the session control summary and can be explicitly released. |
| Explicit release/disconnect | Release the writer and attachment, preserving an authorized read-only snapshot where allowed. Explain that reconnect requires a new connection; do not label this uninterrupted continuity. |
| Process ends/resumes | Seal the old run's input and receipts. A new run gets a new entry and starts as an observer. Never resume/start a CLI from layout restoration. |
| Permission revoked/sign-out | Stop new input immediately at the server, close attachments, remove cached sensitive content and invalidate local handles. A placeholder reveals no unauthorized title, transcript or path. |
| Browser reload/crash | Runtime objects do not survive. Restore authorized references and acknowledged drafts, not sockets, writers, queued keystrokes or permission grants. |

Archived, removed or unauthorized saved references use a neutral unavailable placeholder with explicit recheck/remove/reset actions. Never substitute another resource by name. This implements S02, not a claim that authorization can be restored from saved layout (`state-contracts.csv:3`).

### Budgets and hidden work

Use these as initial **qualification limits**, not measured capacity: four visible terminal attachments (retain Grid's four panes), eight total attached terminals per browser tab, at most eight live xterm hosts, 10,000 scrollback rows per host, and 32 MiB of serialized draft content per tab. Include retained hidden hosts in the counts. Track estimated retained bytes and attachment/listener counts; profile real browser heap separately because serialized text is not xterm's heap cost. Measure on phone and desktop before P2 release; revise limits from evidence, not screenshots (`claude-handoff.txt:128`).

At a limit, refuse another attachment with a choice to focus an existing view or explicitly release one. Only clean, read-only, hidden entries with no unresolved operation can be evicted, oldest idle first after ten minutes. Mark evicted views detached; never imply the socket survived. Do not evict dirty buffers, active writers, IME preedit, unsent input or uncertain sends to satisfy a budget. If pins exhaust the limit, explain the limit rather than silently dropping work. Large drafts/attachments that exceed storage limits stay in memory with a failure warning and block destructive transitions; do not truncate them.

Hidden views stop expensive page-specific polling and rendering. Shared low-cost session/attention state may poll with a bounded backoff (initial target 15–30 seconds); connection/revocation handling and output drains remain active. Bound output buffers and expose a gap when older history is unavailable. Never stop reading a live socket and accumulate unbounded data. Query subscriptions must be shared, not multiplied per placement. Existing hidden polling tests (`web/e2e/terminal-polling.spec.ts:114`) cover current components, not these proposed limits.

Layout persistence is separate: versioned server records scoped by installation/person/project-or-private-workspace/device viewport, containing references and presentation only. Use expected revision/CAS, retain an offline local pending arrangement, and offer explicit conflict resolution. Never store drafts, transcript bytes, credentials, writer grants or delivery receipts in layout JSON; never silently overwrite another device's layout (S03, `state-contracts.csv:4`; `claude-handoff.txt:122`). Drag/resize saves are coalesced and do not flood browser history. Preserve deep links and back/forward resource semantics.

## One input arbiter

### Ownership and the P1 exception

The server must arbitrate Terminal, future Chat and team delivery against the same current session **and run**. Client focus, a layout reference or a cached token is insufficient. Extend the existing writer boundary (`src/acc/server/tty.py:21`) rather than introduce a parallel Chat writer. Ownership includes an authenticated actor, channel, run, authority epoch and opaque token. Recheck desired state, authorization, run and token immediately before each byte submission; any asynchronous safety lookup occurs before that final check. Current bridge input awaits `on_input` before writing (`src/acc/server/tty.py:86`); carrying that pattern forward without rechecking would leave a handover gap.

| Action | P1 compatibility | P2 / future Chat |
| --- | --- | --- |
| Open/restore a view | Existing terminal may automatically claim **only if free**. This is a documented P1 exception; no new auto-take. | Always observer, including route restore, expand, reset layout, reload and second view. |
| Start controlling a free session | Existing claim-if-free behavior. | Person explicitly chooses Control; server atomically claims if still free. |
| Another holder exists | Read-only until explicit take. | Show authorized holder/reason, offer focus existing controller or explicit Take control with reason. Failed CAS refreshes the holder, never silently retries a take. |
| Approval/login/stop attention | Opening the relevant view does not take from another holder. | Does not claim or send input. No automatic approval, shell typing or answer to a provider permission prompt. |

Take reasons use a bounded enum (for example `manual_handover`, `controller_unavailable`, `team_intervention`) and optional short note. Audit actor, target run, previous/new channel, reason and outcome, excluding prompt bytes and secrets. Revoke the previous token before admitting a new writer. An in-flight kernel write cannot be recalled; serialize handover with the last authorized submission and identify the boundary in receipts. A stale/offline client cannot continue typing merely because its UI still says Control. This is S08 (`state-contracts.csv:9`), not a promise to undo bytes already delivered.

Only the canonical visible interactive host controls PTY geometry. Read-only secondary views must not race resizes; current bridge resize handling is separate from its writer check (`src/acc/server/tty.py:89`, `:93`). Define an explicit canonical resize authority in the new protocol.

### Drafts, handover and receipts

A Terminal draft, Chat draft and automated message are separate buffers. Changing focus never copies text between them. The arbiter serializes whole automated/Chat submissions and cannot insert a message into partial human terminal input. If the terminal prompt is nonempty, composition is active, or input state cannot be established safely, leave the message queued/blocked with a visible reason. Ask the person to resolve it in Terminal; never clear human input using Ctrl+U or another editing key. Existing team delivery clears its own expected input as part of a guarded procedure (`src/acc/server/orchestrator/delivery.py:431`); moving to the shared arbiter must preserve its refusals and must not generalize this to human drafts.

Proposed receipt envelope: `operationId`, actor/channel, session/run, request digest, authority epoch, monotonic status version, timestamps, acknowledged boundary and safe error code. Persist the receipt server-side before sending bytes, deduplicate operation IDs against the digest, and query it after reconnect. A changed body/run with the same ID is refused. A restored draft is never an instruction to submit.

| Receipt | Meaning and allowed next action |
| --- | --- |
| `queued` | No bytes sent. May cancel or submit once after fresh authority/safety checks. |
| `submitting` | Durable claim exists and bytes may have crossed. A crash or lost acknowledgement becomes uncertain, not queued. |
| `accepted` | A provider-specific, verified acceptance signal identifies this operation/run. Show which signal. Echo, socket write success and an uploaded attachment alone do not qualify. |
| `failed` | Definitely refused before submission, or a definitive rejection with known boundary. Explain whether an explicit new attempt is safe. |
| `uncertain` | Some bytes/Enter may have arrived but acceptance is unknown. Inspect/query evidence; no automatic replay, including after resume or browser reload. |

The current delivery vocabulary includes `typing`, `submitted` and `acknowledged`; adapt it with explicit mappings rather than rewriting historical evidence. Its durable pre-key claim is at `src/acc/server/orchestrator/delivery.py:380`; its guarded per-key writer check is at `:423`. Preserve the existing no-retype and echo-not-ack tests (`tests/test_orchestrator_delivery.py:86`, `:156`). A raw interactive keystroke stream is not a resumable Chat operation: reconnect must not replay locally buffered key events. Separate attachment upload, send and provider acceptance in the UI (`claude-handoff.txt:124`).

## One unsaved-work registry

### Registration and coverage

Create `UnsavedWorkRegistry` alongside the view registry, with one route guard and one beforeunload listener. Each adapter registers a stable, person-scoped draft ID, resource identity, complete normalized desired value, original/base revision, current draft version, persistence policy, validation, explicit save/discard actions and any in-flight operation ID. Dirty means any meaningful desired value differs from the baseline, including changed permissions or access with unchanged text. Registry ownership lasts beyond component unmount; mounting another view subscribes to the same draft.

Required coverage from decision 04, with migration anchors:

| Draft family | Current owner / required state |
| --- | --- |
| Files | `web/src/components/Files.tsx:87`, `:123`: buffer, base revision, dirty/conflict state and view cursor. Keep local/base/new server versions until explicit resolution (S04). |
| Project brief | `web/src/components/ProjectBrief.tsx:39`, `:67`, `:81`: text, references and original revision. Existing common blocker is `web/src/components/UnsavedBriefGuard.tsx:19`; replace it only after equivalent registry coverage. |
| Security settings | `web/src/components/SecuritySettings.tsx:95`, `:107`: desired policy, scope, base revision and stale state. Do not persist verification tokens or preview authorizations; fresh verification remains required on apply. |
| Remote access | `web/src/pages/RemoteAccess.tsx:20`, `:23`, `:39`: the full policy and expected revision. Never restore pairing secrets or replay enable/revoke operations. |
| Publishing request | `web/src/pages/Publishing.tsx:29`, `:35`, `:51`: project, target, selection, name/zone/port and **access**, including access-only edits. Confirmation is not a reusable authorization. |
| Publishing address editor | `web/src/components/PublishingAddresses.tsx:19`, `:23`: address, label and **access**. The current text-only warning predicate is insufficient for access-only changes. |
| New project | `web/src/components/NewProject.tsx:30`, `:32`: every form field, references/files and source/host/account/trust choices; retain a created-project operation receipt so restoring a form does not create it again. |
| New session | `web/src/components/NewSessionDialog.tsx:100`, `:371`: task/topic/name plus model/provider, permissions, placement and all other meaningful choices. Current dirty detection does not cover every configuration-only edit. |
| WorkList | `web/src/components/WorkList.tsx:126`, `:170`, `:311`: main editor, reply and item drafts, each with its source revision. |
| Plan | `web/src/components/Plan.tsx:170`, `:205`, `:208`: origin/base revision, idea, scope, phases, reason and editing state. |
| Register a folder | `web/src/pages/Projects.tsx:24`, `:32`: folder/host/account and all registration choices; distinguish a draft from a successfully registered folder. |

Nested New project/New session workflows have separate draft IDs and a parent relation. Closing a dialog retains its draft unless the person explicitly discards it. An already completed creation/registration is reconciled by operation identity, never resubmitted because a local draft exists. File attachments need actual retained bytes plus their own quota/expiry policy; saving only their names must not claim they are recoverable.

### Persistence that can fail honestly

Replace the void best-effort writes in `web/src/lib/drafts.ts:16` with an explicit asynchronous result: `durable(version)`, `unavailable`, `quota`, `corrupt`, or `conflict`. Use a versioned, person/installation-scoped IndexedDB transaction as the proposed local store. Resolve durability only after transaction completion, not after enqueueing a write. Persist base revision and schema version with the content. Local durable means saved **on this browser profile**, not backed up or applied on the server. Browser eviction, profile removal and abrupt termination remain limitations.

Separate resource dirty state from persistence state: `dirty + persisting`, `dirty + local-durable`, `dirty + volatile`, `dirty + conflicted`, and `server-save-pending/uncertain`. A later local edit remains dirty even if an earlier version finished persisting. Use compare-and-set versions across tabs and notify via BroadcastChannel; conflicting drafts retain both copies for explicit resolution, not last-writer-wins. A storage read failure is not an empty draft. Do not replace the server baseline with a local draft or overwrite dirty data on focus refetch (`web/src/components/Files.tsx:105`; S04, `state-contracts.csv:5`).

Show “Draft saved on this device; not applied” only for the acknowledged current version. On failure show “Could not save draft. Keep this tab open or copy your changes before leaving.” Keep the content in memory, announce the failure politely, and offer retry plus a deliberate export/copy action where appropriate. Never automatically copy private policy or prompt text to the clipboard. Resource “Saved” requires a successful server revision acknowledgement, not a browser write. Retain the original base on server conflicts; review/merge or explicitly discard before reloading.

Do not store credentials, recovery keys, passkey challenges, pairing tokens or fresh-verification capabilities in drafts. Persist desired settings only. Scope access to the current person and clear displayed/cache data on sign-out or revocation; another signed-in person must not see prior drafts. Local storage is not an encrypted secrets vault. Legacy sessionStorage keys (`web/src/lib/drafts.ts:5`) may be imported only when ownership/schema are established; never expose an unscoped legacy draft to a different person. Keep the old copy until the same version is durably acknowledged or the person explicitly discards it. Handle blocked storage upgrades without throwing away the in-memory draft.

### Navigation, reload and switching

Registry-preserving navigation/moves need no discard prompt. Closing the last dirty view or crossing a boundary that destroys its owner offers Save, Keep draft, Discard and Cancel as applicable. Keep draft requires an acknowledged current local version and a recoverable authorized reference; if persistence fails, stay put. Save may require fresh verification and can fail independently. Discard is explicit and identifies the affected drafts; a blanket confirmation must not silently cover newly dirtied forms.

A future in-app layout switch needs a registry snapshot barrier: enumerate participants, persist exact versions, wait for acknowledgements, recheck the registry version, then switch only when no newly dirty or in-flight participant was omitted. Conflict, quota failure, uncertain submission or a late edit cancels the transition. Do not run two guards with competing dialogs. Browser reload/close cannot wait for asynchronous persistence: persist during editing and use the native beforeunload warning for remaining risk. Do not promise protection from OS kill or browser crash.

**P1 still has no in-app switch**, even if some drafts have been migrated. Retain decision 04's exact preference copy, “Takes effect the next time you reload Rynhult.” Owner disablement uses “An owner turned the new layout off. Rynhult will use the standard layout the next time you reload.” with no reload action. Full registry coverage and tested failure handling are prerequisites to revisiting that decision, not authorization to add a switch now.

## Migration and proof required

Implement in small independently reviewable steps; keep old deep links and server safety checks:

1. Add registry interfaces and fake-clock/storage tests without moving shell components. Inventory every mounted owner and its disposal. Agree a narrow host-slot/subscription interface with the shell owner; this design edits none of the shell-owned files.
2. Introduce one persistent terminal host and one shared file document behind the P2 flag. Extract current construction/cleanup into registry-owned handles; keep existing Terminal/FileEditor presentation and input adapters. Prove one socket, one xterm, one input listener and one native process across routes/placements before expanding coverage. Add a run-bound read-only attach protocol first; older-server fallback must not claim P2 restore semantics.
3. Add acknowledged draft storage and migrate adapters one family at a time. During migration, legacy forms retain their existing guards; registry-owned forms unregister legacy guards. Do not allow a global layout switch until **all** rows above are covered. Stop silently swallowing failures for migrated drafts; do not keep two competing writers to legacy and new storage.
4. Extend the existing server writer to the shared arbiter, add run/epoch checks, explicit takeover and receipt querying, then adapt team delivery. Preserve its refusal/no-replay tests before adding Chat. Introduce no second provider process and no new path around guardrails.
5. Qualify hidden polling, budgets, keyboard placement/resize, screen-reader order and phone behavior; only then permit P2 layout moves. Chat read-only rendering can follow stable cursors; Chat sending waits for the arbiter and verified acceptance adapters. Unsupported, unknown, empty, stale and disconnected stream states remain distinct (`claude-handoff.txt:126`).

Focused evidence and acceptance matrix (future tests are requirements, **not run or implemented by this document**):

| Area | Existing evidence | Additional focused qualification |
| --- | --- | --- |
| Terminal identity | `web/e2e/termfull.spec.ts:108`, `:216` cover mounted geometry/editor and reading position. | One socket/xterm/process across route, tab, columns, fill/restore; no attach during restore; selection and IME retained. Count identities/listeners, not only pixels. |
| Input | `web/e2e/terminal-input.spec.ts:66`, `:80`, `:99`, `:129` cover ordinary input, reconnect, composition and committed characters. | Two browsers plus Chat/team race for one writer; take during awaited checks; stale run/token refuses; partial human input blocks automation; uncertain receipt never replays. |
| Delivery | `tests/test_orchestrator_delivery.py:86`, `:156`, `:265` cover no-retype, echo ambiguity and permission prompts. | Crash before/after write/Enter/acceptance; idempotent operation IDs, mismatched digest refusal and definitive versus uncertain outcomes. |
| Hidden views | `web/e2e/terminal-polling.spec.ts:53`, `:114` cover current parent/hidden polling (project-specific skip applies). | Eight-host limit, pinned saturation, clean eviction, hidden output flood, auth revocation while hidden, no observer resize races. |
| Files and drafts | `web/e2e/12-files.spec.ts`, `project-brief.spec.ts`, `security-settings.spec.ts`, `61-publish-addresses.spec.ts` exercise separate current surfaces. | Table-driven adapters for every row: configuration/access-only dirtiness, unmount/remount, failed read/write/quota, reload, cross-tab conflict, stale server revision, sign-out isolation and attachment loss. |
| Layout transitions | Existing tests do not qualify a registry-wide switch. | S02/S03 unavailable refs and CAS conflicts; every draft blocks a destructive transition on failed persistence; new edit during snapshot barrier invalidates it; P1 has no Switch now. |

Browser tests run one focused file at a time with fake brokers/providers and an isolated harness, never the live instance. Preserve keyboard focus order when geometry differs from DOM order; announce control/persistence changes without stealing focus. On phone, one visible resource does not destroy background entries; cap retained work using the same explicit budget policy.

## Risks and limits

- Stable DOM plus native IME across browser engines is unqualified. The prototype below establishes a Chromium synthetic-event baseline only; production browser/OS qualification is still required before a P2 continuity claim. React object identity alone is insufficient.
- Initial memory/socket limits are proposals requiring measurement. Keeping hidden writers pins resources; reaching the cap must be visible and reversible, not a reason to silently dispose one.
- Server-run identity, serialized writer handover and durable receipt queries need additive protocol work. Existing per-websocket checks and team delivery receipts are useful foundations, not proof that a unified arbiter already exists (`src/acc/server/tty.py:86`; `src/acc/server/orchestrator/delivery.py:380`).
- Local drafts can contain sensitive text. Person scoping, retention cleanup and truthful failure status are required; neither IndexedDB nor layout sync is a credential store or a backup guarantee.
- This protects intentional same-tab view transitions once implemented. It does not preserve a live terminal socket across page reload, keep unauthorized content after revocation, recover evicted browser storage, prove provider acceptance from terminal echo, or recall bytes already sent.


## Prototype results

On 2026-11-01 the isolated [prototype and run instructions](prototypes/continuity/README.md) were added under `docs/design/prototypes/continuity/`. It uses the repo-installed xterm plus the app's in-memory composition patch, FitAddon, a loopback static server and a test-owned websocket fake, never a Rynhult session. The standalone Playwright config has one worker and no app harness dependencies.

**Recommendation: use A, the stable host layer, for the next implementation experiment.** Raw reparenting is not safe during composition. The raw diagnostic reproduces cleared preedit text after blur (`docs/design/prototypes/continuity/continuity.spec.mjs:17`). A also needs care: its raw resize clears selection. Guarded A retains its parent and focus; guarded B succeeds only by postponing every physical reparent until composition commits and restoring focus afterwards (`docs/design/prototypes/continuity/prototype.mjs:98`). Neither strategy may commit/cancel composition on the person's behalf.

Final verification: Chromium **156.0.8078.4**, four focused cases repeated three times, **12 passed**, one worker. Each guarded case makes 20 actual moves across Column 1, Column 2 and Expanded fill while numbered output streams, with one synthetic `compositionstart/update/end` cycle per move. These are native DOM composition events dispatched by the test, not a real OS IME. B's requested move remains pending while preedit is active; it physically moves after each commit, not only once at the end.

| Final-run measurement, per 20-move case | A: stable host | B: deferred reparent |
| --- | --- | --- |
| xterm / socket / textarea identity | Same throughout | Same throughout |
| Server connection count / unexpected closes | 1 / 0 | 1 / 0 |
| Parent unchanged / blur events | Yes / 0 | No / 20 |
| Physical moves / moves deferred until commit | 20 / 0 | 20 / 20 |
| Seeded and streamed scrollback | Sentinel + all 120 seed lines + ordered stream retained | Same |
| Selected sentinel / focus after each move | Retained / restored | Retained / restored |
| Composition starts / ends / exact input messages | 20 / 20 / 20 | 20 / 20 / 20 |
| Explicit placement geometry reads | 21 | 0 |
| Fit passes / resize observer callbacks / resize frames | 21 / 21 / 21 | 21 / 21 / 21 |
| Maximum fits in one animation frame | 1 | 1 |
| CDP layouts, range over three repetitions | 236–242 | 216–225 |
| CDP style recalculations, range over three repetitions | 252–256 | 276–284 |

Tests also resize inactive slots and assert no fitting/resize traffic. Application work budgets are at most 21 fits/resize sends/placement reads and 42 observer callbacks; browser-wide layout/style totals include streaming output and 20 IME cycles, bounded at 240 + twice the output chunk count (`docs/design/prototypes/continuity/continuity.spec.mjs:98`). These counters detect excess work/feedback loops; they do **not** establish frame-time, memory or production multi-view capacity. In particular, fewer explicit placement reads in B do not make it preferable to A: B still blurs and recalculates style more often in this experiment.

Selection is explicitly restored around fit/commit for one short, single-line sentinel (`docs/design/prototypes/continuity/prototype.mjs:47`); its buffer coordinates do not change with reflow. This is not qualification of wrapped/multiline selection or arbitrary scroll-position anchors. Stable A defers PTY fitting during composition while allowing its container geometry to change. Production must investigate clipping/caret/candidate-window behavior during that interval, plus overlay stacking, page scroll/zoom, font changes and keyboard/screen-reader order.

Firefox/WebKit remain unrun. They need separate focused projects, engine-neutral rendering measurements instead of CDP, and investigation of their raw-move outcomes rather than assuming Chromium's failure pattern. Native IME/candidate windows need real OS testing on relevant platforms; synthetic events cannot establish that behavior. React portal target changes, routed integration, writer takeover/revocation and process generation changes remain future acceptance work. The throwaway page proves neither provider acknowledgement nor shared input arbitration.
