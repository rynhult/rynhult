# Child permission enforcement

Implementation: `session_capabilities.py` (portable validation and executor
barriers), `server/session_caps.py` (live authority), `server/child_caps.py`
(parent proofs). Migration **0092** follows 0091; 0093 remains reserved for the relay migration.

## Records and authority

Every newly managed launch records `cap_requested`, a `cap_effective` generation
snapshot and a narrowing-only `cap_limit`. The current effective set is computed
from its request and limits and every ancestor's request and limits. `null` is
unconstrained **by that grant**, not an exemption from machine, account, project,
Security, consent or operation checks. `[]` denies the dimension. Sets intersect;
folder roots intersect hierarchically; expiry uses the earliest timestamp.

`GET /api/sessions/{id}/permissions` returns requested/current effective sets,
the generation snapshot, immutable lineage and `native_helpers_capped: false`.
`PATCH` accepts `{permissions: {...}}` from the recently verified root person's
browser, and can only narrow. Installation tokens and agent identities cannot
mutate it. An API-owned root retains the installation-owner fallback. A request
cannot borrow a person's later approval to exceed the parent's current ceiling.

0092 first normalizes NULL session owners to the existing installation principal
`@api`, then backfills `parent_chain` and `root_owner` from retained links, refusing
orphaned/cyclic lineage. SQLite and PostgreSQL triggers refuse **any UPDATE OF**
`parent_session_id` or `parent_chain`, including no-op assignments. `root_owner`
can change only with `owner` changing to the same value in the same statement.
NULL owner/root-owner updates are refused. `chain()` also refuses missing recorded
ownership, including on retained rows. The ORM permits the paired change only inside the owner-only removal/transfer
operation. It transfers the whole owned chain and writes `session.owner.transfer`
in the removal transaction. Before ownership/project updates, it checks each
saved proof and its ancestors. Only previously valid proofs with a recognized
digest scheme are refreshed, in ancestor order; missing, malformed, stale or
unrecognized evidence stays fenced. Root specs are untouched. Changed child specs
are updated in place, or copied when still shared by other sessions, leaving no
orphan spec rows. Their existing digest formula is retained (compiler inputs/files,
legacy files JSON, or compact files JSON). The audit records refreshed/preserved
sessions, digest changes and schemes, and consent-review requirements. Links stay
immutable. A head-migration test checks all three SQLite triggers survive upgrades.
A retained NULL-owner database is upgraded and its root/child chain checked.
The migration refuses downgrade while capability evidence exists. Restoring a
pre-upgrade backup or forward repair preserves that evidence; do not run an old
binary against the upgraded database.

Parent grant digests include recursively chained ancestor digests, frozen launch
inputs/files, scope-relevant Security switches/enforcement and custom blocks, person/account/project binding,
team limits/expiry and per-session requests/limits. A changed ancestor
invalidates saved child generations. Legacy children without parent proofs fail
closed; no authority is backfilled into their launch files. Global Security revision,
source metadata and resolver digest are excluded: edits in another scope do not
invalidate a child. Team liveness/revision is checked separately from grant values;
ordinary children require running ancestors, while person-launched explanations
retain caps/Security/expiry without requiring their source or its team to run.
A gate already waiting for cancellation keeps its recorded stop reason while
stopping and polling the recorded process; source-cap failures cannot overwrite
an explicit Team stop. Uncancelled gates retain source checks. Root preview and
launch use the same default `parent_session_id: null` guardrail field, with child
launches replacing it with their recorded parent ID.

Pending parent/child chains bind at insertion in the same transaction; persisted
links remain immutable. Ancestor Security checks validate the child's frozen
inputs and protections against each ancestor's current policy, rather than
requiring an older parent's own generation to remain resumable. A newly compiled,
more constrained child can therefore satisfy a tightened policy.

A person's explicit Team limit edit or Team resume renews only proofs that were
valid before that operation, in ancestor order, using their recognized digest
schemes. The Team audit records `proof_digest_changes`. Missing, malformed, stale
or unknown proofs remain fenced. An expired session cannot resume independently
of its Team's reviewed deadline renewal.

