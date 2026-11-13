# Delegated approvals: consent foundation and bounded execution

Phase-one base: `475e6063`; migration: `0083`. This slice implements scoped consent records,
shadow eligibility, exact pending-tool batching and decision provenance. It does
not qualify a new broker executor or enable automatic tool installation/main
finalization. `GET /api/approvals/consents` explicitly returns
`execution_qualified: false`. The new shell and setup-level templates come later.

## Phase-two implementation status

The phase-two brief is only partially implemented in this branch. Account installs
and recipes remain unqualified: the unchanged npm executor does not prove the
canonical dependency/lifecycle/destination footprint. No new delegated tool install
is enabled. A broker admission wrapper needs resolution before completing that path.
Main finalization, provider prompt mediation and setup-level templates remain outside
this slice.

`approvals.enabled` now defaults Off when absent; explicit existing choices are
preserved. The existing Teams availability ceiling also applies. Revocation and
history remain available when disabled. A disabled feature cannot admit new work;
replaying an identical completed request only reads its retained outcome.

Migration **0089** adds `delegated_action`: stable caller-bound request key, typed
payload/digest, real deciding lead, person-issued consent/revision, authority snapshot,
state and result. It does not backfill consent or change guard switches. Nonempty
operation history fences downgrade, including failed or uncertain attempts.

The team spool adds `delegated_port`, `delegated_image`, and `delegated_status`.
Delegation is appended to the slice registry and MCP discovery after the existing
slices. Only `delegated_status` is a read in both MCP annotations and the guard's
exact read allow-list. Read-only Codex team launches explicitly approve that
status lookup and prompt for both delegated mutations; unknown tools retain the
existing default prompt. The two mutations receive no Plan auto-approval.
The logical caller must be the live bound Build lead. Native helpers share the
lead's Unix account and can append records to the lead's `events.jsonl`, so this
is not per-session OS isolation: a helper can act as the lead and consume the
person's bounded consent. Separate per-session spool credentials would be needed
before wider delegation. Local native scopes only; remote,
container, custom endpoint and unknown families fail closed. Requests cannot select
another caller, account, node or project. Short write transactions use the existing
writer fence and off-loop worker path. Duplicate keys with identical payloads return
the recorded operation; changed payloads refuse. Crashed/dispatched image operations
remain inspectable and are never retried by replay.

Port consent is `port.reserve` with exact `range` (`dev` or `prod`) and
`max_reservations` (1–10). This implementation is stricter than the design's L port
capability: it currently requires person-issued C consent. The aggregate cap covers
the parent and all children; release or another child does not reset it. The local
registry is the only side effect; outside-service reservations remain untouched.
It grants no publishing, listener binding, remote port or firewall authority.

Image consent is `image.check` with exact `provider`, `model`, numeric non-root
`uid` and broker `runtime_id`, matching the lead's
frozen execution provider/model. The existing bounded probe and evidence sanitizer
are shared with the person route; consent/revision/scope are rechecked immediately
before probe dispatch. Only the explicit `image-check-delegation-v1` broker protocol
is accepted; older/unknown protocols refuse before dispatch. The broker independently
checks kernel UID and installation runtime immediately before its probe. Attempts count toward two per exact account/machine/model
per rolling 24 hours and five per account/machine per rolling 24 hours, across
consents/teams; failures count too. The existing one-minute provider cooldown and
budget pause remain additional limits. Native probe deadline is 30 seconds.
Unknown and failed results never become verified. No assigned private image is sent.

Security displays active and revoked saved permissions and supports revision-bound
revocation. Inbox's Team activity digest reads the latest 50 retained operations
through `GET /api/approvals/activity`, without answer cards. It is personal by
default; an explicit authorized personal project lets owners inspect all issuers.
Group projects and installation API tokens refuse. Activity stays readable after
revoke/disable. Consent responses expose per-record `execution_qualified` for ports
and images; the legacy list-level flag stays false while tool execution is unqualified.

`approval.use` records the consent/lead/digest chain; `approval.result` records the
observed bounded result, distinct from admission. A dispatch is not completion.
Refused delegated admissions write `approval.refused` after rollback, at most once
per caller/action/fixed reason per minute. Security's refused-attempt history shows
these entries even without consent and while disabled. Reads are not audited.
Dispatched image checks older than the 30-second lease plus 30-second probe deadline
become uncertain at accd startup and later reconciliation; quota consumption stays.
Unexpected failures store the fixed reason `Internal error`, never exception text.
Port reservations use the project's owner and also retain the ordinary `port.reserve`
audit entry with the delegated operation identity.

