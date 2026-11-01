# Security settings

Status: **design for owner approval; no implementation**. Date: 2026-10-30.
Target: rc.3, Friday November 6. Branch: `secsettings-design`, based on
`existingrepos-design` (`769b32f5`, itself main `5e81a727`).

The owner requested individual security switches and an **Unrestricted** preset
for solo development. The [existing-repositories design](existing-repos.md) is
now owner-approved, including R5: Allowed folders is a default-on switch scoped
to machine/agent account; its privileged permission-grant restrictions survive
Unrestricted. This proposal builds on that contract without changing its defaults.
Everything else marked proposed below still awaits owner approval.

## Meaning and limits

**Unrestricted turns off every configurable guardrail for the selected scope.**
It never turns off dashboard authorization, grants root/sudo, reveals the vault
master key, skips publish approvals or disables audit. These are authority and
privilege boundaries, not the optional agent guardrails the preset controls.
The page must state this distinction before confirmation, not hide it in help.

A configurable guardrail is an owner-controlled check/prompt/advisory that can be
removed without granting another principal's authority or changing OS provisioning.
A non-negotiable boundary remains enforced even though an administrator may
separately choose features, containers, credentials or machine configuration.
“Non-negotiable” here means **no Security bypass**, not “this product setting can
never be changed through its existing authorized workflow.” A setting's existing
checkbox does not automatically make it part of Unrestricted.

Native CLI guard hooks are a safety net against mistakes/prompt injection, not a
filesystem sandbox. They do not stop every possible program, external process,
network connection or same-account attack. OS access and provider sandboxes are
separate, explicitly reported controls. UI copy must never promise that turning
one check on provides isolation.

## Inventory of current controls and proposed classification

This inventory follows source on the base revision, not inferred claims from
marketing text. **Current default** describes today's behavior; **scope** names
the proposed Security-edit scope unless an existing separate workflow is named.
C = **CONFIGURABLE**, included in the preset when applicable. N =
**NON-NEGOTIABLE in Security**, with the reason and separate workflow stated.
These tables cover the control families rather than every parser/size constant;
all input bounds and authorization checks remain mandatory.

### Configurable controls: one registry, no hidden exceptions

| ID / UI label | What it prevents; current enforcement | Current default | Proposed scope / classification |
|---|---|---|---|
| `force_push` — Block force pushes | History rewriting via force/lease/+refspec push; `server/guardrails.py`, `bin/acc-guard` push parser | On | Instance baseline → machine/account → project; **C** |
| `push_default` — Block pushes to main or master | Direct pushes instead of reviewed branch work; same guard; teams currently force this rule on in `server/teams.py` | On; forced on in teams | Same hierarchy; **C** for CLI checks, including teams after resolver integration. Team API authorization remains N. |
| `merge_pr` — Block agent pull-request merges | `gh pr merge`, merge/auto-merge API spellings; same guard | On | Same hierarchy; **C**. Off does not authorize a team MCP action or impersonate the owner on GitHub. |
| `rm_outside` — Block recursive deletes outside the workspace | Recognized shell/file-operation delete forms outside workspace, with existing scratch exceptions; `bin/acc-guard` | On | Same hierarchy; **C** |
| `secrets` — Block credential-file and environment reads | Recognized .env, provider auth, key paths, env/printenv dumps and proc environ accesses; `bin/acc-guard` | On | Same hierarchy; **C**. This is the tool-hook check, not vault access control. |
| `custom_rules` — Apply custom command blocks | Owner regex patterns in guard policy, up to 50; `server/guardrails.py`, `bin/acc-guard` | No patterns; saved patterns apply | Same hierarchy; **C**. One master switch plus existing pattern editor; preset disables enforcement, never deletes patterns. |
| `allowed_folders` — Limit account-service operations to allowed folders | Broker `_under_roots`/filesystem operations, `broker/config.py`, `broker/ops.py`; currently installer configuration | On, configured root list | **Machine/account**, with project views read-only; **C**, exactly approved R5. Root-helper/system/service denylist and OS rights remain N. |
| `tool_install_check` — Ask before account-wide tool changes | Guard checks account/global package-manager operations using `tools.required`; `server/tools.py`, `bin/acc-guard`, `tooling.py` | On; existing person/project exceptions can turn guard off | Instance → machine/account → project; **C** for non-root account tool commands only. Server tool-request approvals and system package helpers remain N. Project-local dependencies keep existing policy. |
| `provider_prompts` — Ask before provider tool actions | Provider-native permission/approval/sandbox choices in `providers/*`, compiler and launch inputs | Provider-specific; API defaults Claude acceptEdits, Codex workspace-write/on-request | Instance defaults → machine/account → project; **C** only for eligible build sessions. Off maps to qualified provider bypass/no-sandbox mode on a new approved launch; mandatory read-only roles still clamp it. |
| `budget_checks` — Enforce saved usage budgets | Notify/pause new launches from opt-in personal cap/pace windows; `server/budgets.py`, launch/team/schedule checks | No budgets until set; saved rules enforce | Instance → machine/account → project effect; **C**. Rules remain person-owned and stored unchanged. Off suppresses this scope's saved warning/pause checks, never provider quota enforcement. |
| `risk_labels` — Show advisory risk labels | Static labels, not permission decisions; `server/risk_labels.py`, settings preferences | On when Admin feature available; per-person opt-out | Instance → machine/account → project display policy; **C**, combined with viewer's existing personal preference. It cannot enable an unavailable Admin feature or change approvals. |

