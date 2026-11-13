# Session relay qualification

These are test fixtures, not commands for a live account. Keep `session_relay`
default Off. Never point a fixture at a live account, broker or shared native
Codex daemon. No fixture uses the worker API key.

Run the bounded safety regression/report with the project's Python environment:

```sh
PYTHONPATH=src python scripts/session_relay_qualification/safety_gates.py --output /tmp/relay-safety.json
```

This selects only `tests/test_session_relay_*.py`, writes its exact command,
JUnit counts and missing proofs, and **exits 2 while release proofs are missing**.
A fixture pass cannot promote native capabilities. Skips remain incomplete
verification. `required_proofs` is an explicit manual qualification inventory,
not a claim that a test has been implemented for each missing gate. The complete
R1 qualification harness is still unfinished.

Run `python3 scripts/session_relay_qualification/probe.py` for metadata only.
It creates no conversation. Installed bubblewrap is mandatory; namespace failure
has no unisolated fallback. Account homes, live broker sockets and the shared daemon are absent from all
native fixtures. The production-placement proof additionally mounts only its
disposable pytest workspace/run folders, trusted source and existing SDK dependencies;
its account home/passwd and API are artificial.

`probe.py` runs fixed native metadata commands in bubblewrap with all namespaces
unshared, an empty environment/home and no external network. It checks installed
versions/digests and generates schema hashes. `capture_tui()` runs the pinned
TUI against synthetic Unix WebSocket replies to discover its wire shape. Its
terminal text is fixture evidence; it proves neither native execution nor input
processing or actor correlation.

`capture_claude_resume()` checks the exact installed Claude version/digest and
runs two real CLI processes with a synthetic local model. The HTTP listener is
inside the offline network namespace; there is no host listener or external
route. Only an artificial key created in the fixture is supplied. A Python hook
bridge loads the unchanged reviewed guard source, checks hashed fixture policy,
and returns its denial through the actual native CLI hook path. This executes
no direct `bin/acc-*` command. It proves print-mode persistence/resume and guard
code consumption, not native TUI handover or production installed hook placement.

The seven subprocess crash barriers use the real main SQLite store and broker
journal with a fake provider admission log. Workers exit abruptly before/after
registration, main claim, broker claim, provider admission, acknowledgement and
receipt commit. Unknown outcomes stay uncertain, and recovery does not dispatch.
This complements the existing duplicate/restart/final-admission regression tests;
it does not substitute for native provider crashes or node/HA proof.

The M3 handoff and results JSON record the exact scope and remaining blockers.
The current stock native TUI capture has not established the required processed
input watermark. Do not infer one from socket drain, a quiet screen, a draft,
client identity or the current controller. No `terminalMediated` attestation is
issued. Production Claude switch plans and broker hook/decision routing are now wired
behind disabled public enrollment. Fake CLI tests exercise the real private
broker/main transport. Native idle/drain/tree-exit and bidirectional SDK/TUI
qualification remain mandatory before enrollment is offered.

`gates.json` maps every R1 design gate to bounded test modules and separately
records missing native, browser and placement evidence. Run
`PYTHONPATH=src <project-python> scripts/session_relay_qualification/safety_gates.py
--output /tmp/relay-gates.json`. JSON and JUnit record per-gate coverage from that
exact run; absent, failed or skipped modules cannot qualify a fixture gate.
Timeout/collection failure writes an incomplete report rather than consuming old
XML. Exit 2 deliberately means release blocked, even if all fixture tests pass.
There is no enable/override option. The manifest is a complete requirement
inventory, not a claim that its missing production proofs have been achieved.

Josh approved exclusive Codex input at idle: both views keep their read paths,
only the leased view/person may send, and take-over is explicit. Immutable writer
route generations and full operation control revisions replace the simultaneous
writer correlation target. A retired route is never rebound to today's holder.
The private broker read proxy and core take-over are implemented; public Terminal
and take-over remain unavailable until native retirement, TerminalHost and
attachment authority qualify. No native attestation or input capability is enabled.

`capture_owned_tui()` prepares the real private app-server/stock-TUI probe using
this source's proxy. It refuses any version/digest mismatch before creating a
conversation. The restored0.167.1 pin now passes the owned Host decoder/drain/
retirement and exact-owned screen capture selection; historical0.163.0 failures
remain diagnostic only. No alternative build or live daemon is tried.

`capture_claude_resume(placement=...)` runs the actual production account broker,
ClaudeOwnerFactory, SDK0.1.50, private tmux/opener, Host and hook path inside the
same offline namespace. Parent main uses the normal authenticated Unix broker
stream. The fixed artificial person creates SDK history, switches idle to the
same native id in Terminal, sends granted input and switches back to SDK; kernel
receipts prove old-owner trees empty before replacement. The public-cookie HTTP
observer selection reads its actual native screen without browser route mocks.
The browser contract test separately verifies escaped read-only rendering; it is
not a joint real browser/native input or takeover proof. No terminalMediated
attestation is issued. The fixture records additional zero-tool native HTTP
traffic explicitly; its admission/billing semantics remain unqualified.

`capture_claude_resume(enclosure=True)` runs pinned real Claude print/guard/resume
through the production process-group/subreaper supervisor in the existing offline
namespace. It uses only the fixture model and artificial credential. Both runs
must produce kernel-empty receipts before replacement. This qualifies print-mode
enclosure integration, not native TUI idle/draft/drain or approval semantics.


