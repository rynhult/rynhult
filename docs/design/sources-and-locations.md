# Sources, working locations and multi-repository projects

> **Status: approved design (Josh, 2026-11-01).** P4.1 has an additive implementation on `p4-sources`, pending its focused security review. Later phases remain unimplemented.

Status: **approved P4 design; P4.1 implemented for review, not release-qualified**. 1 November
2026. Baseline: `main-475e`, `475e6063dc15a94d676eac468f6bcc2c20ac86a0`.
This document recommends a contract and implementation gates; it grants no new
permissions. The P4.1 implementation adds identities and read projections only; it does not enable discovery, connection setup, multi-repository selection or a Sources UI.

## 1. Inputs and baseline

Read the assigned design package's Work and identity model, W2, P4, AC-035–AC-041,
S16 and four workflow mockups, plus reconciliation finding C8. The mockups describe
future grouping and connection views; they are not evidence of implemented
multi-repository membership, enterprise connectivity or saved-layout restoration.

Verified repository facts:

| Area | Current source and constraint |
|---|---|
| Project | [models.py](../../src/acc/server/models.py): one `repo`/`remote_url`, `host`/`account`/`workspace_path`; personal `owner` or `group_id`, not both; stable project ID and slug |
| Placement | Same file: unique project/person/host/account/path routing snapshot, with trust, synchronization and machine-account association; not a permission grant |
| Session | Same file: project/placement references, actual host/run-as/cwd/worktree/workdir and immutable LaunchSpec; detached history may have no project |
| Launch | [launch_machines.py](../../src/acc/server/launch_machines.py): explicit machine/account eligibility and recorded checkouts; remote checkout preparation is already a separate operation |
| Deletion | [project_delete.py](../../src/acc/server/api/project_delete.py): folder removal requires `managed_checkout && import_mode == managed`, confirmed quiescence, authenticated folder/worktree identity and cross-reference checks; imported folders stay |
| Provenance migration | [0079](../../src/acc/server/migrations/versions/0079_project_delete.py): unknown older creation provenance defaults to false; deleted history must not be reattached or IDs recycled |
| GitHub reads | [github.py](../../src/acc/server/github.py) uses a server-held dashboard PAT and a fixed github.com API origin; agents use their own `gh` login |
| Existing API distinction | [projects.py](../../src/acc/server/api/projects.py) gates dashboard GitHub snapshots to owners' projects; [pulls.py](../../src/acc/server/api/pulls.py) selects the dashboard token for eligible owner operations or the person's account-local broker login. This is not one interchangeable credential pool |

Keep the current private Research/Development/Council Workspaces and explicit
promotion workflow. A Project organizes work; it is not intrinsically a folder,
a GitHub organization, a host or a new tenancy boundary.

## 2. Identities and invariants

| Identity | Durable key and display | Must remain distinct from |
|---|---|---|
| Hosting instance | Opaque ID plus provider and canonical service origin; initially the qualified github.com adapter | Rynhult installation, remote execution host and source owner |
| Source owner | Hosting-instance ID + verified provider owner ID; mutable login/display name and organization/personal kind | Rynhult person/group or credential subject |
| Repository | Hosting-instance ID + verified provider repository ID; mutable owner/name locator and canonical web URL | Fork parent, similarly named repository, local checkout and connection |
| Authenticated connection | Opaque ID, controlling Rynhult principal, hosting instance, authentication method, verified provider subject, credential generation and applicable execution scope | The source owner, repository permission or permission to act as another Rynhult person |
| Working location | Opaque record ID with host identity + agent-account identity + exact recorded path; canonical/descriptor identity verified at use | Repository, directory display group, saved UI layout or another account's identical path string |
| Session | Existing native conversation/process ID and frozen launch context | Project membership, a repository-wide worker, a layout tab or a connection |

The same `github.com/cedar-labs/api` name under another hosting instance is a
different source. `github.com/maple-labs/api` is also different. A fork has its own
repository identity and an optional descriptive parent relation. A rename or
transfer preserves a repository only after the selected adapter verifies the same
provider ID. Names, URL redirects and matching Git remotes alone cannot merge IDs.
A deleted repository recreated at the old name is a different identity.