## Authority model

| Source | Meaning | Enforcement in this slice |
|---|---|---|
| L | Lead within the frozen grant | Existing validated team coordination paths keep their limits and record the actual session in successful coordination audit entries. |
| C | Recorded person consent | Exact typed resolver, bounded team children and single-use admission claims. Eligibility is separate from executor qualification; unqualified execution refuses. |
| P | Person only | Existing approved tool paths, all unmatched actions, root/shared/removal, publication, secrets, account/role/security changes, group approvals and unknown provider prompts. |
| I | Information | Existing alerts and advisory risk labels; acknowledgment never authorizes execution. |

Effective authority is the intersection of Admin availability, OS/broker limits,
object ownership, current Security revision, frozen role/team grant, applicable
recorded consent and operation-specific validation. A project or worker cannot
widen its parent. Deny/revoke beats an allow; conflicting or unknown policy fails
closed for execution. An AI may explain or propose; it cannot classify its way
past a mandatory check.

**Never delegated:** arbitrary/root/system maintenance; new OS/folder/ACL/identity
or security-policy grants; publication/DNS/Caddy or public-access expansion; new
secret grants/export/remote-delivery consent, credential login/MFA; device pairing;
recovery/restore/fencing overrides; report-sharing consent; changing a person's
constraints or widening Prepare to Build; bypassing a Stop/expiry, group approval,
required independent review, branch protection or signatures; accepting a test
failure as clean; rewriting remote history. Main integration without its explicit
person-issued consent is never allowed. No level or Unrestricted overrides these.

**No raw terminal auto-answering.** No provider adapter, keystroke mediation,
sandbox changes or input arbitration changes are introduced. Unsupported, screen
and free-text permission prompts require the person. See
[the continuity contract](design/continuity-contract.md).

## Consent and requests

Only a recently verified authenticated person can issue typed consent through
`POST /api/approvals/consents`; existing CSRF/Origin/session checks apply.
Installation API tokens cannot mint, bind or inspect named-person consent.
Existing token-authorized tool decisions remain specific P decisions with the
visible attribution **Owner via API**. No setup level is inferred or backfilled.
Legacy false guards, tool exemptions and provider options retain their behavior.

Records bind issuer database ID/role/account, project/owner/workspace/remote,
stable node identity/account, Security revision, launch digest, team generation
and frozen grant digest. Group projects fail closed pending a separately reviewed
group/account-authority integration. Tool-change account UID qualification is still
missing from its broker protocol, one reason tool execution remains unqualified.
Approval routes check resource visibility and refuse group checkouts before
validating body/query fields, including for installation owners. Consent issuance
and binding check recent person verification first. Mutation paths recheck authority
under their existing write lock; the early checks grant no authority.

The phase-one action classes are `tool.install`, `main.merge` and `main.push`;
this branch also adds the port/image classes described above. Unknown fields or
action classes refuse. The narrow tool schema records an exact npm name/version
and canonical registry origin. It does not classify npm's current executor as
qualified: dependencies, registry overrides, destination/UID checks and artifact
integrity need a separately qualified typed executor. System/shared operations,
removal, Go, Homebrew, recipes and other managers stay P. Project-local dependency
rules are unchanged. No registry policy authorizes arbitrary shell text.

Once consent binds an exact normalized request/candidate digest; team consent
ends on stop, expiry or generation change. Project consent is an explicit durable
policy with optional expiry, and only issues same-scope live-team child records.
`POST /api/approvals/consents/{id}/bind` requires the recently verified issuer;
that person is the audit actor, with the target lead recorded separately.
Children cannot outlive the team/parent, widen constraints or survive parent
revocation. Security/issuer/object changes invalidate eligibility. Grants do not
approve pending requests. Inspection reports stored and effective states separately.