`capture_owned_tui()` uses the exact pinned 0.167.1 stock TUI against one real
private app-server inside that namespace. A synthetic local Responses endpoint
materializes genuine history and a live update. The test checks rendered fixture
responses and a correlated refusal of real native input, with no extra provider
turn. This qualifies readable attachment only. Native extension-list RPC errors
remain visible. Public Terminal input/take-over and `terminalMediated` remain
unavailable; no actor or processing proof is inferred from these reads.


`capture_owned_tui(guard=True)` additionally loads the unchanged guard source via
a private Python hook wrapper. Eight minimal command hook identities are compared
with native `hooks/list`; a synthetic native tool call is denied even with native
permissions bypassed. `guard_tampered=True` changes the policy after freezing its
launch digest and checks native refusal. These fixtures exercise native hook
consumption, not production installed hook placement or Terminal input grants.


`capture_owned_tui(writer=True)` exercises the stock text envelope through the
real immutable `WriterRoutes` gate. A fixture person receives Terminal epoch 2,
then Chat epoch 3; late input on the old native route is refused while that same
TUI displays the next Chat turn. The socket route is bound to the fixture-owned
native child's kernel peer PID. Authority/admission are synthetic and draft
retirement is controlled by the script; this is not a production Host or public
take-over qualification. The production native text projector checks requested
settings against an immutable copy of the actual native start response, strips
client metadata and rejects config changes/rich input before route admission.
No production route is installed and no capability is promoted by this fixture.


`capture_owned_tui(host=True)` exercises the unchanged production `attach.py`
bridge and `pty_exec.py`, real private tmux, stock native TUI and private
app-server. Only placement command runners/config and main authority/admission/
receipt acknowledgement are fixtures. Kernel peer PID/executable bind the native
route. NativeInputHost captures the authenticated input ticket's immutable lease;
exact decoded text plus conclusive admission acknowledges its prefix. Typing or
socket drain leaves processed zero; a consumed prefix then drains. Native idle
and empty tracked text permit retirement, old input is fenced, and both the
framed reader and a native tmux observer retain live output from the same owner.

This conservative proof profile supports ASCII text, backspace/delete and Enter.
It refuses slash/shell controls, escape sequences, Unicode and rich editing
before transport. It cannot be offered as an ordinary native Terminal without a
product/retirement decision; see the latest M3 handoff question. No production
Host is installed, no native-to-main ingress is promoted, and terminalMediated
remains false. Native ID capacity exhaustion refuses rather than forgetting old
requests. Missing production placement and R1 release proofs remain missing.


`capture_claude_resume(enclosure=True, tui=True)` proves real CLI print/TUI/print
same-id persistence in the enforced offline namespace. `sdk=True` replaces both
print legs with the installed **0.1.50 SDK** driving the pinned **2.1.308 CLI**.
Only installed public Python dependencies are copied into the disposable fixture;
no packages are installed, bundled alternate CLI is copied, or worker key used.
Artificial project/startup preferences and a synthetic key are explicitly fixture
state; this does not qualify account onboarding, billing or personal consent.
Native SessionStart/UserPromptSubmit/Stop hooks identify the resumed session.
The TUI exits with its native /exit command; each SDK/CLI/TUI owner must leave a
verified kernel empty-tree receipt before the next starts. Saved history in the
last SDK request must contain all three turns and the original guard denial.
This qualifies native mechanics, not the production L1 Hub switch, full-editor
draft/drain observation or native approval CAS. Keep enrollment/attestation off.


For a focused continuation, select only reviewed related modules or gates:

```sh
PYTHONPATH=src <project-python> scripts/session_relay_qualification/safety_gates.py \
  --module test_session_relay_native_ingress --module test_session_relay_safety \
  --output /tmp/relay-r1-focused.json
```

`--gate <inventory-id>` expands only its mapped modules; repeated `--module`/
`--gate` options take their union. Both selectors are allowlisted. The exact
selection/command and JUnit counts are recorded, unrun proofs stay incomplete,
and exit 2/pilot_ready=false remain mandatory despite passing fixtures. This is
not the complete production release qualification. No broad runner invocation
was used in this continuation.

Josh's approved2026-11-11 writer take-over change supersedes empty-draft retirement:
retain the old draft in a permanently read-only client and give the new writer a
distinct fresh input on the same app-server/thread; never replay. Design and UI
wording are updated, but actual native client-generation composition and positive
retirement proofs are pending. The current nonempty refusal remains a missing-
proof gate, not the product requirement. Do not relax it by rebinding the old Host.

The new full production Codex broker/main/cookie writer fixture is explicitly
**unqualified**: on-request0.167.1 cold TUI bootstrap fails on history APIs and
processing stays0. A strict expected-failure test recognizes only that concrete
bootstrap condition; other errors fail. The standalone hooks-enabled control
passes, so disabling hooks/changing the pin is not justified. No candidate read
projection was retained in production; the primary handoff records diagnostics.

### Groups 5–6 isolated broker lifetime proof

`lifetime_probe.capture()` pins stock Codex 0.167.1 and runs a fixed private
account-broker worker plus a controller inside the offline mount/network
namespace. Only artificial pytest workspace/run roots are writable. Its local
model accepts synthetic fixture turns; no host account/config/key is mounted.
The installed Codex package is copied into the private disposable artifact,
its native digest is rechecked, and it is mounted read-only at its normal path.
This prevents host package replacement during a long run from removing the
reviewed executable; it installs or changes nothing on the machine. The
controller SIGKILLs and restarts an actual broker process. The production
main, typed NodeLink and node Relay/Channel run outside that namespace; the wire
is simulated, so this does not qualify real partition, leader fencing or HA.

