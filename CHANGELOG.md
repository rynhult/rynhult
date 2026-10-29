# Changelog

Rynhult follows [semantic versioning](https://semver.org). Stable tags use `vX.Y.Z`; release candidates use
`vX.Y.Z-rc.N`. The installed version appears in **Settings → Updates**.

## [Unreleased]

## [0.15.0-rc.1] - 2026-10-29

These changes are included in **0.15.0-rc.1**, the first public release candidate of Rynhult, a self-hosted
supervisor for coding agents. This is not a stable-release announcement. See the
[RC release notes](docs/release-notes/0.15.0-rc.1.md) for installation, upgrade from 0.14 and known issues.

**v1 scope:** the self-hosted Linux server, browser access
including the phone web app, and existing Linux/macOS/Windows-WSL nodes.
Native apps follow v1 one platform at a time, in an order not chosen yet.
Existing desktop/mobile source is experimental groundwork, not a v1 app release.
See [decision 0073](docs/decisions/0073-v1-linux-server-scope.md).

Everything marked "off by default" stays off until an owner enables it. "Preview" means it is not fully
qualified. Containers and direct public remote access are Previews and off by default.
Ordinary browser access over a private network or VPN does not require the direct-public Preview.

### New name
- ACC is now **Rynhult**: new name, commands, Guide and brand colours (WCAG AA in light and dark). Existing installs
  keep working: the old commands, services and paths stay, and the new `rynhult` commands are added beside them.

### Remote access without a VPN (Preview, off by default)
- Pair phones and laptops as access devices with their own certificates. Revoking a device currently ends all
  sign-ins and push subscriptions for that person, not just that device's sign-in.
- An owner applies remote access from Settings with a matching-code check; a public status page shows what is live.
- Machines get their own identities, with rotation, cut-off and a backup of the device certificate authority.
- Lost-device and restore drills are documented.
- Machines that roam can still back up their projects' git history to the main they belong to.

### Backups
- New S3-compatible storage target, with optional independent retention upkeep and a "retention runway" that shows
  how long your backups can still be kept (off by default).
- Planned downtime for always-on machines: a backup that was missed while a machine was off runs once when it
  reconnects. A downtime window can also last "until it reconnects".
- The server backup is steadier: a short write pause instead of errors while a capture runs (the API answers "busy,
  retry" instead of failing), stale repository locks are explained and cleared safely, and failed or interrupted
  jobs say why.
- The backup space ceiling is saved when a target is prepared (half the free space, or 20 GiB).

### Teams and sign-in
- **Company sign-in with OpenID Connect** is an optional, off-by-default pilot, not a requirement for using
  Rynhult. It requires recent MFA proof and passkey enrollment. Real-company qualification is still pending;
  SCIM and SAML are outside v1. Invitations can be withdrawn, and removing company sign-in never removes an owner.
- **Groups**: a shared work board with claims and quotas for same-machine group sessions, and group machine accounts
  (off by default) that need the owner's approval and proof of control of the account.
- Tasks run on other machines now have a clear owner and are cleaned up.

### Orchestrator v2
<!-- lane F -->
- **On by default, not a Preview**, with owner-granted teams. Automated gate commands still need explicit approval.
- Selection follows the owner's ordered allow-list per task type; scores only break ties, not claim model quality.
<!-- lane C -->
- Role/profile assignments, attributable attempts and disposable helpers; replacements continue from earlier
  reports, handoffs and branch heads rather than losing partial work.
<!-- lane D -->
<!-- lane E -->
- A private, versioned project brief with captioned references, working-list questions and prepare/build modes.
<!-- lane B -->
- Gate evidence bound to branch head, base and commands; retry-passed is distinct from owner-listed known flakes.
  Stop team cancels running gates; expiry gives the lead a bounded 30-minute reporting wrap-up.
<!-- lane G -->
- Council uses managed helpers or labelled single-session perspectives. Native subagents use the parent's
  slot/permission, are not shown in the tree and cannot be capped by Rynhult.

### Agent sessions in containers (Preview, off by default)
- Each session can run in its own container, started by a trusted helper, with a read-only image, its own terminal
  connection, limits per account and checks inside the container. Phase 2 sign-in and HTTPS adapters still need
  real runtime/provider qualification; earlier host checks do not qualify them. Leave containers off outside an
  owner qualification window.

### Platforms
- Ubuntu 24.04: the installer lets Codex's sandbox work under AppArmor.
- WSL machines can be isolated further, and Windows machines that missed an update recover.
- Mac machines report their data volume only, not system or read-only volumes.
- Ubuntu 24.04+ and Debian 13+ with systemd are the qualified Linux server targets. Non-Debian-family nodes,
  including Fedora, openSUSE and Arch, remain experimental; test packages do not qualify those server platforms.
  Windows nodes use WSL2. RC downloads contain source, `BUILD` and checksums, not native app installers.

### In preparation (post-v1, not in this release)
- Desktop apps: a self-contained Linux app that runs sessions on this computer, plus groundwork for the Mac app.
  Neither is offered yet.
- Native Windows: experimental source and qualification groundwork, not a supported app.
- Mobile: experimental native enrollment/server and Android library code, not
  released phone apps. The existing browser-installed phone web app stays in v1.

### Domains and certificates
- DNS for publishing and certificates through Azure DNS, Route 53, Google Cloud DNS, DigitalOcean and Hetzner, besides
  Cloudflare. **Cloudflare is qualified for this release; the others are marked "Preview".**
- A qualification kit checks a provider against a disposable test zone.

### High availability
- A second main can take over by hand, with protection against two mains acting at once (needs PostgreSQL).
- Automatic failover is designed and runs as a dry run only: it shows what it would do, and never does it.

### Agents
- Codex questions and errors are recognised on real screens, including on phones; Claude's background work is no
  longer shown as idle; Copilot's working state stays accurate during quiet tools.
- Copilot is not started in a repository whose settings switch its hooks off.
- The command guard explains each refusal and covers PowerShell, script files and aliases.

### Using Rynhult
- Clearer screens throughout: Overview shows what needs you first; Settings, Setup, publishing, pull requests and
  workspaces were reorganised; Search and Jump to are distinct; alerts never cover the screen; a compact phone header.
- The terminal works with screen readers, can be left with the keyboard and has a text view.
- Workstations can sleep: Host shows what keeps a workstation awake, and idle sessions can pause and resume.
- Opt-in usage statistics and bug reports that you review before they are sent (off by default). The privacy
  notice describes what is collected.
- The project's collector at `telemetry.rynhult.dev` runs as a pre-release staging service; anything it
  receives before 1.0 is deleted at launch.
- Telemetry is not required for testing or feedback. Review the [privacy note](docs/PRIVACY.md) and the notice
  linked in Settings before opting in; scrubbing is best effort, and each bug report needs individual approval.
- RC feedback now opens [public Issues](https://github.com/rynhult/rynhult/issues/new), without an invitation.
  Use [Discussions](https://github.com/rynhult/rynhult/discussions) for questions, or
  [support@rynhult.dev](mailto:support@rynhult.dev) if you cannot file an issue.
  Never include secrets or logs containing tokens. Report security problems privately to
  [security@rynhult.dev](mailto:security@rynhult.dev); see [SECURITY.md](SECURITY.md).

### Security
- Security hardening includes:
  - keys can no longer move an agent tool's settings folder;
  - root helpers and Python child processes can't load code planted in a session's checkout;
  - keys can't attach to archived group projects;
  - leaving a group revokes its key consents;
  - multiline command aliases and checkout-controlled git transport rewrites cannot bypass the relevant checks;
  - release dependency builds run without the publishing job's write permission;
  - uninstall cleanup excludes the owner's login account and unrelated system services;
  - stronger report redaction and cleanup of incompatible native-phone sign-ins on schema downgrade;
  - team permissions are checked again immediately before each automated gate launch, including reruns.

### Fixed
- Reduced SQLite write contention; retryable backup-busy conditions are reported rather than hidden.
- Sessions list queries and repeat transfers are cheaper; unchanged idle sessions avoid repeated ingest work.
- Phone-pairing QR rendering and cancellation during in-app navigation; closing a tab still has expiry limits.
- Failed **Stop goal** requests remain visible instead of disappearing with the confirmation dialog.
- Restore preparation accepts equivalent historical numeric defaults while retaining the other schema checks.
- A race that could delay a catch-up backup.
- The agent `sudo` check reads sudo's own report.
- Windows installs install the main's copy, and the owner WSL user is detected.

## [0.14.0] - 2026-10-23

### Containers
- AppArmor: Playwright's headless shell gets the same user-namespace allowance as Chrome for Testing, so the browser tests keep their sandbox under Ubuntu's user-namespace restriction.

### Fixed
- Container checkout Git snapshots preserve the index timestamp, so racy same-size edits remain dirty and cleanup does not remove their work.
- New sessions on the main host launch again. The container launcher's run-folder hardening opened `/srv/acc/run` for reading, but it is traverse-only for agent accounts.

### Container qualification (owner-run; still NO-GO)
- Local Linux amd64 image recipe pins the base digest, verified Node checksum and exact Claude Code, Codex and
  Copilot CLI package integrities. No registry push or automatic host CLI update.
- Opt-in phase-1 harness uses the registered broker and trusted launcher with a separate owner-selected policy;
  normal session behavior and the default-off flag are unchanged. Real CLI tool executors consume offline
  loopback model calls; every mandatory check reports pass/fail/skip, and GO requires all passes in both generations.
- Check nested Codex bubblewrap, headless Chromium's renderer sandbox, network none, kernel resource controls,
  session stop/recreation and immutable effective inputs. The deployed writable hook/policy setup remains NO-GO;
  image build/runtime, credentials and the remaining security-review gates still require owner work.

### Containers phase 1 security review
- A repository's `.codex/config.toml` can no longer turn Codex hooks (and with them the guard) off: ACC ends every
  Codex command line with `-c features.hooks=true`, natively and in containers.
- Session image entries are root-only: no group/other write, set-id bit or ACL; the build refuses otherwise.
- Option A (container is the sandbox) also requires loopback as the only network interface, and `container.launch`
  audits `sandbox=container`. Qualification runs each CLI in a repository that tries to disable hooks and refuses GO
  while other processes of the payload account run. Open gate: the image's agent-owned rootless store.

### Container launcher security review
- Pin mount sources through private systemd staging; serialize lifecycle operations and quarantine leftover
  containers instead of trusting labels. Keep networking at `none` and suppress image-added writable volumes,
  healthchecks and runtime file logs. Bound helper resources, disk scans and separate audit buffers; import helper
  audit even when container launches are off, without recording private launch arguments.
- Host Git reads of container checkouts use bounded checked metadata snapshots; host Git writes are refused (use
  the session terminal). Recreate older unmarked container checkouts before qualification. Run-folder writes refuse
  links. Shared accounts with members use member defaults when no limits have been saved.

### Containers per session: the trusted launcher (off by default)
- New root helper `acc-containers` (socket-activated, one process per request; `deploy/install.sh` installs it with
  a reconcile timer). An account's broker asks it to launch, stop or check a session's container with six fixed
  fields; it acts only for the broker's own main process and only on a grant accd wrote for that session. It runs
  `systemd-run --uid=<agent> -p Delegate=yes` with the account's limits, so Podman runs as the agent account outside
  the broker's NoNewPrivileges. Only images pinned by sha256 in `/etc/acc/containers/policy.json`; only the session's
  private checkout and run folder as mounts. Failed starts are cleaned up, every outcome is audited into the Audit
  page, and a refusal is never a native fallback. Design: `docs/CONTAINERS.md`; decision 0038.
- `ACC_CONTAINERS=1` (off by default) lets owners switch a project's new sessions to containers on its page. Such
  sessions work in a private checkout whose branch comes back into the project when archived. With the flag off,
  or for any other project, sessions launch exactly as before.
- **Settings → Container limits** (owners): CPUs, memory, processes and disk per session, and sessions at once, per
  agent account. Members' accounts start at 2 CPUs, 4 GiB, 512 processes, 20 GiB and 4 sessions; owners' accounts
  are unlimited unless an owner sets limits.
- The broker runs git in a container's checkout only with hooks off and a safe `.git/config` (no fsmonitor, filters,
  textconv, includes, ...), since the container can write it.
- AppArmor profiles for newuidmap, newgidmap and pasta (`deploy/apparmor/acc-podman`, installed by an owner during
  qualification, not by the installer) and `scripts/qualify_containers.py --user-manager`. Phase 0 qualification is
  still open: credentials inside containers, immutable hooks, nested sandboxes and LAN egress denial are not done.

### DNS providers phase 1
- Owner-only Infrastructure Keys with separate encrypted custody and purpose-bound revisions, write-only mutation,
  current-password confirmation, strict legacy import and no project/agent/member/MCP/remote delivery.
- Settings DNS bindings with confirmed provider suggestions, separate certificate/Publishing references and explicit
  saved/applied states. Cloudflare only; Azure DNS and Route 53 remain unavailable pending phase 2 qualification.
- Confined private root helper with restricted atomic credential generations, candidate-aware validation/reload,
  rollback/recovery/restart, fixed errors, 24-hour retention and audit metadata. No live service changes in this task.
- Caddy sandbox, header/idle/dial timeouts and bounded private access logs omitting URIs and headers; internal CA
  remains the no-provider default. Pinned CI build inputs and offline checksum-enforcing binary installation;
  release is held until independently reviewed CI hashes and qualification evidence are committed.
### Groups: private Git exchange (phase 2)
- Same-host private placement clones and standalone session worktrees; explicit,
  bounded commit publication/fetch through authenticated Git HTTP, pack helpers and
  the account broker. Per-request staging keeps unrelated objects out of shared fetches.
  Short-lived one-use grants bind person/project/placement and current group authority.
  Writers only fast-forward their own published branches; viewers fetch published code.
- Integration consumes an exact current named-project-owner approval with an old-ref
  race/replay fence. Maintainers create immutable tags. No force, delete or arbitrary
  object fetch; hostile packs are quarantined and only selected history is promoted.
- **Groups → Projects → Published Files & Wiki** shows committed text without reading
  anyone's live checkout. Group and private recovery have separate authorization,
  identity checks, local snapshots and existing encrypted host backup capture/restore.
  Group restores create a new branch. Private recovery remains personal after removal.
- Group launches remain gated until phase 3's coordination, revocation, key cleanup,
  source review and quota checks. Own-machine collaboration remains phase 4.
- Security review (groups2): exchange waits for Git before taking the database writer,
  so a busy capture no longer blocks other ACC writes; promotions store only new objects;
  32 published branches per person with a maintainer reserve; decompression-bomb packs
  refused before Git inflates them; per-person grant and per-grant request limits;
  receipts for backup downloads and private recovery.


### Passkey sign-in
- Register, name, list and remove personal passkeys in Settings; adding/removing requires recent two-factor verification. Discoverable passkeys with user verification replace password plus TOTP at sign-in.
- Owners can Allow or Require passkeys separately for owners and members. Require checks spare owner credentials and offline recovery material; password plus a one-use recovery code preserves sole-owner recovery after losing every passkey.
- Exact configured origins and hostname-scoped RP IDs work behind Caddy and with a trusted private CA. Invalid origins fail cleanly. Pinned py_webauthn, persistent single-use challenges, shared sign-in throttles and audit events protect the ceremonies.
- Inviting an owner or resetting someone else's sign-in now needs fresh verification (a passkey, or password and authenticator where allowed, within five minutes) under any passkey policy. Refusals are audited, and the API token can no longer do either.
- Security review fixes: changing a role that requires passkeys needs the acting owner to verify again; someone else failing sign-in for your name no longer blocks your correct verification; a flood of sign-in attempts can't refuse everyone's passkey prompts; a passkey whose counter goes backwards (a possible copy) is refused and raised in the Inbox; recovery and relaxed passkey policy are raised in the Inbox for owners; policy audit records before and after.
- Software authenticator tests and Chromium virtual authenticator coverage include phone layout and accessibility. The Guide documents enrollment, separate hostnames, policy, resets and recovery.


### Backup server phase two: enrolled Linux targets
- Host → Backups prepares enrolled Linux targets and shows exact owner-operated root commands on **Target setup**.
  Pinned/checksummed rest-server 0.14.0 runs as a dedicated non-login account in append-only/private-repository mode.
  Setup blocks the existing main-controlled root update/package helpers; target updates need independent owner review.
  ACC never gains root, SSH or target maintenance credentials. S3 and other platform qualification follow later.
- Typed backup channels use the existing authenticated node link and root-owned target/installation binding.
  Separate transport and encryption keys; only ciphertext reaches the target. Checks prove deletion/overwrite refusal.
  Remote prune is refused, and append-only alone never claims timed immutability or global protection.
- Capture/readback, historical listing and isolated restore tests; offline recovery from administrator-exported ciphertext
  with `--repository`. Enabling the schedule requires a kit exported with the verified repository identity.
- Capture lock contention no longer sleeps on the asyncio event loop. Transactions there fail fast with retryable
  HTTP 503; workers retain bounded waiting, and failed BEGIN releases its lease.
- Security review fixes: restore, `--snapshot-id` and **Test restore** select a generation by snapshot ID, so a
  compromised main reusing an old backup ID cannot make it unrestorable; history flags duplicate IDs and a target
  that no longer lists the last verified backup, and captures to such a target stop. The main-side adapter is a
  private Unix socket instead of a loopback port other local accounts could reach. The receiver unit drops all
  capabilities and is loopback-only. Viewing target setup commands is audited.

### Machine roles and quiet reconnects
- Host and Add machine offer editable Always-on / Workstation roles. Enrollment and migration use OS defaults once; reconnects preserve the owner's choice.
- Disconnected links suspend background broker and spool work. Workstations show neutral offline state, sessions and projects say **Machine offline**, and scheduled occurrences skip with a clear note.
- Always-on outages alert after a configurable threshold (10 minutes by default), through Inbox, the status board and owner push notifications. Maintenance snoozes, reconnect resolution and recorded downtime are audited.
- Owners can **Check machines now** on Host and the status board: bounded live-link round-trips refresh snapshots and versions and report latency, unresponsive brokers or recorded offline state. Offline machines are never dialed; checks are audited and rate-limited.

### Adding machines
- Rerunning the Linux node installer from a source unpacked in a private folder no longer leaves ACC's folder unreadable to the node service (it failed with "Permission denied" and the machine went offline).
- The Linux node command refuses the main ACC machine before it downloads or installs anything. Run there by mistake, it used to upgrade Node.js, which restarted ACC's services, before it refused.

### Mac machines
- Agents on a Mac can use their tools again: the Mac installer points ACC's Python helpers (the guardrail hook among them) at Homebrew's Python instead of Apple's 3.9, under which every tool call was refused. Rerun the Mac installer to apply.

### Self-hosted git issue sources
- Owners can allow HTTPS git servers in Settings → Issue sources, confirm exact DNS addresses, upload a host-scoped
  CA certificate, re-confirm DNS changes and remove entries. Decisions are audited; private destinations require
  an allowed entry. Connections pin checked addresses and still refuse redirects, metadata and special addresses.
- GitLab self-managed and Gitea/Forgejo issues and pull requests support filtered lists and session launch with
  project-granted vault credentials and untrusted issue framing. Public HTTPS sources need no allowlist entry.
### Multiple Publishing addresses
- Owners manage labeled public/internal IPv4, IPv6 and CIDR targets in Settings → Publishing, with defaults, audited changes and review-only HTTPS/interface detection.
- Sites pin a selected host IP; existing replacement approvals update managed A/AAAA records. Apply rechecks membership and removal refuses targets in use.
- Existing origin environment values bootstrap the inventory once without changing current targets. Members cannot access the inventory or detection.
### Installing additional machines
- Windows Setup installs WSL itself before Ubuntu when the inbox `wsl.exe` is a stub, with a DISM fallback for older options and resume after a required restart.
- Installer failures show bounded, redacted log context and the full log path; Windows Setup adds **Open log**. Direct node installers keep logs too.
- Add machine lists Linux, Windows and Mac prerequisites before the command, with installation Guide links and phone-width accessibility checks. Guide links land on the requested section after loading. Installation guides cover missing WSL and firmware virtualization errors.

### Backup server phase one: capture and offline restore
- Owner-only **Host → Backups**: confirmed non-cached recovery-kit download, durable capture/prune jobs,
  verification and captured-data age, hourly scheduling, two-hour stale warnings and quarterly restore reminders.
  Keep the kit in a password manager plus one offline copy. Members cannot see or trigger these controls.
- Coordinated SQLite/secrets/catalog and git capture, signed versioned manifest, authenticated whole-backup
  restic encryption, readback and isolated validation. Official restic 0.19.1 is pinned/checksum-verified by
  `acc-packages`, never installed as a system package. Local/mounted directory targets only; always **backed up,
  not protected** without independent immutable/offline evidence. Retention 48 hourly/30 daily/12 monthly,
  10 MiB/s default, owner-only prune.
- `acc-restore restore` (or `python -m acc.backup_server restore`) validates an explicit kit/backup ID in private
  scratch before producing a fresh tree. `RESTORE-HOLD` starts only quarantine HTTP 503 before ordinary startup.
  DB/key epoch mismatches, tampered/replayed manifests, unsafe paths and wrong/missing kits are refused.
  Main-host workload state and private TLS/CA escrow need separate recovery. Remote targets and activation follow.
### Groups: authority and migration (phase 1)
- Owner opt-in group creation/archive, maintainer/member/viewer memberships, revision-fenced updates, named project maintainers and exact merge approval receipts. Maintainers manage existing users without instance privileges.
- Safe metadata and audit projections; legacy private-content routes remain closed to group access. Same-host placement reservations and immutable legacy session routing snapshots; explicit key-owner consent records with revocation, no key delivery.
- Groups page and Guide. Group launches, private clones, Git exchange, board, quotas and shared Files/Wiki remain gated for later phases. SQLite migration DDL and backfills roll back together.

### Finish pull requests in ACC (design: `docs/PR-FINISHING.md`)
- A **Pull request** tab on sessions of GitHub projects: create the branch's pull request (title and description prefilled from the session's handoff note or last summary, editable), edit it, mark it ready or back to draft, and request reviewers. ACC acts only when a person clicks, with their own sign-in: the dashboard token for owners, the person's agent-account `gh` login (new broker op `github_api`) for members.
- **Send failed checks** (failing step and job log tail) or **unresolved review comments** to the session's agent as one message. A preview shows the exact text first; it is redacted (vault keys and common secret shapes), trimmed to the 4,000-character limit and framed as untrusted input to evaluate.
- **Merge** is a person's confirmed action: only methods the repository and branch rules allow, refused while it is a draft, required checks haven't passed, it conflicts or is behind, or the branch moved; GitHub's branch protection decides last. Nothing merges automatically. Audited as `pr.create`, `pr.update`, `pr.ready`, `pr.merge` and `pr.send_to_agent`. GitLab merge requests are a follow-up to the GitLab issue source.
- New guardrail **No merging pull requests** (`merge_pr`), on by default for every session and switchable in Settings → Guardrails. It refuses an agent's `gh pr merge` in any form the guard parses (wrappers, `-R`/`--repo` anywhere, nested shells, gh aliases including `!` shell aliases, creating such aliases), and `gh api` calls that merge (non-GET `…/pulls/N/merge`, GraphQL `mergePullRequest`/`enablePullRequestAutoMerge`). Unclear forms are refused conservatively; reads like `gh pr view`/`checks`/`create` are allowed. ACC's own Merge button is unaffected. Sessions started before the update keep their rules until restarted.

### OpenCode, the sixth agent (preview; design: `docs/providers/OPENCODE.md`)
- **New session** offers **OpenCode** (2.x). Each session runs its own private OpenCode server (`--standalone`) and its own config folder, so sessions never share the account's background server or its global settings. ACC picks the session id, so resuming after a crash or restart reopens the same conversation.
- **Guardrails apply.** ACC's plugin calls `acc-guard` before every tool, including the calls OpenCode chains in Code Mode and MCP tools. A call the guard can't check is refused. A repository's own OpenCode settings can't switch the plugin off. If the plugin couldn't load, the session's shell refuses commands rather than running them unchecked. The guard also keeps agents out of OpenCode's credential database, `opencode auth export` and `.opencode/plugins/`.
- **Real state:** working, idle, failed and interrupted turns, permission prompts and the agent's questions ("needs you") come from OpenCode's own events through the plugin, with prompts on screen as a fallback. Interrupt sends Escape twice, as OpenCode wants.
- **Profiles:** instructions, skills, subagents and MCP servers go to the session's config folder. Options: **Permissions** (ask before commands, the default; ask before commands and edits; approve everything not denied, which needs confirming) and **Primary agent** (Build or Plan). Models are `provider/model`. **Model endpoints** work as a custom `acc` provider.
- **Models without a vendor sign-in:** keys from the vault under the provider's usual variable (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, …), a model endpoint, or `/connect` once in a session; free models need nothing. Its Providers card shows **Ready** and the connected providers.
- **Install:** `acc-packages` installs OpenCode pinned to the release line ACC was checked with (2.1, at least 2.1.3). The platform binary from npm is checked against the registry's SHA-512, and auto-update stays within that line. Setup and the Windows installer offer it.

### Session auto-resume and goals
- Optional launch and running-session settings resume a detected Claude/Codex/Copilot plan limit once per reset. Durable claims prevent duplicate continuations across restarts; typing cancels pending work. Unsupported providers explain why auto-resume is disabled.
- Goal panels show bounded continuations, hours, optional plan usage percentage, completion/blocked markers and Stop. Permission prompts, personal input and budget pauses stop or suspend automation. Session owners and ACC owners can edit settings; changes are audited.
- Separate literal text insertion and Enter submission for provider TUIs, including Codex. Migration 0036 and the new broker screen operation require updating each machine.

### Multi-run
- New session can send one task to 2–5 agent/profile/model choices, each as an ordinary session in its own worktree and `acc/<name>-<agent>-<id>` branch. Permission-skipping choices require explicit confirmation; budget refusals are reported per run without cancelling accepted runs.
- Sessions has collapsible group rows and independent session rows. The group page compares state, observed cost/usage, change summaries and branch-matched CI, with file-by-file Changes comparison for any two runs.
- Keep a winner, then separately confirm stopping and archiving the alternatives. Branches and Multi-run worktrees remain for later comparison. Member/owner scope and `multirun.start` / `multirun.keep` audit apply; migration 0037 stores groups and ordered attempts.
- Initial launches waiting for broker trust/start are excluded from recovery, preventing parallel launches from being mistaken for missing conversations.

### Session recovery
- Preserve failed-resume exit results when a CLI closes its terminal before exiting, including older tmux builds that miss child collection. ACC collects and records the exit status or signal before replacing the pane; retry limits and timeouts are unchanged.

### Optional Orchestrator (M17)
- People can grant the role in New session, off by default, with allowed agents/profiles and permission modes, machines, shared running/total/depth limits and expiry. Ordinary session defaults and launch files are unchanged.
- Token-free `acc-team` MCP requests travel through the caller’s spool; accd enforces membership, subtree access, budgets and whole-team limits. Helpers have worktrees; verified broker bundles carry work between approved machines.
- The lead’s Team tab shows the tree, states, branches, commits, usage, decisions and Stop team. Sessions/sidebar group helpers; helper prompts stay in the person’s Inbox. Team guards block main/master integration and PR merges. Migration 0032 and new broker operations require updating each machine.

### Security review #1 fixes

- Files pins the workspace directory through every operation and local commit, refuses symlinks leading to its root, and applies secret/git protection to registered roots as well as their children.
- Release verification and extraction stream under cumulative file/header/metadata limits, rejecting oversized or nested metadata before parsing and budgeting repeated global PAX fields.

### Scheduled sessions
- People can save and enable project sessions with their agent, profile, permissions and prompt, using once, daily, weekly or numeric cron timing in an IANA timezone. Dangerous permissions require explicit confirmation for the saved configuration.
- Schedules shows next runs, recent results and ordinary session links, with edit, pause/enable, Run now, Cancel run and delete. Budgets and active previous runs cause audited skips; failures raise personal Inbox items and existing push notifications.
- Migration 0035 stores schedules and stable run intents. Recovery runs at most once or skips missed timing; ambiguous dispatches fail visibly without replaying. Repository schedules are not supported and cannot run from repository files.

### Private development workspaces
- **Workspaces → New workspace → Development** creates a local-only git environment in the member’s own agent account. Sessions use isolated worktrees and branches, with Changes, Files and Markdown preview, reserved ports, approved Docker compose stacks, vault keys, budgets and internal git backup to the main ACC.
- Development sessions always refuse git pushes, remote add/set-url and remote repository creation; issue and PR views are hidden. Owners can convert stopped Research / Council workspaces. Explicit promotion preserves ports, vault grants, compose approval and backup identity.

### Advisory permission risk labels
- Inbox prompts and the session's **Needs you** banner show **Routine**, **Review** or **Risky** with a short reason. Rules reuse the guard's shell parser, structured tool fields and workspace paths; screen-only prompts say **Review: details on screen**.
- Labels never answer, approve, acknowledge or dismiss a prompt, change permissions, or call a model. **Settings → Show advisory risk labels** is on by default and saved per person across devices.
- Migration 0034 adds nullable advisory metadata to attention items. Existing prompts fall back to Review; guardrails remain unchanged.

### Tools on approval (M16; design: `docs/TOOLS.md`)
- Installed tools and project-local dependencies remain usable. The parsed guard command identifies account/system installs, upgrades, removals, installer pipelines and executable-folder writes, including wrappers; refusals explain `acc-request tool`.
- Validated registry package requests appear in the person's Inbox with destination, version and reason. Members approve their own account's tools; only owners request and approve system-wide tools. All decisions and results are audited.
- System tools use each machine's `acc-packages` helper; account tools use that person's unprivileged broker, including over node links. Providers → Tools records resolved versions, history and approved removal requests. Interrupted changes fail visibly instead of replaying.
- Owners can exempt a person or project in Settings → Tool approvals for newly launched sessions without changing other guardrails. Session instructions explain the rule. Migration 0030 adds approvals and installed pins; update every machine's helper, request CLI and broker.

### Explain these changes
- Changes offers an explicit, agent/model-selected walkthrough with ordered steps, exact old/new line links, navigation, provenance and a generated-content warning. The panel stacks above the diff on phones.
- One read-only, short-lived helper runs as the requesting person's signed-in CLI account, with no project keys, tools or inherited MCPs. Launch budgets, ownership checks, bounded input/output and strict JSON validation apply. Results are cached by source session and diff revision; regeneration is explicit.
- Helpers remain visible in Sessions and leave `walkthrough.json` in Artifacts. No automatic runs, recovery or extra model passes.

### Live preview

- Preview now works for projects on connected, updated machines over their existing authenticated link, including hot-reload WebSockets. Typed streams have bounded buffers, backpressure, cancellation and lower priority than terminals/control; nodes enforce fresh project reservations and loopback-only targets. Older nodes show update guidance. No new node listening port.
- Project and session Preview tabs embed running apps with a listening reserved-port picker, reload and open-in-new-tab controls. Previews use isolated private origins, short-lived project-scoped access and authenticated HTTP/WebSocket proxying; both Caddy certificate modes are supported.

### Files and editor
- Projects, workspaces and session worktrees have a Files view with lazy folder loading, sizes, git markers, bundled CodeMirror syntax highlighting and live Markdown preview; the tree collapses on phones.
- Broker-confined reads, bounded downloads and atomic saves run as the project's agent account, including over remote machine links. Secret entries stay locked, vault values are redacted and concurrent edits show a conflict instead of overwriting the file.
- Create, rename and delete files/folders with destructive confirmations. Save and commit uses your stored personal identity on the current local branch, preserves unrelated staged work and never pushes. Writes and commits are audited without contents.

### Linear and GitLab issue sources
- Project-scoped GitLab issues and merge requests, including nested groups and HTTPS self-hosted installations; Linear teams with optional project/label restrictions. Lists have state, label and assignee filters and pagination.
- Start a session from either source on the project page or in New session. Preview and launch fetch the title, description and link server-side and frame them as untrusted reference data.
- Credentials are project-granted vault keys, never plain settings; source grants stay server-only after disconnection. Requests are briefly cached, rate-limited and permission-checked before cache access.
- Optional Linear PR-link comments are off by default, tied to the session's own worktree branch and delivered at most once. Nothing is created, assigned, closed or merged beyond that opt-in comment.

### Catalog restore
- Owners can preview and restore one or all built-in skills, agents and MCP entries from this ACC version or a verified latest stable release. Text diffs, compatibility refusals, risk review, audit entries and rollback protect the change.
- Restored content lives in the writable catalog layer. Custom items win ID collisions; an explicit alternate ID keeps both copies. Profiles, overrides and MCP vault references are preserved.

### License
- **ACC is now under the MIT License** (was AGPL-3.0-or-later): use, copy, change and distribute it for any purpose, keeping the copyright and license notice. Settings still links the source and **Source of this version**. None of ACC's dependencies is copyleft (`THIRD_PARTY_NOTICES.md`).

### Updates (design: `docs/UPDATES.md`)
- Owner-only **Settings → Updates**: a daily check for the latest stable GitHub release, one Inbox item per version, plain-text notes, **Back up and update** with step results and rollback, and an opt-in daily installation time.
- Tagged releases publish the source bundle, `SHA256SUMS` and `BUILD`. The main and node helpers verify checksums, the source's build identity and a safe archive layout. A git checkout keeps updating with git and `deploy/install.sh`.
- Machines report their content build as well as their version. Owners update Linux and WSL machines over their signed link, one at a time or all at once, or let a machine follow the main ACC. Macs link to the new installer.

## [0.13.0] - 2026-10-22

Machines are run from the dashboard, every project is backed up to the main ACC, and the fixes from three pre-release reviews (Codex, Copilot and Claude) are in.

**Upgrading:** run `deploy/install.sh` on the main ACC, then update every machine (its broker gets new operations). Stacks that had Autostart on wait until an owner approves their compose files once. Rows created with the API token now belong to `@api`.

### Machines from the dashboard (M14)
- Sign-ins on any machine: each sign-in can pick a connected machine and one of its accounts, and runs there through the machine's link. Sign-in health is shown per machine.
- **Providers** has a machine picker that shows the installed agent CLIs and their versions, with **Install / Update / Remove**. Linux and WSL machines get the `acc-packages` helper, and the main ACC sends only catalog names and actions. On a Mac, CLIs marked as Mac-ready are installed for the signed-in user with Homebrew's npm. Owners only, audited and rate-limited. **Host** shows each machine's CLIs and sign-ins.
- The machine installer installs Node.js with `--install-prereqs`, which the Linux one-liner passes.
- A project on another machine runs as one of that machine's agent accounts. It uses yours if the machine has it; otherwise an owner picks one (**Register → Agent account**). Members always need their own account on the machine.

### Git backup to the main ACC (M15; design: `docs/MULTIHOST.md`, "Git backup")
- Every project with a git repository is backed up to the main ACC: one bare repository per project in `/srv/acc/git`, which refuses rewrites and deletions. Each project's machine (this one included) pushes every branch and tag every 15 minutes when there are new commits, and right after a session stops. A branch rewritten upstream is kept as `backup/<date-time>/<branch>`.
- accd serves git's smart HTTP for these pushes only (`/api/git/<id>.git/`, `git http-backend`). Each push gets a token minted for that one push and project, valid for 10 minutes and kept in memory only. Brokers pass it to git through its environment, never on a command line or in a file. The push trusts exactly the project folder (`safe.directory`).
- Project page **Backup** card: last backup, branches, tags and size, errors, rewrites kept, **Back up now**, **Download bundle** (owners and the project's member; audited) and an on/off switch (audited). **Host** shows the total size of all backups. `acc-backup` copies `/srv/acc/git` daily, hard-linking unchanged files.
- Settings: `ACC_BACKUP_URL` (default: the first https origin; none turns backups off), `ACC_GIT_DIR`, `ACC_BACKUP_INTERVAL`, `ACC_BACKUP_MAX_PUSH`. Caddy's 4 MB body limit skips `/api/git/`. Migration 0022 (`project.git_backup`). New broker operations `git_refs` and `git_backup`, so update every machine.

### Identity and access (pre-release reviews, F1)
- Rows created with the API token belong to `@api`, which can never be a username (migration 0023). Names such as `api`, `acc` and `root` are reserved. A removed person's username is retired for good (0024), and their things go to the person who removed them or are archived.
- Built-in local workspace projects have their own identity (0025), checked when they're reused.
- Members never run as an owner's, a service's or another person's agent account. A member's runtime check isn't a folder probe, and `/api/host` leaves out host-wide readings for members.
- Lockout can't lock out an owner who signs in correctly. IPv6 rate limits apply per /64. Fewer responses reveal whether something exists, and private key names stay out of the audit log and launch errors.

### Installers and root helpers (F2)
- Caddy's admin API is always a Unix socket. `install.sh` refuses a config with a TCP admin listener or none at all.
- Root never follows links owned by `acc` in `/srv/acc/data`, and walks shared folders as their owner. It runs only a uv binary that root owns outside home folders, or the pinned one. The web build and npm run unprivileged. Apt keys for NodeSource and Docker are checked by fingerprint, and the Caddy build with the Cloudflare module needs a reviewed SHA-256.
- Quoted Mac hook paths. The GitHub MCP binary is checksummed.

### Guardrails (F3, D14)
- **An agent can't switch off its own guardrails.** At launch the broker records the policy files' hashes (`ACC_GUARD_FILES`). The guard refuses a changed, missing or invalid policy, and blocks tool calls that write, move or delete it.
- One 4-second deadline for every check. ACC's own hooks stay on where the CLI allows it.
- The secrets rule covers everything the Guide says, and custom patterns also match the parsed words.
- **The parser understands more command forms**:
  - abbreviated options (`rm --rec`, `git push --force-w`);
  - command names that are only known when the command runs;
  - `find -exec`, `script`, `su`, `flock` and `busybox`;
  - git aliases given on the command line;
  - `git subtree push`;
  - the working folder a tool call names.
- The Guide, README and Security page say what guardrails are: a safety net against mistakes and casual prompt injection, not a sandbox.

### Data, secrets, backups and stacks (F4)
- Git backups: every backup repository has an `update` hook that ACC writes and keeps. It refuses deletions, replacing an existing tag, branch updates that aren't fast-forwards, and refs outside branches and tags, whatever a pusher sends (X5/Q6). Existing repositories get it when accd starts.
- Git backups record the project's slug and creation time, so after restoring an older database, a reused project id no longer serves another project's backup. That backup is moved to `/srv/acc/git/orphaned/`.
- **A publish request can't silently retarget a live site** (D18). It shows the site it would replace and needs **Replace the live site** to approve. Agents' requests are bound to their own project's reserved ports and sites, whoever started the session.
- Machines: one address can hold at most 4 of the 32 unauthenticated handshake slots, and a node has 5 seconds to answer the challenge. A host on the network can no longer block every node's reconnect (D19).
- **Keys are on disk only while a session runs** (D21). Stopping, exiting or archiving removes `session.env` and any MCP files that hold values, and a resume writes them again. A machine that was offline gets the removal when it's back. Backup mirrors skip `session.env`.
- **Replacing or deleting a key no longer stops its redaction** (D22/Q5). The old value stays sealed for the sessions that had it, so it never reaches search, exports or Activity in plain text.
- Redaction also catches a key's percent-encoded and base64 forms (including HTTP Basic credentials) and values cut short. The copy of a remote session's spool is redacted as it's written.
- Backups: the docs say plainly that a backup holds the database next to the key that decrypts its vault. `ACC_BACKUP_PASSPHRASE_FILE` encrypts each day's secrets with a passphrase (D24).
- Machines: sessions on another machine are compiled with that machine's own paths, which its broker reports, so Claude finds its settings on a Mac or an `/opt/acc` Linux node. A broker refuses a launch that names another folder (Q4).
- Migrations 0027 to 0029 (compose approval, publish context, retired vault values).
- History search: sessions whose transcripts can't be read no longer stop older sessions from being indexed (Q11).
- **Autostart runs only compose files an owner approved** (D4). If the files change later, the stack is held at its next start, an Inbox item is raised, and the project card shows the diff until an owner approves it.

### Terminals and UI (F5)
- Demoting someone closes their terminals on other people's sessions at once.
- Terminal attach and backpressure fixes. An unavailable broker no longer leaves a dead browser in control.
- Compose approval is easier to find: **Approve compose files** with a *Not approved* / *Changed* badge, on the project card and in a new **Docker stack** card on the project page.
- Login dialogs and focus fixes. Activity is readable on phones and shows when a query fails.

### Open-source readiness (F6)
- Tests and code defaults use artificial names, domains and addresses. `scripts/public-snapshot.sh` builds a clean public tree with no history (`docs/PUBLISHING-THE-SOURCE.md`); nothing has been published.
- `SECURITY.md` for private vulnerability reports. The `@claude` workflow reacts only to the repository owner, read-only, and never on pull requests. Migration 0026 (models no longer default to an operator account).

### Also
- **Copilot "needs you" comes from its approval prompt on screen**, as Cursor's and Antigravity's do. Its permission hook fired before every tool, even approved ones.
- Reviews and outcomes are in `docs/reviews/2026-10-22-prerelease-*`.

## [0.12.0] - 2026-10-22

One installer per system, a key vault, and ACC is now free software under the AGPL.

### License
- ACC is licensed under the **GNU AGPL v3 or later** (`LICENSE`): use it, change it and run it, including commercially; anyone who distributes it or offers a changed version over a network shares their source. **Settings** links the source (`ACC_SOURCE_URL`); `THIRD_PARTY_NOTICES.md` lists what ACC builds on.

### One-step install (design: `docs/INSTALLER.md`)
- **`deploy/acc-install`:** a terminal wizard for Linux: **Main ACC** (setup.sh's questions in dialogs) or **Additional machine** (paste the command from Add machine). `--main`, `--node '…'`, `--yes` and `--dry-run` for scripts; the pasted command is parsed strictly and never run as given. `setup.sh` gets `--dry-run`.
- **Add machine** has a tab per system: one command on Linux that downloads ACC from this ACC and installs and enrolls the machine (with a private CA it checks the CA's fingerprint before trusting anything), and the installer for this version plus the command to paste on Windows and Mac. A machine on another ACC version is marked on the Machines card.
- **Caddy's CA is automatic:** without a Cloudflare zone, `deploy/install.sh` copies Caddy's root to `data/node-ca.pem` and sets `ACC_NODE_CA_FILE`. **Settings → Certificate** shows the certificate and offers `acc-root.crt` and `acc-root.mobileconfig` with steps per device.
- `GET /api/nodes/bundle` serves this ACC's source at its version (built at install), which is also **Source of this version** in Settings; `GET /api/nodes/install.sh` serves the Linux bootstrap. Both are rate-limited.
- **Windows installer:** `ACC-Setup-<version>.exe` (unsigned, Inno Setup, built in CI) sets up a **main ACC** inside WSL2 (with mirrored networking so other devices reach it) or an **additional machine**; it handles the one restart WSL may need. Guide: **Install on Windows**.
- **Mac installer:** `ACC-<version>.pkg` (unsigned, built in CI by `.github/workflows/installers-macos.yml`) installs ACC and opens **ACC Setup**, which takes the command from **Host → Machines → Add machine**, checks it strictly (it is never run as given), installs the node for the signed-in user and enrolls it. Open ACC Setup again to update or enroll again. Guide: **Install on a Mac**.

### Key vault
- **Keys:** API keys stored once, encrypted (AES-256-GCM, `data/secrets/vault.key`), named, and given to projects (archived ones too). Add one or paste a `.env` under **Settings → Keys** (values are never shown back), give keys on a project's page, and see in **New session** which variables a session will get. Private or shared; sessions on other machines get a key only when its owner allows it. Design: `docs/VAULT.md`.
- Sessions get the keys at every launch and resume through their 0600 `session.env`, never on a command line, in the launch spec, the database or the logs; their instructions name the variables (never the values). Two keys with one variable refuse the launch.
- What agents produce is stored and shown with key values replaced by `«key: NAME»` (Activity, doing now, Changes, search, export). The secrets guardrail also blocks reading `session.env`.
- MCP server secrets in the catalog are vault keys now; existing ones move into the vault once when ACC starts.
- Migration 0021 (`vault_key`, `vault_grant`, `session.vault_keys`). `deploy/install.sh` creates `vault.key`; restore it with the database (see Operations).
- Guide: one **Install** page for both roles on Linux, Windows and Mac.


### Also
- **Codex runs with its sandbox on:** an AppArmor profile lets Codex's own bubblewrap create the namespaces Ubuntu otherwise restricts (installed by `deploy/install.sh`).
- **Guardrails:** agents may clean up their own temporary files (in `/tmp` or `/var/tmp`, when everything removed is theirs), including through `S=$(mktemp -d)`-style variables set earlier in the same command.
- **Budgets:** a demoted or removed owner's budget is deleted, with its alerts.
- **uv** is installed from a pinned release checked by SHA-256 (no installer script piped into a root shell).
- CI uses `actions/checkout` v7.0.1.

## [0.11.1] - 2026-10-22

### Fixed
- Projects created before v0.9.0 had their machine recorded with an installation-specific machine name; since 0.11.0 only `local` means this machine, so their sessions were sent to a machine that doesn't exist and couldn't launch. Migration 0020 sets every project whose machine isn't an enrolled one back to `local`.

## [0.11.0] - 2026-10-22

Several machines from one dashboard, a stricter guardrail, and the findings of an overnight review by Claude, Codex and Copilot sessions run through ACC.

### Several machines
- **Machines:** add another computer (a Linux server, a Windows PC through WSL2, or a Mac) under **Host → Machines**. It enrolls with a one-time code and keeps an outbound, signed connection to this ACC; projects on it run their sessions there, with the same terminal, activity, Changes, guardrails and Inbox. People still choose the machine and the agent. Design: `docs/MULTIHOST.md`.
- **acc-node** and installers for Linux (`deploy/node/install.sh`), Windows 11 (`deploy/node/windows/install.ps1`, through WSL2) and macOS (`deploy/node/macos/install.sh`), each with a dry run; a CI workflow checks them on all three systems.
- Machines check this ACC's certificate the normal way, or pin a private root CA by fingerprint (`ACC_NODE_CA_FILE`), so certificate renewals don't break them.
- Hardened from a security review of the design: what a machine can make this ACC hold is bounded (frames, buffers, queued bytes, remote event mirrors), a slow or broken machine can't hold up the others, removing a machine ends its connection at once, machines cap what this ACC can open on them, and the Linux installer never runs code another user can change. Known limits: `docs/MULTIHOST.md`.

### Sessions
- **Start a session from a GitHub issue or pull request** on the project page: New session opens pre-filled (own worktree, a name, a first prompt using `gh`); you choose the agent and press Launch.
- **Session tools:** open the session's folder in VS Code (Remote-SSH), its branch with the open pull request and CI, comments on diff lines sent to the agent, and an export of the whole session as Markdown.
- **Artifacts tab:** screenshots, short videos, test logs and reports an agent leaves in `.acc/artifacts/` for its reviewer (the acc-workflow skill asks for them), read without following links, size-limited, and served so a browser never runs them; also for sessions on another machine. `.acc/` stays out of git through the repository's own `.git/info/exclude`.
- **Profiles on every agent:** Cursor and Antigravity sessions now receive the profile's instructions, skills, subagents and MCP servers through a per-session plugin (verified live); the New session dialog lists anything the chosen agent can't receive instead of dropping it silently.

### Guardrails
- `acc-guard` reads shell commands structurally (quotes, heredocs, subshells, `$(...)`, `sh -c`, `eval`, wrappers such as `sudo`, `env`, `xargs`, git global options and push refspecs), with a 329-case test corpus. A command too deeply nested to read is refused rather than allowed.
- An edit tool's patch text is no longer mistaken for a shell command.

### Fixes
- Copilot and Antigravity take a first prompt with `-i` (their sessions exited at launch).
- Claude Code's one-time Bypass Permissions dialog no longer holds an unrestricted launch; a session whose CLI never reports starting shows under **Needs you**.
- Copilot sessions no longer see every catalog skill (Codex's skill links moved to `~/.codex/skills`).
- Correctness review: worktrees holding ignored files are kept on archive; stop and archive record the intent first; a failed launch keeps its session and worktree; +5% on a budget is saved; unreadable transcripts are retried; Cursor's SQLite changes are noticed; failed git reads and screen captures are errors instead of "nothing".
- UI and API review: ten fixes, among them budgets of a demoted owner go inert, usage readings past their reset neither pause nor alert, and a worktree with a local workspace is refused before anything is made.
- Accessibility pass on the new pages (no axe violations).
- The sessions API uses `sandbox`, `approval` and `permission_mode` sent inside `options` (they were ignored).

### Security
- Wiki, knowledge and transcript reads walk folders without following links, refuse special files and read within a size limit.
- Catalog frontmatter is parsed as strict YAML and rebuilt from an allowlist at launch.
- Model discovery reads at most 2 MiB within a deadline.
- Run folders are walked without following links; GitHub views skip members' projects.
- Cursor's conversation store is read from a private copy, never opened by the agent's path.
- A release review of tonight's last changes: artifact replies from another machine are checked here against this ACC's own limits, a machine's relay has total queue budgets, an artifact listing looks at a bounded number of entries, and the git exclude file is never written through a link.
- Model endpoint URLs are checked strictly, and a key for a plain-http server on another machine needs a confirmation.

### Documentation
- Guide: **Nodes**, **Host → Machines**, several machines in **Features** and **Projects**, sessions from issues; fresh screenshots.
- `docs/reviews/` (correctness, security, UI/API, multi-host, guardrail gaps, skills per provider) and `docs/research/` (competitors, open-source checklist, node test plan).

## [0.10.0] - 2026-10-22

More agents, and better ways to watch and steer them. Ideas from CodeAgentSwarm, kept to ACC's rule that people decide what runs.

### Agents
- **GitHub Copilot CLI**, **Google Antigravity CLI** and **Cursor CLI** alongside Claude Code and Codex: a provider registry, per-agent launch options, sign-in from the dashboard (device code, pasted code or a link), Copilot's plan and AI credits, and the models each login may use. All verified end to end on real accounts.
- **Model endpoints:** run Claude, Codex or Copilot on a local LLM (Ollama) or another compatible API.
- **acc-packages:** a sandboxed root helper installs, updates daily and removes the agent CLIs (and Ollama) from the dashboard.
- Gemini CLI dropped: Google retired it in favour of Antigravity.

### Watching sessions
- Live sessions grouped into **Needs you**, **Active** and **Idle** in the sidebar and on the Overview.
- A **doing now** line on every session ("Running npm test", "Editing src/api.py", "Asking to run …"), from each agent's own tool events.
- **Needs you** for Cursor and Antigravity from their approval prompts on screen (neither has a permission hook).
- **Changes** tab: everything a session changed since it started (commits, edits, new files), with "+N −M · K files" on its row.
- **Grid:** up to four live terminals side by side.
- **Search:** every conversation (prompts and replies), for every agent, archived sessions included.

### Steering safely
- **Guardrails** for every agent, whatever its permission mode: no force pushes, no pushes to main/master, no recursive deletes outside the workspace, no reading secrets, plus owner-defined blocked commands. Blocks reach the agent with the reason, the Inbox and the session's activity.
- **Worktrees:** a project session can run in its own git worktree and branch; archiving removes a clean worktree and keeps the branch.
- **Budgets** (opt-in, personal, none by default): a cap or a pace per usage window; notify, or also pause new launches of that agent (confirmable per launch).

### Fixes
- Inbox items from sessions started with the API token now reach owners (they belonged to a pseudo-user nobody signs in as).
- A busy database could drop a hook event; ingest now retries the line instead.
- The supervisor no longer holds the database write lock while waiting for the broker.
- Antigravity sign-in (it reads the code only from a terminal) and its hooks (agy runs only flat handler lists for non-tool events).

## [0.9.0] - 2026-10-21

The first versioned release: everything built from the first commit (2026-09-19) to today, made installable on any machine.

### Install and setup
- `deploy/setup.sh`: first-time setup on Debian or Ubuntu (dependencies, this machine's settings, then `deploy/install.sh`), interactive or unattended (`--yes`).
- Nothing is tied to one machine any more: the installer reads `/etc/default/accd`, the Caddy site is rendered from templates (a Cloudflare wildcard zone, or Caddy's own certificates without a domain), the host snapshot finds the disks itself, and backup mirrors are configured in `/etc/default/acc-backup`.
- The version shows in **Settings** and in `/api/auth/state` (signed-in users only).

### Sessions
- Native Claude Code and Codex CLI sessions in tmux, supervised through the CLIs' own hooks: states, a bounded automatic resume of the same conversation, and Remote Control links.
- The embedded terminal: one writer at a time with **Take control**, a phone key bar, and closing on sign-out or a revoked sign-in.
- New-session dialog with profiles, models, permission modes, skills and MCP servers; observe-only external sessions.

### Projects, workspaces and wikis
- Register, import or create GitHub repositories (protected `main`); GitHub views: CI, pull requests, issues, commits.
- Local research workspaces with promotion to a project, and deletion with a typed confirmation.
- Project wikis (Azure DevOps layout) and the **Guide** tab: ACC's own documentation with screenshots.
- Port registry (dev 8000–8099, prod 8100–8199) and per-project Docker stacks with Autostart.

### People and security
- Sign-in with password and TOTP, `__Host-` cookies, HSTS, sign-in throttling, and a list of your sign-ins with revoke.
- Owners and members: one-time invites, per-person agent accounts (created from the portal), each with its own broker, logins and folders; members see only their own work.
- Sandboxed root helpers for publishing, accounts, host state and backups; secrets placed in MCP configs at launch; MCP servers installed from a lockfile; catalog imports checked for risky content.

### Operations
- Inbox with verified signals only, Web Push notifications, and installation as an app.
- Publishing through Caddy and Cloudflare DNS with approval; host health (disks, SMART, GPU, services, containers, agent accounts) for everyone; per-provider login health and plan usage; audit log; daily backups.