Main merge and remote push are separate consents. Recording either does not change
a ref. The shadow schema stores a repository/remote/ref and verification-policy
label; it does not yet prove the repository’s configured default ref, canonical
remote or a structured person-owned verification-policy reference. Private workspaces require promotion first. The current repository has no
qualified candidate/base/export-bound finalization verifier and integration fence;
`claim_main` always refuses. No generic gate/report can substitute. Default-ref
resolution, independent review, group approvals, required test freshness, ref CAS,
revocation leases and uncertain remote outcomes must be qualified before enablement.
M1 additionally requires resolving the actual default ref and canonical repository
remote server-side, validating a typed person-owned verification policy, and
bumping `REGISTRY_VERSION` to invalidate these version-1 shadow consents.
A metadata-field match, arbitrary ref or free-text policy must never enable execution.

Claims bind request digest, real deciding lead, consent revision, operation ID and
30-second expiry; a reused ID with changed payload refuses. New admission checks
current authority again. Revocation cancels queued claims and recursively revokes
children even when delegation is disabled; dispatched work cannot be recalled.
No offline broker/node operation is added, so there is no cached-consent start.
Revocation never uninstalls a tool or undoes a ref change.

`POST /api/approvals/batches/preview` takes 1–50 distinct pending tool IDs. It
returns every normalized operation, a digest and a five-minute expiry. System and
account requests use separate previews. Approval binds the original person,
Security revision and exact request digests and validates the entire batch before
changing any status in one transaction. The existing executor runs per item;
results can independently fail. Used/stale/expired/changed previews refuse.
No selection is implied and no batch creates durable consent. A new person
approval cannot execute a payload changed after decision; legacy accepted
requests with no new decision envelope retain their specific prior approval.

Identical pending tool operations in one project/account/team share one request
and Inbox card. Changed versions/actions or another team cannot borrow that ID.
A failed/uncertain operation is never silently retried through the consent model.
Solo sessions never satisfy the explicit-team de-duplication check; a conflicting
pending account/tool change must be resolved before a separate solo request.
The tool history and resolved Inbox card show who decided and the decision reason.

`GET /api/approvals/consents` defaults to `state=active`; `state=revoked` selects
history. `limit` defaults to 50 and is bounded to 1–200. Follow `next_cursor` using
`cursor` to visit every page, ordered by immutable creation time and ID. Revoking
a page's final row does not invalidate its continuation. By default the list is
personal. An explicit `project_id` uses existing project authorization; human
owners can inspect all issuers within that authorized personal project, while
members still see only their own records. Cursors are resolved within the same
authorized person/project scope. Group projects retain their existing refusal.

## Audit and rollback

`approval.grant`, `approval.bind`, `approval.claim`, `approval.use`,
`approval.revoke`, `approval.batch.preview` and `approval.batch.use` retain issuer,
lead, request/operation identities, digests and revision evidence without secrets.
Tool execution keeps `tool.approve`, `tool.reject` and `tool.result`; successful
existing team coordination records L provenance with the actual acting session,
its lead/helper role and the root lead as separate fields. A shadow match is not a use.
Read auditing is not claimed; delegated admission refusals are covered above. Existing Security denial auditing is separate.

Migration 0083 makes no existing request into durable consent and preserves all
previous settings. An empty migration can downgrade. Any retained consent,
operation, batch preview or non-null `tool_request.decision` blocks downgrade,
including revoked records, to avoid
losing authority/audit evidence. Fence affected work and restore the consistent
pre-upgrade backup, or forward-repair. Never translate these records into global
guard Off or `tools.required=false`. Unsupported old binaries must not be run
against a newer database. Full-suite and release qualification belong to the
coordinator; fake local checks do not qualify a real account/node/provider.

## Reviewed account extractor build

Migration **0091**, following session relay **0090**, retains immutable reviewed
recipe/metadata/artifact bytes and an
accd-owned account install journal. Retained recipes or install evidence fence
its downgrade. Capability registry version **2** invalidates older authority
snapshots; coordinate-only shadow consents cannot authorize the extractor.
`QUALIFIED_TOOL_EXECUTORS` remains empty. No recipe has been qualified or published
by this build. The legacy four-field tool consume path always refuses, including
if a manager name is accidentally added to the fingerprint set.

The new `delegated_install` team mutation accepts only `request_key`, `recipe_id`
and `recipe_digest`. It has no read-only or Plan auto-approval. Its server path
uses exact recipe-digest consent and the local account's enrolled broker, with
single-use nonce, account-wide rolling dispatch quota, 160-second lease and
`queued → dispatched → staged → publishing` transitions. Accd rechecks current
consent/parents, Security and frozen grant revisions before publication and each
step. Artifact chunks travel over the existing accd-to-broker connection; no
broker reverse authority or registry connection is added. Accd resolves the local
account UID through passwd at consent creation and each current-consent check;
broker identity replies must agree before an install option is offered.