`test_session_relay_process_restart.py` covers failed-first enrollment, actual
broker SIGKILL/EOF, durable main sealing, explicit fresh native enrollment, old
opaque grants and delayed channel frames, with no model request or replay. It
also checks that broker death stops the old owned native client; its supervisor
pins the broker incarnation using a pidfd. Default Claude enclosures are unchanged.

The group6 tests use the same private worker for native lifetime/bridge/resource
churn and fresh main RuntimeManager/context incarnations on one persisted DB.
Main object recreation is not an OS-main restart. Journal epoch/clock advancement
in the retention test is explicitly fixture administration, never broker-only
production renewal. Uncertain native admission is injected after real native
send and before journal settlement; nothing retries that send.

The churn workload pins only the disposable namespace to eight available CPUs,
uses at most four concurrent launches/eight active sessions, and measures broker
FD/task/thread/RSS bounds after cleanup. The exact 2049-lifetime selection has a
three-hour controller bound; it is not a full suite or performance qualification.
The smaller `smoke` cases exercise the same assertions for repeated race checks.
Codex lifetime evidence does not qualify Claude lifetime/hook-handler churn.

Run the long native proofs explicitly from the checkout root (one selected test
per command; installed pins and bubblewrap are mandatory):

```sh
PYTHONPATH=src <project-python> -m pytest -q tests/test_session_relay_native_lifetimes.py -k 2049 --relay-long-native-proofs
PYTHONPATH=src <project-python> -m pytest -q tests/test_session_relay_runtime_retention.py -k 129 --relay-long-native-proofs
```

The long parameters skip unless `--relay-long-native-proofs` is supplied, so a
routine suite cannot accidentally start this multi-hour workload. Use `-k smoke`
for their bounded race repetitions. The long selections are kept
outside `safety_gates.py`'s 900-second runner budget. Passing any of these leaves
the pilot off; the F4/F5-F6 inventory records the remaining scope explicitly.

### Claude native owner churn timing

`test_session_relay_claude_churn_long.py` selects131 actual SDK/TUI owner
lifetimes (65 complete cycles) on one production factory. Run it serially with
`--relay-long-native-proofs`; its explicit private-fixture budget is1800 seconds.
This is a qualification-run deadline, not a production session lifetime limit.
The shorter2/17-cycle cases retain their1200-second ceiling. No native count,
resource/admission assertion or fresh auth check changes with the budget.

The proof records per-cycle terminal startup/native turn/chat startup/SDK turn
phases and lock-protected broker RPC/auth-status timing counters. Intermediate
parent evidence is `/tmp/relay-g56-claude-profile-65.json`; successful final
properties are also in JUnit. RPC timings contain nested auth work, so their
cumulative seconds must not be added as independent wall time. Preserve a failed
run separately; partial starts/exits are never a successful131-owner proof.
Fixture teardown retrieves the capture task's primary timeout even when its
inspection socket has already closed, and attaches cleanup errors without
replacing the original failure.

Measured serial envelope (local Linux, pinned Claude2.1.308/SDK0.1.50, synthetic
API, one native proof worker):131 owners on one production factory passed in
1107.18 seconds, below the former1200-second ceiling. All131 kernel empty-tree
receipts passed;65 cleanup samples held FDs at23 and async tasks at6–7. The run
made131 tool-bearing model requests plus1 retained unclassified zero-tool
request. It does not establish an HTTP exactly-once or billing guarantee.

This workload performs actual SDK and TUI turns as well as ownership changes.
Mean cycle phases were1.801s to terminal,8.770s for native input/turn settlement,
4.719s back to SDK and1.592s for the SDK turn.3585 fresh native `auth status`
probes took612.410 cumulative seconds (maximum0.311s each); those calls are
nested in the phase/RPC timings. First/last10-cycle means were14.619/17.560s.
No owner-count cliff or broker resource accumulation appeared through131 owners.
Fresh classification remains mandatory; no auth cache was introduced.

131 is the validated sequential proof count, not a new production hard cap.
The1800-second qualification budget supplies headroom for this workload. Larger
counts, parallel long proofs under1200 seconds, physical deployments and service
latency guarantees are outside this measured envelope. The separate
`test_session_relay_claude_churn_budget.py` is an explicit serial diagnostic of
the old1200-second deadline with the same131-owner assertions.

The serial repeat with the original1200-second ceiling also passed all131 owners
in1103.25 seconds. It again held FDs at23/tasks6–7 and made3585 fresh auth probes
(602.448 cumulative seconds). These two serial runs support a workload/deadline
explanation for the prior concurrent long-run timeouts; they do not establish an
unbounded capacity claim or a guaranteed1200-second runtime under shared load.

Natural Claude background completion is a separate native case. A real Bash
`run_in_background` process exits after its fixture release file appears; its
pinned pidfd proves exit. The adapter reads the SDK message stream past the turn
ResultMessage until the known task notification drains the background ledger.
Turn admission/completion is published once. New submits/idle switching remain
fenced through the notification drain; EOF with unresolved work is failure,
never invented completion. Task completion cannot settle unknown permission
receipts, which continue to refuse ownership until separately resolved.

`test_session_relay_claude_process_death.py` runs the real broker/factory in a
separate OS process while the offline model stub and controller stay alive.
SIGKILL therefore cannot pass by destroying the entire namespace. The old TUI's
pinned pidfd and kernel empty-tree receipt must prove exit before broker restart;
restart has no automatically adopted owner. A fresh reviewed conversation opens
SDK/TUI, rejects the old input grant, accepts a fresh unsent draft and cleans up.
This is local account-service coverage; enrolled-node/HA/TLS evidence is separate.