Repository identity is independent of credentials: two authorized connections can
observe the same verified repository, but do not share credentials, observations
or permissions. An unresolved legacy URL remains an authority-scoped locator;
without a verified service ID it is not globally deduplicated. Git transport URLs
are not API origins. Keep SSH/HTTPS transport preferences separate from source
identity; never rewrite a remote to normalize identity.

The same path on two hosts or under two agent accounts denotes different
locations. Machine labels and Unix usernames can be reused: bind to enrolled host
identity and account provisioning generation, resolve OS identity on the target,
and refuse a replaced identity until explicitly rebound. The main host needs a
stable installation-local identity, mapped from today's `local` route key.
`GroupMachineAccount` remains its existing group-routing authority, not a new
universal permission inferred from a username.

## 3. Proposed additive data model

Names below are logical entities, not a committed migration numbering scheme.
Opaque IDs must survive renames and must not be reassigned to replacement objects.

| Entity | Fields and integrity rules |
|---|---|
| `HostingInstance` | Provider, canonical origin, adapter/qualification version; unique provider/origin. Only qualified adapters may make API requests |
| `SourceOwner` | Instance FK, provider owner ID, observed login/kind/time; unique instance/provider ID. Keep rename aliases descriptive |
| `Repository` | Instance FK, provider repository ID when verified, current source-owner FK, name/web locator, optional fork-parent ID and observation revision. Verified IDs unique within instance |
| `RepositoryLocator` | Authority scope, sanitized legacy locator, optional resolved repository FK, verification state/time; unresolved locators never become cross-person lookup keys |
| `SourceConnection` | Principal, instance FK, method (`legacy_dashboard_pat` or `agent_gh` initially), provider subject ID, optional host/account identity, opaque secret reference or broker handle, credential generation, state and last checked time. No credential bytes in DTOs |
| `ConnectionRepositoryAccess` | Connection + generation + repository, observed operation capabilities, state/error category/time and expiry; no global connected/access flag |
| `ProjectRepository` | Existing project FK, repository or unresolved locator FK, stable membership ID/revision and metadata visibility scope. At most one compatibility-primary membership per project; membership is not a permission |
| `WorkingLocation` | Authority domain, host/account identities, recorded and verified canonical path, checkout/worktree/local-folder kind, optional parent checkout, verification revision/time/state. The scoped coordinate tuple identifies the physical target; aliases require verification |
| `ProjectLocation` | Project/membership/location FKs, existing Placement ID where applicable, person, trust, synchronization and approval references. A local-only binding has no repository membership. It must not bind a location to an unrelated source |
| `LocationRegistration` | Original project/Placement registration ID, raw legacy coordinate reference, creation provenance, legacy import/trust values and deletion evidence. Routing deduplication cannot discard or strengthen this custody record |
| `ProjectLaunchDefault` | Explicit actor-appropriate project/membership/location binding and revision; no last-used-location inference. A compatibility default names the original registration |
| `SessionTarget` | Sidecar to existing session/run: chosen membership/location/connection IDs and revisions plus frozen repository/host/account/path/branch identity. Existing LaunchSpec stays immutable |

Enforce the source/locator alternative, membership-to-project consistency and
location-to-membership source consistency with FKs/checks plus transactional
validation, not UI filtering alone. Local-only targets are explicitly typed.
History/custody FKs restrict destructive cascades; retire referenced records.
Compatibility-primary/default changes use compare-and-swap revisions in one
transaction. Provider IDs are opaque values; normalize display locators only by
qualified adapter rules, never lowercase arbitrary filesystem paths. A broker
connection requires a host/account identity pair; an installation PAT has no
execution account and cannot masquerade as that connection type.

Stable execution identities require an implementation schema review against the
actual enrolled-machine/account lifecycle. Do not use a recycled numeric user ID,
a display hostname, a supplied UID or a GitHub login as that identity. Preserve
current reserved usernames/authority references until any dedicated principal-ID
migration is separately proven.

A project can have many repositories; a repository can be referenced by multiple
already-authorized projects. Each membership can have zero or more locations.
A repository without a checkout is valid for browsing, but is not launchable.
One repository with three checkouts is **1 repository, 3 working locations**.
A local-only project can have **0 repositories, 1 local working location**; do not
invent a Git repository just to fill the new model.