The trusted extractor runs as the non-root agent UID, writes only staging, then
publishes declared files by no-follow descriptor traversal, hash-while-copy,
fsync and no-clobber rename. No npm or package code runs. The shared advisory lock
also covers legacy person `tool_change`. Qualification includes protocol, recipe,
broker installation identity, interpreter, adapter and inspector bytes, machine
architecture and kernel. Hashes and broker reports do not attest a hostile
same-UID account. Copies match at publication; another account process can change
them later. The interpreter digest is pinned once per process; accd and broker
must use the same qualified interpreter bytes and restart after interpreter
upgrades. Scratch writes maintain a running byte count, with complete tree scans
after staging and at finish; publication does not rescan once per path. These
counts are monitoring, not a kernel disk quota.

Already-installed admissions re-hash every declared file/mode/link and are capped
at ten per account per rolling hour. A descriptor-relative package-tree walk
also rejects extra unmanaged files, directories and links. Identical caller-bound keys return retained
results. Only dispatched operations consume the five-per-account rolling daily
quota; failed dispatches retain consumption. Unproved publication or restoration
is uncertain and blocks new installs. Compensation requires a committed service
intent for each journaled created path, matching identity/content, and empty
directories. Lost replies never cause automatic re-execution or cleanup retry.

Security now offers an explicit enable/consent form for ports, exact-model image
checks and qualified account recipes. No consent box starts checked. A signed-in,
recently verified person reviews the proposed diff; changing global enablement
requires an installation owner. `POST /api/approvals/configuration` checks both
configuration and Security revisions and saves enablement and selected consents
in one transaction. Grant, bind and revoke through `/consents` also advance the
configuration revision, so stale unified drafts conflict. Project-duration choices create bounded team children in the
same transaction. Conflicts preserve the browser draft. Teams availability remains
an independent ceiling. The native whole-account warning is shown explicitly;
revocation remains available while Off and never uninstalls a tool.

Independent implementation security review and owner qualification against pinned
node-tar semantics and disposable accounts remain required before any executor
fingerprint is added. No real package install or qualification is claimed by
fixture tests.

Raw archive framing verifies every unsigned header checksum, requires consistent
POSIX ustar or GNU magic, rejects nonzero GNU prefix bytes and tracks pending PAX
size/path and GNU long names. PAX overrides apply to filesystem members, never
intervening extension bodies. The independent tarfile walk must agree on every
member name, size, data offset, padded end, count and final EOF offset. Ambiguous
or unsupported encodings refuse; this is still not runtime qualification.

The Round 2 canonical header boundary also validates fields tarfile otherwise
ignores: octal digits with only NUL/space terminators; positive base-256 only for
size and within JavaScript's safe-integer range (then the lower artifact ceiling);
all-NUL optional uid/gid/mtime/device fields; UTF-8 string fields with all-NUL
padding after their first NUL; all-NUL linkname; exact magic/version and zero
500–512 pad bytes. GNU prefix bytes remain zero. PAX paths and GNU long names
replace the entire ustar name/prefix. A real offline npm-pack long-path fixture
and an optional host node-tar fixed-mutant comparison exercise this boundary;
they do not replace independent security GO or disposable-host qualification.

A zero-byte regular file whose effective name ends in `/` now refuses as well,
including PAX path overrides; only an actual directory header can declare that
output. The differential corpus retains that refused seed. For owner
qualification, `--install-qualification` or `ACC_INSTALL_QUALIFICATION=1` makes
absence of host Node.js/node-tar a failure, not a skip. Explicit accd/broker
interpreter parameters run each corpus under that exact Python with `-I`;
role-specific evidence records pin the node-tar version/path and actual
interpreter/stdlib/inspector identities. The complete command and record format
are documented in `catalog/tool-recipes/README.md`.

Production broker and accd identity checks share a pure positive-integer,
non-root UID predicate. Positive disposable unit fixtures may explicitly replace
that predicate in root CI, preserving actual kernel UID/inode ownership checks.
Independent root-refusal tests restore the default predicate. No environment,
RPC field or runtime configuration can opt production into accepting UID zero.
The legacy person-tool root guard is unchanged.