The active SDK/background broker-death variant uses the same real OS worker with
a real permitted Bash background process. The controller pins that child as well
as its enclosure, waits for SIGKILL(-9) and kernel-empty exit before restart,
requires no automatic owner, and rejects the old operation grant on the matching
fresh SDK writer path. A fresh SDK/TUI review remains usable. Accepted turn
receipts remain accepted and unresolved permission outcomes remain unknown after
cleanup. The background PID is published atomically in this fixture.

### Joined public browser/new-session Codex proof

The normal `session-relay.spec.ts` file has an explicitly enabled native case.
Without the opt-in below it skips; default browser checks need no vendor CLI.
The fixture uses the same mandatory unshare-all/clearenv private broker probe,
installed0.167.1 digest check, private app-server and stock `--remote unix://` TUI.
Its project/run folders must be inside the disposable base shown here. There are
no relay HTTP response mocks or external model calls. Only this fixture's feature
and capability declarations are elevated; public terminalMediated stays false.

```bash
cd web
ACC_E2E_RELAY_NATIVE=1 ACC_E2E_PORT=18769 \
ACC_E2E_BASE=/tmp/relay-m3-browser-native \
ACC_E2E_PYTHON=/srv/acc/app/.venv/bin/python \
PLAYWRIGHT_BROWSERS_PATH=/srv/acc/tools/ms-playwright \
npx --no-install playwright test e2e/session-relay.spec.ts \
  --project=desktop-stateful --workers=1 --grep 'offline native:'
```

The person checks the initially unchecked new-session opt-in, previews/launches
through production APIs, explicitly takes initial Chat, sends, takes Terminal, connects its
opaque input grant, processes native typing, retains an unsent draft on idle
Chat take-over, and sends from a fresh composer on the same app-server/thread.
It checks three artificial model requests and zero replay of the retained draft.
Keyboard availability reflects both causal processing and a fresh idle read.
The model fixture emits complete Responses content-part/text events and sequence
numbers; a sparse completed-history fixture does not qualify live TUI output.
Teardown closes the browser and cached preview probe connection before collecting
the native server; otherwise server.wait_closed and fixture collection deadlock.
This proof does not close all lifecycle, Claude browser or remaining R1 gates.

The reload variant restores history on a new observer attachment without input
reclaim or model replay; the person explicitly takes the next writer lease.

The second-person variant uses a separate authenticated owner/browser context:
observer history, explicit fresh Terminal take, holder visibility in both views,
fourth native turn and unchanged owner/thread, with no retained-draft replay.

The native browser file also includes compact390x844 desktop Chromium, deliberate
identical Terminal turns (two distinct user history items), Grid observer transfer,
and reconnect readiness/focus. Connect focus follows the committed enabled field;
real physical typing checks enabled+focused state. G1.33 and G1.35 artifacts record
separate20/20 real native failure/reconnect runs. These scoped cases do not enable
Terminal mediation or replace the remaining safety gates.

Claude placement now records bounded run/native-ID/hook names (no payloads),
asserts real SessionEnd before the next SessionStart, and has an explicit frozen
ask-policy permission case. Real TUI permission blocks handover; person hub decline
prevents a harmless workspace marker write and real Stop/processing follows.
Decision settlement remains outcome_unknown and further input refuses; public
Claude switching/decision/browser integration is still unqualified. Empty-first
safe refusal is unchanged pending Josh's card.

The Claude native background case uses the real Bash schema/run_in_background
flag and an explicitly frozen allow policy. It must refuse before a marker job
starts; native PostToolUse backgroundTaskId evidence conservatively blocks L1
handover until qualified completion or explicit Stop/recovery. TaskStarted SDK
messages are not a native TaskStarted hook. The G1.36 artifact records the final
20/20 native refusal runs; positive native background completion is unqualified.

Registered warm and registered-empty-refused cases exercise the reviewed internal
prepare/commit seam through the production broker and pinned CLI. They assert the
exact pre-registered switch rows with correct outcomes and no shadow registration.
HTTP/selector/browser switching remains unavailable; these cases do not enable it.

G1.38 adds `http-warm`: real cookie/CSRF registered HTTP prepare/commit in both
native ownership directions, exact four operation records, ended attachment
refusal and fresh public Chat input on the same UUID. Public Claude qualification
is false; fixture switches cannot enable rollout. HTTP cancellation/withdrawal/
revocation/lost-reply checks use fake CLI ownership separately. Enrollment,
selector/browser and conclusive native decision application remain unqualified.

G1.39's dormant public enrollment uses person opt-in, API-key vault grants or the
existing approved subscription consent. The installation probe is read-only and
not a runtime attestation; SDK preview constructs flags without connecting. The
broker resolves/pins its configured Claude path, including symlinks. API15 fake
CLI proofs do not qualify actual native new-session/browser or promote capabilities.

G1.40 native HTTP cases add actual post-await capability withdrawal before stop
and a lost accepted reply with one dispatch/no replay. Both native Chat turns now
use public HTTP. Enrollment remains internal to the native fixture; no browser or
public native new-session qualification is inferred. Post-stop target-launch
failure stays sealed/recovery-required with its original uncertain receipt.

G1.41 `http-post-stop` and `http-post-owned` withdraw at actual native starting/
owned verification boundaries. They require positive source/target whole-tree
exit records, original uncertain HTTP receipts and exact retry refusal. Current
enclosure-handle count is bounded and is not a cumulative launch count. See the
tracked Claude public lifecycle evidence summary for pins, hashes and limits.