Location records and repository metadata are partitioned by existing authority.
Within a domain, identical verified coordinates may share a routing record, but
keep every registration/custody edge. Across private/group domains, do not reveal
or merge records through a public deduplication endpoint. Server-side deletion
checks still account for all physical references, with redacted refusal messages.
Conflicting source bindings for one coordinate are a conflict to resolve, not
permission to overwrite the checkout. Offline lexical matches are not proof of
physical identity; preserve ambiguous registrations rather than guess.

### Visibility and mutation boundaries

Every lookup first authorizes the existing personal project or group resource;
then applies its source/location/person visibility. Preserve group viewer rules
and the current non-owner Placement-person filter. A project list must never
expose another person's private location or session through counts, suggestions,
"used in project" labels, error details, pins or discovery caches.

A discovered private source starts in the connection principal's metadata scope.
Attaching it to a broader group is refused unless its metadata is already
explicitly authorized for that audience. Any required metadata-sharing approval
is a separate operation and implementation gate; grouping must not create it.
Existing group source metadata retains exactly its existing audience on migration.
Neither a global Repository row nor membership authorizes Git contents, filesystem
access, launch, merge, publish, keys, account use or policy changes.

Project membership administration uses the existing project's metadata-management
authority, with revision checks, CSRF/origin controls and audit. Connection control
belongs to its principal; installation PAT management retains current owner rules.
Group maintainership does not confer installation-token use or another person's
broker identity. Active sessions retain their frozen target when membership or
presentation changes. Unlinking an in-use source is refused or explicitly retired
for new work, leaving history references; it never stops work or deletes files.

## 4. Migration and compatibility

Use an additive, restartable migration and staged reader/writer rollout. No
network, broker, filesystem canonicalization, clone, Git rewrite or permission
change runs in the database migration.

1. **Inventory and freeze mappings.** Back up and record existing project IDs,
   slugs, owner/group, archive/deleting/private/builtin flags, raw source/location
   fields, trust, import mode, `managed_checkout`, backup identity, Placement IDs
   and dependent session/schedule/gate/runtime/publishing references. Preserve
   SQLite non-reuse constraints. Do not repair an existing deleting project or
   resume an interrupted destructive operation through migration.
2. **Backfill deterministic edges.** Each ordinary project gets one compatibility
   membership if it has a source locator, and its original location/registration.
   A project with no remote remains local-only or unresolved local Git, not a
   provider repository. Each Placement keeps its ID, person (including null),
   machine-account association, trust/sync state and exact tuple. Associate it
   with the original single source only when that relation is established;
   otherwise mark unresolved, non-launchable through the new selector. Null
   person never means "all group members."
3. **Copy provenance without upgrades.** Copy `managed_checkout` separately from
   `import_mode`. Preserve false/unknown as imported or unknown, with files kept;
   managed mode, current path under a default root or a matching remote is not
   creation evidence. Preserve readonly, trust and archive independently. Keep
   every original custody edge when routing records coincide. Private/builtin
   workspace projects retain their existing special lifecycle; do not backfill
   public memberships or promote them.
4. **Resolve identity separately.** On a person's explicit recheck through a
   qualified connection, verify provider IDs, owner and capabilities. Use a
   revision-checked proposal to coalesce locator references only within allowed
   visibility; preserve membership/registration IDs and audit the mapping. Renames
   update display locators only. Unavailable credentials leave unresolved records
   usable as authorized historical/local metadata, without fabricated access.
5. **Shadow and compare.** New read projections must match existing personal/group
   visibility, exact targets, legacy-primary values and deletion plans. Persist
   idempotency keys linking each old registration to exactly its intended new
   edges. Interrupted reruns do not duplicate memberships, locations or approvals.
6. **Enable exact target selection.** Only after parity, switch launch consumers
   to immutable target bindings. Existing `/projects/:id`, slugs, session IDs,
   bookmarks/fragments and group/Placement URLs continue to resolve. Old singular
   `repo`, `remote_url`, `host`, `account`, `workspace_path` fields project the
   original compatibility binding, never the first result of a membership query.
   They remain accurate even when another membership is added. Updates to that
   binding are explicit, reviewed operations, not grouping side effects.

