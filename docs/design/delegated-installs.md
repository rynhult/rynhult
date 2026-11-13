# Delegated account installs: admission wrapper

Status: round-two design security GO; Josh approved the recommended decisions on November 9, including extractor option (b). Implementation is qualification-fenced; see [the delegated approvals contract](../DELEGATED-APPROVALS.md).
Base: main `22f5b54b5` (#435), migrations through 0089. Date: 2026-11-09.
Round-two GO covers `6d4ab9f544`; its five follow-up points are folded in here.
Nothing here qualifies an executor or enables delegated installs.

## Basis and current boundary

This narrows step 4 of the approved October 31 delegated-approvals design.
Read [the current contract](../DELEGATED-APPROVALS.md) for the phase-two
implementation and authority boundary.
The approved source is the owner-held `handover/design-delegated-approvals.md`.
`src/acc/tooling.py` validates top-level coordinates and builds fixed argv.
It does not constrain dependency origins, lifecycle scripts or all writes.
Its npm command installs globally into `~/.local`; each call times out at 900s.
Its PATH starts with account-writable directories. Version observation alone
is not footprint proof. `broker/server.py:tool_change` rejects root and requires
exactly manager/name/version/operation. Account installs run in that unprivileged
broker. System managers use a separate root-helper path in `server/tools.py`.
“Root-side admission” must not mean running npm as root.
`server/approvals.py` records exact npm coordinates and a registry in consent.
`QUALIFIED_TOOL_EXECUTORS` is empty. `server/tools.py` also refuses non-person
execution decisions. Keep both fences until the entire new path is qualified.
Do not route a delegated decision into the legacy four-field RPC.

## Smallest useful v1

Recommend npm only, local native Linux, install only, reviewed recipes only.
Each recipe installs one command-line package with an exact name and version.
No ranges, tags, omitted versions, aliases, URLs, paths, Git or alternate managers.
V1 packages must have no dependencies, optional dependencies, peer dependencies,
bundled dependencies, lifecycle scripts or native build inputs such as binding.gyp.
This is deliberately smaller than general npm support. If no useful package meets
it, ship no install capability rather than quietly relaxing the rule.
Updates, overwrites and removals remain person-only in v1, including downgrades.
For an already-installed no-op, re-hash every declared real-prefix path at
admission against the recipe, including modes and link targets. Drift or missing
paths mean a collision for the person path. A matching no-op is still an audited
admission; only dispatched installs consume the daily execution quota. Accd caps
new no-op admissions at ten per account per rolling hour, across keys/teams.
Identical request keys read retained results; excess attempts get a fixed refusal
with rate-limited audit. Roll up matching no-ops and refusals in Inbox.
Only `https://registry.npmjs.org` is an origin. At catalog publication or consent
time, accd under the service UID fetches the packument and tarball. Verify exact
identity/version, HTTPS, no cross-origin redirects and `dist.integrity` SHA-512
over the `.tgz` bytes. Store both by digest in service-owned storage. No registry
credentials, mirrors or install-time registry access. Later upstream unpublishing
does not remove retained bytes. Hashes prove identity, not safety.
Send bytes over the accd→broker socket within the artifact ceiling, or use a
service/root-owned read-only path opened with `O_NOFOLLOW`. Broker re-verifies.
For npm, require packument version-entry and tarball `package.json` agreement on
name, version, all `*dependencies`, both `bundleDependencies` spellings, bin, man,
directories, scripts, gypfile, os, cpu, libc and engines. `hasInstallScript` must
be absent or false. Reject dependencies, nonempty scripts, workspaces, +build
versions, `npm-shrinkwrap.json`, any `node_modules/` entry, `.npmrc`, any `*.gyp`,
and unknown installation-affecting fields. Exact allowed empty/absent forms belong
to the closed schema. Preseed a one-version packument synthesized only from verified
data. Record upstream packument and synthesized cache-entry digests separately.
Inspect archives in accd/service code using isolated Python `-I`, stdlib `tarfile`
without extraction; repeat inspection in the broker. Never extract as root.
Match pinned node-tar/pacote semantics: strip the first path component, including
PAX path/linkpath and GNU long-name overrides, before checking effective paths.
Reject global PAX, duplicate effective paths, non-file/directory types (including
links, devices, FIFOs and sparse files), setuid/setgid/sticky bits, absolute paths,
`..`, NUL or non-UTF-8 names, multiple gzip members and data after tar EOF.
Allow only required tar EOF padding within a single gzip stream. Bound bytes and
entries while streaming. A generic tarfile acceptance result is insufficient;
qualify raw-header/gzip framing checks against pinned node-tar extraction.
All lifecycle scripts stay disabled. Also reject scripts in the inspected package;
this gives two defenses. A script exception is an exact person-approved operation
on the person path, with its own preview. It never qualifies a delegated recipe.
Playwright browser payloads need a later typed recipe adapter and qualification.
System browser dependencies, including apt/install-deps, remain person-only.
Destination is a new package slot under the agent account's `~/.local` prefix.
The recipe declares every permitted file, directory, mode and bin shim.
Bin links are `bin/<command> → ../lib/node_modules/<pkg>/<path>`; targets must
resolve inside that package. Expand a scoped package's string bin to its unscoped
command name before validation. Forbid `man`, `directories.man` and
`directories.bin` in v1. Account for npm mode normalization and bin-links fixBin;
reject CRLF shebangs instead of accepting hidden byte rewrites. Pin expected
post-npm bytes/modes. Exclude staging `lib/node_modules/.package-lock.json` from
publication: it is shared mutable metadata. Qualify real-prefix `npm ls -g` with
an existing hidden lockfile. Record scope-directory creation; remove only if empty.
Commands must match `^[a-z0-9][a-z0-9._-]{0,63}$`. Refuse names resolving on
trusted PATH `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`.
Reserve every command called by tooling.execute/installed_version, including
npm, pnpm, yarn, python3, pipx, uv, cargo, go, gem, brew, apt-get, dpkg-query,
snap and flatpak, plus node, npx, corepack, env, sh, bash, git, ssh, sudo, tmux,
claude, codex, copilot, and the patterns `acc-*` and `rynhult-*`.
Refuse names already present in ~/.local/bin, ~/.cargo/bin, ~/go/bin or
/opt/homebrew/bin. Recheck at publication; no-clobber also covers prefix races.
Refuse unmanaged files, command collisions and shell startup or PATH changes.
Account-wide tools can affect every project sharing the account. Show that scope.
No system/shared prefix, other account, project checkout or credential writes.
A private per-operation cache and temporary directory are the only extra writes.
Proposed fixed ceilings, lowerable by consent: 25 MiB compressed artifact,
100 MiB unpacked artifact, 10,000 entries, 200 MiB total private scratch,
60s fetch, 120s execution, 30s verification and 1 MiB captured output.
Reject archive expansion before exceeding limits. Total scratch disk usage is
monitored by a broker byte counter and aborted on excess, not a hard disk quota.
A hard total ceiling would require a separately provisioned sized filesystem.
Use RLIMIT_FSIZE, RLIMIT_NPROC and RLIMIT_AS with qualified fixed limits.
Use start_new_session and killpg on deadline/failure. One service-driven account
mutation at a time: the broker lock covers stage through publish/compensation,
legacy person tool_change installs/removals and every service writer of ~/.local.
Record lock ownership in accd. It cannot lock out hostile same-UID processes.
Allow at most five dispatched installs per account per rolling 24 hours across
teams/consents. Failed dispatches count; new children do not reset the counter.
These are recommendations pending Josh's decision and disposable-host tests.

## Typed admission and accd-driven two-phase apply

Add a versioned `account-install-admission-v1` protocol. Keep legacy person
requests separate. Unknown protocol, extra fields or unsupported runtime refuse.
A wrapper is enforcement code, not a metadata label around the current RPC.
No code or migration is proposed in this document.

Agent input contains only a stable request key and recipe ID plus recipe digest.
The server constructs the execution envelope, with these typed fields:

- Protocol version, operation ID, normalized request digest and single-use nonce.
- Authenticated spool source, requesting session, deciding lead, assignment/attempt
  where present, team ID/generation, project and frozen grant digest/revision.
- Stable local machine ID, broker runtime ID, account ID and numeric non-root UID.
- Recipe ID/revision/digest, manager, package, exact version, registry,
  metadata/artifact integrity and expected file-manifest digest.
- Issuer, consent ID/revision and parent chain, Security/authority revision,
  registry version, qualification fingerprint, limits and lease deadline.

Account and provenance are derived from enrolled transport and server records.
Agent-supplied identity, UID, paths, environment and command fields are rejected.
The existing native spool authenticates an account boundary, not each session.
The broker itself runs as the agent UID; its files, process environment and
credentials are agent-held. No broker replay cache or credential is authority.
An agent can kill/restart it and alter account files. The lead decision adds no
security against that agent. No human token enters the agent spool.
An agent already can run npm and alter its ~/.local; there is no account-wide
outbound firewall. Kernel DAC stops cross-account writes. This route must keep
consent/publication records truthful and prevent laundering into trusted runs.
It must not expose privileged/service code to unsafe package parsing/execution.

Refuse non-local hosts at server admission and in the broker protocol. Require
whole-account authority: project.account == user.account or instance owner,
plus current authority over the account footprint; project ownership alone fails.
Server admission checks live Build lead, ownership, Teams availability,
`approvals.enabled`, current issuer authority, consent scope and all parent states.
Intersect these with the frozen team grant, machine/account and OS ceilings.
Stop, expiry, changed generation, deny or revoke wins. Project consent creates
bounded team children; no widening or consent inferred from prose/setup level.
Once binds this exact request and recipe digest. Team consent ends with the team.
A durable project consent names explicit recipe digests and may have an expiry.
New recipes never enter an old wildcard. Bump the capability registry version;
old coordinate-only shadow consents cannot silently authorize the new protocol.

Accd owns single-use, nonce, quota, lease and state in its database. It resolves
trusted recipe bytes and verifies all digests. A changed payload under the same
caller/key refuses; identical replay reads retained state. No offline start.
The accd client drives both phases over its existing broker connection:

1. Accd atomically consumes the operation (`queued→dispatched` CAS), consumes
   bounded consent allowance and daily quota, and starts the qualified start lease.
   Failed dispatches retain consumption. Nonce uniqueness is enforced in accd.
2. Accd sends `stage(envelope)` and artifact bytes. The broker takes the shared
   lock, checks UID/home inode/runtime/digests/deadline and returns a staged
   manifest. Accd stores that manifest and CASes `dispatched→staged`.
3. Accd re-runs current() for consent and every parent, Security/grant revisions,
   Stop, expiry, availability, qualification and the start lease. In the same
   revision-fenced transaction, CAS `staged→publishing` with manifest digest.
4. Accd sends `publish(operation_id, manifest_digest)` and records per-path
   results. Publication starts only under that accd decision. No broker reverse
   online-consume channel or broker-side authoritative nonce is assumed.

Qualification pins the start lease to the bounded staging time plus verification
and dispatch margin: initially 160s (120s execution + 30s verification + 10s).
Step 3 still rechecks current authority; no publication after lease expiry.
No DB transaction spans execution or socket waits.
Broker checks are defense in depth against server errors. Agent-UID reports are
not independent attestation against a compromised broker. Accd's records remain
authoritative even if broker-local state is forged, lost or restarted.

Option (a) leaves tooling.execute and its fixed argv unchanged. Its run callback
validates argv and substitutes absolute service/root-owned qualified npm and Node.
It ignores the executor's env, timeout=900 and implicit cwd=/ entirely.
Use an empty private cwd and one execution deadline across install and npm ls.
Environment: HOME (staging home), LANG, PATH exactly
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` (no user bins),
and these lowercase npm_config_* keys: registry=https://registry.npmjs.org,
update_notifier=false, audit=false, fund=false, userconfig=/dev/null,
globalconfig=/dev/null, ignore_scripts=true, offline=true, cache=<scratch/cache>.
No other inherited variables, NODE_* or uppercase NPM_CONFIG_* are allowed.
The home argument is staging; its .local is the temporary install prefix.
Preseed the pinned npm cache with service-fetched verified bytes and the synthesized
one-version metadata. Qualify cache layout, offline lookup and observation.
Before npm starts, apply unprivileged Landlock write restrictions to staging,
cache and tmp; require Landlock ABI 4 TCP connect/bind denial. Fail closed if
unsupported. Qualification records that ABI 4 on kernel 6.8 does not restrict UDP
or filesystem/abstract Unix-socket connections. Pin the observed “no Unix-socket
connections attempted” result to the exact npm runtime. This constrains npm's
descendants, not other account processes. Offline mode is additional protection.
Do not claim hidden
credentials or protected staging. No namespaces, new OS grant or distinct UID.
Same-UID staging tampering is handled by verify-while-copy publication below.
Option (b)'s fixed extractor runs in the agent-UID broker, writes staging, then
uses the same verify-while-copy. Never extract in accd or as root. No npm execution;
Josh must approve departure from the parent's unchanged-executor thin path.

## Recipes and review

A recipe is data with a closed schema: ID/revision, exact coordinates and hashes,
platform/runtime fingerprint, allowed origins, archive and output manifests,
command names, limits and fixed adapter type. It contains no shell or free argv.
The selected npm-package adapter is the only v1 step. Future multi-step recipes need
separate schema, review and qualification; mixed system/account steps refuse.

Maintainers write recipes. An independent security reviewer checks origin,
package identity, scripts, outputs, collisions and isolation assumptions.
The person approves catalog publication and each selected consent scope.
Store source manifests in a versioned reviewed catalog, proposed path
`catalog/tool-recipes/`. Deploy a digest-addressed service-owned copy outside
agent-writable repositories, homes and spools. The broker uses that trusted copy.
Use approvals.digest() canonical form: sorted keys, compact separators,
ensure_ascii, no floats. Forbid floats in schema. Retain artifact SHA-512.
A changed byte creates a new digest requiring review and fresh consent.
Revocation of a recipe or qualification blocks new execution immediately.
A task checkout's recipe is only a proposal, never executable authority.

## Footprint, results and recovery

Accd retains the journal of record, staged manifest and pre/post footprint
hashes. Before dispatch record prefix identity, managed versions and declared
paths' type/mode/owner/size/hash/link target, recipe/runtime digests and revisions.
Exclude credential reads. Read versions from verified metadata, never package code.

Before each publication step, send its intended action and footprint to accd and
persist there. Send observed results back after each step. No accd acknowledgement
means no further step. Broker-local journals and seven-day agent-space scratch
are advisory and editable, never authoritative evidence.
For every declared regular file, use descriptor-relative no-follow parent traversal
and openat(O_NOFOLLOW) on staging, require a regular file, then stream once into
a fresh destination-directory temporary file while hashing. Check hash and mode
against the recipe, fsync, then renameat2(RENAME_NOREPLACE) and fsync the directory.
Refuse changed destination directory identities. Bin links are made by the broker
from recipe data, never copied from staging; publish them with no-clobber semantics.
Record directory creation, including scope directories. Publish no hidden lockfile.
Tampering encountered during copying refuses. The claim is that copied bytes
matched at publication; same-UID processes can alter staging, destination temps
or installed files afterwards. There is no lasting integrity or hostile-broker
attestation guarantee. Re-hash observations and record that time-bound limitation.

Mark installed only after all declared paths and versions verify. Whole-tree
publication is not atomic; readers may see partial files/shims. On failure kill
the process group. Compensate only journaled new paths still matching accd evidence;
remove directories only if still empty. Never delete another writer's changes.
A crash, restart, lost reply or unproved restoration becomes uncertain and blocks
further installs. Only a person may clear this block. Reconcile the real prefix
against accd's journal, never a local copy alone. No automatic re-execution;
replay reads retained state and quota stays consumed. Keep durable service-side
evidence even after advisory scratch expires (recommend seven days).

Map every executor error, including ValueError with stderr or OS text, to fixed
reasons: authority_changed, lease_expired, artifact_invalid, collision,
footprint_mismatch, runtime_unqualified, resource_limit, executor_failed,
transport_lost, or internal_error. Never forward exception strings. Sanitized,
bounded npm output is private evidence only, not a result reason or approval text.

Use existing `approval.claim/use/result/refused` and `tool.result` conventions.
Extend the audit dictionary during build for recipe withdrawal and reconciliation.
Record issuer versus actual lead, request/operation/recipe digests, revisions,
broker identity, timestamps, fixed refusal/outcome reason, footprint reference
and cleanup status. Bound and sanitize output; no raw exception strings or secrets.
Keep result envelopes in the service-owned reply store with the operation digest.
Retain uncertain/failed history and downgrade fences. Dispatch is not completion.

## Threats and hard refusals

| Threat | Required response |
|---|---|
| Dependency confusion or alternate origin | Zero dependencies in v1; exact reviewed artifact and canonical registry; offline npm; reject config/mirrors/redirects. |
| Typosquat or malicious official package | Review exact identity and bytes; no arbitrary registry-wide consent. Official is provenance, not trust. |
| Postinstall/native build code | Inspect manifest/archive; reject scripts/build inputs; enforce ignore-scripts and no network. Script exception stays person-only. |
| PATH/shim hijack | Trusted absolute runtime; inspect all declared shims; reject collisions, escaping links and startup/PATH changes. |
| Cross-account or shared writes | Kernel DAC, UID and path checks; Landlock for npm writes; verify while copying; no root execution. |
| Replay or concurrent changes | Caller-bound key/digest, atomic counters, accd-owned lease/nonce/quota, shared legacy lock and service-side journal. |
| Revoke during install | Accd rechecks before publishing; refuse cleanly there. Publication itself has a residual race. |
| Unknown manager/runtime or footprint | Fixed refusal; show person path. No generic shell fallback or automatic retry. |

Revocation cannot recall a syscall already in progress or remove published bytes.
A revoke racing the last publication check can leave effects; report the actual
outcome and reconciliation state, not “cancelled” by inference. Revoke does not
uninstall. Stop/disable uses the same cancellation and result rules.
Host root compromise and hostile same-account runtime use remain outside this
proof. Hashes and enrollment do not attest a compromised machine or agent broker.

Person-only forever: root/system/shared installs, OS/account/ACL/folder grants,
security-policy changes, publication/network-access expansion, secrets/login/MFA,
recovery/fencing overrides, protected-branch/history bypass, and overriding Stop,
expiry, required review or failed checks. This capability never grants main or
remote authority. Account removal is outside install consent and requires its
own person decision and dependency checks. Future removal delegation would need
separate review; do not mislabel it as a permanent prohibition in the parent design.

## One enable and consent screen

Extend Security → Delegated approvals, rather than adding another allow file.
Phase two has inspection/revocation and Inbox activity, but lacks enable/grant UI.
Show Off by default, Teams availability and why a capability is unqualified.
Enablement alone grants nothing. Render a proposed effective diff before saving.
No checked default or setup level silently creates consent for existing installs.

One screen offers separate typed cards for account installs, ports and image checks.
Show issuer, project/team/lead, machine/account plus numeric UID, shared-account
warning, duration (Once / This team / Remember for this project), expiry and limits.
Install cards list recipe digest/version/origin, outputs, commands, scripts disabled,
account-wide effect, qualification and that revoking does not uninstall. Ports
show dev/prod pool and aggregate count; reservation grants no publication. Images show exact provider/model,
UID/runtime, quota and read-only synthetic probe; no private references are sent.
Use current recent-reauthentication, CSRF/origin and human authorization rules.
Unavailable, group or token-only scopes retain their current refusals.

Save enablement and every selected consent in one database transaction with
revision checks; any failure rolls back all changes. Refresh conflicts while
preserving the person's draft. Require authority over the whole account.
Warn explicitly: “Any process in this Unix account may install these exact recipe
digests, up to N per day, affecting every project on the account. The lead's
approval does not isolate helpers. Revoking does not uninstall.”
Show stored versus effective consent, parent linkage, remaining allowance,
last use, revoked history and Revoke even while delegation is disabled.
Do not render a usable install consent while qualification is missing.

Inbox Team activity links to each request, recipe and footprint/result. Group
matching admissions without hiding failures, uncertain cleanup or refusals.
Unmatched requests remain person action cards with exact preview; a digest is
information, not an approval button. Dismissal does not retry or approve.
Escape catalog/package text. Show partial outcomes item by item.

## Tests, qualification and delivery gate

Design security GO is recorded; build awaits Josh's open-question decisions.
Re-inventory main, reserve a migration then, and update Guide, authorization
inventory, audit dictionary and upgrade notes. Preserve default Off and old
person decisions. Retained operation/recipe evidence must fence downgrade.

Use bounded fixture tests for strict schemas/types, canonical hashes, missing or
changed provenance, all revisions/parent limits, duplicate keys, concurrency,
unknown protocols, offline start refusal and every legacy bypass route.
Test revoke/stop/disable before admission, between claim/start, during npm and
during publication. Assert fixed errors, retained quotas and honest replies.
Test malicious manifests, optional/peer/bundled dependencies, scripts, native
build inputs, tar traversal/links, decompression bombs, poisoned cache/config,
PATH substitution, collisions, prefix races, UID/runtime swaps and cross-account
writes. Inject timeout, process crash, disk full, lost reply and restart after
every journal transition. Prove no silent retry or destructive compensation.

Test Security enable/save/revision conflicts, all three consent families, narrow
screens, keyboard/accessibility, revoked inspection and Inbox partial results.
Test reserved system commands, scoped string bin, existing hidden lockfile,
CRLF shebangs, packument mismatch, PAX overrides, gzip members, broker restart
between stage/publish and concurrent legacy tool_change. Test false broker reports
without treating broker credentials as proof. Assert atomic enable/consent save.

Qualify in owner-approved disposable Linux fixtures with two accounts/projects,
the exact Node/npm/runtime and actual sandbox. Record filesystem/network effects,
resource ceilings, hostile same-UID interference and denied root/shared writes.
No live instance or package installation belongs to this design task. Unit mocks
cannot qualify this boundary. Enable a precise runtime/adapter/recipe fingerprint
only after independent security GO and owner qualification. Changed fingerprints
fail closed until requalified. Replace manager qualification with a set of
protocol/runtime/adapter/recipe fingerprints. Test that adding a manager name alone
admits nothing and a REGISTRY_VERSION bump invalidates old consents via current().
Track the existing person-path writable-PATH risk separately: an agent can already
plant npm/python3/etc. A trusted absolute runtime there needs a separate fix.

## Approved decisions (November 9)

1. Accept npm-only, dependency-free reviewed recipes for v1? Recommend yes;
   select a useful qualifying package before build, or keep installs disabled.
2. Choose (a) npm + Landlock + verify-while-copy, or (b) trusted extractor with
   no npm execution? Recommend (b) for the smaller attack surface and no npm cache,
   packument resolution or fixBin behavior; this needs approval to change the
   parent's execution seam. If that seam must stay unchanged, recommend (a) only
   after exact-runtime Landlock/cache/publication qualification. Both keep accd
   authority and verify-while-copy; neither requires protected staging. Under (b),
   packument-resolution/cache agreement is moot; tarball checks still apply.
3. Accept explicit recipe-digest consent rather than a registry-wide default?
   Recommend yes for v1; wider dependency/recipe policies need later review.
4. Accept the proposed size/time/daily ceilings and seven-day scratch retention?
   Recommend yes initially; disk is monitored/aborted and scratch is advisory,
   while accd retains durable evidence. Tune from disposable qualification only.
5. Keep script exceptions, updates/removal and Playwright outside v1 delegation?
   Recommend yes; exact person approvals remain available through existing paths.
6. Ship one Security enable/consent screen for installs, ports and images?
   Recommend yes, with enablement and consents saved atomically in one transaction;
   preserve default Off and current group/token refusals.
7. Accept the explicit shared-account impersonation and cross-project footprint?
   Recommend only with whole-account authority and the explicit “any process in
   this Unix account” warning above. Project ownership alone is insufficient.
   If session identity is required, wait for separate spool credentials.
