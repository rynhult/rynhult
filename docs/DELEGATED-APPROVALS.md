# Delegated approvals: phase-one server foundation

Base: `475e6063`; schema: `0082`. This slice implements scoped consent records,
shadow eligibility, exact pending-tool batching and decision provenance. It does
not qualify a new broker executor or enable automatic tool installation/main
finalization. `GET /api/approvals/consents` explicitly returns
`execution_qualified: false`. The new shell and setup-level templates come later.

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
group/account-authority integration. Runtime account UID qualification is still
missing from the broker protocol, one reason execution remains unqualified.
Approval routes check resource visibility and refuse group checkouts before
validating body/query fields, including for installation owners. Consent issuance
and binding check recent person verification first. Mutation paths recheck authority
under their existing write lock; the early checks grant no authority.

Action classes are `tool.install`, `main.merge` and `main.push`. Unknown fields or
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
Read/refusal auditing is not claimed; existing Security denial auditing is separate.

Migration 0082 makes no existing request into durable consent and preserves all
previous settings. An empty migration can downgrade. Any retained consent,
operation, batch preview or non-null `tool_request.decision` blocks downgrade,
including revoked records, to avoid
losing authority/audit evidence. Fence affected work and restore the consistent
pre-upgrade backup, or forward-repair. Never translate these records into global
guard Off or `tools.required=false`. Unsupported old binaries must not be run
against a newer database. Full-suite and release qualification belong to the
coordinator; fake local checks do not qualify a real account/node/provider.