Do not mutate active LaunchSpec records, worktree branches, runtime cwd or running
processes. Add SessionTarget sidecars only from established recorded context;
unknown history remains unresolved history. Keep `deleted_project` snapshots and
null project references detached; never recreate a Project to satisfy a new FK.
Existing sessions resume their exact original frozen target under current resume
eligibility checks, not a new project default.

Schedules, helpers, team starts, gates, project runtime/autostart, backup, ports,
issue/PR actions and exports must each acquire an explicit source/location binding
before multi-repository writes are enabled. Migrate existing deterministic jobs
to the original compatibility target. Fence launch publication against concurrent
migration, defaults/membership changes and deletion. A legacy job with an
unresolvable target pauses for resolution; it cannot select a new "first" source.

Compatibility URLs stay, but ambiguous legacy writes are refused with a target
selection/review path. A previously configured exact compatibility default can
satisfy an omitted target only when the response/review clearly shows it. Preserve
legacy source-specific views against the original binding until the person selects
another source; they must not combine CI or PR results from different repositories.
All target-specific caches include membership/repository/connection-generation
identity, not just project ID or owner/name strings.

### Deletion and rollback

Unlink membership, unregister location, delete a project record and remove a
managed checkout remain distinct operations. Grouping, connection revocation and
unregistering an imported location never remove files. Project deletion retains
existing confirmation, barrier, quiescence, live-process/team/gate/publication/
container checks and authenticated descriptor-bound folder/worktree identity.
Add reference checks across **every** membership/location/session/custody edge,
including aliases and other authority domains; no cascaded physical deletion.
Uncertain provenance, shared/overlapping references or an offline owning machine
refuse physical cleanup. They cannot be converted into successful deletion or an
assumed safe relocation. Existing record-only deletion checks remain in force.

For an imported or unknown registration, show files kept regardless of how many
repositories now belong to the project. A managed clone's exact original custody
evidence is necessary, not sufficient, for cleanup: account authority and current
reference/identity checks still apply. Removing the last display membership does
not transfer that custody to another project. Explicit private-workspace promotion
keeps its own provenance and backup-identity workflow.

Before enabling multiple memberships, rollback can return to legacy readers while
retaining additive records and the original binding. Once multi-repository-only
state exists, disable new writes and keep a compatible reader; refuse downgrade
to a single-repository schema. Exporting a report is not a lossless downgrade.
A pre-migration backup restore requires its matching filesystem/session state and
an approved recovery procedure; never silently drop extra memberships or reattach
deleted history. Migration qualification includes upgrade, interruption, rerun and
rollback refusal, not only a fresh database.

## 5. Exact launch contract (W2 / AC-039)

The immutable target is `(project, membership-or-local-target, repository identity,
location, host identity, agent-account identity, path, checkout/worktree/branch,
connection where required, relevant revisions)`. A project ID alone no longer
implies "every repository in this project."

1. Start with the task/project. Select one intended repository and recorded working
   location, or show the explicitly configured exact default. Local-only work has
   an explicit local-folder target. Multiple eligible sources/locations without a
   valid default require a choice; sorting or last use does not resolve ambiguity.
2. Review the repository's full host/owner/name or unresolved/local status,
   connection/provider subject where applicable, host, agent account, path/branch,
   provider/model/effort, team and effective access. A repository read credential
   is separate from the execution account and any push/merge identity. Navigation,
   discovery and review do not launch anything.
3. Server resolution authorizes the current principal, project/membership and
   selected person's Placement; verifies machine enrollment/account assignment,
   availability/freshness, trust/import mode, current folder-policy acknowledgement,
   OS access and existing launch/provider/container restrictions. A stale/offline
   or unverified location is unavailable, not an invitation to use another path.
4. Existing-checkout launch must verify the recorded repository/location binding
   without rewriting remotes. Unexpected checkout identity, account replacement,
   changed target/default, revoked required connection or stale review returns a
   conflict/refusal with the draft and exact selection retained. Recheck or choose
   a different target explicitly, then review again. Local work requiring no
   network may continue when its source connection is unavailable, if all existing
   local launch checks pass; do not invent a network requirement for local reads.
5. Immediately before publication, revalidate revisions and descriptor-bound target
   identity; compile and freeze the exact binding. Idempotency/replay is bound to
   this target and actor, not merely project ID. A race cannot publish into a
   replacement checkout or consume approval for a different source/account.

