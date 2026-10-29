# Group projects and workspaces

**Status: approved by the owner, 2026-10-22, with the answers in decision 0008 (private decision record). Phases 1–3 are implemented for same-host groups (phase 3 in #229, after its independent security review; fixes in #236, #244 and #245). Phase 4 has an off-by-default own-machine first slice with owner-approved account-control proof (#325); native rollout qualification remains pending.**

2026-10-22. **Approved 2026-10-22 (see status above); authority phase 1 and private exchange phase 2 implemented.** Covers the approved organizations proposal, including people collaborating from their own machines with their own agents. Approval authorizes a phased implementation, not a claim that groups, cross-machine projects or collision prevention work today.

## Recommendation

Use **private clones per person and machine, with a separate worktree per session**. Exchange explicitly published commits through a service-owned Git repository on the main ACC. Keep the append-preserving git backup separate from the collaboration endpoint. ACC maintains a group work board containing safe task summaries, branches and advisory file claims, never other people's transcripts, terminals or credentials.

Do not share writable Git metadata or use a Unix group to share people's working directories. This costs storage and a new authenticated fetch/publish protocol, but preserves the per-person account boundary and works across machines. A work board reduces collisions; it cannot guarantee agents never overlap.

## Existing boundaries and integration points

Source links include line anchors at design time; descriptions here distinguish existing behavior from proposals below.

| Today | Evidence / change required |
|---|---|
| Instance roles are owner and member; members need their own agent account. Owners are fully trusted. | [User model](../src/acc/server/models.py#L201), [account checks](../src/acc/server/api/users.py#L26), [people model](wiki/User-Guide/People-and-Sign-in.md), [Guide](wiki/User-Guide/People-and-Sign-in.md). Group roles must not become instance privileges. |
| A project binds one owner, account, path and machine. Authorization mostly compares `owner`; launch uses the project's account and machine. | [Project model](../src/acc/server/models.py#L34), [Project API](../src/acc/server/api/projects.py#L25), [scope helpers](../src/acc/server/api/context.py#L138), [launch](../src/acc/server/api/sessions.py#L166). Widening `scope` alone would run collaborators as the wrong person. |
| Brokers run as individual accounts behind protected sockets; the account helper creates separate workspace roots. Existing ACL sharing is for an agent and the original checkout/services owner. | [accounts helper](../deploy/accounts/acc-accounts#L1), [broker unit](../deploy/broker/acc-broker@.service#L1), [socket](../deploy/broker/acc-broker@.socket#L1), [ACL helper](../deploy/broker/acc-agent-share#L1). Do not extend that ACL pattern to groups. |
| Session worktrees isolate edits, but share their source repository's Git metadata. Research/Council sessions can share one folder; Development has an individual Project record and local-only policy. | [worktree operations](../src/acc/broker/ops.py#L221), [private workspaces](PRIVATE-WORKSPACES.md), [workspace Guide](wiki/User-Guide/Workspaces.md). |
| Backups are project-scoped, receive-only, append-preserving and broker-triggered; fetching is disabled. They copy committed refs, not dirty work. | [gitbackup](../src/acc/server/gitbackup.py#L1), [scheduler](../src/acc/server/backups.py#L1), [transport and limits](MULTIHOST.md#git-backup-m15-2026-10-22). Not an existing collaboration remote. |
| Vault sharing is private or instance-shared, plus project grants and an explicit other-machines switch. Plan budgets currently apply only to owners, using the default account's readings. | [vault](VAULT.md), [key model](../src/acc/server/models.py#L384), [grant model](../src/acc/server/models.py#L413), [budget eligibility](../src/acc/server/budgets.py#L78). Do not sum unrelated people's subscription percentages. |
| Inbox items have a personal owner; owner Inbox additionally gets host-wide items. Audit is owner-only. Hooks provide activity and spool-based requests. | [Inbox](../src/acc/server/api/inbox.py#L1), [Inbox scoping](../src/acc/server/api/context.py#L142), [audit route](../src/acc/server/api/projects.py#L127), [ingest](../src/acc/server/ingest.py#L215), [acc-request](../bin/acc-request#L1). |
| A project lives on one machine; nodes connect to one main. Orchestrator helpers belong to the lead's person/account. Organizations will have sites, not just more execution nodes. | [multi-host](MULTIHOST.md), [Orchestrator design](ORCHESTRATOR.md), [approved HA direction](research/high-availability.md#owner-decisions-2026-10-22). |

## Model and authority

- **Group:** stable UUID, name, creator, optional future organization ID, home site, status and authorization revision. Initially only instance owners create/archive groups and appoint maintainers. A maintainer manages existing ACC users within that group; they cannot invite people into ACC or create agent accounts. Keep at least one maintainer. Later org admins may create groups and delegate creation to team admins within their organization.
- **Membership:** unique `(group_id, user_id)`, role `maintainer | member | viewer`, provenance `local | identity-provider`, optional external group ID. Maintainers manage membership/resources; members develop; viewers read shared code and coordination metadata but cannot launch. Instance owner/member remains a separate axis: an instance member can maintain a group without gaining host, people, publishing or global audit powers.
- **Group project:** one owning group, not several groups with ambiguous policy unions. Add `group_id` alongside legacy personal ownership, with exactly one authority: personal owner OR group. Preserve creator/steward as attribution, not an authorization bypass. Sessions always retain their launching person, `run_as` and immutable placement; group ownership never changes whose provider login runs them.
- **Placement:** unique project/person/machine/account binding to a private clone path, trust state and sync status. An owner approves machine/account bindings; nobody chooses another person's account or arbitrary path. A session references its placement directly, rather than resolving its machine/path from mutable `Project.host/account`. One person can have placements on several approved machines; local account names are not identity. A separate deployment placement hosts owner-approved services; development clones do not move live sites.
- **Group workspace:** the Development project-record pattern with a group owner and no external repository. Use the same private clones/worktrees and ACC-only exchange. The current local-only rule must gain a narrowly scoped broker exception for internal exchange, not a general permission to add remotes or push. Promotion to an external project is explicit, confirmed and audited; instance owners initially approve external creation. Existing sessions retain frozen stricter policies. Research/Council folders must be converted/copied into this model, never shared writable across accounts in place.
- **Group projections:** sessions list, Changes and Inbox aggregate authorized group resources. Share session ID/name, person, machine alias, status, branch, safe task, claims and published changes. Raw prompts, activity commands, error bodies, transcripts, exports, search results, artifacts, run folders, terminal streams and unpublished diffs remain personal. A session's group-safe task is explicitly entered/confirmed, not copied from its opening prompt.

Implement separate capabilities for metadata, published code, private session content, launch, own-session control, group administration and host administration. Enforce them on every API, WebSocket, Git request, background job, search/export, wiki/files/preview view and notification, not just list queries. Group Files/Wiki read a selected published commit; editing opens the caller's own placement. Never proxy another person's live checkout through its broker just because the project is visible. Inaccessible objects remain 404.

**SSO/SCIM later:** map immutable `(issuer, subject)` to ACC users and namespaced external group IDs to memberships. Group membership never grants instance owner automatically. Default new mappings to member/viewer; explicit admin mapping grants maintainer. SCIM-managed memberships are not locally editable except an audited break-glass path; removal triggers the same revocation protocol. Organization policy is an upper bound users/groups cannot loosen. Initial groups need neither PostgreSQL nor SSO; use portable transactional constraints compatible with the later enterprise database.

## Shared files: options and chosen protocol

| Option | Sharing and operational cost | Security consequences |
|---|---|---|
| One filesystem bare repository, linked worktrees across people's accounts | Minimal object duplication; same-host only. Worktrees require writes to common refs, config and worktree administration. | Private worktree directories can hide dirty bytes, but shared Git metadata is not an account boundary: one account can tamper with hooks/config, refs or worktree pointers affecting another. Common index/reflog/admin data may disclose unpublished work. Read-only bare access alone cannot support ordinary linked worktree writes. Reject. |
| Shared checkout/repository via Unix group, setgid and default ACLs | Familiar Linux collaboration; defaults must survive masks, renames and generated files. Does not solve multi-machine use. | Members can read/write each other's uncommitted files in the shared tree. Writable `.git`, agent settings and dependency scripts become cross-account execution paths; swapped symlinks race privileged file/backup operations. Revoking group membership does not revoke existing processes' supplementary groups or copied data. `acc-agent-share` is not sufficient isolation. Reject. |
| **Private clones, exchanging commits through main ACC Git infrastructure** | Duplicate objects/dependencies; explicit sync and offline state. Each account's worktrees share only its own `.git`. Same protocol on one host or several. | No direct cross-account dirty-file or metadata access. ACC mediates committed code exchange; malicious shared source still executes with the recipient's authority when trusted/run. Requires transport isolation, authorization and safe file access below. **Choose this.** |

### Repository and sync contract

1. The group project has a service-owned **exchange** bare repository; agents have no filesystem access to it. Use separate storage and credentials from `/srv/acc/git` backups. ACC creates a private clone inside each approved account root (0700, no inherited cross-person ACLs), with no cross-person alternates, shared object directories or local hardlinks. Session worktrees are created only from that person's clone; mandate them for group sessions. Check ownership/modes/ACLs explicitly: today's [`safe.directory=*` setup](../deploy/broker/acc-agent-add#L96) is not an ownership boundary.
2. Published branches use `refs/heads/people/<stable-user-id>/<session-id>`; integration branches/tags are maintainer-controlled. Writers may fast-forward their own published branches only. No force update/deletion initially; after a rebase publish a new versioned branch. Maintainers cannot rewrite someone else's branch. Promotion to integration uses expected-old-SHA comparison and a reviewed merge; no automatic merge to main. A GitHub-backed project's external protection and each person's own GitHub identity remain independent.
3. **Publish** sends selected committed session refs; **Sync** fetches group-published refs into remote-tracking refs. Neither auto-commits dirty work, checks out another branch, resets a worktree, runs project code, nor auto-merges. The default base is the selected published integration SHA, recorded at launch; refresh requires explicit sync/rebase/merge. Changes lists published diffs plus board path/count metadata for ongoing work; sharing an unfinished diff requires an explicit reviewed snapshot publication. Committing privately is not publication.
4. Add authenticated upload-pack and narrowly authorized receive-pack **only to the new exchange endpoint**. Reuse backup identity checks, TLS trust, resource limits and broker plumbing, not its receive-only authorization. Tokens are short-lived and bound to group/project/person/placement, read or write action, allowed destination refs and authorization revision. Check current membership per request and again before atomic ref updates, serialized with revocation under a project authorization fence; HTTP authentication alone cannot authorize a long-running push after removal. Viewers get read only. Namespace checks limit writes, not confidentiality: every Git object admitted to an exchange repository must be considered readable by the entire group.
5. Trusted brokers transfer from a sanitized temporary Git directory with copied/validated objects and selected ref OIDs, not by running credentialed Git inside agent-writable `.git/config`. Disable hooks, global/system includes, credential helpers, URL rewrites, external protocols, redirects, filters and submodule recursion; pin the ACC origin and approved TLS trust. Import fetched objects/refs without checkout. Protect against malicious packs with fsck, size/time quotas and patched Git. Existing `backup_env` still permits local config URL rewrites ([implementation](../src/acc/broker/ops.py#L1139)); merely adding fetch to it is not sufficient.
6. Broker file/snapshot operations pin directory descriptors, reject symlinks at every component, constrain roots, and accept regular files only, following the [Files API pattern](../src/acc/broker/files.py#L66). Never recursively chown/chmod an agent-controlled tree as root. Published symlinks are displayed as links, not dereferenced by Files/Preview/export. Git hooks/config are never copied between clones; tracked agent instructions/build scripts remain untrusted source requiring the recipient's trust decision.
7. Audit publication/sync with actor, placement, refs and OIDs; show errors per placement. Idempotent request IDs and expected-old-SHA checks handle lost replies without duplicate work or last-writer-wins ref updates. Keep exchange publication enabled even if archival backups are disabled: live collaboration is not backup.

**Across people's machines:** enroll each box under the same main ACC, approve a per-person account binding, and create its private placement. Provider OAuth logins stay on that box/account. All coordination and commit exchange goes through the main; no SSH access between members or shared NFS checkout. The UI chooses a permitted machine at launch and reports each placement's last sync and published SHA. WSL2 follows the Linux model; macOS currently uses the signed-in user's broker, so a separate OS user is needed for a stronger local boundary. A machine administrator can read every secret and file on that machine; do not place other people's agents there without their trust.

If disconnected, existing agents may keep editing their own branches. Show board/claims/sync as **stale**, not empty or safe. No new ACC-authorized launches, key delivery, claims or publication while the main cannot authorize them. On reconnect, reconcile membership first, then exchange commits; do not replay revoked operations. No live-session migration is implied.

**Several sites later:** use organization/site UUIDs, keep one home-site writer for each project exchange and board, and replicate only authorized published refs and safe metadata. Per-site autonomy from the HA design does not mean concurrent authority over the same claims or Git refs. Disconnected sites can work on namespaced local branches with visibly stale coordination; they cannot obtain global exclusivity or fresh home-site grants. Federation, residency, cross-site key consent and ownership transfer need a separate protocol; do not bidirectionally rsync repositories or merge databases.

## Agent awareness: ACC work board

**Row:** group/project/session/person IDs, machine alias, actual branch and base/published SHA, group-safe task (max 500 characters), claimed relative files/directory prefixes, observed touched paths, state, heartbeat, source and board revision. Store it in ACC, not a writable `TEAM.md` in the repository. Treat tasks and paths as untrusted data, never executable instructions. No raw command lines, prompts, transcript excerpts, secret filenames/values or private absolute paths; filter protected paths and redact before persistence/distribution. Redaction is a safety net, not a guarantee of safe arbitrary text.

- At launch/resume every group session gets a bounded board snapshot in its compiled instructions, its own identity/branch, freshness timestamp, and rules: check claims, work in your own worktree, publish before handoff, do not read teammates' homes. Refresh a broker-delivered board context file after changes and at turn boundaries; hooks/tool responses include a revision and overlap warnings. Since launch specs are immutable today, the refresh file is a separate data attachment, not a rewrite of frozen policies. Providers without turn hooks get explicit refresh instructions and a visible degraded-awareness indicator.
- ACC refreshes branch/status from broker Git observations. Hooks report normalized tool target paths and task/claim events only for their originating session. Periodic status/diff scans catch writes made by scripts or tools without hooks; scans cannot detect intent before a write and omit ignored/protected files. Heartbeats come from machine/session liveness, not agent assertions.
- Proposed commands, **not available today**: `acc-request work list`, `work claim --path src/api/ --task "Update validation"`, `work release --path src/api/`, and `work task --text "Review validation"`. Use the existing spool/request pattern with session-scoped replies; no dashboard token. ACC derives group/person/project from the session, validates relative paths and role, applies quotas/deduplication, and reports accepted/conflict/stale/refused explicitly.
- Claims are **advisory leases**, not filesystem locks. Default 15-minute TTL, renewed by live sessions; overlap means identical files or directory-prefix intersection, normalized per repository with conservative case handling across platforms. An overlapping claim returns the current holders and a warning; caller must explicitly confirm shared work to record another claim. Idle/finished sessions release claims; lost heartbeats expire them with an offline/stale marker. A removed member's claims end immediately.
- On overlapping claims or observed touches, deduplicate by session pair/path and notify both people; group Inbox gets a safe conflict item, escalation goes to maintainers if unacknowledged for 15 minutes. Instructions tell agents to narrow scope, coordinate through the board or choose another task. No automatic kill, overwrite or merge; a person resolves conflicting edits in their own clone. Renames, dynamic file generation, missed hooks and semantic conflicts remain possible.

**Orchestrator:** its helpers use the same board and claim protocol; subtree tasks/branches are labelled under the lead, with one group charge per actual session. The lead can allocate/release its helpers' claims, never another person's. Helpers still run as the lead's person/account under that person's confirmed agent, machine, depth, time and budget limits. Existing `read`/`message`/`merge` powers stay within that same-person helper tree; group membership never allows reading another person's transcript, typing into their terminal or starting helpers on their subscription. Cross-person collaboration uses published commits and board metadata. Design reference: [ORCHESTRATOR](ORCHESTRATOR.md).

## Keys, budgets, backups, Inbox and audit

| Resource | Proposed group behavior |
|---|---|
| **Keys** | Retain private and existing instance-shared keys; add explicit key-to-group use grants chosen only by the key owner, optionally limited to projects and approved machines. Group maintainers may attach eligible group keys to group projects, not widen sharing, rotate someone else's value or enumerate their private keys. Injection requires current launch identity/membership, project attachment, group grant, machine allowlist and the existing remote opt-in; conflicts on variable names still refuse launch. Each person's private project keys are injected only into their own sessions. |
| **Secret boundary** | Reuse write-only encrypted vault storage, 0600 launch files, stop cleanup and retired-value redaction from [VAULT](VAULT.md). No values in board, audit, exports, Git or group backup. A grant allows recipient sessions to use/read that credential; it cannot prevent a malicious recipient exfiltrating it. Collaborators must also trust shared code before running it with private keys. Provider login files are never group-shared; an optional group API key is not a shared CLI subscription. |
| **Budgets** | Add group policy and per-session usage ledger keyed by group/project/person/provider/machine. Record measured credits/cost in native units and reporting coverage; unknown usage is unknown, not zero. Do not pool subscription percentages or expose personal usage outside the group. Start with reliable concurrent-session/launch quotas and measured-usage notifications; money/credit launch gates require a supported accounting source. Maintainers set group thresholds within owner/org ceilings; only owners/org admins can raise ceilings. |
| **Enforcement** | A launch/resume/helper must satisfy personal rules AND group/org policy. Reserve group concurrency/launch capacity transactionally before dispatch, using idempotent launch IDs; reconcile failed/uncertain launches before releasing slots. Group pause cannot be bypassed by today's personal `budget_ok`; only an audited authorized group override can permit it. No automatic stopping of running sessions for spend thresholds. Delayed usage can overshoot: not a hard provider spending cap. Unsupported accounting is shown explicitly and cannot enforce a configured monetary gate silently. |
| **Backups** | Group policy covers **published** refs and group metadata, retained off-host using the enterprise backup direction. Preserve append-only recovery history separately from mutable exchange refs; maintainers trigger backups/restore to a new branch, viewers read published backup status, and downloads require member or maintainer code access. Each placement reports last publication and last successful backup, not one misleading green project timestamp. |
| **Private recovery** | Never put all placement refs into a group-downloadable bundle: they may contain unpublished commits. Personal recovery backups, if enabled, stay separately authorized to that person and instance owners, with no group browse/download. Current backups do not capture dirty trees, ignored files, native transcripts or provider logins. Any future dirty-file backup requires explicit personal opt-in and separate encrypted storage. Enabling group backup does not grant access to a home directory or personal recovery store. |
| **Inbox** | Personal needs-input/permission/auth/recovery details go only to the session's person (instance owners retain trusted administrative access). Group view gets sanitized status, not the prompt or provider login error. File conflicts go to affected people plus group-safe conflict items; backup/sync failures and group budget thresholds go to maintainers, with affected people notified. Host/publish/system-tool/compose approvals remain instance-owner-only; maintainers can request, never approve them by group role. |
| **Assignment** | Group items have group/project, kind, audience, assignee, revision and resolution state; per-person read/ack is separate from shared resolution. Maintainers assign/resolve operational items; affected members acknowledge their conflicts. One person's acknowledgment never hides another's permission prompt. Push recipients and click-through access are checked against current membership, not a cached audience. Viewers see safe status but receive no action notifications by default. |
| **Audit** | Add explicit group/project IDs, actor and subject IDs, machine, action and request ID to events. Maintainers read an allowlisted **group-only** projection: membership, publication, claims/overrides, budget changes, grants/use by key ID, backup/restore and group operational decisions. No generic unfiltered `detail` JSON, private key names, prompts or unrelated personal activity. Instance owners retain global audit; members see their own receipts, viewers no group audit. Later stream scoped events to the organization's SIEM with site provenance. |

**Revocation:** membership/role changes increment the authorization revision, revoke exchange/key capabilities, close group subscriptions, cancel queued operations and refuse new launches/resumes immediately. Member-to-viewer or removal stops that person's group sessions and removes delivered secrets; do not affect their unrelated personal sessions. Keep historical attribution and group-published commits; do not transfer a group project to whoever removes a user. Offline machines cannot guarantee immediate stopping or erasure: show cleanup pending, block future access, notify maintainers/owners and require reconnect reconciliation. For leaked/shared keys, the key owner must rotate/revoke at the provider. Already copied code/keys cannot be recalled.

**Owner answer, 2026-10-24:** leaving or being removed also deletes all of that
person's group key consents in the membership transaction, with one
`group.key_revoke` receipt per consent. Archival deletes every consent for that
group in the archive transaction. Sessions using or potentially receiving those
keys stop through the explicit-revocation cleanup path, including other members'
sessions. Other groups and personal sessions are unaffected. Rejoining requires a
fresh explicit grant; old consent is never restored. A viewer downgrade still
stops the person's execution but is not departure and does not delete consent.
See decision 0020 (private decision record).

## Permissions

All group rights below require current membership in that group; no access to another group follows. **Self** means one's own session/placement; **shared** means only the projections/publications above. Instance owners retain today's fully trusted administrator powers, including access to people's terminals; this is not tenant isolation from the operator.

| Action | Instance owner | Group maintainer | Group member | Group viewer | Nonmember (not instance owner) |
|---|---|---|---|---|---|
| Create/archive group; appoint initial maintainer | Yes | No (later delegated team admin) | No | No | No |
| Add existing users; change group roles | Yes | Yes, retain a maintainer | No | No | No |
| Create/archive group project/workspace; set group policy | Yes | Yes, within org ceilings | No | No | No |
| Read board, session summaries, published code/diffs/wiki | Yes | Shared | Shared | Shared | No |
| Launch; edit own placement; claim; publish own branch | Yes, as self | Self | Self | No | No |
| Read dirty files, transcripts, artifacts, terminal; prompt/stop/resume | All (trusted) | Self only | Self only | No | No |
| Integrate published branches; resolve group operational items | Yes | Yes | Own conflict acknowledgment only | No | No |
| Approve host enrollment/account binding, publish, compose/system tools | Yes | No | No | No | No |
| Grant key to group; rotate/value/sharing changes | Key owner; existing admin deletion retained | Only if key owner | Only if key owner | Only if key owner | Key owner may grant; gains no project access |
| Attach eligible group key to project; change group backup policy | Yes, within key consent | Yes, within key consent | No | No | No |
| Download published group backup; request restore to new branch | Yes | Yes | Download only | No (status/code remain readable) | No |
| Group audit / personal receipts / global audit | All | Group + own receipts | Own receipts | No | Own unrelated receipts only |

For future organization admins, management capabilities are explicitly delegated and scoped; neither org nor team admin implicitly receives another person's transcripts or terminal. Today's instance owner exception is not copied into group roles.

## Threat model and limits

Protect people from another member's untrusted agent, a hostile viewer, a compromised execution node and accidental parallel editing. Trust accd, its database/vault, the broker deployment and instance/root administrators. Same-account sessions are **not** isolated from one another today; [container isolation](CONTAINERS.md) is a separate proposal.

| Threat | Required boundary / residual risk |
|---|---|
| Cross-person dirty-file or secret reads | Private roots/clones/run folders; no group ACLs; capability-filtered broker/UI access. Existing world-readable or inherited ACL paths must fail placement checks until repaired by an owner. |
| Hook/config injection and hostile shared source | No shared `.git`, config, hooks or credentialed Git in writable config; trusted transfer staging. Published build scripts/agent instructions can still steal keys when run: show changed trust inputs and require recipient approval before running new source; board warnings cannot sandbox code. |
| Symlink/path races through privileged helpers | Descriptor-relative no-follow access, no cross-person copy/chown, safe archive extraction and no automatic submodule checkout. Test swaps during read/export/restore, not merely initial path validation. |
| Forged board/hook data and prompt injection | Bind requests to session/placement; allowlisted bounded fields; sequence/dedupe/revision checks. A compromised node can lie about its own sessions/paths/usage; signals are advisory and labelled, not proof of exclusivity or exact spend. |
| Unauthorized Git reads/writes or resource exhaustion | Check current role per operation, validate ref ownership server-side, separate whole-project object stores, quotas and pack limits. Hidden refs are not a confidentiality control. |
| Revoked/offline node, copied data or poisoned backup | Short-lived grants, explicit stale/cleanup state, immutable recovery history, restore into new branches and operator review. Cannot retract published history/copies or guarantee immediate offline secret deletion. |

No group member's agent receives another person's transcripts or private keys through ACC. This does not promise secrecy against root, the trusted instance owner, a same-account process, a machine administrator or deliberately malicious shared code run with the victim's credentials.

## Migration

1. Add groups, membership, placements and capability checks behind an owner-enabled feature flag. Existing personal projects/workspaces remain personal; no default group, retroactive sharing, automatic key grant or budget change. Backfill a legacy placement for existing session/project routing, preserving machine/account/path, backup identity and frozen launch specs.
2. An owner explicitly selects **Move to group** after stopping active sessions. Preview members, reachable machines, published ref selection, keys, budgets, backups and service placement. Require a clean committed source and validate private destination roots. Import only reviewed committed refs into a new exchange; create separate participant clones. Refuse conversion if inherited ACLs defeat isolation; do not repair the host silently.
3. Keep legacy sessions, transcripts, dirty/unpublished material and recovery backups personal and read-only through their existing rights; the group sees only newly created group sessions/publications. Re-authorize group key grants with each key owner; never convert a private key to group-shared because its project moved. Group workspace conversion preserves the no-external-push policy; public promotion remains separate.
4. Validate all placements and a backup/restore round trip before switching authority atomically; retry provisioning by operation ID. On failure leave the personal project authoritative and report the incomplete resources. Rollback before new group work restores the legacy pointer; afterwards export/preserve new refs and require an explicit migration back, never discard collaborators' work. Last-member removal archives group resources only through an explicit owner action.

## UI sketch (proposed, not shipped)

Follow [DESIGN](design/DESIGN.md): existing `Page`, `Tabs`, `SessionList`, `QueryState`, `StaleNote`, badges and confirmation dialogs; no new visual system.

```text
Scope: Personal | All my groups | Demo team
Demo team                    3 active   1 conflict   Board updated 12s ago
Projects | Workspaces | Sessions | Changes | Inbox | People | Settings
demo-api   Work board | Published changes | Files | Keys | Backup
Person         Session/task          Machine       Branch       Claims
Alex Example   API validation        dev-box-a     people/...   src/api/
Sam Example    Validation tests      dev-box-b     people/...   tests/
Warning: 2 sessions touched src/api/routes.py     [Review overlap]
```

**Settings -> Groups** (owners): create/name group, appoint maintainer. Group **People** shows role/provenance; maintainers add existing users. New project/workspace chooses **Personal** or a maintained group. New session requires a group-safe task and permitted machine, previews key names allowed to that launcher, and shows claims/freshness before launch. Group Changes defaults to published branches; ongoing rows explicitly say **Unpublished; path summary only**. Group Inbox separates **Assigned to me** from **Group operations**; private prompts link only for their person.

Dangerous actions name the consequence: granting key use, moving personal code into a group, removing a member and promoting externally. Hide unavailable controls but enforce on the server. Keyboard-labelled scope/role controls, focus-managed dialogs, text plus icon for conflicts, and reduced-motion status follow existing primitives. Phone uses stacked board rows and explicit person/machine labels. Loading, failed, offline/stale and genuinely empty are distinct; never imply a stale board proves no conflict. No screenshots until these screens exist; the [Guide proposal](wiki/User-Guide/Groups.md) is explicitly future-facing.

## Phases, effort and approval questions

Rough engineering estimates for one experienced developer, including focused tests/docs/review; not measured delivery dates. Enterprise HA, SSO/SCIM and federation are additional work.

| Phase | Deliverable / exit gate | Effort |
|---|---|---|
| 1. Authority and migration | **Implemented.** Groups/roles/placements, default-deny projections, owner opt-in. Test the complete permission matrix, legacy behavior, removals, concurrent membership changes, exports/search/WebSockets and failed migration rollback. | 2-3 weeks |
| 2. Private Git exchange | **Implemented, same-host only; launches still gated.** Same-host clones/worktrees, scoped publish/fetch, private vs group backups, safe Files/Wiki, controlled integration. Test ref races, replay, unauthorized object/ref access, malicious config/hooks/packs/symlinks, dirty-file non-disclosure and restore round trips. | 3-4 weeks |
| 3. Coordinated group UX | **Implemented, same-host (#229); independently security-reviewed, findings fixed.** Board/instructions/hooks/claims, grouped Sessions/Changes/Inbox, scoped audit, key grants and transactional launch quotas; Orchestrator adapter only where implemented. Test missed hooks, overlapping/expired claims, prompt/key canaries, duplicate notifications and concurrent/helper launches; browser/keyboard/phone flows. | 2-3 weeks |
| 4. Own-machine collaboration | **First slice and local account-control proof implemented behind `ACC_GROUPS_REMOTE`; native qualification and cross-person remote key consent deferred.** Explicit machine/account bindings, sync/reconnect, remote key consent, pending revocation cleanup; qualify Linux/WSL2/macOS. Two-person/two-machine partition, revoked-node reconnect, wrong-account refusal and private-key boundary tests. | 2-3 weeks |
| Later enterprise | SSO/SCIM mappings, organization ceilings, measured cost attribution, SIEM, machine pools and site federation with fencing/residency decisions. | Separate design/estimate |

Initial usable same-host release requires phases 1-3; own-box acceptance requires phase 4 (roughly **9-13 developer-weeks total**, potentially overlapping after stable contracts). No container, HA or federation guarantee is bundled into that estimate.

Owner decisions before building:

1. Approve private clones and explicit commit publication, rather than shared dirty files? Accept storage duplication and that the board cannot eliminate collisions?
2. Approve maintainer/member/viewer powers above, including adding existing users, stopping only one's own sessions and instance-owner-only host approvals? Is a dedicated read-only auditor needed initially?
3. Approve private transcripts/artifacts/unpublished diffs by default, with group-safe tasks and path summaries visible to all members/viewers? Should any path summaries require a tighter audience?
4. Approve explicit key-owner group/machine grants and stopping group sessions on removal/downgrade, acknowledging offline cleanup cannot be immediate? Must group credentials come from an external secret manager first?
5. Approve concurrency/launch quotas plus measured-usage warnings first, rather than pretending personal subscription percentages are a group spending cap? Who may override a group pause?
6. Is own-machine support a first-release requirement or may same-host ship first? Which machine administrators are trusted, and must session containers be a prerequisite?
7. What group backup retention, off-site destination and residency are required? Approve home-site ownership with stale offline coordination until a separate federation protocol is designed?

**Verification boundary:** this document is a source-grounded design, not an isolation or collaboration test result. No code, schema, machine permissions, live keys, services or runtime behavior are changed here.

## Phase 1 implementation contract (2026-10-22)

`/api/groups` is the explicit owner opt-in: no default group or retroactive sharing.
Migration 0038 (renumber at merge if necessary) adds group authority, role/provenance
constraints, exclusive group versus personal project authority, named project owners,
exact merge receipts, explicit key-owner consent and immutable session placements.
Legacy nullable owner rows remain personal with their existing trusted-owner visibility.
SQLite migrations explicitly transact DDL and backfill; failure rolls back both.

Group resources use `/api/groups/{id}` projections. The existing project/session,
Files, Wiki, Preview, search, exports and personal Inbox surfaces do not gain group
access. Group project paths/accounts are empty placeholders, excluded even from the
trusted owner's legacy project routes. All group launches, including internal
supervisor entry points, are refused until private Git exchange is qualified.
There are consequently no executable group sessions, delivered group keys, board
paths, claims, shared notifications or live group subscriptions to revoke in phase 1.
Do not open that gate in phase 2 without the stop/cleanup and reconnect protocol.

Membership and policy writes compare an expected `revision` and advance it in the
same transaction. Keep a maintainer and a named maintainer for every project; ACC
user removal uses the same invariants, removes memberships and advances revisions,
without transferring group projects. Maintainers manage existing users; only instance
owners appoint maintainers or archive groups. The group audit projection contains
only action, actor, time and receipt ID, never generic detail JSON.

Project creation requires named maintainer IDs. `POST .../projects/{project}/owners`
replaces that set atomically. `POST .../approvals` requires a currently named
maintainer (including instance owners), exact branch/old/new SHA and expected group
revision. The receipt is not a Git operation; exchange must recheck current named
ownership and receipt revision under the same authorization fence as an expected-old
ref update. Initial integration branch names are main, master, develop and release/*;
a configurable branch policy belongs with the exchange. Never treat a receipt as a
bearer capability or an automatic merge.

Placement approval reserves a derived private same-host path, never creates it or
marks it trusted. Only a person and the trusted instance owner can read that person's
placement. Phase 2 must validate modes/ACLs and provision standalone clones before use.
Reserved paths include a digest of the never-reused ACC username: even trusted
owners sharing an account get different clones, and recycled numeric user IDs do
not inherit a retired person's destination. Legacy placement snapshots preserve
project/session routing, including helper host
and worktree overrides; newly launched personal sessions also snapshot their binding.

`POST .../key-grants` records consent only when the caller owns the key, even if the
caller is an instance owner. Optional project/local-host limits are stored.
The phase 3 lifecycle below supersedes phase 1's retained consent after departure
or archive. No injection or attachment consumes this model in phase 1.
Launch quotas and usage warnings remain phase 3; `/unpause` records the approved
maintainer/instance-owner override authority now. No monetary cap is claimed.


## Phase 2 implementation contract (2026-10-23)

Phase 2 adds same-host explicit publication, without enabling execution or converting
existing personal projects. [Decision 0036](decisions/0036-group-pack-exchange.md)
records the bounded pack protocol and standalone session worktree choice.

- Owners provision an already approved placement through its account's existing
  peer-checked broker. ACC creates private 0700 directories and independent Git
  metadata; foreign ownership, extended ACLs and symlink components fail closed.
  A placement's `sessions/<id>` folders are standalone worktrees, with no shared
  Git administration. These checkout IDs become live session bindings only in phase 3.
- `/api/groups/{group}/projects/{project}/exchange` supplies grants, transfer,
  refs, publish, checkout, integrate, tags, backup, backups and restore operations.
  Source config/hooks/remotes/alternates are never copied or used for export.
  High-level publish/checkout uses the existing authenticated broker connection;
  no Git remote credential is delivered to an agent. External API clients use
  the existing HTTPS/device/TLS boundary; no new receiver or CA is installed.
- Low-level grants last 60 seconds and are hashed in memory. Pack-helper requests
  consume them once; Git HTTP advertisements and intermediate read negotiation
  can repeat, writes consume at POST and reads before their first pack is returned.
  They bind current authorization revision, group, stable project
  identity, person, placement (optional for published-only readers), operation,
  commit and expected old ref. A restart forgets them. Each person holds at most 64,
  and each grant serves one Git HTTP request at a time. A supplied read placement
  must also belong to the caller. Viewers fetch published commits without placement
  paths and cannot publish. Removed people and stale grants cannot fetch or push.
- Published branches are derived as `refs/heads/people/<stable-user-id>/<checkout-id>`;
  the stable component hashes the never-reused ACC username, not its recyclable
  numeric ID. A writer can only fast-forward their own branch. There is no force,
  delete, arbitrary ref namespace or arbitrary-object fetch. Standard Git HTTP
  upload-pack/receive-pack lives at `.../exchange/git`, uses protocol v0 and serves
  only per-request trusted staging. Receive-pack must match its exact grant before
  selected-history promotion; upload-pack sees only the selected published closure.
- A fresh quarantine repository validates each pack, then promotes only the selected
  commit's objects that the store does not already reach. Unselected objects in a malicious pack are discarded.
  Imports neither check out code nor run dependency scripts. Git subprocesses have
  time, memory, output, descriptor and file-size bounds. Initial limits: 8 MiB full
  pack (declared expansion at most 128 MiB per object and 512 MiB per pack, scanned
  before Git inflates it), 128 published refs and 128 MiB object store, of which
  people's branches (32 per person) use at most 112 refs and 112 MiB, 1 MiB text view, 5,000 files per
  tree, 64 MiB snapshot transfer budget, 128 local snapshots/512 MiB per recovery
  store. Broker checkout also bounds expanded content to 128 MiB and 5,000 total
  tree entries, with bounded path length/depth, before creating any worktree.
  Limits refuse explicitly; snapshots are never silently pruned.
- Integration requires a current phase 1 exact branch/old/new approval by a currently
  named project owner who remains a maintainer. Maintainers apply it under the same
  database membership fence and Git capture lease as an expected-old-ref CAS.
  The proposed result must already be published for review. Success spends the
  receipt through its old-ref transition. Spent records prevent receipt-ID reuse.
  A crash after Git but before DB commit leaves an ambiguous receipt;
  the old-ref mismatch refuses replay. Inspect current refs and review again.
  Maintainers create immutable tags from published commits; nobody replaces them.
- **Groups → Projects → Published Files & Wiki** reads selected commits reachable
  from published refs.
  It presents regular UTF-8 blobs as text, including Wiki Markdown, without remote
  images, executable HTML, working-tree reads, symlink traversal or submodule checkout.
  Legacy Files/Wiki/Preview and private session routes stay closed for group projects.
- Group recovery snapshots contain published refs and safe group/project/role metadata.
  Maintainers create/restore snapshots, members can download them and viewers see
  status only. Restore creates a fresh `restore/<id>` branch; reaching integration
  still needs named-owner approval. Personal recovery snapshots contain an explicitly
  selected private committed history and are separately authorized to that person
  and trusted instance owners, even after group removal. Group membership never
  grants private recovery access. Neither store captures dirty files or credentials.
- Exchange, group recovery and private recovery live in separate subdirectories
  below `git_dir`, use the existing Git capture lease, and join the backup server's
  encrypted off-site capture, retention and held restore. Inventory validates their
  distinct group/project/person identities. Group snapshot status is local evidence,
  not proof of off-site success or independent protection. There is no new schema
  migration; 0038 remains the current authority migration.

Tests use actual Git packs, private broker checkout/commit, competing requests,
replayed/expired grants, revoked roles, malicious source config/hooks/alternates,
smuggled unrelated objects, corrupt packs, symlinks, unsafe permissions, dirty-file
canaries, private recovery and host capture/restore. Browser checks cover committed
Files/Wiki, accessibility and phone width. Real separate-UID provisioning, production
TLS/device deployment, resource-pressure qualification and remote machines remain
operator/release checks; no service is deployed by this change.

Phase 3 must add the board, explicit group-safe tasks, claims and context refresh,
recipient source-trust confirmation, live immutable session bindings, grouped views
and operational Inbox, key attachment/delivery, scoped notifications, transactional
launch quotas and usage warnings. Before opening launches, qualify removal/downgrade
stop, queue cancellation, key cleanup, stream closure and reconnect reconciliation.
Personal-to-group conversion also remains unavailable until its separate reviewed
preview/provision/backup/rollback workflow exists. Phase 4 adds own-machine execution.


## Phase 3 implementation candidate (2026-10-24)

Same-host group sessions now bind a durable checkout receipt to the same live session ID.
The launcher requires current developer membership, its own approved local placement,
a group-safe task and explicit recipient source trust. Group helpers, schedules,
multi-run, containers and external registration remain refused; personal entry points
cannot bypass the dedicated launch path. Personal-to-group conversion is still separate.

Migration 0044 adds checkout/work/claim/operation/launch records and group quota policy.
Session placements freeze person/account/path/host. Starts and reconnect reconciliation
recheck current authority; membership removal and viewer downgrade persist stop intent,
release claims and retain capacity until stopping is confirmed. The existing cleanup queue
removes delivered secrets and retries broker failures. Affected terminal streams close;
terminal rechecks reject revoked group access. Key delivery requires current consent,
project attachment and local-host eligibility. Group audit includes only allowlisted actions.

Leaving or removal deletes the departing key owner's consents; archive deletes
all group consents. Each deleted consent has its own audit receipt with grant/key
IDs, scope, grantor, actor, reason and the membership/archive revision, never key
names or values. These receipts, consent deletion and durable stop/cleanup intent
commit together. Sessions of remaining members that received or may be receiving
the keys stop as with explicit revocation. Rejoining does not restore grants.
Local members can leave through `POST /api/groups/{id}/members/{own_user_id}`
with `role: "remove"` and the current revision; other role changes still require
administration. Last-maintainer, named-project-owner and identity-provider
membership protections still apply.

The board exposes explicit tasks, liveness age, observed branches, bounded touched paths
and advisory 15-minute claims. Decision 0008 takes precedence over the initial sketch:
viewers see tasks but never ongoing path summaries. Protected paths and known secret
values are filtered before shared persistence; shared redaction never names a private key.
Broker observations use fresh Git metadata and bounded index data, with source config,
fsmonitor, filters and external diff disabled. Observed overlap and confirmed shared
claims generate deduplicated safe notices. Affected people acknowledge independently;
maintainers assign and resolve group operations with revision checks. Unacknowledged
conflicts escalate after 15 minutes, and push recipients are rechecked at dispatch.

Launches and explicit resumes reserve group capacity in the database before dispatch.
Checkout retries return one session and one charge; unknown starts keep their reservation.
Policy starts at 4 concurrent sessions and 50 launches/resumes per UTC day. Maintainers
change thresholds within owner ceilings; owners raise ceilings. An audited override lasts
15 minutes and lifts maintainer limits and the pause, never owner ceilings. Group sessions run
only under an agent account no other ACC person uses; launches and running sessions on a
shared account are refused/stopped. Reported USD totals use per-session statusline observations with unknown,
partial or complete coverage, bounded to 100 recent sessions. Thresholds warn rather than
stop running sessions or pretend to cap provider spending. Personal subscription percentages
are never pooled; personal budget confirmation cannot override a group pause.

A separate broker-written context attachment refreshes during reconciliation without
changing frozen launch policy or secret files. Claude turn hooks attach its bounded local
snapshot; other agents show the explicit-refresh requirement. `acc-request work` derives
identity from its originating session, validates allowlisted fields, records bounded
idempotent receipts and returns replies through that attachment without a dashboard token.

**Groups** now contains Projects/workspaces, a Work board & Sessions view, New group session,
published Files/Wiki/commit comparisons, Group operations, Assigned to me, People and
Usage & Settings, using existing design primitives and stacked phone rows. Provider prompts,
terminal content, artifacts and unfinished diffs remain personal. Published diff endpoints
require two published commits; no private checkout is used for shared reading.

Unit/API coverage includes role matrices and route discovery, overlapping/expired/racing
claims, missed-hook path scans, malicious source config, prompt/key canaries, quota races,
idempotency, resumes, overrides, recipient revocation and cleanup intent. Playwright covers
real fixture broker launches, board/claim/context flows, operations, published comparisons,
keyboard-labelled controls, accessibility and 1440/390-pixel layouts.

Production provider-hook behavior, real separate-UID provisioning and production TLS/key
handling remain operator qualification checks. The independent phase 3 security review
(2026-10-24) assessed this candidate before it merged as #229; its fixes are on main. No own-machine, deployment, package-installation
or machine-configuration change is included.

## Phase 4 first slice: own enrolled machine (2026-10-25 design)

Replay chain on current main: `0054_oidc_followups` → `0063_groups_machine_accounts` → `0064_groups_machine_proof` (Alembic revision IDs `0054` → `0063` → `0064`). There is no dependency on SCIM; SCIM is parked until after v1. ADR 0068 records account-control proof. This replay does not enable the feature or change its reviewed runtime behavior.

This slice is disabled by default (`ACC_GROUPS_REMOTE=1` / `RYNHULT_GROUPS_REMOTE=1`
on the trusted main enables it). Same-host groups and personal remote sessions
retain their existing behavior. Containers are not a prerequisite (decision 0008).

1. A signed-in person requests a binding of their identity to an already enrolled
   node ID and one reported broker account. The member proves local account control
   with a single-use confirmation code (M2, decision 0068); an instance owner then
   reviews the verified request and approves it. Enrollment alone never
   grants group authority. The node/account can belong to only one person; account
   names on different nodes are unrelated. Records bind the immutable node ID,
   not merely its recyclable display name. Revoked records cannot be resurrected.
2. That person requests a private placement in a group project using the approved
   binding. Current developer membership is required. An owner or group maintainer
   approves/provisions it through the authenticated node link. The path is derived
   below a deployment-reported broker root, never caller-supplied. Existing broker
   no-follow/ownership/mode/ACL checks apply. Neither approver gains the person's
   private files, terminals, prompts, provider login or unpublished Git history.
3. The member explicitly checks out published commits, reviews source trust and
   launches an individual Claude/Codex session using the existing group routes.
   The checkout freezes person, node/account and private path; project routing
   cannot redirect it. Quotas, source trust and advisory board rules still apply.
   Sync/publish is explicit, sanitized commit-pack transfer through the main;
   reconnect never automatically publishes, resets dirty files or replays launches.
4. Only the launcher's own vault keys with Other machines consent can cross to
   this account. No other person's private or group-consented key is delivered
   remotely in this slice, including MCP references. Cross-person remote key
   consent UI/protocol is deferred rather than interpreting a legacy unrestricted
   group grant as permission to send a secret to a new machine. Provider OAuth
   files stay on their original account and never pass through the main.
5. Binding revocation, placement revocation, membership removal/viewer downgrade,
   node revocation and group/project archival invalidate execution. Membership
   revocation also invalidates remote placements, so rejoining needs fresh review.
   Persist stop intent and release claims in the authorization transaction; close
   affected streams. A disconnected machine shows cleanup pending and stale board
   state. Existing editing can continue offline; no new main-authorized launch,
   key delivery or commit exchange can proceed there. Reconnect rechecks authority
   before adoption/start, kills revoked sessions and retries secret-file cleanup;
   uncertain stopping retains capacity until confirmed; failed key cleanup remains visibly pending. A revoked enrolled
   node cannot authenticate again; restoring its connectivity requires explicit
   operator action, never automatic re-enrollment or replay of old bindings.

The trusted machine administrator can read that machine's sessions and keys.
Approving grants neither approver anything beyond the approval itself; group
maintainers get no access to the person's sessions. Instance owners keep the
owner-wide session access they have on every enrolled machine (attach to a
terminal, send text, read transcripts), because the main already drives that
machine's brokers; enrolling a machine trusts the main's owners with that.
Native macOS shares the signed-in user's node identity and broker account; it
provides no same-user isolation. Already copied code/secrets cannot be recalled.
Five-minute company execution leases, remote provider-login management, site
federation, session migration, helper/container/scheduled group execution and
cross-person remote key grants are outside this slice.

Acceptance: two people on two simulated machines can independently provision,
checkout and launch only their own account; partition blocks fresh work while
unaffected people continue; binding/membership revocation persists pending cleanup,
reconnect stops rather than restarts revoked work; wrong-person/account/node-ID
substitution fails; private-key canaries never reach another person or machine.
New routes join the discovered authorization inventory. Native Linux/WSL/macOS
and real separate-UID qualification remain release checks.


Phase 4 implementation evidence: authenticated simulated node links and separate
brokers cover two people with identical account names on different machines,
private checkout/launch routing, account-name collision refusal, partitions,
binding/placement/membership/node revocation, revoked-node signed reconnect,
cleanup retries and private-key canaries. Owner account approval and group project
approval are distinct. Pending requests can be cancelled; approved binding and
placement revocations are permanent for this slice. An owner must arrange a fresh
enrollment/account rather than editing a revoked record back to approved. The
new route classifications are in `tests/ownership_inventory.json` and the generated
authorization inventory. Browser controls use the existing group launch panel.

### Phase 4 account-control proof (owner decision M2)

Before owner approval, the requesting member must prove local access to the exact
node/account. A pending request supplies its immutable request and node IDs. On
that machine, as the requested ordinary agent account, the member runs
`rynhult-node group-proof --request REQUEST_ID --node-id NODE_ID --run-dir BROKER_RUN_DIR`.
This local operation creates a random ten-character code, displays it only on
that machine, and stores only its digest in the account's private broker run
directory. It does not call the main, use the enrollment private key, change
socket permissions or add sudo authority. The member enters the code on their
pending request; only that person may verify it. The main checks it over the
authenticated enrolled-node link, selecting the requested account's broker.

Codes bind node ID, account and request ID, expire after five minutes, and are
consumed on success. Each account may issue one code per minute; five failed
checks exhaust it. Issuance, counters and consumption survive broker restarts
and are serialized locally. A successful broker response is fenced again against
current node identity and pending request/person before storing verification.
The owner sees “Verified on the machine” or “Unverified”; unverified requests
cannot be approved. Earlier approvals have no fabricated proof: their existing
project/session authority is fenced until the same person verifies that binding. Verification and refusal are audited without codes/digests.
New generation replaces the account's previous challenge; cancelling or revoking
a request makes its code unusable. No new remote code-generation/read method is
exposed. Owners still review the intended person and machine; proof establishes
account control, not the person's identity or machine trustworthiness.