Inbox card selection uses the session provider's own hook source from the same
batched, authorized metadata query that supplies parent-chain/root-person labels.
Server-only card/input snapshots are stripped from incoming hook data before
server construction; malformed stored/browser card data is refused. Card-based
Push summaries retain the command hint and append the recorded child chain while
keeping the existing root-person recipient. Background tool workers check the HA
fence before request/capability inspection and again immediately before execution.

MCP intersection records `dropped_mcps` in the frozen guardrails policy. Session
summaries (including helper start replies and Team members) return that exact
list; the Team UI shows it beside the helper. Both the start decision audit and
successful `session.create` audit include the names, so narrowing is not silent.

Provider option definitions declare strictest-to-loosest permission orders.
Admission refuses widening any dimension within the same provider. Across
providers it compares shared levels: read-only (0), asks (1), edits without asking
(2), auto/classifier (3), bypass (4). Claude default/dontAsk are 1, acceptEdits 2,
auto 3 and bypassPermissions 4. Codex workspace-write/untrusted is 1;
workspace-write with on-request or never is 2; read-only is 0. Full-access is 4.
A level-4 option takes precedence over read-only; other action levels are bounded
by a read-only sandbox. Provider-specific dangerous flags are checked separately:
Codex never retains its own same-provider approval/bypass requirements even
though its shared action level is 2. All provider mode maps
live beside their option definitions. A read-only choice bounds other non-bypass
options. Cross-provider bypass flags never substitute for the child provider's
own bypass authorization: those still require matching same-provider flags.
Claude default cannot start Codex workspace-write/on-request; acceptEdits can.
Refusals say which action level the child requires and which the parent permits,
e.g. “child requires edits without asking (level 2); parent permits asks (level 1)”.
Codex never still cannot inherit bypass authorization from another provider. Endpoints must equal the parent's or be unset.
The sole approval-order exception is a fixed read-only Codex explanation with no
MCPs: `never` denies escalation instead of stalling unattended output. The fixed
explanation runner also disables shell tools, web search and native multi-agent.
OpenCode explanation launch inputs explicitly select its `plan` agent, including
when the source uses another provider. This is admission/compilation behavior;
OpenCode still has no qualified print-mode explanation runner in this repository.
The explanation agent picker uses the runner's supported-provider set, omits
OpenCode, and reports its unavailable reason beside the picker. The server also
rejects selecting it through the normal explanation API.

## Execution barriers

| Dimension | Admission and execution enforcement |
|---|---|
| Folders | Agent execution resolves the current grant before dispatch. The person's Files browser retains its existing session/project access checks and does not borrow agent caps or parent liveness. Native restricted launches refuse. Qualified containers only mount the managed checkout as project data; a grant excluding `workspace` refuses, and extra roots are not mounted. |
| Network | Native restricted launches refuse. The per-session container gateway reads the effective envelope, intersects its exact HTTPS destination list with the mandatory machine exclusions, and checks before DNS/dial. Existing relays have the session deadline; stopping the gateway closes active connections. Offline containers have no alternate egress. |
| Installs | Tool request validation, person/batch approval and execution recheck the current manager set. The broker checks its typed envelope again. Raw account changes remain hook-blocked even with a prior tool-check exemption. Native restricted launches and legacy lifecycle-capable installers refuse; the reviewed extractor rechecks on each publication step and refuses scoped folder grants it cannot honor. No extractor qualification is newly granted. |
| Ports | Current sets filter delegated reservation candidates and fence publication request, approval, DNS mutation and root-helper dispatch. Native restricted launches refuse; containers do not gain host listeners or published ports from this grant. |
| Provider/model | Frozen selection candidates carry the parent's digest and reject disallowed providers/models. Common session creation, explanation sub-runs, final dispatch and the broker check exact provider/model pairs. A restricted unpinned/default model refuses. |
| Time | Every current-capability admission checks the earliest absolute expiry, including team expiry. Broker launch, hooks, gateway CONNECT/relay and delegated gate dispatch/runner enforce it. Reconciliation stops the saved session generation; gate polling cancels an already running delegated gate. |