All IDs are proposed API identifiers except the five existing built-in rules.
Scope inheritance never crosses machine/account boundaries. A project override
must be owner-created; this is not a new member preference editor. Account-wide
`tools.person.*` exceptions currently span projects/machines; migration must
preserve their effective behavior explicitly rather than silently narrowing or
widening it. Risk labels are deliberately listed as advisory, not a barrier.

### Boundaries that cannot be disabled by this preset

| Control / source anchors (`src/acc/` unless deploy/bin) | What it prevents | Current default and scope | Classification and reason |
|---|---|---|---|
| Authentication, CSRF/origin, reauthentication, roles and object ownership: `server/api/context.py`, `auth.py`, route dependencies | Anonymous/private-network or cross-person mutation/data access | Enforced per request/principal/object | **N**. An owner cannot turn a member/agent into an owner with a safety preset. |
| Broker peer UID, agent/service roles, per-machine/account routing: `broker/server.py`, `broker/config.py`, machine-link APIs | Arbitrary local peer/broker or cross-account command execution | Enforced; installer-selected trusted client UIDs | **N**. Required execution authority boundary. |
| Dedicated unprivileged agent account and no sudo/admin/docker groups: `deploy/broker/acc-agent-add`, broker units | OS privilege escalation and service-account execution | Linux dedicated UID; platform-specific limitations on Mac | **N**. Never change identity/groups or grant sudo through Unrestricted. |
| Privileged folder/access helper validation, system/service/other-home exclusion, atomic policy writes | Root ACL/path traversal and config injection | Approved existing-repos design, not built today | **N**, including when Allowed folders is off. Separate explicit preview/approval grants only. |
| Guard/hook/launch-file integrity: `bin/acc-guard` own_files/load_policy, provider hook loaders, broker launch validation | Agent changing its policy, trusted executable/config or bypassing launch validation | Fail closed for protected launch/guard files | **N**. Only trusted policy application may change enforcement; agent cannot choose its own state. |
| Prepare/read-only assignments, project readonly, team role/ownership/frozen grants/depth/count/expiry/stop, gate verification authority: `server/teams.py`, `orchestrator/*`, `agent_contract.py` | Helper escalation, writes during Prepare, fake verification or work after stop | Enforced when role/team/project requires it; teams opt-in | **N**. Person's assignment/authority cannot be enlarged by a guardrail preset or task text. |
| Private-workspace local-only restriction: `server/local_workspace.py`, compiler/guard `local_only` | Sending private local research to a remote repository | On for private workspaces until explicit promotion | **N**. Separate promotion records consent; preset cannot publish private data implicitly. |
| Tool requests and privileged package approval/coordinates: `server/tools.py`, `tooling.py`, root package helpers | Agent approving its own system/tool request or supplying arbitrary root commands | Requests pending person approval; system managers owner-only | **N**. Off for account tool command checks does not change request status or helper allowlists. Existing human approval rules remain. |
| Publish/DNS/Caddy approval, project port/hostname ownership, public-address apply-time recheck: `server/publish.py`, `publish_addresses.py`, root publish helper | Agent exposing services, hijacking another project's ports/sites or spoofing public target | Owner approval and scoped project reservations; feature available separately | **N**. Network/publication is an owner action, not a prompt guardrail. |
| Preview origin/session auth and port-scoped tunnel: `server/preview.py`, `preview_remote.py` | Cross-origin/private service proxy abuse | Restricted authenticated preview capability | **N**. No unrestricted arbitrary proxy or general network bypass. |
| Self-hosted issue-server allowlist, pinned DNS/addresses and per-server custom CA: `server/issue_sources.py`, API configuration | SSRF, rebinding, credential/CA scope leakage | Owner config; no arbitrary self-hosted targets | **N**. Agent-provided issue content cannot authorize new hosts or CA trust. |
| Vault encryption, grants, reserved variables, remote-delivery consent and redaction: `server/vault.py`, API/vault, `env.py` | Cross-project credential disclosure, master-key access, launch-variable injection | Enforced; keys explicitly assigned/authorized | **N**. Master/server credentials are never handed to agents. Authorized project keys *are* delivered to session environment/MCP config; do not claim agents receive no credentials. |
| Request/approval provenance, signed-node link and enrollment identity: `server/api/nodes.py`, `node/client.py` | Agent-forged transport/authority | Authenticated connections; local broker/socket ownership | **N**. Acknowledgement is never approval. Rebase must retain reviewed service-owned reply-store fix before building this on a newer main. |
| Containers, image digest/qualification, root-helper attestations, limits and egress: `server/containers.py`, `broker/containers.py`, `container_egress.py` | Escape/fallback or arbitrary privileged runtime configuration | Admin feature off by default; opted-in projects require qualified container launches; member limits 2 CPU/4 GiB/512 PID/20 GiB/4 sessions, owners unset by default | **N** within an opted-in project. Container selection/limits are separate owner configuration. Unrestricted never silently switches a container project to native or relaxes egress/helper invariants. |
| Node isolation, WSL automount/interop and Mac privacy: `wsl.py`, node installers and reported capability checks | Cross-machine/Windows-drive access or fabricated isolation | Optional WSL hardening; updates retain choice; Mac same-UID limits reported | **N** in preset. Approved R5 explicitly forbids remount/interop/privacy changes; existing explicit machine setup remains separate. |
| OS cgroup/resource ceilings and provider quotas | Host exhaustion and paid-provider hard limits | Installer/account limits; provider-dependent quota | **N** in preset. Existing host-admin/resource settings remain; budget advisories alone are C. |
| Gate queue host budget and branch-code trust: `orchestrator/gates.py` | Running branch-controlled commands on host without explicit owner consent | Gate host opt-in off; project command/trust approval required | **N**. Unrestricted is not permission to run gates outside a sandbox. |
| Audit production/access, secret/log redaction, safe error mapping | Erasing accountability or displaying credentials/internal exceptions | Mandatory server-side paths; not immutable against root/DB administrator | **N**. Do not promise a cryptographically tamper-proof audit log. |
| Update/release signatures, checksums, pinned package sources and authorized repository: `updates.py`, `release_signatures.py`, `update_helper.py`, deploy repo scripts | Untrusted privileged executable/source installation or downgrade | Signature/checksum/repo checks; explicit audited missing-signature exception exists | **N**. Retain narrow existing unsigned exception; preset never adds one or bypasses an invalid signature. |
| Safe Files/uploads/archive/delete/Git metadata operations, content bounds and provenance: broker ops, Files APIs, `bundle.py`, project deletion | Traversal/XSS, decompression/size exhaustion, destruction of imported folders | Checked per operation, identity/provenance scoped | **N**. Allowed folders Off cannot disable descriptor/path or payload validation. |
| Backup target ownership, receiver auth, encryption/leases and restore checks: backup modules/APIs | Exfiltration/overwriting through backup transport and unsafe restore | Explicit target/recovery setup, validated operations | **N**. Preset cannot create a target, decrypt secrets or bypass restore authorization. |
| Public remote-access pairing/origin/allowlist and mobile credential lifecycle: `server/remote_access.py`, device APIs | Anonymous public access or stolen/unpaired device authorization | Availability separate from consent; public application/pairing required | **N**. Solo development does not disable authentication on the network. |
| Telemetry/report-sharing consent, private/public export deny rules | Unapproved data transmission or publication | Explicit opt-in/consent; scoped export rules | **N**. Preset cannot opt in, upload reports or include excluded/private files. |
| Admin feature availability and qualification: `server/admin_features.py` | Using disabled/unqualified features through alternate entry points | Per-feature defaults; orchestrator available, gates separately opted-in | **N** as an upper bound. Feature toggles stay in Admin features; Security cannot silently enable them. |