G1.42-44 add nonmutating native owner/idle facts and the separate offline Claude
browser fixture. With ACC_E2E_RELAY_NATIVE=1 under the existing private browser
fixture base, the main Playwright session-relay spec can qualify a scoped warm
desktop API-key new-session/Chat/Terminal/Chat join. The fixture alone supplies
synthetic vault auth/model and capability bits. Remote Control stays unsupported;
empty-first card and all production qualification/attestation defaults stay off.

G1.48 adds a scoped **native Terminal denial application** proof. The actual
person browser flow reviews the shipped Inbox card, denies once through the hub,
observes the exact marked action in a completed provider chain, checks its
original accepted answer receipt and returns the same native conversation to
Chat for a fresh turn. The denial retires only its exact tool ledger entry;
other work and draft/drain checks remain. SDK decisions/allow and late-proof
recovery are unqualified. See the primary denial-application artifact and ledger;
all rollout gates and terminalMediated remain false.


G1.50–53 extend the isolated public Claude browser join with native one-time Allow,
SDK Allow/Deny/first-Chat denial, and SDK/TUI held-final-model late application.
Only original receipts settle; no answer/input replay. Placement copies the
shipped acc-hook/acc-guard/settings reader into a private read-only installed
layout and uses that configured path. The guard profile proves actual secret-file
refusal before native tool admission, with no second reviewer/outstanding tool.
Installed hooks also exercise legacy SessionEnd arriving during handover; enrolled
legacy spool events are history-only. Evidence and any unresolved broader browser
failures are in the primary G1 ledger. This is no machine installation or live
account test; all production rollout and terminalMediated defaults stay false.


G1.54 `read-race` browser profile deterministically takes the existing native
runtime operation lock immediately after the second authentic owner-observation
callback; the private diagnostic wrapper releases it only after inspection ends.
No authorization result or Host callback is replaced. Assert the same input
client survives, no retained draft appears, native typing processes, and the
normal same-ID Chat return/fresh turn/whole-tree Stop completes. This models the
real registration interleaving that previously returned ownership OFFLINE; see
owner-read-race evidence for corrected original trace classification. Actual
replacement/revocation still refuses and never revives an input generation.

### G4 scoped service delivery

`tests/test_session_relay_service_native.py` uses the same pinned Codex0.167.1,
offline model, OS account broker and production Relay/NodeLink transport as the
G5/6 process harness. It submits an actual service operation through an existing
person-owned Chat writer, retains the service actor in its durable receipt and
never transfers the writer. Accepted and injected post-admission/pre-journal-loss
cases each require exactly one model request and no replay; the latter records
positive native admission before retaining an uncertain receipt. This is simulated
enrolled-node identity, not G8's enrolled TLS/partition/leader qualification.
`DELIVERY_QUALIFIED` and `PUBLIC_WRITER_QUALIFIED` remain false; fixtures alone
supply qualifications. Team/API entry and actor/assignment/budget/lease/race checks
use the real main/broker transport with synthetic provider retirement separately.
Claude service delivery and a full real browser/native service flow are not
qualified by this Codex proof. No live account, provider key or instance is used.


`tests/test_session_relay_claude_service_native.py` separately exercises actual
Claude2.1.308/SDK0.1.50/account-broker API-key service admission in the offline
placement harness. It requires a SERVICE receipt, positive correlated result,
accepted broker journal and removal of that exact stage before acknowledging the
delivery; repeated observation never redispatches. Existing real background
cleanup also runs, preserving unknown person permission outcomes. This is local
SDK/account-broker proof, not subscription or enrolled TLS evidence.
`CLAUDE_SERVICE_QUALIFIED` stays false alongside the other production gates.


`tests/test_session_relay_claude_team_native.py` adds the production team-message
entry to that actual API-key SDK path. It verifies the current source run,
assignment and attempt, accepted broker journal, and refusal before admission
when the source run ends. The team/source enrollment is synthetic local data;
subscription Claude is never driven. The shared harness also checks real native
background cleanup and owner-switch refusal without changing unknown permission
outcomes. `test_session_relay_claude_team_background_native.py` additionally sends
an incoming team delivery at the real background boundary and requires refusal
without a service journal claim or service payload, while preserving the person's
existing uncertain stage byte-for-byte.

`tests/test_session_relay_native_delivery_race.py` joins the actual cookie/CSRF
public take routes, stock Codex remote TUI and real OS broker to service delivery.
Both deterministic winners run: delivery admitted with a held native turn refuses
take; a take committed at the draining boundary refuses service enqueue before
its real native retirement RPC. Both verify the actual take result, fresh native
unsent input, exact request count and previous-writer/service-channel audit. The
transport barrier never fabricates retirement proof. This is local simulated
enrollment, without a browser or enrolled TLS claim.

`tests/test_session_relay_notice_native.py` routes coalesced lead intents through
the actual Codex/OS broker/Relay/NodeLink path. It requires distinct immutable
intent IDs, one SCHEDULE service admission and one model request, retained person
writer and immutable receipt on repeated observation. `test_session_relay_notices.py`
adds waiting/active stage supersession, unknown admission, untrusted-label fences,
stopped run-mode notices, parent batching and real project-brief notifier checks.
No proof promotes the production qualification flags or changes migrations.

The second-worker R1 regressions additionally revalidate private socket placement
following authority awaits, require frozen guard inputs to be regular files
(opened nonblocking so FIFO replacement refuses), and preserve the native hook's
original decision deadline across publication and verification awaits. A local
hook answer still reports unknown until conclusive native evidence exists.