The final broker launch uses `session-capabilities.json`, included in
`ACC_GUARD_FILES`. Restrictive sandbox launches require the broker's explicit
`session_capabilities: 1` protocol. Unknown/old executors refuse; there is no
fallback to a broader native execution path. The generation snapshot is admission
evidence, not proof that a process started or that a stop has completed.

Parent changes fence launch/resume, relay mutations, managed team actions,
install and publication admission immediately at those checks. The live-process
check is each supervisor reconciliation; it records stop intent and uses the
normal confirmed kill/retry path, closes the run and records `stopped` after
confirmation. A running gate checks its source each poll. Changed sandbox grants
require a narrowed new generation. Offline stops remain unconfirmed until the
machine answers; already dispatched effects cannot be recalled or reported as
undone.

## Launch sources

| Source | Binding |
|---|---|
| Legacy helpers, v2 selections, assignment attempts and continuations | All use `Supervisor.create_session` with a server-bound acting parent; payloads cannot select another parent or widen their requested sets. |
| Other internal session-origin launches | The common creator requires the authenticated `session:<id>` to match its bound parent. |
| Explanation sub-runs, including provider fallback | Bind the source session, inherit its caps/Security and recheck before broker dispatch. Their isolated local project may differ from the source project. A container source cannot fall back to a native explanation executor. |
| Gate preparation, start, rerun and running polling | Resolve the requesting session before preparation and dispatch; refuse capabilities the native gate runner cannot enforce, cap its absolute deadline and cancel after a parent narrowing. |
| Multi-run and schedules | Person-owned roots, using common creation and the same request validation; multi-run propagates its explicit request to each alternative and schedule launch bodies retain theirs. They do not invent an agent parent. |
| Restart, resume and automation | Reuse the recorded session and its immutable chain; final generation admission rechecks current authority. |
| External observation | Does not claim a managed launch or invent a parent. A sub-run refuses an external source with no qualified managed grant. |
| Provider-native helpers / unmanaged subprocesses | **Not capped or tracked by Rynhult.** No synthetic child record is created. The Guide and session Details state this. Use managed helpers when separate enforced grants are needed. |

## Refusals and approvals

The broker supplies protected `ACC_PARENT_SESSION_ID` / `RYNHULT_PARENT_SESSION_ID`
for native launches; the root container grant validates and supplies it too.
The hook's single attribution function prefixes every refusal wire format with
`limited by <parent>`, including missing/tampered policy, deadline, checker error
and timeout paths. A hashed capability envelope also enforces its deadline and
forces account-tool checks. This is attribution plus the existing hook safety
net, not an OS sandbox for native same-account code.

`attention.raise_item` routes default child Inbox/Push items to the root person and adds
the chain. Explicit owners-only alerts remain owners-only. Structured immutable provenance is also returned for Inbox items,
tool requests and publication records; shared UI renders it independently of
agent prose, including publication summaries and confirmation dialogs. A lead's
saved install consent cannot answer a child request. No `delegate_to_parent` or
terminal auto-answer path exists. Child tool approval requires an authenticated
browser session belonging to `root_owner`, through the same shared validator at
individual decision, batch preview and batch approval; an API-owned root uses a signed-in
human instance owner. Tokens, agents and other instance owners cannot approve a
person-owned child's tool request. Publication remains a human **instance owner**
approval, which need not be the root person. Unexpected capability-check exceptions
record stop intent with an explicit refusal reason. Their log entry includes the
exception type and message, with session-vault and common credential redaction,
normalized to one line and capped at 300 characters. Raw exception tracebacks
are omitted; a redaction failure logs a fixed fallback and still stops execution.

Same-UID native spool impersonation remains the documented limitation in
the source design document `docs/DELEGATED-APPROVALS.md`; it is outside this change.
Live OS/provider qualification is separate from the bounded disposable checks.