There is **no general native-agent outbound-network firewall today**. Publishing
approval controls published services, not arbitrary curl inside an agent process.
Do not invent a “Network protection On” claim. Container egress and machine
firewalls keep their separate qualified policies.

## Presets, scopes and effective state

Proposed selector: **Instance defaults**, then **Machine → Agent account**, then
an optional **Project** override (project must belong to the selected scope).
Allowed folders is edited only at machine/account, never project or wildcard
instance scope; instance views link to concrete account rows. No scope can name a
services/root account. Existing-repos root lists and grant/undo remain exactly R1–R5.

Each scope shows **Default**, **Custom**, or **Unrestricted**, plus its parent/source.
“Default” means inherit, not overwrite inherited values with all-on. Fresh installs
keep the five current rules on; custom-block enforcement on but empty; allowed
folders on; tool checks on; provider prompts retain current defaults; budgets
remain opt-in; risk labels retain the viewer preference and availability limit.
Existing owner choices, including explicit false values, survive upgrade.

**Unrestricted** materializes Off for every applicable C registry entry at that
scope and names each resulting switch. It preserves saved custom rules, roots,
budgets and a prior settings snapshot for **Restore previous settings**. Account/
instance selection must list child overrides: applying the preset either explicitly
updates those descendants too after review, or leaves them and labels them as
exceptions. Never show an instance-wide “all off” claim while a child is enforced.
The user chooses the exact affected scope; default action changes that scope only.