The focused transport refusal matrix covers authenticated public HTTP, native
Unix WebSocket and framed Host paths, including supported text after a rejected
control. Browser restore/reconnect journeys verify observer-only navigation in
the production frame. Ownership-plan tests use synthetic process receipts; they
do not establish native draft/drain, installed hook placement or real node
partition qualification. The SDK/TUI/SDK native case explicitly skips when its
optional installed SDK is missing. None of these checks enables a capability or
removes a release blocker. See `docs/handoffs/2026-11-11-relay-r1-groups-8-13.md`
for exact commands, results and remaining requirements.

`partition_probe.py` runs the group 8 enrolled-placement lab. Use a self-contained
locked test environment inside this worktree's `.acc/`; the probe refuses to
mount shared account dependencies. It maps only source and those dependencies
read-only into an empty offline namespace, with a disposable passwd entry, CA,
Ed25519 key, database, broker socket and account home. No host listener or login
is used. Production `Node.connect_once` verifies TLS and signs the enrollment
challenge; production main, node relay, broker and the exact pinned Codex private
owner compose the run. The person/vault fixtures are artificial, capabilities
stay empty, and a staged operation is never delivered to native inference.

```sh
PYTHONPATH=src .acc/relay-r1b-venv/bin/python -m pytest -q tests/test_session_relay_enrolled_partition.py
```

Its three fixed scenarios are bidirectional TLS/TCP blackhole with normal
production deadlines, retained leadership-connection loss with the production
HA monitor, and authenticated node replacement. It checks wrong CA/old key,
withdrawn account, private socket inode substitution, final operation refusal,
sealed/uncertain receipts without replay, and old-token refusal on the fresh
node. Leadership uses the actual Authority class with a disposable SQLite
control store; this is placement/fencing evidence, not PostgreSQL advisory-lock,
replication, failover or external-fence qualification. Read the group handoff for
repetition outcomes before treating the group as qualified. All other release
criteria and default-off/input attestation restrictions remain in force.

`installed_security_probe.py` composes that enrolled lab with the real launch
compiler and materializer. Unmodified guard, hook and environment-reader sources
are copied into a private fixture installation and mounted read-only in the
offline namespace; only the native CLI invokes them. Synthetic loopback model
responses request a forbidden write and must consume the installed guard denial.
The fixed cases cover Codex, its owned read-only TUI, tampered Codex policy,
Claude SDK and tampered Claude policy. The Claude tamper case observes the
production refusal receipt twice without another native model request.

Conservative native tool ledgers still fence Claude idle handover when a denied
pre-tool hook has no matching native completion event. Explicit STOP cleanup can
release that SDK owner only with the authenticated whole-tree exit receipt.
Exclusive owner cleanup shares the base runtime's single shielded cleanup task
so simultaneous EOF/Stop and cancelled waiters cannot double-close resources.
This evidence does not qualify Claude TUI config/auth controls or enable input.

`temporary_claude_probe.py` uses generated-only HOME and CLAUDE_CONFIG_DIR under
pytest tmp, passed to its subprocess environment without changing the parent.
The only writable ordinary filesystem mount is namespace `/tmp`, backed by that
test directory. Host account/settings paths are absent; executables and source
are read-only. Character devices, PTYs and procfs are kernel interfaces. The real
pinned Claude print/TUI/print sequence must consume guard denials despite a
generated account config attempting to disable hooks. It does not inspect real
protected settings or qualify production Terminal input/config/auth controls.

The fixed enrolled `decisions` scenario starts a real Claude SDK permission and
races two server-side person resolvers through production Hub/owner admission.
One exact choice wins; invalid choice/digest/revision, expired operations and
revocation cannot consume it. A returned native/SDK write remains unknown, even
after the tool/turn completes, and a duplicate cannot replay it. These resolvers
are fixtures: no public native TUI grant or conclusive native-answer receipt is
claimed. Those remain explicit release blockers in the decisions inventory.

The enrolled `controls` scenario refuses20 native mutation shapes on the actual
production private socket before any model IO, then proves the exact same owner
and conversation execute an administrative text turn. This is deliberately not
a public text grant. Separate HTTP and framed Host cases verify unsupported
fields/editor/attachment/L1 controls cannot create operations, consume sequences
or call a transport. Joint public writer qualification remains closed.

The fixed `selector` bridge composes the standard real-frame browser journey
with an offline enrolled Codex owner and its actual owned TUI. Browser relay and
preference requests cross a bounded temporary-file channel to the production Hub
HTTP router; snapshots/tokens are real. A same-client negotiation invalidates the
actual old token to exercise reconnect. The bridge allows only observer methods
and fixture preference changes, checks one unchanged execution owner after every
request, and asserts no operations or model calls. The browser bootstrap rows,
rollout flags, principal and vault remain fixtures. Returning from Grid reuses
the retained session panel; distinct panels have distinct observer IDs.

The normal Playwright config runs one joint journey by default. Setting the test
environment ACC_RELAY_NATIVE_REPEATS=20 declares20 distinct native journeys,
keeping the existing setup dependency single and the existing worker count1.
No additional Playwright config or production input capability is introduced.

The fixed enrolled `handover` scenario composes the real production Hub ownership
plan, SDK owner and native tmux TUI. It requires a completed SDK turn, exact
--resume ID and visible native history, verified old-tree exit, current-person
API auth and final TUI tree cleanup. Revoked people and unapproved subscriptions
cannot stop/replace the original owner. The current unqualified TUI Host refuses
config/auth input and a reverse handover without genuine native idle/draft/drain;
the fixture does not fabricate those observations or use subscription automation.
Generated account HOME/config are confined to namespace tmp. The primary worker's
new input/admission implementation is a separate integration dependency, including
its explicit native transcript-writer protection requirement.

