# Portal-managed DNS and certificates

**V1 scope (owner decision Oct 25): Cloudflare is qualified; Azure DNS, Route 53, Google Cloud DNS, DigitalOcean and Hetzner DNS are in preview.** All remain offered, with **Preview: not yet qualified for this release** on preview choices and bindings and a note that Cloudflare is the qualified provider. One server-owned `qualification` value per provider in `src/acc/dns_credentials.py` drives API and portal status; it is promoted only after the [qualification kit](wiki/User-Guide/DNS-Qualification.md) and review. Saved bindings and credential maintenance are retained. Existing live-consumer, pinned binary and root-policy gates remain in place; a label does not approve a particular installed Caddy binary. Azure wave 3 is separate and unchanged.

**Owner-run evidence:** Cloudflare passed all 10 qualification stages on 2026-10-26
at 17:23 UTC against a disposable zone, after #330 fixed compressed provider
replies. See the [record below](#owner-run-cloudflare-result-2026-10-26) for token
scope, stages and candidate hash. The kit retains `release_qualified: false` by design.

**Status: approved 2026-10-23 (all six owner questions). Phase 1 implemented; phase 2 and wave 2 candidates implemented with release holds below. Deployment remains gated on qualified artifacts and both live consumers.**

Owner clarifications: support all major providers over time; Azure DNS joins Cloudflare and Route 53 in the first qualification wave. Phase 1 ships Cloudflare only; implement Azure DNS and Route 53 in phase 2. Suggest the provider from recognized Keys names and require confirmation in Settings.

## Recommendation and baseline

Manage infrastructure credentials in **Settings → Keys**, using a new owner-only infrastructure purpose that can never be granted to projects, agents, MCP servers or nodes. Configure provider/zone bindings in **Settings → DNS and certificates**. Keep Caddy's internal CA as the working first-run default. Start with Cloudflare, Azure DNS and Route 53, then qualify the other providers below for both DNS-01 and Publishing. DNS-01 is certificate domain validation, not a choice of certificate authority; retain Caddy's ACME automation and expose an optional account email/staging CA setting, not arbitrary certificate plugins.

Today `deploy/setup.sh` installs apt Caddy and, with a zone, selects a trusted `/usr/local/bin/caddy` containing `dns.providers.cloudflare` or downloads one through `deploy/lib/install-caddy-cloudflare.sh`. That download requires independently approved `ACC_CADDY_SHA256`. Setup writes root-owned 0600 `/etc/caddy/cloudflare.env` and, when a token was supplied, acc-owned 0600 `/srv/acc/data/secrets/cloudflare_token`. The latter feeds Publishing; these two copies can already differ. `install.sh` renders the wildcard/internal templates and checks adapted JSON for the private admin socket. Existing binaries are reused after ownership/module checks, without a new download hash check.

`src/acc/server/cloudflare.py` implements zone discovery, DNS-only A/AAAA upsert and managed-record deletion. `publish.py` and `api/publishing.py` bind Publishing directly to that client and plaintext token file. The current client uses a comment marker, not a portable ownership ledger; its zone discovery is only one page. Preserve owner approval, reserved hosts/preview namespace, project/port checks and private/public Publishing choices while replacing this coupling.

See [vault rules](VAULT.md), [operator guide draft](wiki/User-Guide/Caddy-Hardening.md), [approved remote-access direction](REMOTE-ACCESS.md), [root publish helper](../deploy/publish/acc-publish), [package helper](../deploy/packages/acc-packages) and the internal security reviews. Reviews 1–4 inform descriptor-based file handling, bounded parsing/downloads, redaction before truncation, fixed privileged targets and the limits of agent guardrails. This design does not rely on guardrails to hide credentials.

## Provider scope and privileges (all proposed)

Target DNS-01 TXT create/read/delete and Publishing A create/read/update/delete for every row. Add AAAA only with an explicit IPv6 address and reachability test; CNAME is future scope. A module's existence is not a qualification result. Each release manifest must link the exact reviewed module commit, credential schema and tested API/IAM policy. No provider is enabled merely because its module compiles.

Modules below are `github.com/caddy-dns/<module>` / `dns.providers.<module>` (use the required Go major-version suffix, notably `github.com/caddy-dns/hetzner/v2`). These are community integrations; pin and review each. The [caddy-dns organization](https://github.com/caddy-dns) is the module discovery source; [Route 53](https://github.com/caddy-dns/route53), [Azure](https://github.com/caddy-dns/azure), [Google Cloud DNS](https://github.com/caddy-dns/googleclouddns) and [RFC 2136](https://github.com/caddy-dns/rfc2136) document their own integrations. Permission descriptions below are proposed constraints, to be proven against each pinned adapter and provider account before release, not copy/paste production IAM policies.

| Provider / module | Credential and smallest intended authority | Qualification wave |
|---|---|---|
| Cloudflare / [`cloudflare`](https://github.com/caddy-dns/cloudflare) | Scoped API token: Zone Read + DNS Edit for selected zone only. Separate TXT credential and Publishing credential; API permissions may still allow all record types within that zone. DNS-only records; no proxy/tunnel enablement. | 1 |
| AWS Route 53 / [`route53`](https://github.com/caddy-dns/route53) | Dedicated IAM principal; ChangeResourceRecordSets and ListResourceRecordSets on named hosted-zone ARN, GetChange as needed. Restrict change names/types/actions with IAM conditions (TXT vs A/AAAA). Supply zone ID to avoid account-wide discovery where possible. Prefer scoped workload role if both consumers can use it; otherwise dedicated access key/secret and optional session token with expiry warning. | 1 |
| Azure DNS / [`azure`](https://github.com/caddy-dns/azure) | Tenant/subscription/client ID + client secret; custom role on one DNS zone, read zone/record sets and write/delete only required TXT or A/AAAA sets. DNS Zone Contributor at zone scope only if narrower role cannot work; never subscription Contributor. | 1 |
| Google Cloud DNS / [`googleclouddns`](https://github.com/caddy-dns/googleclouddns) | Explicit service-account JSON. Publishing: DNS Administrator (`roles/dns.admin`) on one managed zone. Pinned certificate candidate: project-level managed-zone discovery, so DNS Administrator on a dedicated project; root-owned credential-file delivery/rollback remains subject to live qualification. [Managed-zone IAM](https://docs.cloud.google.com/dns/docs/zones/iam-per-resource-zones). | 2 — implemented, NOT qualified |
| DigitalOcean / [`digitalocean`](https://github.com/caddy-dns/digitalocean) | Separate scoped tokens: certificates domain:read, domain:create and domain:delete; Publishing adds domain:update ([domain scopes](https://docs.digitalocean.com/reference/api/scopes/domain/)); these scopes also permit domain operations, so disclose that broader authority. No per-zone token isolation; use a dedicated account and accept the broader authority. If zone isolation is unavailable, require explicit owner acceptance or dedicated account. | 2 — implemented, NOT qualified |
| Hetzner / [`hetzner`](https://github.com/caddy-dns/hetzner) | Dedicated Console project token; the current module uses Cloud DNS API and documents the `/v2` build path. Qualify that generation, not legacy DNS tokens. Use separate Read & Write DNS API tokens in a dedicated Console project; project tokens also permit other project resources, not only DNS. Do not use legacy DNS Console tokens. [Hetzner announced legacy Console shutdown for May 2026](https://www.hetzner.com/de/news/dns-beta/). | 2 — implemented, NOT qualified |
| Porkbun / [`porkbun`](https://github.com/caddy-dns/porkbun) | API key + secret, API access enabled only on selected domain(s); verify domain restrictions, no general registrar administration. | 3 |
| Gandi / [`gandi`](https://github.com/caddy-dns/gandi) | Dedicated LiveDNS PAT scoped to the selected organization/domain where supported; exclude registration/billing permissions. Broader LiveDNS scope requires disclosure. | 3 |
| OVH / [`ovh`](https://github.com/caddy-dns/ovh) | Endpoint + application key/secret + consumer key; allow only selected `/domain/zone/<zone>` record read/create/update/delete and refresh operations; no registrar/account write. | 3 |
| deSEC / [`desec`](https://github.com/caddy-dns/desec) | Dedicated token with token policies restricted to domain, names and TXT vs A/AAAA types. No token-management authority. | 3 |
| RFC 2136 / [`rfc2136`](https://github.com/caddy-dns/rfc2136) | TSIG name/secret/algorithm + explicitly approved authoritative server/zone. BIND update-policy or PowerDNS equivalent limits names/types; separate TXT and Publishing keys. Discovery optional: enter zone explicitly. Verify server supports RFC 2136; PowerDNS API support alone is insufficient. | 3 |

Prefer delegated `_acme-challenge` validation zones when provider permissions cannot restrict TXT names. Validate Caddy delegation behavior before offering it. Publishing and certificate providers may differ: bind the publication zone to one adapter and the validation zone to another. Public ACME requires publicly resolvable challenge TXT records; private RFC 2136 zones alone require internal CA or an explicitly qualified private ACME service.

## Binary supply chain

**Proposed default:** release CI builds immutable Linux amd64/arm64 binaries with pinned Caddy, Go, xcaddy and module versions/commits, dependency lock/provenance, SBOM and tests. Start with a small reviewed bundle of qualified modules (one binary per architecture/release), expanding deliberately. Provider selection never accepts a module URL, compiler flag or user-supplied code. Modules run inside Caddy with its credential/storage authority; bundle size and shared-process compromise are risks.

| Approach | Decision |
|---|---|
| Pinned prebuilt release artifacts | Recommended delivery. Root installer selects a fixed artifact ID from a root-owned manifest, checks independent committed SHA-256 and release signature/provenance, caps download/extraction size, verifies architecture/modules, atomically switches and keeps last-good binary. Never execute an unverified binary to inspect it. |
| caddyserver.com build service | Existing Cloudflare bootstrap only. Dynamic output can change; retain independent approved hash and fail closed. Do not derive expected hash from the response or an adjacent untrusted checksum. Not a portal-time installer. |
| xcaddy on destination | Operator escape hatch for a reviewed build, not root portal functionality. CI uses xcaddy with explicit pins; production hosts need no compiler/network dependency resolution. |

Check upstream advisories weekly; propose a monthly qualified release and expedited security releases. Show available version and approval to the owner, never `caddy upgrade` or silently rebuild modules. Version/hash/module changes need reviewed manifest changes and issuance/renewal/reload/rollback tests. Coordinate apt Caddy updates with custom binary selection so apt cannot silently switch the active executable. Retain a tested previous release, but do not roll back to a known vulnerable version without explicit owner choice.

## Portal and vault boundary

**Proposed data:** infrastructure credential (ID, provider, fixed typed fields encrypted by vault, purpose, owner, revision, timestamps); binding (zone ID/name, credential reference, certificate or Publishing purpose); applied generation (desired/applied revision, binary manifest ID, health/expiry, sanitized last result); managed DNS ledger (provider/account identity, zone, host, type, remote ID or RRset, exact prior/applied value, request ID). Encrypt values with the existing AES-GCM vault and key custody; bind new purpose/provider/field identity in authenticated data. Back up database plus vault key together; missing keys block application, never recreate over ciphertext.

Owner-only APIs require normal CSRF/Origin protection and recent reauthentication for add/replace/apply/delete/import. No value-read endpoint, including for owners; replacement is write-only. A purpose is immutable: reject project grants, sharing, MCP references, session context/environment delivery, exports and other-machine delivery at every resolver, not just the UI. Members and agents see neither credential metadata nor values. Publishing may expose only approved zone choices and generic availability to authorized requesters, not account IDs or credential details. Root and accd remain trusted and can decrypt; this is not protection against main-host compromise.

Keys shows an owner-only **Infrastructure** view: name, provider, purpose, selected zones, missing fields, last validation/rotation, expiry if known, desired/applied status and impacted certificate/Publishing bindings. Actions: Add, Validate, Replace, Apply, Disable. No reveal/copy/export; deletion is blocked while bound, and disabling explains renewal failure risk. The separately planned common-variable dropdown remains for ordinary project keys. Infrastructure forms use fixed provider field labels; selecting a familiar variable never converts a project key into infrastructure authority. Do not silently reuse an existing granted key.

Settings owns zone/provider configuration and links to Keys rather than storing duplicate secrets. Follow `docs/design/DESIGN.md`: existing Page/Dialog/QueryState/useConfirm, accessible labels, keyboard/focus behavior, single-flight mutations, actionable error vs empty state. Show “Saved, not applied” separately from “Applied”; no raw provider error under Details. No UI or screenshots are delivered in this design-only change.

### Privileged delivery, rotation and migration (proposed)

1. accd checks owner authority and binding revision; decrypts only for the relevant operation. Send a bounded typed message over a root-helper Unix socket, root-owned parent, peer-credential allowlist for acc only; never argv, shell, request artifacts or journals. Credential JSON may be multiline; validate field sizes and serialize safely rather than sourcing shell text. Accept fixed provider IDs/zone bindings/revisions, not arbitrary paths, service names, endpoints, Caddy snippets or downloads. Custom RFC 2136 endpoints are a separate root-approved allowlist; never permit user-chosen HTTP API bases/SSRF or arbitrary cloud credential-chain metadata discovery.
2. Helper executable, policy and ancestors are root-owned and not group/world writable, with fixed PATH/argv and no inherited path/environment overrides. Root-owned policy bounds allowed zones, providers, artifact IDs and file destinations. Approval is enforced by accd; an acc-compromised caller can forge its messages, as with acc-publish. Do not claim a queue actor label authenticates an owner. Root policy expansion requires local operator authority in the first phase; later portal policy expansion needs separate review.
3. Write versioned credentials under proposed `/etc/caddy/acc-dns/` (root:root 0700), files root:root 0600; only Caddy-bound material, not Publishing keys. Use descriptor-relative no-follow opens, regular-file/owner checks, exclusive temporary creation, fsync and atomic rename under a lock. systemd reads an EnvironmentFile as root and supplies allowlisted variables to User=caddy; fields requiring files get root:caddy 0640 files in a root:caddy 0750 subdirectory. No agents or acc group access. Avoid generic ambient AWS/Azure credentials shared with unrelated bindings. Caddy's process can necessarily read its credentials.
4. Render fixed templates; adapt and validate with the candidate credential generation and expected Unix admin address. Capture secret-bearing output only in restricted transient memory; return fixed error codes, not raw config/provider responses. Reload over `/run/caddy/admin.sock`; confirm success and certificate automation health. An EnvironmentFile change does not alter a running process: use a tested credential-aware reload process that expands the candidate config, and a controlled restart when SDK environment/file caching requires it. Persist the generation for the next restart. Warn about brief stream interruption. Reload alone is not proof of a successful ACME renewal.
5. Keep last-good config/credential generation until a controlled staging issuance test and production health check succeed. On failure restore both and report pending/failed, never “applied”. Separate saved/validated/applied/issuance states and serialize changes with Publishing. Replace at provider, stage new key, test TXT create/cleanup and renewal, apply, verify, then revoke old key at provider. Rotation in ACC alone does not revoke it. Purge superseded material after bounded rollback retention (proposed 24 hours); restore must not resurrect revoked credentials silently.
6. Publishing decrypts its own purpose-bound credential in accd, never asks Caddy for it. Redact current and retained previous infrastructure values before formatting/truncating logs, API errors, audit detail and diagnostics. Never log request bodies, Authorization, secret JSON, env files, expanded Caddy config or provider response dumps. Root errors use sanitized categories. Audit actor, action, binding/credential ID, revision, zone, applied generation and result, never values. Proposed events: `dns.credential.add/replace/disable`, `dns.validate`, `dns.apply/rollback`, `dns.migrate`; owner-only metadata projection.
7. Migration is explicit and idempotent, not an unattended upgrade. Owner chooses import or replacement; root helper reads only the fixed legacy `/etc/caddy/cloudflare.env` (never sources it), checks no-follow/owner/mode/size and imports through the authenticated private channel to vault. accd separately imports its legacy `data/secrets/cloudflare_token`. Compare secretly: preserve separate bindings if values differ. Prefer newly issued least-privilege credentials. Never overwrite existing vault entries or ask an agent to paste keys. Validate and apply before switching references/drop-ins; then remove legacy files after the rollback window, revoke replaced tokens and document backups containing old copies. A failed migration keeps existing service running and reports incomplete state. Never restore `--environ`.

## Publishing target addresses (implemented)

Settings → Publishing maintains an owner-only inventory of labeled public and internal IPv4/IPv6 addresses and CIDR blocks, one default per access level. This inventory is the source of A/AAAA targets for **every provider**: provider adapters receive a selected host IP, never a whole block or a fresh public-IP lookup. Public entries require globally routable unicast; block membership and usable-host checks are provider-independent. IPv4 blocks are bounded at /16 and IPv6 at /48; selection never expands a block.

Each request pins `context.target_ip`; applying rechecks membership in the current inventory before DNS mutation. Defaults affect new requests only. Approved/applying/applied publications prevent removal of their containing entry; removing an unused pending target makes that request fail safely at apply. Existing replacement approval and port reservations remain mandatory. Environment origin variables bootstrap the inventory once and pin legacy requests, preserving existing behavior. The settings row `publishing.addresses` and `publish.address_*` audit events require no new schema migration. Detection compares fixed HTTPS endpoints and machine interfaces, saves nothing, and is not evidence of address ownership.

Cloudflare currently updates A or AAAA through its managed-record path. A family change updates the same remote record; ambiguous multiple address records are refused. This does not implement the proposed provider ledger or update rollback below: if Caddy rejects an update to an existing record, the pre-existing DNS rollback limitation remains. Failed requests that may still have a DNS record also reserve their address until a later successful publish/unpublish reconciles that hostname; a confirmed rollback of a newly created record clears that reservation. The root helper still renders only fixed routing templates; address authority and owner approval are enforced in accd immediately before DNS application, as DNS is performed by accd rather than root. A compromised accd remains trusted under the existing threat model.

## Publishing abstraction (proposed)

Use an async Python `DNSProvider` protocol, libdns-equivalent in semantics, not a Go module subprocess: `capabilities`, `validate_credentials`, `zones` (paginated or explicit zone), `list_records`, `ensure_record`, `delete_owned_record`, `close`. Typed normalized records/errors; provider factories accept only registered IDs. Use maintained SDKs when they meet bounded timeout, credential and retry requirements, otherwise small adapters like today's Cloudflare client. RFC 2136 can use dnspython updates and prerequisite checks; no arbitrary `nsupdate` shell invocation or full-zone transfer requirement.

Persist the selected binding/provider/account identity per request and applied publication. Do not rediscover a different provider on deletion after Settings changes. Reject overlapping ambiguous zones; use explicit longest-zone binding with apex/www/ACC/preview reservations. Only owner-approved bindings appear in Publishing. A provider change requires reconciliation, not blind reuse of opaque record IDs.

Ownership must use the ledger plus remote marker when available. For providers without comments, require matching remote ID and exact expected RRset/value; use conditional updates/prerequisites where available, re-read otherwise and disclose residual races. Refuse unmanaged conflicts or external drift; never replace an entire shared RRset or zone. Adopt legacy records only after explicit owner review. Record intent before mutation, reconcile crash/timeout with remote state, and never blindly retry uncertain creates. Use per-zone/name locks, bounded exponential backoff with jitter/Retry-After, pagination, short credential-revision caches and 20-second operation deadlines with an overall job budget. Strip secrets from normalized errors.

Apply DNS, then fixed Caddy routes; failure restores only the records changed by that request if they still match its expected values. Existing code only cleans up newly created records; the proposed ledger adds safe restoration for updates too. Unpublish removes Caddy routing first, then deletes only verified owned DNS. Failed cleanup remains visible/retryable; never report removal when remote state is unknown. Certificate TXT cleanup touches only the challenge value; parallel validations must preserve other TXT values.

## Setup and first run (proposed)

Fresh setup starts private `tls internal` with no DNS provider or API key. Trust the Caddy root on owner devices or use existing secure local-console setup access; never submit credentials over an untrusted warning-clicked connection. Publishing remains unavailable until a zone and provider binding are validated. Internal previews retain private wildcard DNS and internal CA behavior.

After owner creation, an optional wizard step **DNS and certificates** offers “Use internal certificates” (default) or provider/zone selection and links the same infrastructure Keys form. Settings is the canonical configuration for later changes; do not duplicate a mandatory shell-token prompt. Wizard can finish while DNS is unconfigured or validation fails. Transitioning names/CA shows device trust, origin/cookie and node pinning effects; no automatic insecure trust fallback.

DNS setup never opens router/firewall ports, changes private source filtering, enables a proxy or creates a public ACC route. Preserve the approved [REMOTE-ACCESS](REMOTE-ACCESS.md) rules: separate public SNI, mandatory device gate before ACC code, explicit exposure confirmation and external negative tests, LAN recovery; that design is not yet implemented. Published project sites retain their separate approval and never confer public dashboard access.

## Delivery, evidence and owner questions

Estimates are engineering days for one engineer after approval, excluding provider account procurement and review waits; confidence is moderate for Cloudflare, lower for other live APIs.

| Phase | Deliverable and release gate | Effort |
|---|---|---|
| 1 | Infrastructure vault/API boundary, Keys/Settings flow, helper/pinned release manifest, Cloudflare migration and guide; prove no agent/member delivery, filesystem attacks, apply/rollback/restart and fresh internal-CA installation. | 6–9 days |
| 2 | Python protocol/ledger + Cloudflare, Azure DNS and Route 53 for both purposes; preserve Publishing approval/reservations, live staging issuance/renewal and record cleanup/drift/crash tests on dedicated zones, desktop/phone browser path. | 5–8 days |
| 3 | Google, DigitalOcean, Hetzner; exact module/API compatibility, IAM fixtures and live disposable-zone tests per provider. | 6–10 days |
| 4 | Porkbun, Gandi, OVH, deSEC, RFC 2136; TSIG policy, concurrency and negative privilege tests. | 6–10 days |

Total 23–37 days plus independent security review (2–3 days) and owner qualification. Deliver each provider only after both consumers pass; display unavailable/pending, never imply all rows ship in phase 1. Tests must cover expired/revoked keys, redaction canaries including retired values, pagination/429/timeouts, missing vault key, symlink/ancestor swaps, provider outage without losing certificates, multiple TXT values, drift and uncertain mutation recovery. Verify browser owner add/replace/apply and member denial; production root/systemd, outside-network and physical-device tests need an operator environment. This document does not claim those tests ran.

Main risks: broad registrar/account scopes; module supply chain and shared Caddy process; secret persistence in service environment/backups; ACME rate limits/propagation and outages; DNS/Caddy non-atomic changes; API generation drift (especially Hetzner); credential rotation restart interruption. Reduce these through separate tokens/delegated zones, pinned artifacts, staging CA tests, explicit state/reconciliation and retaining LAN/internal-CA recovery. A compromised accd/root can still reach infrastructure secrets.

Owner approval requested:

1. Approve infrastructure-only Keys entries, with no reveal, project grant, sharing or remote delivery, and a separate Settings binding page?
2. Approve Cloudflare + Route 53 first and the qualification waves above, or name a different second provider/priority?
3. Approve CI-built pinned multi-module binaries and reviewed updates, with no on-host portal builds or arbitrary modules?
4. Require separate certificate/Publishing tokens by default, and explicit acceptance where providers cannot isolate zone/name/type privileges?
5. Approve optional wizard setup with internal CA default, explicit migration and a proposed 24-hour permission-protected rollback window (old token revoked after verification)?
6. Approve the guide's proposed sandbox/timeouts/log policy as a later implementation scope, with public access remaining governed by REMOTE-ACCESS?


## Phase 1 implementation evidence and release hold

The owner-only infrastructure vault uses a separate encrypted namespace, with purpose/provider/field/revision
bound into AES-GCM associated data. Owner browser mutations require current-password confirmation and CSRF/Origin
checks. Keys, binding save, private helper apply/rollback, strict legacy import, fixed errors and candidate CI pins
are implemented. Temporary-root tests exercise malicious files, failed apply recovery, restart, rollback, import,
redaction and real Caddy adaptation of internal/default templates. See the session handoff for final suite results.

The manifest intentionally has no artifact hashes: CI cannot run in this no-remote worktree, and this host must not
build Caddy. It remains unqualified and cannot be installed/executed by the new release path. An operator must review
CI outputs/provenance and commit independent hashes before live activation. No live Caddy configuration, service or
provider account was touched. Initial managed EnvironmentFile activation, legacy removal after verified 24-hour
retention, service sandbox qualification and live staging/production renewal remain operator release checks.
At the end of phase 1, Publishing credential selection was saved but unused. Phase 2 now implements the consumer cutover and ledger below; retain the legacy file until the owner confirms tested cutover. The setup wizard keeps its existing internal-CA path in phase 1.

## Phase 2 candidate implementation (2026-10-23)

Portable `DNSProvider`/wire-adapter interfaces and the migration 0043 ownership ledger are implemented. Every
Publishing mutation commits intent before network I/O and requires ledger plus exact remote snapshot for existing
records. Ambiguous writes retain pending/uncertain state; they do not retry. Azure uses ETags; Route 53 uses an
atomic exact-delete/create batch; Cloudflare re-reads with a disclosed residual race. Caddy refusal restores only
still-owned DNS, including prior updates. Explicit legacy adoption requires an applied historical publication,
matching remote ID and pinned address. There is no marker-only adoption.

Cloudflare Publishing selects the purpose-bound vault credential per job. The legacy file is retained until owner
confirmation; a password-confirmed source switch supports deliberate rollback, without automatic error fallback.
Azure and Route 53 forms, adapters, fixed Caddy rendering and exact CI module pins are candidates only. The release
manifest still has no binary hashes and no live qualification evidence. All new-provider production use remains
unavailable. The default root policy still allows only Cloudflare; portal requests cannot expand it.

The owner-run `acc.dns_live_test` requires an explicit disposable-zone acknowledgement, separate private credentials
and independently reviewed candidate hash. It tests record lifecycle/refusal and staging issuance with a seeded TXT
sentinel that must survive challenge cleanup. It preserves private intent for uncertain trial mutations. Fixtures
are synthetic API-schema transcripts, not live account recordings. No live DNS or machine configuration changed.

**Known certificate blocker:** the pinned `caddy-dns/azure` dependency uses `libdns/azure v0.5.0`; its `deleteRecord`
removes an entire matching RRset and its append path refuses an existing set. This fails the required shared-TXT
semantics. Do not qualify or activate Azure certificates until a reviewed upstream fix or independently reviewed
pinned patch passes the sentinel/parallel-issuance tests. Merely changing an availability flag is not a fix.
See [the pinned dependency source](https://github.com/libdns/azure/blob/v0.5.0/client.go).

Live issuance/renewal, negative IAM tests, both architectures, CI build compatibility/provenance and root/systemd
activation remain operator gates. See the Guide's DNS Providers page and the phase 2 handoff for exact trial-account
requirements. The owner expects all major providers over time; later waves remain unchanged.

## Wave 2 candidate implementation (2026-10-24)

Google Cloud DNS (`googleclouddns`), DigitalOcean and Hetzner adapters share the phase 2 `OwnedDNS` ledger,
owner-only infrastructure vault, fixed schema parser and release gates. All three remain **NOT qualified** for both
consumers in `deploy/dns/release.json`; no live DNS calls were made. Exact module commits and the Hetzner `/v2`
major path are pinned there. The same disposable-zone harness accepts all three IDs. Synthetic response transcripts
exercise create/update/delete, drift/foreign ownership refusal, pagination and redacted errors; they are not live recordings.

Google Publishing uses explicit project/managed-zone identity and atomic exact-delete/create changes. Its certificate
candidate requires a service-account file and project-wide discovery; the harness supplies a private temporary file,
and the root helper delivers content-addressed root-owned, Caddy-readable files with generation retention and purge.
File custody/rotation/rollback still needs live qualification and security review.
DigitalOcean mutates record IDs; Hetzner uses current Console Cloud DNS RRsets and waits for actions. Hetzner refuses
non-atomic address-family changes. Neither exposes conditional mutations here, so external edits between the ledger
read and the write remain a disclosed limitation, as with Cloudflare. A shared name or changed snapshot blocks ACC.
See the [Guide](wiki/User-Guide/DNS-Providers.md) for least-privilege setup and scope limitations.

## Owner qualification kit (2026-10-25)

Use `PYTHONPATH=src python -m acc.dns_qualify` on a disposable Linux operator host. The
[qualification Guide](wiki/User-Guide/DNS-Qualification.md) gives exact per-provider permission checklists and the
single command template for all six current IDs. Wave 3 is not merged in this base and its IDs remain refused.
Azure emits a failed JSON hold report before any network writes: the known pinned shared-TXT deletion defect
must be repaired and reviewed first. No release qualification metadata or production availability changes here.

The command requires an exact `--disposable-zone` repeated as `--confirm-zone`, two distinct purpose credentials
through hidden interactive prompts only, a fresh private state directory and independently reviewed candidate
SHA256. No secret argv, environment credential chain, private credential input file or vault export is added.
The existing server adapters and durable ownership ledger perform A create/read/update/read, foreign-ledger refusal,
delete/absence. Scoped Python wrappers refuse any other zone ID or hostname outside random trial prefixes.
Caddy gets only the trial hostname, fixed staging CA and explicit credentials; it runs twice against the same
private temporary storage, with renewal forced through its automation policy on the second launch. A changed
serial and fingerprint, shared-TXT preservation after both cycles, sentinel deletion/absence and an empty ledger
are required. This is a renewal-path test, not proof of real lifetime/ARI scheduling. The fields used are documented
in the [pinned Caddy automation source](https://github.com/caddyserver/caddy/blob/v2.11.7/modules/caddytls/automation.go).

Credentials' IAM scopes enforce Caddy's external boundary; the reviewed executable is trusted, not sandboxed by
Python's adapter wrapper. Discovery of one exact zone does not prove there are no inherited grants. Dedicated
projects/accounts remain mandatory where tokens cannot isolate a zone, and negative IAM tests remain a release gate.
The command stops at its first observed error, never blindly retries a mutation and retains private ledger/intents
for owner reconciliation. Existing adapters retain bounded read throttling and asynchronous operation polling.
Private bounded subprocess output is inspected for error events then discarded, never copied into reports.
`report.json` contains only fixed stages/errors plus the explicit zone, provider, timestamp and candidate hash;
`release_qualified` is always false. No installed service, live vault or production DNS is used by local tests.


### Owner-run Cloudflare result (2026-10-26)

The owner ran Cloudflare qualification on **2026-10-26 at 17:23 UTC**, after #330
fixed compressed provider replies, against **a disposable zone**. Two separate
zone-scoped API tokens were used, one for Publishing and one for certificates,
each with **Zone DNS Edit + Zone Read** for that zone only.

Candidate Caddy SHA256:
`22e29521abb1ce00c030192f3c6f9126ecd84c431a672872d04095c1b1c15aba`.

All **10 stages passed**:

1. Explicit single zone.
2. Publishing create.
3. Publishing update.
4. Publishing ownership refusal.
5. Publishing delete.
6. Certificate staging issuance.
7. Shared TXT preservation.
8. TXT cleanup.
9. Forced staging renewal.
10. Ledger empty.

The kit's `release_qualified` stays **false by design**: this records the owner-run
evidence, not an automatic release-flag change or approval of another Caddy binary.