Creating a new location is separate: choose qualified host/account, destination,
clone or supported committed transfer, source/ref and exact credentials; show
side effects and review. It reuses qualified existing preparation primitives,
then registers the resulting verified location. Missing access never triggers a
clone/transfer, remote rewrite, folder grant or destination substitution inside
launch. Offline recovery retains task text and the exact target. Changing location
opens comparison/review; it does not relocate or start work.

One repository is selected initially. A future multi-source session must name an
explicit target/access manifest and independently authorize each location; adding
project membership cannot expand an existing session's manifest, mounts, vault
keys or Allowed folders. This is an application targeting contract, not a claim
that native processes cannot read other files already permitted to their OS
account. Existing OS/container/security boundaries and frozen session policy
remain authoritative.

## 6. Connections, discovery and S16 recovery

### Supported identity and credential boundaries

**github.com is the existing hosting/authentication baseline.** A consolidated
organization/repository browser and its pagination/access semantics are proposed
P4 work and still need fixture and qualification evidence. Existing PAT/agent-gh
support does not qualify OAuth, GitHub Apps or enterprise hosting automatically.
An enterprise origin such as `git.example.lan` is illustrative/unsupported for
this discovery adapter. Do not offer a working Connect action or send credentials
to it; existing manually configured Git operations keep their existing support.
Any enterprise adapter needs separate API-origin, TLS, authentication, redirect,
rate-limit, stable-ID, broker and permission qualification before enablement.

Represent the existing dashboard PAT as installation-owned, legacy use limited to
its currently authorized owner-project operations. Never offer it as a member's
personal connection or send it to an agent/broker. Account-local `gh` authentication
is bound to an exact person, host and agent-account generation; credentials remain
in that account, and reads go through its authorized broker. Discovering on host A
does not establish authentication on host B. Multiple provider subjects on the
same hosting instance are separate connections. A friendly connection label is
not an authenticated identity; show the verified subject and last-checked age.

Preserve current legacy URL resolution while making its actual credential choice
visible. New P4 operations bind the selected connection ID and credential generation;
no automatic dashboard-PAT/gh/other-account fallback after a refusal. Converting a
legacy operation to an explicit binding is gated on action parity and fresh review.
Replacing a credential with a different provider subject creates a new identity
binding; it cannot silently repoint old defaults, caches or pending approvals.
Reauthentication as the same subject advances generation and invalidates old
access observations/previews before recheck.

### Discovery workflow

Account → Connections chooses a qualified instance and exact connection; Browse
GitHub then lists authorized source owners and repositories through that connection.
Source ownership may be personal or organizational and differs from the signed-in
subject. Membership in an organization is neither sufficient nor necessary evidence
of permission to each repository. Observe capabilities per repository/operation.

Use bounded, connection-scoped pagination, cancellation and explicit completeness;
a failed or partially enumerated page is not an empty organization. Key caches by
Rynhult principal, connection/generation, hosting instance and filters/cursor;
provider identifiers deduplicate results only inside the authorized response.
Never combine one connection's successful pages with another's. Redact credential
material, private locators and inaccessible project names from errors and logs.
Provider redirects cannot cause a credential-bearing request to an unqualified
origin. Discovery only lists metadata: opening an existing project requires its
Rynhult authorization; adding/grouping a source is separate from checkout creation.

| Observed state | Visible behavior and permitted recovery | Prohibited inference/effect |
|---|---|---|
| Checking / partial discovery | Loading/partial state, page completeness and checked time; retry the exact selected connection | No "no repositories" until the authorized enumeration succeeds |
| Authenticated, repository access sufficient | Show the observed subject and operation-specific capabilities with age | Signed in does not imply clone, push, merge or publish authority |
| Expired / revoked | Access unavailable; retain authorized project/location identity and task draft; reconnect the exact supported identity, then recheck | No switch of credential, host/account or source membership |
| Insufficient access | Connection may be valid; name the denied operation and required provider access where safe; choose explicit provider-side recovery/recheck | No claim the repository was deleted, no automatic invitation or scope escalation |
| Denied / not found, cause unknown | Repository unavailable, cause not established; retain last authorized identity with age | Do not leak existence or classify a private resource as deleted from an ambiguous response |
| Rate limited / transport failure / offline broker | Unavailable/retained stale observations, retry guidance and provider delay where known | No credential failure claim or empty-list replacement |
| Verified rename / transfer | Update locator using same verified provider repository ID; invalidate owner/name caches and recheck access | No Rynhult ownership or filesystem remote rewrite |
| Removed access / repository deleted | Mark unavailable when evidence supports it; retain history and authorized local work | No unregistration, file deletion, private-workspace promotion or relocation |
| Unsupported host/auth method | Explicit unsupported/not qualified state and preserved local context | No invented Connected badge or credential-bearing probe |

