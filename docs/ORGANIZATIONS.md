# Organizations

**V1 scope (owner decision Oct 25):** v1 targets small teams. SCIM, SAML (brokered and encrypted), organization roles/teams and LDAP are parked until after v1, built on request or from contributions. The shipped OIDC company sign-in pilot remains off by default. The enterprise design below is future scope.

**Status: approved by the owner, 2026-10-22, with the answers in decision 0008 (private decision record). Phase 0 foundations delivered; phase 1 has started with OIDC sign-in only (see [Phase 1 status](#phase-1-status)), off by default and not yet qualified.** Addresses companies managing access, many teams and execution machines. Approval should select the first-release scope and defaults below before implementation.

## Recommendation and boundaries

Build one organization per installation first: company identity, scoped administration, enforceable central policy and durable audit export. Keep personal installs simple. Teams organize people and cost; [Groups](GROUPS.md) separately defines collaborative project access, private clones and work boards. Do not turn today's trusted owner into the template for every company administrator.

[HA owner decisions](research/high-availability.md#owner-decisions-2026-10-22) already accept PostgreSQL for company installs, SQLite for personal installs, manual active-passive promotion first, automatic fenced recovery later, and eventually autonomous sites under one organization. This document does not reopen those choices. Integrate the first company release with the separately delivered PostgreSQL/manual-standby work; several concurrent dashboards require that design's command ownership, outboxes and fencing, not just a shared database. [Backup server](BACKUP-SERVER.md), [Remote access](REMOTE-ACCESS.md) and [Containers](CONTAINERS.md) remain their own delivery tracks; their proposed behavior is not assumed to exist today.

| Current implementation | Enterprise change |
|---|---|
| Passkeys or password + TOTP, per-role passkey policy and offline recovery, 14-day server sessions; owner/member and username-based ownership. | Add external identities, stable principal IDs, account status, scoped capabilities and configurable session lifetimes. [Auth](../src/acc/server/auth.py), [auth API](../src/acc/server/api/auth.py), [models](../src/acc/server/models.py), [Guide](wiki/User-Guide/People-and-Sign-in.md). |
| Each member needs a separate agent account; a root helper provisions fixed account names and roots. | Preserve this boundary through provisioning and placements. SSO is dashboard authentication, not provider authentication. [People API](../src/acc/server/api/users.py), [account helper](../deploy/accounts/acc-accounts). |
| Guardrails/custom regex rules are frozen at launch; person/project tool-approval exemptions can relax that one rule. | Add a mandatory organization policy ceiling and revocation protocol. [Guardrails](../src/acc/server/guardrails.py), [hook](../bin/acc-guard), [tools design](TOOLS.md). Hooks are safety checks, not isolation against hostile code. |
| Audit is a local database table with an owner-only recent-events API; budgets use the owners' default-account plan readings. | Durable external export, delegated audit access and separately attributed usage. [Audit model](../src/acc/server/models.py), [audit API](../src/acc/server/api/projects.py), [budgets](../src/acc/server/budgets.py). |

Proposed records: organization UUID; immutable principal UUID linked to the existing User; external identity; organization/team membership with source; scoped role grant; machine/account placement; versioned policy and acknowledgments; revocation job; usage ledger; audit outbox and sink cursor. Migrate ownership references from usernames before rename/reuse or federation. Preserve retired-name protection and historical attribution; no automatic sharing of existing projects or private keys. An installation is a trusted administrative domain, not a hostile multi-tenant service: host root and local owners remain fully trusted.

## Identity and lifecycle

**OIDC first.** One configured identity provider (IdP) per organization initially, using authorization code flow with PKCE, bounded one-use state/nonce, exact redirect URIs, and validated issuer, audience, signature and token times. Pin approved issuers/endpoints; do not discover arbitrary URLs from user input. Keep ACC's server-side sessions, CSRF/Origin checks and throttles; provider tokens stay server-side and are not agent credentials. Refresh signing keys safely; unknown/unverifiable signatures fail closed. Store a unique `(organization, issuer, subject)` mapping to a principal, never auto-link by email or display name. The stable identity rule comes from [OIDC Core](https://www.openid.net/specs/openid-connect-core-1_0.html). Linking an existing local user requires recent authentication to both identities and audited administrator approval; reject collisions.

Support SAML-only companies through an operator-managed SAML-to-OIDC broker first, making the broker an explicit trusted identity boundary. Native SAML is later demand-driven work using a maintained library: signed assertions/responses, issuer/audience/destination/time validation, replay prevention and metadata/key rotation. Neither path may trust unsigned identity headers. Qualify the chosen IdP/broker and logout behavior before claiming support.

**Provisioning.** JIT (just-in-time creation at first SSO login) is opt-in for explicitly assigned IdP groups, with developer permissions at most; no domain-wide auto-enrollment. SCIM-managed organizations default to pre-provisioned users only: JIT cannot resurrect a suspended identity. SCIM 2.0 Users/Groups create, PATCH, deactivate and delete operations are idempotent, organization-scoped and rate limited, with conditional updates and reconciliation reports; use a separately scoped, rotatable service credential, not a human owner token. Map SCIM external IDs to OIDC subjects through a configured immutable IdP attribute; ambiguous matches need review. [SCIM protocol](https://www.rfc-editor.org/info/rfc7644/).

Creating a dashboard user does not synchronously create Unix users everywhere. Queue an audited, retryable account job only for approved machine placements. Derive safe local names from ACC identity, never an unchecked claim, and use the constrained helper rather than arbitrary root commands. Status is **Access ready / Agent account pending / Failed**. No launch until the person's approved account exists; never fall back to the owner's default account. Provider OAuth, GitHub logins and machine-local credentials remain that person's, established separately on each machine. An auditor needs no agent account.

**Deprovisioning is suspension first.** SCIM `active=false`, DELETE, or an admin suspension atomically disables the principal, increments its authorization revision, removes grants and queues effects. Keep a tombstone; DELETE does not erase evidence or uncommitted work. The same revision check guards every API, WebSocket, background command, publish/sync and launch/resume, including requests already in flight at their commit/effect boundary.

- Revoke all ACC login sessions, user-scoped API/service tokens, invites, passkeys for sign-in, terminal streams, pending approvals and push subscriptions. Revoke paired remote-device identities where applicable; shared devices require explicit reassignment. Rotate today's installation-wide API credential if exposed to the departing person; it cannot be selectively revoked per user.
- Stop the person's agents across reachable machines; disable their ACC broker/account launch capability, terminate existing agent processes and clean secret-bearing run files. Preserve the OS account and files in quarantine until reviewed; do not recycle it. Disable OS login/SSH access managed by ACC; inventory separately managed SSH keys and provider sessions for operator revocation. Removing a Linux password alone does not stop running processes.
- Revoke key/project grants and external secret leases; queue provider-side rotation for static secrets they could access. Previously delivered secrets and copied code cannot be recalled. Transfer stewardship of company keys only through explicit review; personal private credentials are not automatically handed to a team admin. Preserve encrypted redaction material under the approved retention rules.
- Preserve group-published commits and work-board history. Release claims; archive personal sessions/projects by default. A designated custodian may recover dirty work or transfer company resources through an explicit audited process, without silently restarting under another person's credentials. This deliberately differs from today's removal option that can transfer resources while sessions keep running.

Show **Access revoked; machine cleanup pending** until every required machine confirms. Disconnected machines cannot be instantly stopped: enterprise execution requires broker-enforced authorization leases (proposed five-minute maximum), local stop/cleanup on expiry, and revocation reconciliation before reconnect work. Fail closed for new grants and launches during an outage. This trades long disconnected execution for bounded revocation; fully autonomous sites need separate locally authoritative grants as described below. IdP account disable without SCIM/back-channel notice is only detected on reauthentication: JIT-only customers need short sessions plus a manual suspension runbook, not a promise of immediate IdP revocation.

**Local passkeys are implemented** (see [decision 0013](decisions/0013-passkeys.md) and the [Guide](wiki/User-Guide/People-and-Sign-in.md)); company break-glass lifecycle integration remains future work. The implementation provides WebAuthn registration, sign-in, naming, listing, revocation and offline recovery for local users. Require user verification, fresh single-use challenges, exact approved origins/RP ID, credential-to-principal binding and replay checks; store public credentials, not private keys. Use a maintained implementation and validate counter anomalies without assuming every synced passkey has a strictly increasing counter. [WebAuthn specification](https://www.w3.org/TR/webauthn-3/). Fix the company hostname/RP strategy before enrollment: private and public remote names may need separate enrollment; do not assume credentials transfer to unrelated site names.

Keep two separately controlled local recovery owners with two enrolled hardware authenticators each and sealed offline recovery material; alert on every break-glass login and require recent reauthentication for identity/policy changes. Rehearse IdP-outage recovery before enabling enforced SSO. Password + TOTP remains a migration/compatibility fallback where policy allows, with replay protection, recovery procedures and no silent downgrade from required passkeys. Let the IdP enforce SSO MFA; do not require a redundant ACC TOTP enrollment for SSO users. TOTP is not phishing-resistant. Recovery resets revoke existing sessions/credentials and require a documented owner-controlled procedure, never email-only recovery.

## Roles, teams and groups

Roles grant capabilities within an explicit scope; they are not a numeric hierarchy. A person may have several grants; resource access still requires membership and all policy restrictions. Default deny on every API, terminal, export, search, notification and worker, not only UI controls.

| Role | Proposed authority |
|---|---|
| Owner (today) | Installation/root-trusted operator: recovery, host enrollment, update, root helpers and ownership. Explicit local appointment only; never automatic IdP mapping. |
| Member (today) | Compatibility personal-access role; migrate to developer plus existing personal resources, with no new team visibility. |
| Org admin | Organization people/teams, IdP mapping, policy, sinks, retention and team budgets within installation limits. No implicit private terminal/content access or root-helper authority. Publishing/remote configuration still requires owner authority where the existing helper needs it. |
| Team admin | Team membership not controlled by SCIM, team budgets, approved pool assignments and delegated group creation within that team. Cannot change organization identity/policy or grant org-admin/owner. |
| Developer | Launch/control own agents on authorized projects/placements; use explicitly granted secrets and published collaboration content. No administration merely from team membership. |
| Auditor | Read organization-scoped security events, policy versions and usage reports; export them. No launches, terminals, secret values or private transcripts. Code access requires a separate group-viewer grant. |

IdP group mappings use namespaced immutable external group IDs, reviewed by an org admin, to explicit scoped grants. Unknown groups grant nothing; administrative mappings require a separate confirmed rule. Use SCIM as membership authority when enabled, otherwise refresh group claims at login and periodic revalidation; expired authority stops renewal of grants. Local edits cannot override SCIM-managed membership. Emergency overrides are time-limited, justified and audited. Preview mapping changes and privilege removals before applying; removals trigger revision revocation, not a wait until the next login.

A **team** is an administrative and cost scope; a **group** from [GROUPS.md](GROUPS.md) owns collaborative projects with `maintainer/member/viewer` permissions. They are distinct records, optionally linked by explicit managed membership rules. Team admin does not automatically become group maintainer, nor does group maintainer become team/org admin. Each group project has one owning group and one charge team; cross-team collaborators need explicit group access. Preserve Groups' per-person private clones, per-session worktrees, safe board summaries and personal transcripts. Existing orchestrator `Team` records in [models](../src/acc/server/models.py) describe same-person helper sessions, not these organization teams; use unambiguous schema/API names.

## Central policy

Support two explicit authority modes: a root-owned, schema-versioned configuration file managed by configuration management, or a database policy edited by org admins. In file mode ACC displays source/revision and cannot loosen it through the API; a DB overlay may only tighten it. In UI mode installation limits still apply. Never merge competing writers by last write wins. Validate and preview the effective policy before atomic activation; keep last-known-good on invalid input and visibly report failure. Policy rollback is a new audited revision and still must satisfy installation limits.

Policy covers required guardrails and additive custom deny rules; allowed agent/provider versions, models and endpoints; approved MCP identities/configuration digests and individual tools; account/machine tool installation approval; publishing targets and approvers; remote-access mode/device gates; allowed pools/residency; session idle/absolute lifetime, concurrent launches, runtime/resource ceilings; retention and audit requirements. Denying a catalog item must also deny direct launch/API aliases. Match models/endpoints and MCP destinations to canonical configured IDs; a project cannot supply a different URL under an allowed name.

**Precedence:** installation hard limits → organization → team → owning group/project → person → session. Effective allowlists intersect, deny rules accumulate, numeric ceilings take the minimum, required approvals combine, and required safeguards stay enabled if any parent requires them. Empty intersections refuse launch. A session has one charge team/project scope; unrelated team membership cannot widen it. Exceptions require an explicit expiring grant from the authority that set the restriction, within installation limits. Existing [TOOLS](TOOLS.md) exemptions cannot bypass organization-required approval, and approval itself cannot authorize an organization-denied tool. Show the limiting source next to disabled controls.

Enforce permissions in ACC APIs and privileged helpers, placement and secret delivery in brokers, and tool rules in hooks. Central policy does not turn hooks into a sandbox: arbitrary scripts, model traffic and shell-launched MCPs can bypass tool-level intent checks. Strong execution/egress restrictions depend on the separate [Containers](CONTAINERS.md) boundary and network controls; unsupported machines must be excluded from pools requiring those guarantees.

Distribute canonical policy bundles (org/site ID, monotonic revision, digest, expiry, minimum enforcement version) over enrolled authenticated machine links; pin the policy-signing public key at enrollment and support signed overlapping key rotation. Brokers verify signature, expiry and capability, persist acknowledgments, and include revision/digest in launch receipts/audit. A scheduler uses only acknowledged compatible machines. No unsigned project file can replace policy. Reconnect reconciles revocations and policy before jobs/secrets. Retain current policy locally for lease checks; expired/stale policy refuses new work and lease renewal. Root helpers must revalidate the relevant approved operation and policy revision at execution, including queued approvals.

Today's frozen launch files remain immutable. Loosening applies only to new launches/resumes. Tightening or revocation invalidates affected authorizations, stops affected sessions at the execution boundary and requires a new compliant launch/resume; do not pretend rewriting instructions changes a running agent. Emergency deny uses the same acknowledged stop protocol, with the five-minute offline lease bound. Show desired/applied revisions per machine and alert on drift.

## Audit, monitoring and retention

Commit a security event and durable outbox row with each state mutation; record denied operations and authentication attempts through a bounded durable path. Cover identity/provisioning/role changes, session/terminal access, policy/approvals, secret use/rotation, placement, publishing/remote access, updates, exports, retention and recovery. Never include tokens, secret values, prompts, command bodies or private diffs. Private key events use opaque IDs; scrub untrusted error strings before persistence.

Versioned JSON schema: `schema_version`, globally unique `event_id`, UTC `occurred_at`/`observed_at`, org/site/installation IDs, site sequence, actor principal/type (human/service/system), delegated initiator, action, target type/ID, outcome/reason code, request/correlation ID, authorized team/group/project/session/machine IDs where relevant, policy/auth revision and bounded redacted change metadata. Explicit nulls mean unknown; do not infer an actor from a mutable username. Map timestamps/severity/trace context to the [OpenTelemetry log model](https://opentelemetry.io/docs/specs/otel/logs/data-model/). Ordering is per-site sequence, not a global wall-clock guarantee.

Ship signed HTTPS webhooks first: TLS validation, fixed admin-configured destinations, no arbitrary redirects, SSRF restrictions, HMAC timestamp/body/event ID with rotating secrets, retry/backoff and receiver deduplication. ACK only on durable receiver acceptance; replay by cursor with access checks. Later add [RFC 5424 syslog](https://www.rfc-editor.org/info/rfc5424/) over TLS to a durable collector and OTLP logs over authenticated TLS. TCP/TLS transport acceptance is not proof of SIEM indexing; syslog has no application acknowledgment by itself. Label guarantees per sink; UDP is unsuitable for required security delivery.

At-least-once delivery for acknowledged sinks while the outbox is retained; duplicates are expected, so consumers deduplicate by event ID. Crash/restart retries cannot lose committed events. One exporter claim/cursor per sink, fenced across mains. Configure spool byte/age limits; show backlog, oldest age, last ACK, drops, certificate/credential expiry and dead letters. Alert at 80% capacity. At capacity, required-audit mode refuses new launches and privileged mutations rather than discarding evidence; keep suspension/stop/recovery possible through reserved emergency storage and an independent local emergency journal. If even that storage fails, perform safety revocation and surface an explicit audit gap for operator recovery. No finite disk promises unlimited outage survival.

Proposed defaults, subject to owner approval: 90 days searchable audit, 30 days acknowledged delivery history, 30 days stopped-session content/artifacts, 13 months aggregated usage; holds override expiry. Unacknowledged required events never silently age out: reaching their configured limit invokes the capacity policy. Org admins set class-specific minima/maxima within installation rules; team/project retention can only tighten privacy where mandatory retention/holds allow. Separate security evidence from user content and raw usage identifiers. Auditor cannot delete evidence; reducing retention or removing a sink requires recent admin reauthentication and is externally audited before effect.

Deletion is a previewed, audited job covering DB rows, search indexes, artifacts, remote mirrors, machine workspaces/transcripts, retired redaction values and external copies under ACC control. Track pending offline cleanup. Release retired redaction secrets only after dependent content is gone; external backing must preserve this protection. Holds have scope, reason, authorized custodian and explicit release. Immutable/offline backups expire on their own schedule; SIEM deletion is the recipient's responsibility. State these limits before confirmation. Use [Backup server](BACKUP-SERVER.md) retention/maintenance identities and restore manifests; apply deletion tombstones and revocations before reopening a restored site. Hash chaining can reveal gaps but cannot make local-owner-controlled storage tamper-proof; an independent SIEM/immutable destination supplies stronger evidence.

## Cost, secrets and execution capacity

**Team cost.** Capture append-only usage facts per actual session/run, principal, charge team/project, machine, provider/model, credential billing scope, source event ID, timestamp, token categories and currency/rate version. Freeze charge team at launch; transfers affect future runs, with audited adjustments for corrections. Count orchestrator helpers once each. Deduplicate source events and keep missing/stale data visible. Subscription percentages are account-level capacity readings, not dollars and not additive across people. Separate provider-reported charges, token-priced estimates and allocated subscription costs; reconcile invoice imports later. Developer sees own usage, team admin team aggregates, org admin/auditor organization reports. CSV/API exports use the same scope and omit private task content.

Start with team reports and warning thresholds. Later enforce company caps using atomic reservations before launch, periodic reconciliation and limits on concurrent/runtime exposure. A user cannot click today's personal “+5%” or confirm-past-budget to override a company cap. Unknown pricing/usage cannot be presented as zero. Decide whether unknown usage blocks launches for hard-cap teams; a real-time dollar ceiling is impossible with delayed provider billing, so publish the possible overshoot.

**External secret backing.** Preserve [VAULT](VAULT.md)'s opaque key IDs, write-only values, explicit project grants, remote-machine consent, redaction and stopped-run cleanup. Add provider references and version/lease metadata behind the same authorization layer; no secret retrieval endpoint for humans. External references are admin-configured allowlisted paths, not agent-supplied URLs or shell commands.

| Backend | Contract and consequence |
|---|---|
| Local encrypted vault (default) | Existing behavior; remains usable offline. Migration is explicit, verified and reversible before deleting old backing. |
| HashiCorp Vault | KV references first; workload identity and least-privilege paths. Dynamic secrets later require renewal, bounded lease lifetime and revoke-on-stop/deprovision, following [Vault leases](https://developer.hashicorp.com/vault/docs/concepts/lease). |
| Cloud KMS | Envelope-encrypt ACC data keys with a customer key; KMS is a cryptographic key service, not a generic password vault. Use a separate cloud secret-store adapter for secret references. Recovery depends on retained key versions and IAM; see [KMS overview](https://docs.aws.amazon.com/kms/latest/developerguide/overview.html). |
| 1Password | Restricted vault/item/field references via [Connect](https://developer.1password.com/docs/connect/) or a qualified service-account adapter. Connect caches locally; document that copy and its lifecycle rather than promising immediate upstream revocation. |

Resolve at authorized launch/resume using control-plane workload identity; agents never receive backend root tokens. Deliver only selected values over the trusted placement channel; hosts remain trusted secret recipients. No silent fallback to old local values during outages. Cache only under an explicit bounded policy; dynamic lease expiry stops affected work. Rotation cannot remove secrets already in a process: revoke upstream and restart affected sessions when required. Keep encrypted historical redaction material for delivered versions under retention rules, even when the upstream value rotates/disappears. Back up references, required key versions and recovery authority; test external-provider loss and restoration before migration.

**Machine pools.** Named, site-scoped sets with admin-assigned trust/residency labels, OS/architecture, runtime/enforcement versions, GPU/memory and agent availability. Team grants authorize eligible pools; machine owners cannot self-assert trusted labels. Placement filters policy, person/account binding, secret remote consent, residency and capability, then reserves quota/capacity atomically and picks a healthy machine. No compatible placement means queued with a reason, never fallback to a personal laptop or another person's account. Show capacity, queue, policy revision and estimated wait; drain stops new placements and lets permitted work finish. Maintenance/revocation may explicitly stop work. Running sessions stay pinned to their workspace/transcript machine; pool reassignment does not live-migrate them. Use Groups' private per-person placements, not a shared service identity.

**Sites later.** Each object has a home site and one writer. Organization-signed policy is an upper bound; each site may tighten it. During WAN outages a site can use only explicitly delegated, unexpired local authority and locally authoritative identities/resources; it cannot mint global grants, bypass revocations it knows, or claim global budget/placement exclusivity. Define maximum offline authority, spend allocation, residency and conflict rules before federation. Replicate authorized safe metadata/published commits, not private transcripts, provider logins or every vault secret. This preserves the approved HA direction without claiming immediate global revocation during a partition.

## Supply chain, offline delivery and accessibility

Today's [release workflow](../.github/workflows/releases.yml) produces source bundles, BUILD and SHA256SUMS; [UPDATES](UPDATES.md) validates hashes/archives and backs up before update. A checksum delivered beside an archive is not publisher authentication. Sign a manifest binding version/build, all artifact hashes, dependency lockfiles and SPDX or CycloneDX SBOMs (Python, web, native helpers and supported runtime images). Keep signing authority outside accd and ordinary release artifacts; use protected release identity, provenance and key rotation/revocation. Main and node root installers verify against pinned release trust before extraction or code execution. Offer an offline-verifiable signature bundle with pinned trust roots/checkpoint; do not require a live transparency service in an air gap. Reject tampering, unknown signers and rollback; emergency downgrade needs an explicit audited recovery procedure with compatible schema restore.

Offline media must include signed source/built UI, Python wheels, node bundles, pinned platform prerequisites/agent tools and container images where supported, SBOM, licenses and an exact OS/architecture inventory. Install/update from a local verified repository with network access denied in qualification tests; no hidden curl/pip/npm downloads or automatic latest-version lookups. Operators import reviewed update/revocation bundles and retain the last known good kit. Air-gapped installation is distinct from offline inference: provider CLIs may require internet/login. Publish only a tested local-model/provider combination as fully disconnected execution; corporate IdP, secret manager, SIEM and time service must also be reachable locally or have explicit offline alternatives. Package licensing and redistribution need review before promising an all-tools kit.

Make **WCAG 2.2 AA a release gate for the supported dashboard**, including sign-in, setup/recovery, approval, organization settings, reports and failure/empty/stale states. Follow [DESIGN.md](design/DESIGN.md); use its existing components and token system. Automated axe/browser checks cover each route/dialog in dark/light themes and desktop/phone sizes. Manual qualification covers keyboard-only operation, visible/unobscured focus, screen readers, zoom/reflow, contrast, non-color status, reduced motion, target sizes, accessible authentication and timeout extension where security permits. These requirements derive from [WCAG 2.2](https://www.w3.org/TR/WCAG22/); automated checks alone do not establish conformance. Provide alternatives to QR-only enrollment, dragging-only controls and terminal-only outcomes (readable status/transcript views and ordinary controls). Record tested assistive-technology/browser versions and known limitations; no AA claim with unresolved applicable A/AA failures. Audit existing screens as part of the gate, not just new enterprise screens. No UI or conformance claim changes in this documentation session.

## Delivery and approval

Estimates are engineer-weeks including tests/docs/review, not elapsed promises; two engineers cannot halve every dependency. Prototype identity/account lifecycle and policy leases first. First Organizations release is a **single-organization company pilot**, not federation or concurrent controlling dashboards.

| Phase | Required outcome and acceptance evidence | Effort |
|---|---|---|
| 0. Foundations | Stable IDs/authorization inventory, schema migration and recovery rehearsal; IdP, passkey hostname and lease prototypes; existing-UI accessibility gap assessment. Owner accepts defaults/scope. | 2–3 |
| 1. Identity and delegation (first release) | OIDC + brokered SAML qualification; SCIM Users/Groups + suspension; passkeys/break-glass; roles/teams; per-person account jobs. Tests for claim collision, replay, group privilege escalation, API/WS scoping, recovery and offline deprovision. | 5–8 |
| 2. Policy and evidence (first release) | File/UI policy ceiling, broker leases/acknowledgments and helper checks; webhook outbox, lag monitoring, retention/deletion/holds; team-attributed usage with warning budgets and basic approved-machine pool selection. Crash, partition, full-spool, stale-policy and deletion/restore tests. | 6–9 |
| 3. Company qualification (first-release gate) | Signed artifacts/SBOM and verified installer/node updates; documented recovery and supported-platform matrix; full applicable WCAG gate with browser/manual evidence; operator and user guides. PostgreSQL/manual active-passive dependency must pass HA fencing/restore tests before company release. | 3–5, excluding HA work |
| 4. Extensions | HashiCorp Vault KV/KMS/1Password adapters and rotation/redaction drills (3–5); syslog/OTLP collectors (1–2); enforced team caps and capacity scheduler/drain (3–5); offline media and isolated-network qualification (3–5); native SAML if needed (2–4). | 12–21 total if all selected |
| 5. Federation and automation | Multi-dashboard connection ownership, automatic fenced failover and autonomous sites under the separately approved HA sequence; organization-wide revocation/spend/residency protocol. | Separate estimate after prototypes |

First-release incremental effort: **16–25 engineer-weeks**, plus separately scoped Groups, PostgreSQL/HA, backup-server, remote-edge and container execution work. Groups can ship independently; organization team reporting does not imply group collaboration is complete. Native SAML, external secret backends, hard spend caps and air-gapped kits are later features and must be labeled unavailable until qualified. If pilot customers require them, expand the release and estimate explicitly. Required containers/egress isolation on enterprise pools are a dependency wherever policy claims strong execution restrictions.

No automatic migration enables SSO enforcement, remote access or mandatory stopping. First import current users/resources, dry-run mappings and policy, enroll/test recovery owners, then explicitly activate company mode. Run API/database tests plus browser user paths and isolated machine/helper simulations; never test revocation by stopping real users on a development host. Documentation approval is not operational verification.

Owner decisions requested:

1. Approve one organization per installation first, with org admins separated from installation owners and no implicit access to private sessions? Which actions, if any, need two administrators rather than one?
2. Which IdP/broker and supported OS/browser combinations qualify the pilot? Approve SCIM as required for managed lifecycle and JIT as an explicitly weaker alternative?
3. Approve five-minute execution leases and fail-closed offline stopping for company-managed pools? Which later sites need longer autonomous authority, and with what exposure?
4. Approve file policy as authoritative when configured, two local hardware-key recovery owners, and the proposed retention/required-audit capacity defaults? Who holds recovery material and legal holds?
5. Are signed release/SBOM, warning-only team reports and basic pools sufficient for the pilot, or must external secrets, hard caps, air-gapped delivery or stronger container isolation move into the first release? Confirm the HA dependency budget separately.
6. Which data may cross sites, which SIEM destination is required, and must SCIM/SSO and audit endpoints be private-only? Public SSO redirects and SCIM ingress must preserve [Remote access](REMOTE-ACCESS.md)'s device gate; do not add an unauthenticated bypass for IdP compatibility.

These are proposed product choices, not new ADR decisions. Record the owner's answers and resulting ADRs before implementation.

## Phase 0 foundations (2026-10-24)

This bounded foundation delivery enables nothing and does not complete every prototype in the phase-0
estimate above. Existing installs keep current personal/group authorization, schema and supervisor behavior.

- [Authorization inventory](ORGANIZATIONS-AUTHZ.md): generated from reviewed classifications, enforced by
  the existing ownership MATRIX tests for every API method and both WebSockets; current audit gaps are explicit.
- [Stable ID inventory and migration plan](ORGANIZATIONS-STABLE-IDS.md): copy-only SQLite report via
  `python -m acc.organization_ids`; no migration/apply path, no real tables changed.
- Execution lease prototype: `src/acc/policy_lease.py`, tested with simulated time/partition/stale policy;
  deliberately unwired. A future trusted broker adapter verifies signed policy, computes remaining lifetime,
  reconciles revocations, uses a suspend-aware monotonic clock and independently schedules checks/stops/cleanup.
  Lease expiry is latched; a changed revision requires a new compliant launch even for a policy loosening
  (a conservative prototype restriction). Poll delay/process kill latency must be included in the five-minute
  bound before production; this state machine alone cannot terminate a process.
- Internal accessibility assessment: `docs/reviews/2026-10-24-organizations-a11y-gaps.md` (review records
  are excluded from public source exports). See also the [rollback rehearsal runbook](ORGANIZATIONS-RECOVERY.md).
  Automated synthetic restore evidence is distinct
  from a replacement-host/production rehearsal; PostgreSQL qualification remains with the HA dependency.

Decision 0008 overrides earlier proposed defaults here: **one year audit / 90 days transcripts**; two admins
for recovery-owner changes, policy loosening and disabling audit streaming; webhooks **and RFC 5424 syslog**
for the pilot, OTLP later. Five-minute fail-closed company-pool leases are already approved.

## Phase 1 status

**First slice (2026-10-25): OIDC sign-in core, tested only against a local test identity provider.** Decision
[0053](decisions/0056-oidc-relying-party.md) records the implementation choices.

- Relying party: authorization code + PKCE, one-use browser-bound state and nonce, exact redirect URI, RS256-only ID
  token verification (issuer, audience/azp, exp/iat/nbf within 120 s, nonce, at_hash), pinned https discovery and a
  cached JWKS with rotation. No implicit flow; no tokens in URLs, logs or audit.
- Identity: unique (organization, issuer, subject) → stable person ID (the phase 0 uuid5 proposal, persisted on first
  binding). Never keyed by email. Unknown subjects are refused unless an owner pre-provisioned them or sent a one-time
  link; a link is bound to the invited directory account (verified email or UPN must match), single use, 7 days.
  Linking an existing member needs that member's recent local sign-in. Owners always sign in locally.
- MFA: company MFA counts only when the ID token proves it: `amr` contains `mfa`, `hwk` or `fido`, or `acr` is one of
  the tenant values an owner entered (none by default; decision 0061). Without proof, while passkeys are Required for
  members, the member must also pass their Rynhult passkey (a session limited to that check), or is refused.
- Passkeys for company-only members: one, within five minutes of an MFA-proven company sign-in whose signed
  `auth_time` is within the last ten minutes (`max_age=600` requested); owners can turn this off. Required member
  policy exempts only company-only members from recovery codes. Migration 0054 refuses downgrade while their
  passkeys exist; a completed reset link clears company-only status, since the member then has local credentials.
- Company sessions: 12 hours by default, an owner setting between 1 and 24 hours.
- Owner settings: **Settings → Company sign-in**, visible only with `ACC_ORGANIZATIONS=1`; SSO stays off until an
  owner turns it on. Client secret sealed with the vault key, write-only. **Test connection** reads discovery and
  keys only.
- Break-glass: passkeys and password + TOTP are unchanged and work with the provider down or misconfigured.
- Tests: `tests/test_oidc.py` with `tests/oidc_idp.py` (rotation, wrong audience, expired, replayed nonce, mismatched
  state, alg=none, HS256 with the public key, claim collision across issuers, session fixation, logout, login CSRF).
  Browser: `web/e2e/60-company-sign-in.spec.ts`.
- Guide: [Company sign-in with Microsoft Entra ID](wiki/User-Guide/Company-Sign-In-Entra.md), marked not yet
  qualified.

**Directory-driven membership (designed, not built).** The owner prefers membership from a directory group or
organizational unit over individual links. Path: the SCIM work in this phase synchronizes Users and Groups from the
IdP; an owner maps chosen directory groups (never a whole domain) to member access; SCIM `externalId` / an immutable
attribute such as Entra's object ID is mapped to the OIDC (issuer, subject) at provisioning, so a synchronized person
signs in without a link. Removal from the group or `active=false` suspends, per Identity and lifecycle. Owners stay
outside any mapping. The callback already calls `admitted_by_directory()` (`src/acc/server/api/organizations.py`)
before invites; it admits nobody until that sync exists. Group claims in ID tokens are not trusted for this
(overage and staleness); the synchronized directory is the source.

Not done in phase 1 yet: Entra/Google/Okta/Keycloak qualification on a real tenant (the owner's Entra test tenant
isn't ready), brokered SAML, SCIM and suspension, provider logout, organization roles/teams and per-person account
jobs, group-based access, ES256/PS256 tokens.

## Phase 0 open questions

These do not block copy-only analysis or the unwired prototype. They block the relevant future qualification
or activation; do not ask again for defaults already answered in decision 0008.

1. Which supported OS/browser/assistive-technology versions qualify the pilot, and which named Entra test
   tenant plus SAML-to-OIDC broker will be available for isolated IdP testing? Entra is the approved first
   available test provider; Google Workspace, Okta and Authentik/Keycloak remain qualification targets.
2. What canonical company hostname and WebAuthn RP ID should recovery owners enroll against, and which
   two company custodians hold hardware keys/offline recovery kits? Private/public hostname transitions need
   a tested enrollment/recovery plan; no passkey hostname or IdP prototype is delivered in this bounded task.
3. Which SIEM/webhook/syslog receivers, required-audit spool capacity/outage budget, acknowledged-history and
   aggregated-usage retention qualify the pilot? Audit/transcript defaults are settled; these operational
   values and receiver acknowledgments are not. Private-only/device-gated access remains the approved rule.
4. What recovery RPO/RTO and rollback compatibility window apply, and who accepts the independent PostgreSQL/
   manual-standby restore evidence? After org writes, an old personal-only binary may not enforce current
   suspensions/holds; recovery must remain quarantined until a safe repair/forward migration is approved.

5. Should organization terminal snapshots fail closed when their access audit cannot be persisted?
   The integrated `GET /api/sessions/{acc_id}/terminal-output` handler attempts `terminal.read` but catches
   `OperationalError` and continues to fetch output (`src/acc/server/api/terminal.py:36-48`). Existing session
   ownership checks still apply; this is an audit-dependent access question for company mode, not a change
   to current personal authorization. Phase 0 leaves the handler unchanged.

6. Should archived group projects reject key listing and attachment? `eligible_keys` checks only project
   existence and group binding (`src/acc/server/api/group_board.py:134-145`); `attach` checks writable group
   authority and eligibility but never `project.archived` (`src/acc/server/api/group_board.py:152-163`).
   A current maintainer or installation owner can therefore add a project key attachment after that project
   is archived while its group remains active. Launch separately refuses archived projects. This looks
   inconsistent with an archive authority boundary; phase 0 records the question and leaves authorization
   unchanged.