A project cannot override Allowed folders, so **Unrestricted for this project**
requires its account's folder check already off, or the UI offers a separately
reviewed account change naming sibling projects. Otherwise label the result
**Custom — account folder check still on**, not Unrestricted. Similar unavailable/
mandatory role constraints appear as explicit exceptions, never hidden checkboxes.

Preset membership is versioned. New configurable controls default safely until
the owner reviews them; do not auto-disable a newly added control using an old
blanket wildcard. If an upgrade adds a control, show **Custom — new protection on**
until reconfirmed, preserving the earlier snapshot. Unknown IDs or unavailable
provider bypass modes fail validation; no fake Unrestricted success on an adapter
that cannot implement the provider prompt choice.

A person may narrow provider permissions at launch even in an unrestricted scope.
They cannot widen the effective owner policy. Prepare/reviewer roles always force
read-only. The policy’s effective switch report says **Read-only assignment
required** where that mandatory constraint applies; it does not advertise a writable
reviewer. Per-provider bypass behavior/qualification must be explicit, never a
best-effort conversion or appended raw command-line flag.

## UI and exact consequence copy

Use DESIGN.md's Page/Card/Tabs, existing switches, `QueryState`, `StaleNote`,
`ErrorNote`, `Dialog` and `useConfirm`; no new palette/modal framework. The current
Guardrails controls move into **Settings → Security**, with one link from their old
location. Admin features remains availability, not a second Security editor.

Desktop layout within the normal page width:

1. **Security** title and scope selector; **Effective now** with revision and freshness.
2. **Mode**: Default / Custom / Unrestricted. A small explanation and **Review changes**.
3. **Agent guardrails** rows with On/Off/Inherited, parent/source and consequence text.
4. **Allowed folders** card for the selected machine/account, deep-linked from Host;
   policy and filesystem access remain separate as approved in existing-repos.
5. **Always enforced** collapsed list of N boundaries, with short summary always visible.
6. **Pending session changes** and recent policy audit entries; pinned **Review changes**
   footer only when dirty. Apply errors retain the draft and old effective revision.

On phone these are stacked cards, not a horizontal matrix. Scope selectors have
full labels; rows wrap copy under switches. Review is a standard focused dialog
with scrollable consequences and fixed Cancel/Confirm footer. Do not put the scope
or applied/pending distinction only in a tooltip. Keyboard, focus restoration,
labels and the app's single polite announcement follow DESIGN.md. Offline mutation
is disabled and stale state remains labelled, never presented as fresh protection.

Each switch uses a positive label from the C inventory and these Off consequences:

| Switch | Warning when switching Off |
|---|---|
| Force pushes | Agents may rewrite remote branch history if their Git credentials allow it. |
| Pushes to main/master | Agents may push directly to main or master; remote branch protection still applies. |
| Pull-request merges | Agents may merge through their own Git credentials. Rynhult's owner-only/team actions remain restricted. |
| Outside recursive deletes | Agents may delete files outside the session folder wherever their OS account can write. |
| Secret reads | Agents may read credential files and environment values accessible to their OS account. Vault grants and server secrets remain protected. |
| Custom command blocks | Your saved command patterns will not block tools in this scope. The patterns are kept. |
| Allowed folders | Rynhult's account service may use other accessible repositories. This grants no filesystem access or root permission. |
| Account tool checks | Agents may install or remove account-wide tools with their existing OS rights. System package requests still need an owner. |
| Provider prompts | Eligible build sessions may run tools without provider permission prompts or sandbox protection. Read-only assignments and Rynhult authority checks still apply. |
| Saved budgets | Saved usage warning/pause rules will not apply in this scope. Provider charges and quotas still apply. |
| Risk labels | Advisory labels will be hidden here. This changes no permission or approval. |

Unrestricted confirmation, after recent owner reauthentication:

> Turn off all configurable guardrails for alex-agent on dev-node?
> Agents may read accessible credentials, modify or delete accessible files, change
> account tools and push or merge using their own credentials without these checks.
> This can affect every project listed below. Authentication, owner approvals,
> vault access rules, OS permissions, machine isolation and audit remain enforced.
> Existing sessions will keep their current protections until you explicitly
> replace or restart them with the reviewed policy.

Show exact switch diff, affected projects/descendants, provider exceptions and
session actions. Confirmation button: **Apply Unrestricted**; Cancel is neutral.
Do not describe it as “safe solo mode.” Use the same review for an individual
loosening, with only its real consequences; a tightening still reviews stops.
Reauthentication is bound to this browser principal and short-lived preview,
not a stored confirmation bit or approval token an agent can replay.

Persistent **Unrestricted** badge, with scope words, in sidebar and applicable
session headers; a phone's collapsed sidebar still exposes it in the top bar.
An account badge reports current account policy; a session badge reports its
*frozen effective launch revision*, so old protected sessions are not mislabelled
when future-session settings change. For partial relaxation show **Custom security**
with Off-count and details, not an Unrestricted label. Badges have text, not color
alone, and cannot be dismissed as an ordinary notification. The Always enforced
summary stays accessible from the badge.

Members see their own effective session/project protections read-only, with
**An owner manages these settings** and a request link. They cannot see other
accounts, root paths, custom regex contents or saved budget/credential metadata.
Retain their personal risk-label opt-out; the effective value is feature availability
AND scope display policy AND viewer preference. No member can turn on an owner-
disabled control or use a preference endpoint as a policy write.

## Data, migration and API sketch

Proposed authoritative `security_policies` rows: ID, scope kind, machine stable ID,
account stable ID/UID, optional project ID, revision, registry version, preset,
explicit C overrides, custom-rule enable flag/reference, actor and timestamps.
Separate `security_policy_changes` state: operation ID, before/after digest,
expected revision, target policy revision, approval principal/time, affected-scope
snapshot, transition state and target acknowledgements. No credential values in
either row. Folder roots/manifests stay in the approved target-local policy/ledger;
main stores their applied revision and safe owner-only metadata.

Sessions/launch specifications freeze effective policy, source/revision digest,
provider mapping and mandatory role clamps with each launch generation. A current
policy fence governs server/broker calls as well as launch. Do not rely only on
editable guardrails.json or a stale compiled file to enforce a tightening.
Audit events remain in AuditLog, not a user-removable policy history table.

Use a reviewed new schema migration number after rechecking the actual migration
head at build time; do not reserve or guess a number in this proposal. Upgrade
imports existing `guardrails` rules/custom patterns, `tools.person.*` and
`tools.project.*` choices and applied machine root policies. Budgets and personal
risk preferences are referenced, not destroyed. Preserve team mandatory authority
and existing provider launch inputs. Compute pre/post effective policy for existing
projects, with compatibility tests proving no silent widening. Unavailable node
root state stays unknown/pending, not “all allowed.” Existing running generations
keep their frozen record until an explicit transition. Rollback must refuse new
unsupported policy revisions rather than interpreting an empty legacy config as
all checks off; retain a recoverable prior snapshot and documented forward repair.

Proposed routes (names subject to API review):

- `GET /api/security/effective?project_id=...` for authenticated own-scope redacted
  effective values; owner-only `GET /api/security/scopes` and `.../policies/{id}`.