S16 revocation invalidates server discovery pages, capability observations and
pending network-operation previews for that connection generation. Invalidate
principal-scoped browser caches, and replace no-longer-authorized cached rows with
neutral placeholders rather than revealing stale private names. Historical project
metadata remains only where current Rynhult authorization permits it; it is not a
live provider access claim. Local files/processes are neither erased nor stopped.
Explain that native account-local credentials or already-running processes may
have their own provider-side lifecycle: dashboard disconnect is not proof that
all native Git credentials or established processes lost access. Provider-side
revocation and Rynhult disconnection are distinct observed facts.

## 7. Presentation and mockup reconciliation

- Project Overview and Repositories & locations show one repository row with its
  authorized location rows underneath; count repositories and working locations
  separately. Offline rows retain target and last checked/seen ages. Local-only
  and unresolved sources remain explicit. "Resume workspace" is a separate P2
  contract, not a side effect of grouping or Browse GitHub.
- Directory uses compact bounded rows and server-side authorized pagination/search.
  Owner/host filters must match the **same membership/location edge**: an owner
  match on repository A plus a host match only on repository B must not qualify
  the project. With no host filter, sources without locations can still match.
  Counts and "used in" labels include only authorized resources; label partial
  counts as visible/partial. Preserve selected IDs, position and search on refresh.
- Browse GitHub separates selected connection, source owner and execution location.
  The mockup's Open project is available only for an authorized existing project;
  multiple matches require a choice. Display full hosting-instance/owner/repository
  identity wherever names collide, including forks and renamed locators.
- Account → Connections exposes personal connection metadata; installation legacy
  credentials keep the existing owner surface. Project → Sources holds membership
  and explicit defaults; locations link to the existing Security/Allowed folders
  editor for the selected machine/account rather than copying its writer.
- Reuse DESIGN.md tokens, native links/forms, keyboard focus and QueryState-style
  loading/error/stale distinctions. Verify 1440/390, both themes, owner/member and
  group roles, keyboard and axe when implementing. On phones, identity/target
  fields wrap and review remains explicit; no hidden fallback replaces a choice.

## 8. Phases, acceptance and release gates

| Phase | Deliverable | Required exit evidence |
|---|---|---|
| P4.0 — inventory and contract review | Approve stable execution identity mapping, metadata audiences, legacy consumer inventory and deletion custody rules | Baseline fixtures cover personal/group/imported/readonly/local-only/private/archived/deleting and detached history; no unresolved destructive migration decision |
| P4.1 — additive migration/read model | New identities/edges and compatibility projections; no multi-repository execution or new connection methods | AC-035/036/038 parity; interruption/rerun/rollback refusal; old URL and ownership matrix; identical deletion provenance; zero filesystem/network/grant side effects |
| P4.2 — qualified connections/discovery | Explicit github.com PAT/agent-gh identity and paged owner/repository reads | AC-040/041 + S16; connection/generation isolation, revoked/partial/insufficient access, rename/fork/transfer and cross-origin refusal; no PAT exposure or tenancy changes |
| P4.3 — membership/location UI | Multi-repository grouping, authorized counts/filters and explicit defaults; separate reviewed preparation | AC-035/037/038; whole relationship filter tests, private metadata/Placement isolation, filesystem/remotes/grants unchanged by grouping; sharing remains separately authorized |
| P4.4 — target-bound launch and all consumers | Freeze explicit source/location for launch, resume/jobs/helpers/runtime/PR/backup/publishing as applicable | AC-039 and full regression matrix; race/replay/deletion fences; no fallback, cross-source credential reuse or implicit access expansion |

