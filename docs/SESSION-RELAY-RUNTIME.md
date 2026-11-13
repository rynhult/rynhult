# Session relay broker runtime (disabled integration candidate)

The relay runtime is an internal broker seam, not an enabled provider mode.
Ordinary launches are unchanged. `Broker(..., relay_transport=None)` refuses
`session_runtime`; its ping reports version 1 and `available: false`.
Main must supply the authenticated callbacks and the opt-in feature gate before
using the seam. Availability is never a permission grant.

## Transport and authority

`BrokerClient.session_runtime(enrollment)` opens a dedicated, non-retrying
connection to the existing account broker. On a node, use
`NodeLink.open_session_runtime(account)` as its opener; this requires the node's
`session-runtime-v1` transport capability. The node advertises transport support,
not a qualified Codex capability. No new network listener is added; the bounded
Terminal observer endpoint uses the existing authenticated HTTP router.
The existing broker peer-UID gate and signed/enrolled node channel still apply.
A root-only policy channel does not gain the relay surface.

`RuntimeTransport` receives callbacks for authenticated enrollment, opaque grant
resolution, adapter dispatch and authorized receipt reads. Main must bind them to
the installation, node enrollment/link, account, session/run, actor, operation,
canonical digest, frozen policy and deadline. A native request cannot supply these
callbacks or authenticate itself. The broker's own `verify` callback must recheck
current authority after asynchronous work. Wire requests inside this channel are:

```json
{"id":2,"version":1,"method":"dispatch","params":{"grant":"opaque-main-credential"}}
{"id":3,"version":1,"method":"receipt","params":{"operation_id":"hub-operation"}}
```

All other methods/fields are refused. Enrollment must return the current fence
for the broker-generated incarnation/link identity. Authority epochs are persisted;
replacing a broker or hub requires a newer authenticated main epoch. EOF removes
only that link. Runtime channels abort queued node writes on close rather than
using the ordinary broker channel's graceful drain. Recovery reads never dispatch.

## Durable admission and process ownership

`AdmissionJournal` uses SQLite `user_version=1` in an account-owned 0700 directory,
with a 0600 database. This is its own file schema, with no Alembic migration.
An immediate transaction stores an uncertain claim before the native call. Exact
replays return stored evidence or unknown; changed fingerprints conflict. Raw
prompt bytes are absent from the journal. Defaults are 4096 records and one day
of concluded-result retention. Unresolved claims are never evicted. Collecting
concluded records advances a durable grant deadline floor; expired claims cannot
be recreated after collection. Capacity refuses before a new claim.

`PrivateRuntime` holds an account-local lock keyed by provider/native conversation,
including across distinct binding IDs. It pins private directories, refuses
symlink traversal and runs one private `codex app-server --listen stdio://` child.
There is no provider socket symlink or shared daemon. The owner lock is inherited
by the child, and explicit Stop signals the owned process group. A durable start
marker prevents automatic restart/resend after a crash. A retained marker requires
explicit recovery; replacement/recovery is not yet qualified. Root or a hostile
process running as the same account remains outside this managed boundary.

The reviewed compiler supplies environment and frozen overrides; hooks are forced
on last. `Codex.relay_args` translates the existing compiler's sandbox, approval,
model, MCP, instructions and other overrides into private app-server arguments.
`PayloadStaging` stores bounded exact outbound bytes under generated names, with
no-follow/no-clobber writes, digest checks, private permissions and a retained-byte
quota. Only settled payloads may be removed by the trusted hub integration.

## Adapter and Terminal limits

The default Codex declaration marks every capability **unqualified**. Startup
requires a matching recorded qualification and live version/digest/schema/guard
probe. It initializes the private owner and verifies the reviewed native thread
on resume. Text admission requires a correlated native turn and in-progress
status; lost or malformed responses stay unknown and block another turn.
Interrupt targets the exact mapped run/turn and reports requested separately
from confirmed. Configuration, steering, attachments, rich history, L1 switching
and ordinary questions remain unavailable. The adapter exposes no raw RPC tunnel.

Trusted hub decision registration supports only bounded, exact command/file tool
requests and only native-offered one-time `accept`/`decline` or explicit `cancel`
(turn cancellation with a deny effect). Wider permission families, session grants
and policy amendments are refused. The hub must win its decision CAS first. A sent answer is
single-use and remains unknown: a response write or native request-resolution
notification alone does not prove the answer applied. The journal is the durable
outcome source; the adapter never retries an answer or prompt.

The default native proxy refuses writable Terminal. An isolated fake can inject
proven request/watermark correlation to test attribution and hub admission.
That does not qualify the stock TUI. No production proxy socket is exposed until
correlation, native refusal/draft behavior and decision interception qualify.

`TmuxTerminalHost` wraps the existing framed attachment byte path and preserves
paste markers and key bytes. Its opener and authority callbacks belong to the
trusted integration; use one host per attachment. Reads require an explicit
current read-authority callback. Input is sequenced and revalidated at the write
barrier. A partial write poisons the stream against replay. The existing bridge
has no qualified decoder acknowledgement, so `processed` stays zero and a drain
or takeover after written input refuses. This limitation keeps terminal input
unqualified, rather than pretending a socket drain proves tmux processed input.

The synthetic schema subset in `tests/fixtures/session_relay/codex/native.json`
is based on [official app-server documentation](https://learn.chatgpt.com/docs/app-server).
It is neither a captured CLI schema nor native qualification. No live provider
or account was exercised. See the dated handoff for test evidence and M2 gates.


Pinned Codex qualification now composes the production owned TUI with opaque,
attachment-bound public input. Unsupported text codec frames are projected before
main reserves a sequence; known refusals leave the stream healthy. Supported
text and final Enter still require exact native decoding and a conclusive main
receipt before processing advances. A submitted Enter with a lost acknowledgement
prevents take-over even when provider admission is known.

At an idle approved take-over, an unsubmitted draft stays in its permanently
read-only previous client. The replacement client has a distinct identity and
fresh input on the same execution session. Neither retained drafts nor previous
answers are replayed or typed automatically. Existing read attachments can see
bounded redacted current and retained screens; reads never claim or launch input.
Qualification fixtures explicitly override capability gates; public rollout and
terminalMediated remain disabled.

Claude L1 replacement retires the old owned ended pane after its authenticated
kernel empty-tree receipt. It pins tmux session/pane/PID/server identity and tests
deadness in the tmux command server before removing that pane. A live/reused pane
or a changed target refuses; neighbouring panes are untouched. The captured
previous screen remains retained, input grants and Hosts stay retired, and the
normal launch name-collision guard remains active. This fixes repeated empty-
boundary SDK/TUI activation without deleting a live view or growing dead views.
Nonempty Claude draft retention still needs separate qualification.

Hub review cards are shared above Chat and Terminal and are independent of an
input lease. They require negotiated decision support and the existing explicit
qualification gate. Claude review shows the frozen session workspace and complete
bounded tool inputs; missing/oversized review context offers denial only. Rollback
allows only server-held denial choices. Distinct exact pending questions can be
reviewed while an older chosen answer remains unknown; that older answer has no
buttons and is never replayed. A browser leaving after a committed CAS does not
cancel/resend the answer task; fresh cookie/attachment checks still fence native
release. Native permission response writes are not conclusive application.
