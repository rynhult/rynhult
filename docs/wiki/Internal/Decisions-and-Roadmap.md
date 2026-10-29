# Decisions and roadmap

## v1.0 Linux server and browser scope

The owner's October 27 decision is `docs/decisions/0073-v1-linux-server-scope.md`.
v1 is the self-hosted Linux server, used from browsers on any client platform,
including the phone web app. Existing Linux, macOS and Windows-through-WSL nodes
remain supported with their current capabilities and limits.

Native applications follow v1, **one platform at a time** to reduce total cost.
The order is not chosen yet; the candidates are native Windows, macOS standalone
desktop, Linux desktop, iOS and Android. For native Windows, Codex is the first provider.
This supersedes starting native Windows immediately after rc.1 and older
native-app-before-publication requirements. Existing desktop/mobile source and
qualification evidence stay on main as experimental groundwork, not v1 native
releases or extra native release gates. Browser/phone-web and supported-node
checks remain; see [platform support](/User-Guide/Install#v10-platform-scope).
The older dated proposals and status snapshots below retain their technical
context; this decision governs their delivery timing.

## 2026-10-27: deployed milestones and freeze decisions

The following shipped and were deployed on the owner installation. This is not
a claim that the public RC has been tagged or released:

- **#366:** RC documentation/readiness, phone checks, screenshots, glossary,
  known issues and root rehearsal.
- **#367:** dependencies, install/update tests, telemetry notice **0.4**,
  RC performance, break-it 10 and test isolation.
- **#370:** decision **0073**, Linux-server v1 scope and the remote-access owner checklist.
- **#371:** busy-server backup readback uses the captured catalog; deployment
  preflight checks the capture lease.
- **#372:** Orchestrator v2 **Preview, off by default**, with planning session,
  versioned plan, team cards, gate queue and delivery; migrations **0069-0073**.
- **#373:** in-app RC feedback link, WSL replacement-main drill runbook and
  the rynhult.dev pre-release site.

The Linux-server/browser scope above governs v1; Linux, macOS and WSL nodes remain
supported, while native applications follow after v1 one platform at a time,
with no order chosen. **v1 remote access is via VPN/Tailscale**; public remote
access retains its separate owner/qualification gates. **HA phase 3 is after v1.**
Company sign-in was switched off on the owner installation, not removed from
the product or made unavailable to other owners. S3 retention maintenance is
operations work, not a release milestone.

The owner selected public **v0.15.0-rc.1 immediately after feature freeze**,
alongside the public `rynhult/rynhult` repository and rynhult.dev. The site's
pre-release page is already serving through edge Caddy, and the staging telemetry
collector was upgraded; neither operation constitutes publishing the RC.

**Incidents and integration follow-ups:**

- Hourly captures failed with a catalog revision mismatch after an undeployed
  merge changed built-ins on disk. #371 fixes captured-catalog readback, and
  deployment immediately after shipping resolved the mixed runtime/on-disk state.
- A real recovery drill found replacement preparation refusing long-lived
  databases because historical numeric `DEFAULT` literal spelling differed from
  a fresh schema. The narrow normalization fix is on `drill-fixes`, merged into
  integration candidate **pf3**; this is not a completed recovery qualification.
- Security review 10 found an uninstaller **High** that could delete the owner's
  login account/home. It is fixed in **pf3**, not included in the deployed claims
  above. Deployment of these integration fixes still needs its own gate.

## Record numbers

Duplicate record numbers were repaired on 2026-10-24 against current main.
Every number already unique on main stays unchanged, including 0021 (Rynhult's
public name) and 0022 (remote access owner apply). For each duplicated number,
the earliest addition from `git log --diff-filter=A` keeps the original;
later records use 0023–0046, after main's highest number, 0022. Records added
by the same commit tie by filename. Every renamed record has a dated note.
Before creating a record, run from the repository root:

```sh
python scripts/next_decision_number.py
```

It prints one above the highest `NNNN-*.md` number in `docs/decisions/`
across all local branch tips (including main) and this working tree, padded to
four digits. Gaps are not reused; nested drafts are excluded. The helper uses
read-only `git for-each-ref` and `git ls-tree`: it never fetches, switches branches
or changes the index. Git failures stop it rather than returning a guessed number.

This is a suggestion, not an atomic reservation. Other worktrees' uncommitted
records and remote-only branches are not visible. Commit the new record promptly
and recheck before integration. The documentation test reports every colliding
filename and suggests the next available number if a collision still occurs.

On 2026-10-25, the current-branch change-baseline record moved from 0053 to 0057
to preserve main's existing 0053 WSL isolation and newly landed 0055 S3 retention
records. Its provisional audit number was 0055; references were updated again.

## Key decisions
- **Native CLIs, not a re-implementation.** Sessions are the real Claude Code and Codex in tmux. ACC adds supervision, not a new agent.
- **Optional bounded orchestration.** People choose the agent, profile and work. Only a person can grant the Orchestrator role and its limits; ordinary sessions keep their original behavior.
- **Two processes and separate users.** The dashboard (`acc`) and the broker (agent user) are split so neither can do the other's job.
- **Never a silent new conversation.** Failed resumes stop at **Recovery needed**.
- **Hooks before inference.** CLI hooks and tmux process state are preferred; provider-specific terminal heuristics fill gaps. Old working-hook evidence becomes [Unknown](/User-Guide/Sessions#when-a-working-status-is-too-old), not permanently current activity.
- **Approvals for outward-facing actions.** Publishing and repository creation need a person.
- **Workspaces vs projects.** Development and research can stay in private local-only workspaces. Adding a remote is a separate, explicit promotion step.
- **Docs as code.** Each project's wiki lives in its repository, in a format Azure DevOps can publish.

Detailed records live in the repository's `docs/decisions/`. `0001-public-source-snapshots.md` records the reviewed-snapshot publication boundary; see [Development](/Internal/Development) for the release procedure.

`0002-update-identity-and-mac-installer.md` records content build identities, release manifests and the Mac installer choice for Updates.

`0053-optional-wsl-node-isolation.md` records the explicit dedicated-node opt-in,
effective per-agent observations, and audited owner warning dismissal. See
[Windows installation](/User-Guide/Install-Windows#isolation-for-dedicated-wsl-nodes).

`0027-opencode-sessions.md` records how OpenCode runs: a private server and config folder per session, ACC's plugin as its guard and status signal (with a shell that refuses unchecked commands), and a pinned release line. The design is `docs/providers/OPENCODE.md`.

`0024-registry-only-tool-approval.md` records M16's registry-coordinate requests,
root/system vs broker/account execution, and the deliberate refusal of arbitrary
installer commands. Full design: the repository's `docs/TOOLS.md`.

`0026-bounded-team-spool-authority.md` records the optional team grant, subtree authority and shared limits. Team tool approvals stay with the person and use the helper’s actual machine.

`0028-vault-backed-issue-sources.md` records server-only project grants, untrusted issue context, bounded provider
requests and at-most-once Linear PR comments. The source-tree design is `docs/ISSUE-SOURCES.md`; see
[Projects](/User-Guide/Projects#connect-gitlab-or-linear) for setup.

`0036-group-pack-exchange.md` records bounded explicit commit packs, quarantine,
standalone private worktrees and separate group/private recovery. See
[Group Exchange](/Internal/Group-Exchange) for the API, limits and recovery fences.
`0033-vault-variable-suggestions.md` records the shared provider-grouped name list,
native select with Custom, and reuse of descriptions in New session. Suggestions
do not replace vault validation or configure credentials. See [Keys](/User-Guide/Keys).
`0034-owner-pinned-issue-servers.md` extends the issue-source boundary with owner-confirmed DNS sets and
host-scoped CA trust for GitLab self-managed and Gitea/Forgejo. See [Settings](/User-Guide/Settings#self-hosted-git-servers).

`0038-trusted-container-launcher.md` records how the container launcher knows who is asking: the broker unit's main
process plus a grant only accd can write, Podman as the agent account, limits from Settings and no native fallback.
See [Sessions](/User-Guide/Sessions#sessions-in-containers) and [Security](/Internal/Security#container-launcher).
`0037-codex-owned-hook-trust.md` records exact-definition trust for ACC’s Codex hooks, reviewed-version gating and manual-review fallback. See [Codex hook review](/User-Guide/Guardrails#codex-hook-review).

`0045-accessible-terminal-output.md` records browser-local screen reader preferences, F6 and skip navigation, bounded scrollback snapshots and the labelled browser-buffer fallback. See [Sessions](/User-Guide/Sessions#reading-recent-output-as-text).

`0018-short-background-database-writes.md` records off-loop, joined write-intent
transactions for transcript persistence and automation cancellation, host-writer
serialization, consistent SQLite connection settings and why lock failures are
not ignored or blindly replayed. See [Data model](/Internal/Data-Model).

`0019-google-dns-credential-files.md` records fixed, content-addressed Caddy credential files and generation retention
for the Google Cloud DNS candidate. See [DNS provider qualification](/Internal/Development#dns-provider-phase-2-qualification).

`0057-shared-server-browser-isolation.md` records dependency-ordered global browser
phases, resource-owned parallel fixtures and unchanged per-case rate budgets.
See [Development](/Internal/Development) for the browser commands and isolation contract.

`0061-oidc-mfa-proof-and-passkey-enrollment.md` records what proves MFA in a company sign-in (`amr` mfa/hwk/fido,
or owner-named tenant `acr` values) and the one-passkey window after an MFA-proven sign-in for company-only
members. See [Company sign-in with Microsoft Entra ID](/User-Guide/Company-Sign-In-Entra#passkeys-for-company-members).

## Owner answers (2026-10-24)

The accepted policy settles the
remote-access lifecycle and direct/raw-TCP boundary (no Cloudflare termination),
automatic group consent revocation on departure or archival, and retaining
20 terminal snapshots per minute per person with warned reads during audit-write
contention. The terminal choices will be revisited together. Remote exposure
still requires the implementation and qualification gates. See
[Groups](/User-Guide/Groups) and [Remote Access](/User-Guide/Remote-Access).

## Roadmap

- **Group collaboration (approved; authority and same-host exchange phases 1–2 implemented).** Private clones
  per person and machine, session worktrees, explicit commit exchange and an
  advisory work board; no shared agent credentials or transcripts. Design:
  `docs/GROUPS.md`; approved ADR:
  `docs/decisions/0006-group-repository-isolation.md`.
  See [Groups](/User-Guide/Groups) for current authority controls and the gated next phases.
- **Per-session containers (Preview in the RC, owner decision 2026-10-26; trusted launcher built 2026-10-23, off
  by default).** Projects switch on only on a host with a recorded passed qualification
  (`acc.container_host_record`); full support after a second host qualifies. Rootless
  Podman/crun, one container per session, started by the `acc-containers` root helper for projects an owner
  switches to containers, with per-account limits. Phase 0 qualification (credentials inside, immutable hooks,
  nested sandboxes, LAN egress denial) is still open. Design: `docs/CONTAINERS.md`; launcher note:
  `docs/CONTAINER-LAUNCHER.md`; ADRs: `docs/decisions/0005-session-container-boundary.md`,
  `docs/decisions/0038-trusted-container-launcher.md`.
- **Additional hosts.** Monitor and manage more machines over Tailscale or the local network. Each gets a `<hostname>-root-admin` session, with its page giving the SSH and tmux commands. ACC itself is developed only on the primary.
- **More providers.** Further agent CLIs through qualified adapters (launch, resume, hooks, usage).
- **Self-hosted inference.** Open-weight models on a GPU host.

## Catalog restore (2026-10-22)

The design and decision record are `docs/CATALOG-RESTORE.md` and
`docs/decisions/0003-catalog-restore-layer.md` in the source tree. Restore writes immutable generations
beneath the writable catalog, preserves custom items and profile changes, and shares the updater's
verified stable-release download path. See [Catalog](/User-Guide/Catalog#restoring-built-ins) for the flow.

## Private Development workspaces (2026-10-22)

`docs/decisions/0025-private-workspace-project-records.md` records the individual project record, mandatory local-only policy, stopped-session conversion and stable backup identity. Design: `docs/PRIVATE-WORKSPACES.md`. See [Workspaces](/User-Guide/Workspaces) for creation, conversion and promotion.

## Diff explanations (2026-10-22)

`docs/decisions/0023-explicit-diff-explanation-helpers.md` records the explicit
native-CLI helper boundary, isolated workspace/configuration and revision cache.
See [Architecture](/Internal/Architecture#diff-explanations) for the trust
boundaries and [Sessions](/User-Guide/Sessions#explain-these-changes) for the flow.

## Current-branch change baselines (2026-10-25)

`docs/decisions/0066-current-branch-change-baselines.md` records the shared
badge/detail merge-base policy, working-changes fallback and nested-scratch
boundary. See [Sessions](/User-Guide/Sessions#how-changes-chooses-its-comparison)
for the visible comparison and older-node limitation.

## Isolated live preview (2026-10-22)

Per-project-and-port origins, login-bound five-minute credentials, a private HTTP/WebSocket gateway, and local-only targets. Source decision: `docs/decisions/0004-isolated-live-preview.md`; design: `docs/PREVIEW.md`.

## Scheduled sessions

`docs/decisions/0030-durable-person-owned-schedules.md` records person-only control, durable run intent, reserved session identity, profile option snapshots and the no-replay policy for ambiguous dispatch. Design: `docs/SCHEDULES.md`. See [Schedules](/User-Guide/Schedules) for timing and controls.

Session auto-resume and goals use durable at-most-once continuation claims; see repository decision `docs/decisions/0031-session-continuation-claims.md` and design `docs/AUTO-RESUME.md`. Optional usage caps refer to the provider plan window. Reliable retries would require broker-side idempotency and fencing; a second model judging goal completion remains a possible opt-in extension.

## Multi-run (2026-10-22)

`docs/decisions/0007-independent-multirun-sessions.md` records independent,
person-launched session groups rather than team grants, partial budget refusals,
reuse of redacted Changes, and retention of archived worktrees for comparison.
Design: `docs/MULTI-RUN.md`. See [Sessions](/User-Guide/Sessions#multi-run).

## Native Windows proposal (2026-10-23)

Native Windows nodes before a native main are proposed for owner approval,
not a v1 deliverable. Delivery is after v1 under decision 0073, not immediately
after rc.1. Source design: `docs/WINDOWS-NATIVE.md`; proposed runtime record:
`docs/decisions/0011-native-windows-node-first.md`. The current installer still
uses WSL; see [Install Windows](/User-Guide/Install-Windows#can-rynhult-run-without-wsl).

The approved container direction in `docs/CONTAINERS.md`
permits owner-only native project exceptions with a **Not isolated** badge.
This Windows proposal requires that exception and does not claim general
Windows session isolation or approve implementation.

Owner-run phase-0 qualification scripts are now in `scripts/windows-phase0/`.
They compile an in-box C# service host locally, journal a temporary standard
account/profile and service, and collect gates 1/2/5 after a no-login reboot.
The owner is refused by the SID-authenticated pipe; provider hooks, sandbox
denial and guard feasibility remain untested. No Windows execution or passing
gate is claimed; see the source README for exact setup, collection and
manifest-driven removal. This does not change the proposed product scope.

The [developer probe boundary](/Internal/Development#windows-phase-0-owner-run-probe)
distinguishes these unrun administrator scripts from the earlier read-only
Windows observations. Dedicated WSL nodes already have an implemented, opt-in
isolation choice, recorded in `0053-optional-wsl-node-isolation.md`; it is not a
native Windows node or a per-session sandbox. See [WSL isolation](/User-Guide/Install-Windows#isolation-for-dedicated-wsl-nodes).

## Usage statistics and bug reports (2026-10-23)

The owner approved the usage schema, separate 90-day rotating keys, reviewed reports, and retention of
90 days raw usage, 13 months anonymous monthly totals, 180 days reports and 90 days local sent history.
Both choices remain off by default. The owner hosts the collector and controls its data; it has run as a pre-release
staging service since 2026-10-26. Notice 0.2 shipped with #335; notice **0.4** shipped with #367 on October 27,
and the staging collector was upgraded that day.
See [Usage and bug reports](/User-Guide/Usage-and-Bug-Reports) and
[Telemetry implementation](/Internal/Telemetry). Decision: `docs/decisions/0012-opt-in-telemetry.md`.


## Container qualification evidence (2026-10-23)

Source decision `docs/decisions/0042-container-qualification-evidence.md` records
the owner-run control-account driver, separate pinned image policy, real CLI
tool dispatch against an offline loopback model fixture and mandatory all-pass
GO gate. Effective hooks and policy must be immutable; writable configuration
keeps the current launcher NO-GO. See [Operations](/Internal/Operations#container-qualification-evidence)
and [Settings](/User-Guide/Settings#container-limits) for the operational boundary.
## Enrolled Linux backup target (2026-10-23)

`docs/decisions/0039-backup-linux-receiver.md` records the pinned append-only receiver, owner-operated root
setup, root-helper update boundary, fixed typed machine channel, separate credentials and independent
maintenance/recovery custody. S3 provider/Object Lock qualification follows Linux review.
See [Backups](/User-Guide/Backups#before-you-start).

## Optional PostgreSQL (HA phase 1)

`docs/decisions/0041-optional-postgresql.md` records the optional database backend, unchanged SQLite default, dialect-specific search, bounded transaction locks and verified offline import. `docs/POSTGRESQL.md` is the owner runbook. One main remains the default. Phase 2 explicitly enables PostgreSQL manual active-passive authority; see [Manual failover](Manual-Failover.md). PostgreSQL backup/PITR is operated independently of ACC's SQLite server-backup capture.

S3-compatible backup targets use native restic S3, purpose-bound write-only infrastructure credentials, and
COMPLIANCE retention observations before any protection claim. Maintenance identities remain outside accd and its vault.
See the source decision `docs/decisions/0044-backup-s3-target.md` and [Backups](/User-Guide/Backups#s3-compatible-storage).

## Manual HA phase 2

`docs/decisions/0014-manual-ha-authority.md` records explicit expected-epoch owner
transitions, a fresh process incarnation on every start, PostgreSQL leadership and
activity barriers, standby admission, local execution refusal and independently
fenced crash recovery. Security review and a no-skip disposable PostgreSQL run
remain deployment gates. Automatic failover is deferred.
The October 27 owner decision places **HA phase 3 after v1**; existing dry-run
and wiring work does not make automatic promotion a v1 deliverable.

## Initial browser JavaScript

Repository decision `docs/decisions/0046-initial-browser-javascript-budget.md` records lazy route loading
and the 250 KiB gzip budget for the initial static JavaScript graph. See [Development](/Internal/Development#frontend-payload-budget)
for the build check, its scope and fixture tests.

`0021-rynhult-public-name.md` records the stage 1 public rename and command aliases.
It preserves all installation paths, environment variables, service and database
identifiers, cookies and protocol names. See [User guide](/User-Guide#command-names-and-compatibility)
for commands and upgrade compatibility.

## Remote access owner apply preparation

Repository decision `docs/decisions/0022-remote-access-owner-apply-contract.md`
records the closed root review/apply/off/status contract, exact revision review,
drift refusal, OFF recovery, 15-second reconciliation and matching-code pairing.
No installation or public-release approval is implied. See
[Device pairing](/Internal/Device-Pairing#uninstalled-edge-helper-contract).

`0049-planned-machine-downtime.md` records local weekly downtime intervals, DST
boundaries, alert suppression without freshening backup age, and durable single
capture recovery on reconnect. See [machine downtime](/User-Guide/Nodes#plan-machine-downtime).

## Standalone desktop apps (unreleased developer qualification)

All desktop qualification below is post-v1 work. Native platforms follow one at a
time, with no order chosen under decision 0073. Existing source/proofs do not
qualify additional main or node distributions. The qualified main remains Ubuntu
24.04+ and Debian 13+ with systemd. Fedora/RHEL, openSUSE and Arch node paths remain
explicit test installs; desktop RPM/AppImage/Arch proofs do not qualify nodes.

The standalone Linux account slice passed automated rootless Arch/systemd guest
qualification on 2026-10-25: unprivileged makepkg, pacman integrity before/after
real fake-provider sessions, separate agent accounts, socket/ACL checks,
synthetic auth, owner user-manager logout/login persistence and uninstall.
The broker remains a system service running as the agent account. Physical
Arch-derivative/GUI/LSM behavior, real vendor auth, signatures and backup remain
release gates. The older proposal below is historical design context. The older audit,
package-name mapping and per-distribution tables in `docs/LINUX-DISTROS.md`
are also labelled historical; phase 4 gives the implemented Arch recipe and
full package dependencies. The Arch preparation helper prints a Bash-escaped
follow-up command, including when the output folder contains an apostrophe.


The owner's 2026-10-24 requirement includes macOS, Windows and Linux desktop apps
with an embedded local main for one person, plus optional dedicated-server use.
Source design `docs/DESKTOP-APPS.md` recommends shared Python/React behind a small
platform host, qualified execution accounts and explicit isolation labels, signed
updates and verified single-authority project/history migration. Those product
features remain planned, not released. The approved comparative spike now has a
self-contained Linux test package with its own main but no broker/agent execution,
plus Linux-prepared Apple-silicon build scripts and an unsigned app/disk-image
configuration without Mac qualification. Neither replaces the current node
installers. The Windows node-first proposal remains an engineering dependency,
not the completed standalone destination. Public remote access and container
qualification gates remain intact.

The private afternoon owner-answer record covers mobile,
roaming-machine backups and planned downtime, not desktop approval.
Desktop spike scope/results are recorded in `docs/DESKTOP-APPS.md`.
See [developer checks](/Internal/Development#linux-desktop-spike) and
[prepared versus available user experience](/User-Guide/Desktop-Apps).

`0048-rynhult-runtime-compatibility.md` records the owner's 2026-10-24 naming
approval, permanent legacy security/data/account/path identifiers, dual-name
release/fleet window and database-preserving stage 2 rollback boundary. Phase 1
adds environment compatibility, safe systemd aliases and `rename-compat-1`;
physical moves and launchd label changes remain deferred.


## Automatic HA protocol model

`docs/decisions/0054-ha-automatic-dry-run.md` selects a fail-closed witness/lease
model with conservative monotonic deadlines and independent external fencing.
Only pure decisions, owner-configured dry-run status and authenticated owner-read
audit are implemented. Promotion remains manual; real PostgreSQL and external
fencing qualification are required before connecting automatic promotion.
See [Manual failover](/Internal/Manual-Failover#automatic-evaluation-phase-3-adapters).

## Independent S3 retention maintenance (2026-10-25)

`docs/decisions/0055-independent-s3-retention-maintenance.md` records the opt-in independent-host identity,
conservative all-version inventory, dry-run/explicit apply, bounded monotonic COMPLIANCE extension, audited
resume, storage commitment budget and fresh-prefix rotation boundary. The main gains no maintenance key or
scheduler. Its runway projection warns at seven days, while protection still requires a fresh verified capture.
See [owner policy and runbook](/User-Guide/Backups#independent-retention-maintenance).
The October 27 owner decision treats retention maintenance as **operations, not
release work**; existing protection/qualification requirements still apply.

## Roaming git backups

`0058-roaming-git-backups.md` implements owner decision 0050 using a scoped
node-local forwarder and project/node token-exchange grants. It preserves private
address preference and existing signed-link scheduling. Public backup success is
audited and shown per machine on Host. See
[Remote access](/User-Guide/Remote-Access#git-backups-while-roaming).

The disposable DNS qualification kit uses hidden interactive credentials, exact zone acknowledgement, forced
staging renewal and non-promoting JSON evidence. See decision **0064**, `docs/decisions/0064-disposable-dns-qualification-kit.md`,
and [the owner checklist](/User-Guide/DNS-Qualification). Live IAM, parallel TXT and production/service gates remain open.

Container teams use separate private checkouts and read-only live reply mounts.
The merge tool prepares a bounded bundle; the lead imports and merges it inside
its container before explicitly finishing the helper. Repository decision:
`docs/decisions/0070-container-team-merges-stay-inside.md`. Real-provider and
trusted-launcher qualification remain required before production enablement;
see [Orchestrator](/User-Guide/Orchestrator#teams-in-container-projects).

## Node task ownership

`docs/decisions/0060-node-task-ownership.md` records generation-bound registries,
joined cancellation and independent exception handling. See the maintained
[task inventory](/Internal/Node-Task-Ownership) for creation sites and lifecycle
contracts. This adds no automatic replay or remote rollback guarantee.

## Dependency automation

`0059-dependency-automation.md` extends native Dependabot to the locked ecosystems
and selects an offline installer-pin inventory gate. Security updates have
separate advisory-triggered groups; GitHub activation remains an administrator
step. See [Dependency updates](/Internal/Dependency-Updates) for the inventory,
manual tool pins, desktop lock regeneration and notices.

## Native device server lifecycle

`0071-native-device-server-lifecycle.md` records the experimental `ACC_MOBILE`
gate, private owner-approved CSR enrollment, zero-overlap rotation, twelve-hour
certificate-bound cookie sessions and owner-only revoke. Apps and relay remain
unimplemented; native OIDC and automatic QR trust bootstrap are deferred.
See [Mobile devices](/Internal/Mobile-Devices).

## Container integration adapters

`docs/decisions/0072-container-session-integration-adapters.md` keeps networking disabled while a gated session-only HTTPS
gateway filters public destinations. Selected provider access credentials reach a read-only sealed file, never
a shared home or refresh store. Immutable image hooks remain in force. Root/runtime/provider qualification
stays with the owner; defaults remain off. See [container adapter boundaries](/Internal/Security#gated-container-integration-adapters).

## Public server release candidate version

The owner selected **v0.15.0-rc.1** for the next public server candidate,
**immediately after feature freeze**, together with the public `rynhult/rynhult`
repository and rynhult.dev. The October 27 pre-release website deployment is not
the RC publication. The v1 platform scope from decision 0073 still describes the Linux
server used in browsers, plus Linux, macOS and WSL nodes; it does not require a
1.x release number or native app release. Package metadata remains at 0.14.0
until the separate release-time bump. See `docs/RELEASE-RC1-CHECKLIST.md` for the
bump inventory, owner publication approval and prerelease tag checks. The updater
accepts stable targets only; RCs are installed manually and sort below 0.15.0.