Do not enable multi-repository execution for a consumer until its target binding
is proven. A metadata-only grouping pilot must keep every legacy consumer pinned
to its exact compatibility-primary target and disable actions on new sources
until ported. Feature flags stage delivery, never grant authority. Read-only
discovery and membership/location views can ship earlier if unsupported actions
are clearly disabled and existing single-source work still works.

| Acceptance | Concrete qualification cases |
|---|---|
| AC-035 identity | Same name under two owners/instances, two connections to one source, rename/transfer preserving verified ID, fork/new source at reused name, unresolved locator not auto-merged |
| AC-036 migration | Compare IDs/URLs/ownership/audiences, raw tuples, flags/custody/deletion plans for every P4.0 fixture; no history reattachment; idempotent interrupted upgrade and refused lossy downgrade |
| AC-037 grouping | Before/after filesystem, remotes, permissions, credentials, private-workspace flags, session manifests and running processes are unchanged; unlink also has no physical side effect |
| AC-038 locations | Same string on two hosts/accounts, replaced host/account, aliases, three checkouts of one repository, shared/overlapping registration references; accurate visible counts and exact targeting |
| AC-039 launches | Two sources/locations with no default, unavailable default, wrong/replaced account, offline/stale host, changed checkout or review revision, concurrent deletion/membership change and replay; refuse without relocation; local-only and network-free work keep existing checks |
| AC-040 authority | Several provider organizations do not create Rynhult people/groups; member cannot use installation PAT, see another private checkout/session or gain merge/publish/OS permission; group roles retain current Placement filters |
| AC-041 qualification | Qualified github.com methods only; illustrative enterprise origin cannot authenticate; partial/rate-limited/revoked/insufficient access and rename collisions produce distinct honest states |
| S16 recovery | Revoke during discovery/review/queued network action; invalidate generation-bound caches/approvals, retain exact authorized local context/draft, reconnect/recheck exact identity, never switch credential or clone automatically |

## 9. Risks and decisions before implementation

1. **Migration loss or deletion escalation:** authority/custody comparisons and
   fail-closed cleanup are release blockers. Grouping must never convert an
   imported registration into managed-checkout provenance.
2. **Legacy consumers assume one source:** inventory all jobs and project-level
   operations before multi-repository writes; compatibility-primary fields cannot
   represent all memberships. Disable an unported action rather than target the
   wrong repository. Per-location runtime/backup policy UI needs separate review.
3. **Private metadata and cache leakage:** execution authorization and metadata
   visibility are separate. Approve the source-metadata sharing model before
   adding private sources to broader groups; do not reuse an owner's connection
   as a shared connection. Test counts, errors and Back/sign-out as well as rows.
4. **Provider identity/capability drift:** access observations expire, login names
   are not stable identities, and a denied request may conceal existence. Adapter
   support and freshness budgets require measured qualification before release.
5. **Location identity races:** path text is insufficient. Preserve descriptor-bound
   broker guarantees and stable enrollment/account identities; unknown aliases,
   replacement accounts and unavailable machines need explicit resolution.
6. **Native ambient access:** target manifests cannot revoke permissions already
   available to an OS account. Keep that limitation visible; source grouping is
   not a sandbox or an account-wide credential revocation mechanism.

Recommended decisions are explicit immutable target bindings, a preserved legacy
primary, additive migration, no automatic credential fallback and github.com-only
initial discovery. Approval of the principal/account identity schema, separate
metadata-sharing operation, freshness budgets and per-location runtime/backup
policy are implementation entry gates, not assumed approvals in this document.

## P4.1 implementation boundary (1 November 2026)

Migration 0084 and `source_schema_v1.py` add the identity tables and backfill
registration sidecars. Legacy project and Placement rows remain authoritative for
permissions, routing and physical deletion. Each source-bearing project gets one
compatibility-primary membership pointing to an authority-scoped unresolved
locator; local-only projects get zero memberships. No service identity is guessed
from GitHub-looking names. Private/builtin workspaces keep their lifecycle.