- Owner + recent reauthentication `POST /api/security/changes/preview`, with bounded
  known controls, exact scope, desired values/preset and expected revision. It
  returns a principal-bound short-lived preview digest, consequences and targets;
  creates no root grant or applied policy.
- `POST /api/security/changes/{id}/apply` verifies scope, role, CSRF/origin,
  reauthentication, preview digest/expiry and expected revision again; changes
  are idempotent per operation ID. No session/MCP or installation-token path.
- Owner `GET /api/security/changes/{id}` reports applied/pending/conflict/refused;
  a fresh preview is required after state drift. No blind retry over changed state.
- Existing folder preview/apply/grant/undo use the same revision/authority envelope
  and approved target-local helper, not a generic arbitrary root operation endpoint.

Compatibility `/api/guardrails` reads/writes route through the same resolver and
owner/reauth/revision rules; do not leave an older endpoint able to bypass review.
Budget/risk-personal endpoints cannot erase scope policy or trigger Unrestricted.
Unknown/extra fields and non-boolean values reject; instances/projects cannot carry
account-only Allowed folders overrides. Bound overrides/pattern count and regex
cost; adding scope settings is not permission to introduce unbounded regex work.

## Enforcement and running-session reconciliation

One pure resolver produces effective C values and provenance for compiler,
server admission, scheduler, orchestrator/continuations, broker policy and UI.
Admin availability, role/assignment constraints and trusted OS boundaries apply
as mandatory upper bounds. Risk labels use the resolved display choice; budget
launch checks use the exact execution scope and owner. No duplicate bool defaults
in the web code. Ensure team launch decoration no longer blindly re-enables the
configurable push rule while keeping team authority checks intact.

`tool_install_check` Off only removes native account-command interception; it never
auto-approves an Inbox/root-package request. Shell secret-read Off never disables
vault redaction or export exclusions. Provider prompts Off must be supported by
the selected qualified adapter; user-supplied argv/profile/environment cannot
select weaker options or override frozen role constraints. Hook integrity checks
remain even with all C checks Off. Root/broker allowlists are not serialized as `/`.

For every change:

1. Preview affected project/session/gate/schedule generations and policy sources.
   Acquire scope transition locks, fence new launches and compare revision again.
2. **Loosening:** apply to future launches only by default. For running sessions,
   show **Keep current protection** or explicit **Stop and restart with reviewed
   settings**. No live editing of argv, hook files, sandboxes, team grants or ACLs.
   A resume counts as a launch and uses an explicitly reviewed generation change;
   old frozen inputs cannot silently acquire a new bypass mode.
3. **Tightening:** immediately fence server/broker operations in scope. Require
   confirmed stops for affected native sessions/helpers and account processes as
   relevant; stop dependent gate work using its existing cancellation contract.
   Do not claim a hook/broker change can contain an already running native process.
   If a node is offline or stops are uncertain, leave enforcement **Pending —
   sessions may still have earlier access**, block affected new work and retry.
4. Publish trusted target policy only after required stops; compile new launch
   generations, require broker/node applied-revision acknowledgement, then release
   admission fences. No native fallback for container projects. Do not delete work
   or force an automatic restart of a reviewed read-only assignment into Build.
5. Audit request/preview/apply/failure/conflict, per-target acknowledgement and
   generation transition. On partial target failure retain fences for that target,
   expose actual applied state and offer repair/previous-policy restore through
   another reviewed operation; never say all machines changed atomically.

Revocation cannot recall copied data, kill unknown external programs by magic or
remove OS grants. Folder access undo/group/ACL work remains the separate approved
workflow, including confirmed stops and conflict-aware rollback. Disabling or
restoring Security does not modify wsl.conf, macOS privacy, group membership,
container mounts, public sites or Git remotes.

## Threat model and acceptance gates

Threat actors: prompt-injected/hostile authorized agent, curious member,
unauthenticated private-network visitor, malicious repository/tool input,
concurrent owner browser, stale/compromised node and accidental solo-developer
misconfiguration. Trust target root/service installation and approving owner;
Unrestricted explicitly accepts greater damage potential within that account's
existing OS and credential rights. It does not assume hostile agents honor prose.