Group9's metadata candidate is deliberately qualification-only:
`metadata_security_probe.py` uses a throwaway HOME/config directory and one
ordinary writable mount backed by pytest tmp. The model fixture requests either
an indirect Python/Bash metadata write, Write, or Edit, followed by an ordinary
workspace write. The policy candidate combines strict sandbox startup/no fallback
with Edit deny rules and protects private run data from tool reads. Mutable
settings are excluded by the fixture CLI. It is not wired into production.

The vendor's [sandbox boundary](https://code.claude.com/docs/en/sandboxing.md)
protects shell subprocesses, while its [Edit permission rules](https://code.claude.com/docs/en/permissions.md#read-and-edit)
cover native file mutation tools. A path deny rule alone cannot cover arbitrary
interpreter writes. On this machine the pinned CLI refuses startup because
socat is absent. Native cases explicitly record that dependency refusal and zero
model calls; passing those checks cannot qualify positive metadata protection.
See the group9 metadata-blocked artifact and Inbox request tool-59a12c64874c.
No replacement binary, protected settings access or sandbox fallback is permitted.


G1.55 `chat-receipt` holds the final native model response after return to Chat
beyond the former aggregate assertion window. Require original receipt uncertainty
while held, exact actor/run/digest/ID acceptance after release, one HTTP submit and
one finished native admission publication. All native fresh Chat cases verify the
original receipt before separately checking rendered counts. This is no runtime
settlement timeout change or implicit resend/recovery permission.


G1.56 compact desktop390x844 and second-person Claude profiles use the same private
API-key launch and pinned CLI. The latter covers5 owners on one UUID, identical
Terminal text from two people, actor-private receipt refusal, fresh Chat, retained
read-only old inputs and5 positive empty-tree records. HTTP attachment404 reset
revalidates current central identity/role and session access; real denial clears
private drafts. The selected r1b1ece39f23 dead-pane cleanup is atomically guarded
by pinned server/session/pane/PID/deadness after whole-tree proof. No whole-worker
merge or release qualification is inferred from this scoped integration.


The G1.57 sdk-window/window-permission and Allow counterparts hold only the private
model's final response35s, beyond the real30s fast-observer window. Require zero
fast observers/one retained original answer before release, then original accepted
receipt, native effect and same-ID fresh Chat/verified Stop. The long barrier is
fixed60s in the enforced offline fixture only. No production timeout is enlarged.

G1.58 `reconnect-reader` explicitly detaches its own original public attachment,
reloads with a fresh observer and requires the same Claude screen/process/run/UUID,
processed/admitted watermark4, no Connect input claim, then explicit Chat handover,
fresh accepted receipt and3 empty process trees. The old writer grant is not
renewed. Screen reads use their own fresh opaque main read grants and still check
process identity/guards/reader authority after awaits; opening/input retains writer
authorization. This profile begins with an empty processed draft.

G1.59 sdk-stop-unknown/sdk-stop-allow/stop-unknown/stop-allow hold the private final
model response, receive one unknown native answer, then intentionally Stop. Verify
first Stop last_error and1 SDK/2 TUI empty process trees. Release after Stop must
leave the exact original operation/actor/run/digest uncertain, one answer, zero
active owner/retained application tasks/records. No acceptance from incomplete
chains, answer resend, restart or claim. Do not send a redundant second cleanup
Stop; repeated-Stop recovery is a separate unqualified integration criterion.

G1.60 logout-reader uses a distinct disposable person cookie to read actual native
Terminal output without an input claim. Public logout must clear/remove that
screen and return401 without native text. Writer process/run/UUID/watermark stays
unchanged; explicit primary Chat handover/fresh receipt/Stop3 trees still succeed.
This does not revoke the writer or qualify device TLS/distributed recovery.

G1.61 reload-window-permission/reload-window-allow reload the actual browser after
35s with one original unknown answer retained past the30s fast observer. Fresh
read attachment sees same process/run/UUID and4 admitted frames without claim.
Release must settle exactly one original operation and native answer, followed by
Chat/fresh receipt/Stop3 empty trees. Enclosure count is live1, not lifetime2.
One-shot reconciliation may briefly run after the fast observer expired.

G1.62 writer-logout uses a disposable actual writer cookie. Public logout removes
input and denies a new unsent frame with the old opaque grant; admitted/processed
must remain4. Another authorized person explicitly hands over to Chat on the
same UUID, gets a fresh actor-private receipt and verifies3 empty trees at Stop.
This covers idle revocation only; it never replays a draft or prior key.

G1.63 sdk-stop-repeat/sdk-stop-repeat-allow/stop-repeat/stop-repeat-allow repeat
public Stop after the original unknown SDK/TUI answer and live-runtime retirement.
Both Stops must clear no uncertainty by inference, report confirmed exit and keep
one native answer/zero owners.1 SDK/2 TUI whole-tree records each. Main's exact
exit fact is separately tested through a new manager/database engine and changed
run/placement/identity/malformed-proof negatives. No provider restart or query.

G1.64 sdk-cache/sdk-cache-allow/cache-permission/cache-allow hold final native
application, clear main's answer correlation cache through the isolated capability
fixture, require durable original1, then release and verify original acceptance,
same-ID fresh Chat and1 SDK/3 TUI empty trees. No permission reply retry. Separate
fresh-engine/identity-negative stubs do not claim full process-restart recovery.

G1.65 revoke-allow uses a disposable writer login. After main's actual Allow
verifier returns true, revoke that login before native reply; require real CLI
error tool_result, no effect, original Allow unknown, read401/other-actor receipt404,
same owner and other-person Stop2 empty trees. Synchronize on submitted operation
and synthetic diagnostic, since the signed-out browser can cancel its response.

G1.66 take-busy holds the actual native response, registers public switch.prepare
using the real connection and canonical full digest, requires INPUT_CONFLICT,
explicitly cancels that zero-write queued registration, and proves same owner.
Release then normal Chat/fresh receipt/Stop3 trees; no frame or registered take is
replayed. RegistrationResult contains nested receipt; cleanup releases its barrier.

G1.67 tests/test_session_relay_stop_placement_stubs.py is deliberately no-IO:
remote node/account/link/key/leader and supported-mode identity changes, API-key
and subscription profile labels, no fresh runtime/query/kill call. This is not
real placement or subscription qualification. Consent/policy tests are stubs;
Josh performs real subscription checks manually at release.
Retained history reads keep the existing item/byte bounds and apply the current
redactor on every replay. If one redacted/JSON-encoded event cannot fit in an
otherwise empty history page, the API returns `LIMIT` rather than an empty
successful page with an unadvanceable cursor. It neither skips nor truncates that
event. A preceding bounded page can still be returned; if the current redactor
later yields a bounded event, the same cursor can resume without data loss.
`tests/test_session_relay_history_capacity.py` covers expansion, retry and
multi-page progress without input grants or new native owners.

G1.68 adapts G56's real OS main-worker/controller (141ece973) into
application_restart_worker.py/application_restart_fixture.py. The outer offline
Claude namespace outlives SIGKILL/wait/restart of main, broker or both; main uses
production HA promotion/write fencing with a simulated account link and SQLite.
The broker persists the actual native Allow/Deny application before the fixture
withholds publication. Version4 is a private authenticated read-only fact reader,
not an enrollment/owner recovery mode. The original receipt can settle without
launch/query/answer replay; pre-fsync crash stays unknown. Original answer count1,
unchanged model-request count, absent old execution, sealed row are asserted.
This does not qualify live node TLS/Postgres or subscription placement.

G1.69 wraps the unchanged Codex app-server executable/argv in the same subreaper
enclosure used for other owned processes. Stop closes every retained TUI/Host and
requires producer plus view empty-tree receipts before its typed enclosure-v1 ACK.
A legacy root-only ACK cannot persist a retired-Stop fact. The warm native public
cookie/stock remote-TUI take-over test checks multiple retained trees and repeated
Stop with a fresh manager; the supervisor-loss variant must remain unconfirmed.
Its deliberate orphan is contained by the outer disposable PID namespace, which
owns final cleanup. No account process or global PID/name kill is used.

G1.70 adds native Allow/Deny durable-fact actor/digest/binding/file-mode faults after actual broker kill/restart; all remain unknown without replay. G1.71 deterministically cancels after the fsync writer commits or fails the post-write guard and requires deletion/no recovery; native both-restart Allow/Deny is rerun after the fix. These do not qualify the full native stream metadata/cancellation matrix.

G1.72 exercises actual native work during a final positive-idle observation await,
pre/post-Host cookie revocation, SDK/Codex dispatch revocation and switch phase
withdrawal. G1.73 kills the owned native child in SDK/TUI at idle/busy. G1.74 kills
the supervisor instead: SDK query count must not advance, evidence cannot be
accepted and the missing empty-tree receipt keeps its bounded quarantine. The
outer disposable PID namespace cleans the deliberate orphan. No live account.

G1.83 adds `test_session_relay_native_decision_scope.py` and
`relay_decision_faults.py`: actual isolated native decision expiry/revision,
policy/epoch/Stop/account/budget changes, positive controls and explicit Deny.
The Codex queued cases hold the actual stdio write lock, mutate main authority,
and require a fresh verification after lock release with zero approval writes.
The Claude TUI reply gate and Codex queue gate each passed twenty consecutive
native expiry runs; see the decision-delivery artifact for source pins and limits.
Pending-exit helpers are separately qualified; their presence is not a release claim.

G1.84 `test_session_relay_native_pending_exit.py`: five isolated native cases
qualify Claude SDK/TUI pending-permission vendor exits, installed guard-helper
loss, and an exact owned PermissionRequest helper exit. This is a Claude slice;
Codex/provider exit variants remain in the primary ledger.

G1.85–89 primary fault proofs (all remain disabled production qualification):
`test_session_relay_codex_native_exit.py` covers producer/TUI/supervisor loss;
`test_session_relay_codex_native_stdio.py` covers stdin truncation and native
reply loss, including the main disconnect/settlement race (EOF20/20).
`test_session_relay_native_retained_capacity.py` covers actual sixteen-generation
Codex and sixteen-stream Claude bounds. Application-task pressure is a separate
fake-CLI/synthetic-identity test, not a native permission storm.
`test_session_relay_native_history_recovery.py`, `native_sdk_metadata.py` and
`native_codex_metadata.py` (all with `test_session_relay_` prefixes) distinguish
real main epoch/compaction from deliberately injected decoded native metadata.
`test_session_relay_native_replacement_recovery.py` uses actual OS main/broker
restart and a different new person-enabled public session; original fact recovery
and stale grant/publication/hook refusals must leave its owner/query unchanged.
It does not qualify same-public-session crash re-enrollment; see the G1.89 Josh
question. Artifacts retain source hashes and exact pass/failure scopes. Final
source execution with other workers remains on acc/relay-integration, including
the legacy stock-decoder framed_live_chat failure recorded in G1.85.