The local execution-host ID is minted once per database installation. Enrolled
hosts use the node ID and public-key fingerprint; label renames preserve their
recorded identity. Enrollment fingerprints remain unverified metadata in P4.1;
the new generation refusal is deferred until an owner rebind workflow exists.
Existing enrollment authentication, revocation and group-machine authorization
remain authoritative, so an ordinary reinstall is not stranded by this sidecar.
Legacy personal account records lack a provisioning-generation proof. Their
opaque registration IDs and optional GroupMachineAccount reference are retained
as **unverified**, never coalesced by username or numeric UID. This phase permits
only the existing compatibility route, under its existing account and folder
checks. A later target selector must obtain account-generation/descriptor proof
and explicit rebind; it cannot promote these records automatically. Unknown
hosts and lexical path aliases likewise remain unresolved.

The read projections are `/api/projects/{id}/repositories` and `/locations`, with
matching `/api/groups/{group}/projects/{id}/…` routes. Existing project/group
authorization runs first; viewers cannot read group locations and non-owner
placements require the current exact person, excluding null-person placements.
Group member repository projections withhold legacy locator text rather than
promoting private source metadata into the shared audience. No connection secrets
are read or returned. Legacy URL fields stay unchanged; sidecar locators strip
userinfo, query strings and fragments.

New launches check the explicit compatibility primary/default before broker work
and again before publication, then freeze a SessionTarget sidecar for the actual session Placement (including
worktrees/helper checkouts) without rewriting LaunchSpec. The compatibility
origin is recorded separately from that execution location. Manual resume and
automatic start/retry refuse a changed frozen location; a changed project default
does not retarget an existing session. Group launches use their already-reviewed exact Placement.
Additional memberships, missing/stale defaults or replaced bindings refuse
rather than selecting another source/location. Existing resumes keep their saved
context; unknown historical targets are not fabricated. Legacy schedules, helpers,
runtime, backups, publishing and other project consumers still read the unchanged
single-source fields. There are no membership/default/connection write routes, so
new-source execution stays disabled until those consumers are ported in P4.4.

Custody remains per original registration. Identical offline coordinates are not
coalesced; deleting a Placement detaches its original ID and reusing that integer
creates a new registration. Project deletion detaches additive project links but
retains provenance, without changing its folder deletion plan or importing
creation rights. Downgrade retains additive compatibility tables for old readers
and repeatable upgrade; verified sources, connections, extra memberships and
new-only location/default state refuse downgrade instead of dropping data.

Round 2 compatibility corrections: existing issue-source saves and private-workspace
promotion explicitly rebind the unresolved single source with an audit entry.
Previous memberships/locators remain detached for frozen history; current location
and default IDs and all custody records stay intact. Verified and multi-source
state cannot be overwritten by those legacy routes. `/locations` exposes only the
allow-listed `import_mode` and `managed_checkout` provenance fields; internal approval,
backup and account-binding metadata remain stored without widening the read API.


## P4.3 read-only view slice (2 November 2026)

The opt-in new shell adds Sources and Working locations sections for personal
projects and a Sources and locations view for group projects. Read-only APIs
under `/api/source-views` preserve current personal/group authority, current
Placement-person filtering and metadata audiences before counts or filters.
Repository identity remains unresolved for legacy locators. No connection,
membership/default writer or launch-target change is included.

Personal directory data uses bounded server-side project search/pagination and
same-membership/location-edge filters; facets describe only the visible page.
Individual projections return at most 100 source/location rows and explicitly
report truncation with full visible counts. Directory UI/filter components are mounted on `shell-project-pages-p4`, with
server-side scope/search/filter/pagination and Sources/Working locations directly
in Overview. A project-menu hook is supplied for Astra’s separate sidebar polish;
this slice adds no sidebar writer or launch capability. Existing
single-source consumers and compatibility endpoints are unchanged.

P4.2 connection qualification, metadata-sharing approval, multirepository
membership writes/preparation and P4.4 target-bound execution remain gated.
See the P4.3 handoff for integration contracts and qualification evidence.

P4.3 review follow-up: remote check-in timestamps remain installation-owner-only
on every projection route, including directory and group views. For group
nonowners, verified repository names, source owners and hosting instances are
redacted with locators; opaque membership identities do not grant metadata access.
After a personal-project transfer, an owner-only audience-mismatch notice can
explain hidden recorded sources/locations without retargeting or rewriting them.
Neither this notice nor reading a projection grants a new audience or launch right.