| Area | Required tests / proof |
|---|---|
| Inventory completeness | Registry test maps every C entry to a real server/compiler/hook enforcement adapter; preset covers all applicable IDs exactly. Every N family retains negative tests under Unrestricted. Source grep is an inventory aid, not proof by itself. |
| Resolver/migration | Missing defaults, explicit false, old custom patterns/tool exceptions, project inheritance, account folder non-overridability, disabled features, unknown controls, new registry version and round-trip restore; no lost or silently weakened settings. |
| Authorization | Anonymous/member/agent/session/MCP/installation tokens cannot preview/apply; cross-user/project/machine scope, stale role, CSRF/origin, expired reauth/preview, replay and concurrent revisions fail without leaking paths. |
| Bypass modes | All supported providers, no-profile, team lead/helper, read-only reviewer, Prepare, resume, scheduled/continuation paths. Profile/raw argv/environment cannot change policy; unsupported provider state refuses rather than weakening another control. |
| Guard commands | Existing full guard corpus for on/off behavior of force pushes, main pushes, merge/API, deletes, secret paths/env dumps, custom blocks and tool changes. Unrestricted disables C guards while own-files/tamper and trusted request authority remain. |
| N boundaries | Under Unrestricted attempt self-approval, root/system tools, publish without approval/ports, cross-project vault delivery, arbitrary issue URL/CA, unsigned/bad-signature update, folder helper system/other-home grant, audit suppression and public export of exclusions; all refuse. Retain existing narrow owner exceptions only. |
| Concurrency/stops | Tightening during active/busy/ghost agents, team helper creation, schedule dispatch, gates, offline node, reconnect, duplicate request, daemon crash and stale launch generation. No new execution until required applied revision; no silent widening of existing terminals. |
| Platform isolation | Multi-UID Linux fixture/VM tests; Mac same-UID truthfulness; WSL isolation/DrvFS unchanged; container opted-in projects have no native fallback. Allowed folders off never grants OS rights or broader Codex Git metadata writes. |
| Advisory/cost | Scope budget suppression leaves saved rules intact and cannot bypass provider quota; risk viewer preference respected; sensitive usage not leaked to members. Audit remains even when labels/alerts are off. |
| Browser journey | Owner chooses account, reviews individual Off, reauthenticates, applies preset, sees all switches and persistent sidebar/session badges, restores saved policy. Phone/keyboard/focus/error/retry/offline/stale/pending checks. Member read-only view hides other-account details. Fake providers and isolated ports only. |

Run affected Python suites with four workers, type check/build and focused browser
specs; full guard corpus is required because switch plumbing changes its context.
Root/helper/OS permission proofs run only in disposable VMs. Coordinator runs full
merge gates; no testing on the live instance. No success claim from labels alone:
verify actual command refusal/allowance and unchanged N boundaries.

## Delivery for rc.3 and owner decisions

Proposed schedule: **Monday Nov 2** registry/resolver and safe migration/API,
**Tuesday Nov 3** Security UI, inheritance and folder-policy integration,
**Wednesday Nov 4** generation fencing/reconciliation plus provider/guard/browser
regressions and Guide, **Thursday Nov 5** independent security review and fix/retest,
**Friday Nov 6** coordinator gates/owner approval for rc.3. Parallel build lanes
need one shared resolver contract and a declared migration owner; do not invent
parallel migrations or conflicting Settings editors.

Three days is a constraint, not evidence the approved folder helper/undo work
already exists. Reuse qualified current boundaries, keep platform automatic grants
at the approved existing-repos limits, and surface pending/offline state honestly.
If provider mapping, multi-UID grant/undo or safe tightening cannot pass by Wednesday,
flag an rc.3 blocker/scope decision to the owner. Do not ship a cosmetic preset,
silently omit a C switch, skip Thursday review or relax a mandatory boundary to
meet the date. A read-only effective-policy page alone is not this completed feature.

Docs to update with implementation: Guide Settings (Security), Guardrails if split,
Projects, Sessions/permission wording, Orchestrator/Prepare, Providers/tool installs,
Nodes, Install-Windows, Mac setup, Keys and Publishing (what remains mandatory);
internal Operations, ownership/authz inventory, audit words, upgrade/rollback notes,
rc.3 release/testing checklist. Use artificial accounts and example.lan/example.com.
Do not present this proposed page as available in the current Guide.

Owner approval requested for this proposal's classification (especially provider
prompt/sandbox mapping and budget/advisory switches), hierarchical scope model,
explicit project Unrestricted exception handling, confirmation/badge wording,
migration/transition semantics and delivery gates. The existing-repos design is
already approved and is not being reopened. **Stop after this document and its
handoff; no code, migration, runtime switch or permission change before approval.**
