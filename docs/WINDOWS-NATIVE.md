# Native Windows ACC

**Status: proposal for owner approval — 2026-10-23. Documents only; no native Windows runtime or installer is delivered.**

**Release timing updated 2026-10-27:** native Windows comes **after v1.0**,
not immediately after rc.1. The native-platform order is not chosen yet; candidates
are native Windows, macOS standalone desktop, Linux desktop, iOS and Android.
For native Windows, Codex is the first provider under decision 0073.
v1 is the self-hosted Linux server used through browsers, including the phone
web app, with existing Linux/macOS/Windows-WSL nodes supported as today.
Existing native/desktop source and probes are groundwork, not a qualified
native release; the technical proposal and recorded evidence below remain.
See [decision 0073](decisions/0073-v1-linux-server-scope.md).

Machine labels in the recorded qualification notes below are anonymized;
the original operator evidence remains private. Anonymization changes no result
or qualification status.

Owner question: “Right now we're dependent on WSL, is there any possibility that this can be ran windows native?”

**Yes, technically. Recommend a native Windows node first, keeping the main ACC on Linux/WSL.** Windows has suitable terminal, service, identity and IPC primitives. The work is a broker/runtime port, not an installer switch. Budget **10–16 engineer-weeks for a qualified node with Claude, Codex and Copilot**, another **3–5** for Cursor, Antigravity and OpenCode, and **6–10 more** for a native main. These are engineering estimates, not measurements or delivery commitments; owner machine time, vendor access and security review can extend elapsed time.

“Native” here means Windows processes and Windows files, with no WSL distribution or Linux VM required. Git for Windows/Git Bash is compatible with that definition; it is not WSL. Windows Sandbox is an optional VM-based alternative, not the proposed default. Linux-only project toolchains will still need a Linux machine.

## Current implementation and scope

[Multi-host](MULTIHOST.md) already puts session work behind per-account brokers and an outbound authenticated TLS WebSocket. Both [node PowerShell setup](../deploy/node/windows/install.ps1) and the [Windows wizard](../deploy/windows/AccWindows.psm1) install Ubuntu 24.04 in WSL2, enable systemd, copy code onto Linux storage and invoke Linux installers. The main role also configures mirrored networking and a Hyper-V firewall rule. A logon task keeps WSL running while the Windows person is signed in. An `.exe` installer today does **not** imply native ACC execution.

The [node client](../src/acc/node/client.py) uses `asyncio.open_unix_connection` and discovers `*.sock`; [configuration writes](../src/acc/node/config.py) use `fchmod`. The [broker](../src/acc/broker/server.py) imports `pwd`, authenticates peer UIDs, and supports systemd socket activation. [Operations](../src/acc/broker/ops.py) depend on tmux, POSIX paths/modes, shell launch commands and Unix file-opening guarantees. [PTY attachment](../src/acc/broker/attach.py) and [pty_exec](../src/acc/broker/pty_exec.py) depend on Unix PTYs, `fcntl` and `termios`. Installing Python on Windows cannot run those paths unchanged.

**Dedicated WSL node isolation is implemented as an option** (owner approved
2026-10-24). `deploy/node/install.sh --wsl-isolate` disables interop and restricts
Windows drives to root through a backed-up merge of `/etc/wsl.conf`; adding
`--wsl-disable-automount` is a second explicit choice. Interactive installs default
to No and updates never change it. Host reports effective interop and per-agent
drive access and offers an audited owner dismissal for accepted-risk development
boxes. This reduces the WSL bridge, not a native Windows or per-session sandbox.
See `docs/wiki/User-Guide/Install-Windows.md`, "Isolation for dedicated WSL nodes".

The roadmap's native mains item is an aspiration, not evidence of a port. [Containers](CONTAINERS.md) and owner answers 0008 (private decision record) approve Linux session containers and owner-only native project exceptions with a **not isolated** badge. Native Windows must use that explicit exception until a separate Windows isolation boundary qualifies. This proposal does not revoke that policy or claim that the approved containers are implemented today.

## Component replacement matrix

Maturity below distinguishes established OS primitives from **ACC integration, which is unimplemented throughout this proposal**.

| Component | What WSL/Linux supplies today | Proposed native replacement | Maturity and principal risk |
|---|---|---|---|
| Interactive terminal | tmux per account; detached sessions, capture-pane, send-keys, process exit status; Unix PTY per viewer | Long-lived per-account terminal host using ConPTY, initially via pywinpty; small signed native host if service/token/handle behavior requires it | ConPTY is established; pywinpty is an established binding, not a multiplexer. High integration risk: ACC must own persistence, snapshots and input arbitration. |
| Account lifecycle | `useradd`, locked passwords, `runuser`, no sudo/docker groups; `deploy/accounts` root request helper | Dedicated standard local Windows user/SID per person, broker Windows service per account, private profile loaded for provider credentials | Windows users/services are mature. High: service logon, password rotation, Credential Manager and noninteractive OAuth need proof. |
| Permissions and limits | Linux ownership/ACLs, systemd `NoNewPrivileges`, optional cgroup CPU/memory/tasks limits | NTFS explicit DACLs, restricted privileges, service ACLs; Job objects for process trees, CPU/memory/process limits | Primitives mature; policy unqualified. Job objects do not restrict file/network access or isolate same-account sessions. |
| AppArmor / session sandbox | Targeted Codex bubblewrap/Chrome profiles permit user namespaces, not general ACC confinement; future rootless containers | No direct AppArmor replacement. Optional AppContainer/restricted-token policy after compatibility work; Codex's own Windows sandbox only for Codex | High security gap. Do not call ACLs, Job objects or hooks a general sandbox. |
| Broker IPC | Unix sockets, protected socket directory, kernel peer UID; systemd owns listener | Local named pipes with explicit DACL, client token/SID validation and protected endpoint registry | Named pipes mature; authentication adapter/security review required. AF_UNIX on Windows is an alternative, not a drop-in for Linux peer credentials/Python asyncio. |
| Run folders and hooks | Per-account run dirs, modes, atomic rename; `/bin/sh` hook sink/statusline; Python guard | Broker-created NTFS run tree, explicit interpreter/executable hook launchers, native JSON spool/statusline writer | Medium/high: Windows rename/open-file semantics, concurrent append, quoting, encoding, owner tampering. |
| Node link | Ed25519 identity; CA/hostname-verified outbound WSS; bounded binary broker channels | Retain wire format/authentication/budgets; replace local connector/discovery and state-file permissions | Low protocol risk. Key protection and Windows service recovery remain integration work. |
| Packages and ACC updates | apt/npm/vendor manifests, root-owned tools, systemd path/timer helpers and staged updates | Approved Windows catalog with exact IDs/versions using MSI/vendor binaries, npm or winget where qualified; privileged installer service for system actions | Tools mature; automation under service identities is high risk. No arbitrary installer text from agents. |
| Git / backup sender | git worktrees/refs; HTTPS pushes with short-lived project token and private CA | Git for Windows, native paths and process environment; keep backup receiver on Linux main initially | Medium: null-device, hooks suppression, locks, long/case-colliding paths and executable-bit differences. |
| Files / Changes / artifacts | POSIX allowed-root and no-follow file operations | Handle-based Windows root containment and reparse-point validation; ACL-separated roots | High security risk: junctions, symlinks, UNC/device paths, alternate data streams and races. |
| Live preview | Loopback project reservations relayed over node WSS; main handles browser auth/origins | Same leased loopback HTTP/WebSocket relay; native Windows dev-server process and firewall qualification | Medium: port ownership, IPv4/IPv6, sleep/resume, leaked LAN bindings. |
| Main ACC | accd + Caddy + SQLite plus Linux privileged helpers | Python/ASGI and Caddy Windows services, SQLite on local NTFS; separate constrained privileged helper service | Core dependencies portable; full main high risk due to installer, helpers, backups, networking and recovery. |

Microsoft documents [ConPTY hosting](https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session), [pipe security](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights), [client identity via impersonation](https://learn.microsoft.com/en-us/windows/win32/ipc/impersonating-a-named-pipe-client) and [Job objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects). Those primitives support this design; their existence does not validate ACC's future security policy.

## Terminal ownership and recovery contract

Separate the terminal host from the request broker, so a broker restart or dashboard disconnect leaves the ConPTY and child process alive. Services must start at boot without an interactive Windows login; prove ConPTY in service Session 0 before committing to the architecture. Keep terminal-host handles out of agent processes, avoid inherited service credentials, and assign children to a non-breakaway Job before they can spawn descendants. The terminal host, not a viewer, owns termination and the Job handle. A UI detach only closes that viewer.

ACC needs a bounded VT screen model plus scrollback, sequence-numbered output, a snapshot/replay handshake without gaps or duplication, resize arbitration, Unicode/alternate-screen support and a defined scrollback retention cap. Disk scrollback needs account ACLs, quotas, rotation and secret-aware retention. A byte log alone does not reproduce tmux's current screen; use the same screen model for permission-prompt detection and automation. Preserve read-only viewers, one authorized input controller, literal paste, provider-specific interrupt keys and exit status. Keep the existing attach wire contract where possible; negotiate a capability/version for snapshot additions rather than assuming old mains understand them.

Dashboard/main restart, network loss and request-broker restart must preserve live terminals. Terminal-host crash, reboot or power loss may kill the CLI: record that state and resume the existing provider conversation, never pretend to keep its process alive or silently create a new conversation. Fence session IDs/host generations to prevent duplicate launches during reconnect or upgrade. Keep a terminal host running during broker updates or explicitly defer the update until sessions stop.

Evaluate pywinpty first for launch/read/write/resize; it supplies no durable reattach itself. A small C++/Rust host gives precise handle/token control at the cost of another signed binary. WezTerm or another multiplexer is a spike alternative only if it proves unattended Windows service operation, stable attach/control APIs, process recovery and account separation. Windows Terminal tabs alone do not supply ACC's server contract; a Cygwin/MSYS tmux port adds a compatibility runtime and should not become the default dependency without qualification.

## Identity, IPC and filesystem trust boundaries

Use three authorities: a private acc-node service identity holding the machine key; one standard local user and broker/terminal service per person; and a tightly scoped privileged installation/account helper. No agent runs as Administrator, LocalSystem, the desktop owner, or acc-node. Agent identities get service logon only, not interactive/RDP logon, and no privilege to install/change services or impersonate privileged tokens. Service credentials belong to the Service Control Manager/OS-protected storage, never config JSON or agent environment. Verify actual token privileges instead of assuming a standard user name is sufficient. If secure unattended per-account startup cannot be demonstrated, stop the pilot rather than falling back to the owner's desktop account.

Admin-owned binaries/configuration live under `C:\Program Files\ACC`; private machine state under `C:\ProgramData\ACC\node`; per-account workspaces and runs under SID-mapped roots. Disable broad inherited write/read grants where inappropriate. Node can reach only registered brokers; agent accounts cannot read the node key, other profiles, privileged request results or installer state. Current Linux same-account ownership already allows tampering with some hook files despite mode bits and hashes. Stronger immutable Windows policy requires a trusted writer distinct from the agent SID: read/execute only for agent, no owner/parent delete or ACL-changing authority. A broker running as that same SID cannot establish this boundary by itself.

Pipe names derive only from an administrator-controlled account-to-SID registry, not a path supplied by the main. Require explicit local-only access (`PIPE_REJECT_REMOTE_CLIENTS`), DACL grants only to intended client/service identities and administrators, first-instance protection and server identity validation against pipe squatting. Inspect the connected client's token/SID using documented APIs; do not trust an account name or PID in JSON. Use identification-level client access where possible, minimize impersonation privileges, and always revert impersonation before dispatch. Fail closed if token validation fails. Preserve method-level roots/provider/role checks and framed-stream limits; transport authentication alone does not authorize a file operation.

AF_UNIX could reduce framing changes but requires explicit research into available peer identity APIs, path ACL semantics and supported Python event loops. Do not replace peer-UID checks with “socket exists” or a localhost unauthenticated TCP port.

Replace colon-separated roots (drive letters contain colons) with a versioned list. The Linux main must compile **Windows target paths**, not use its own `Path`/`shlex` conventions. Brokers advertise OS, architecture, path flavor, hook launch format and supported capabilities in layout/version negotiation. Refuse incompatible primary/node/provider combinations before launch; do not guess slash substitutions.

Use final handle paths/file IDs and containment checks, not string prefixes or `resolve()` alone. Reject unsupported UNC/network shares, device namespaces, ADS and reparse points for v1; test swaps between validation and open. Cover case folding, drive-relative paths, reserved names, trailing dots/spaces, hard links, long paths and inherited ACLs. Files, spool reads, artifacts, team bundles and private workspace operations all need this review.

## Providers: upstream support versus ACC qualification

Evidence checked **2026-10-23** against official documentation linked below. All six advertise native Windows CLI distribution. **None has been qualified as an ACC native Windows provider in this task.** ACC's current qualified Linux pins do not prove the Windows binary has identical behavior. Pin exact Windows versions and requalify hooks on every supported release.

| Provider | Native Windows upstream evidence | Current ACC integration and required port | Risk / release recommendation |
|---|---|---|---|
| Claude Code | [Setup](https://code.claude.com/docs/en/setup) supports native Windows; Git Bash optional, PowerShell tool available. Native Windows CLI sandboxing is listed unsupported. | `claude.py` writes per-session `--settings` hooks and statusLine using `shlex.quote`. Replace shell sink/statusline, choose explicit executable/args or shell supported by the pinned release; cover Bash **and** PowerShell tool payloads per [hooks reference](https://code.claude.com/docs/en/hooks). | Medium/high. First cohort; do not show a CLI sandbox claim. Optional Git Bash compatibility does not make the PowerShell tool disappear. |
| Codex | [Official OpenAI Windows sandbox docs](https://learn.chatgpt.com/docs/windows/windows-sandbox) describe native PowerShell execution with MXC and legacy elevated/unelevated sandbox variants. | `codex.py` bootstraps `~/.codex/hooks.json`, with pre-tool guard and lifecycle sink; requires hook trust, target-path TOML/MCP configuration, Windows transcript discovery and interpreter launch. | First cohort; high sandbox qualification risk. Validate the **pinned CLI's** available sandbox mode, service-account setup and filesystem/network denial. Never translate Linux `workspace-write` into an assumed equivalent Windows guarantee. |
| Copilot CLI | [Quickstart](https://docs.github.com/en/copilot/get-started/cli-quickstart) offers Windows winget or npm (Node 22+); [hook reference](https://docs.github.com/en/copilot/reference/hooks-reference) supports PowerShell and direct executable/args. | `copilot.py` writes `~/.copilot/hooks/acc.json` with only `bash` today. Emit Windows `powershell` or qualified direct `exec`/`args`, preserve event normalization and trusted-folder setup. | Medium. First cohort; qualify prompt detection, lifecycle events and Credential Manager under the service account. |
| Cursor | [CLI installation](https://cursor.com/docs/cli/installation) now includes native PowerShell installation and `agent`. This is CLI evidence, not inference from the Windows editor. | `cursor.py` uses `cursor-agent`, `~/.cursor/hooks.json`, and per-session plugin `hooks/hooks.json`/instructions. Port both hook sources, executable discovery, quoting and transcript paths. | Medium/high. Later cohort; validate native hooks and permission prompts independently of desktop/editor behavior. |
| Antigravity | [Official CLI installation/auth](https://antigravity.google/docs/cli/install/) documents native `agy` for Windows, PowerShell/CMD installers and Windows Credential Manager. The page currently redirects; recheck [getting started](https://www.agy.dev/docs/getting-started?tab=ide) when qualifying. | `antigravity.py` writes `~/.gemini/config/hooks.json`, POSIX command strings; sink must return the expected JSON. Qualify current settings locations, hook shell, OAuth and headless service profile. | High. Later cohort; schema/location drift must fail qualification rather than silently drop hooks. |
| OpenCode | [Official v2 docs](https://dev.opencode.ai/v2/docs) list standalone Windows x64/ARM64 binaries and say Windows package managers are unsupported. | `opencode.py` creates a private server/config/plugin and `opencode/bin/bash`; JS plugin spawns the guard and marks shell creation. Replace `/bin/bash` wrapper with a qualified Windows shell launcher preserving refusal when plugin/marker is missing; port DB paths and server lifecycle. | High. Later cohort; native binary availability is insufficient. Use standalone verified binary, not assume ACC's Linux npm-binary installer works. |

For every provider, verify login and refresh as the **agent account**, with its own loaded profile. Never import the signed-in desktop owner's credentials. OAuth callback/browser launch may fail in Session 0; prefer a vendor-supported device/manual URL flow and relay only the necessary prompt through the existing login broker. Missing headless auth, mandatory pre-tool hooks, resume state or guard denial means that provider is unavailable on that node, with a reason.

## Guard semantics: a required port, not a quoting patch

[acc-guard](../bin/acc-guard) is a Python program with a POSIX shell parser and Unix path/command assumptions. [acc-hook](../bin/acc-hook) and [acc-statusline](../bin/acc-statusline) are `/bin/sh` scripts using `date`, `cat`, `mv`, `printf` and `cut`. Supply bounded native Python/executable equivalents with explicit UTF-8 JSON, concurrent-safe spooling and exact provider exit/output contracts. Shebangs and executable mode bits are insufficient on Windows. Hook invocation quoting is separate from parsing the **agent's proposed command**.

PowerShell needs AST-based inspection with a pinned parser: aliases (`rm`, `del`, `sc`), call operator `&`, scriptblocks, pipelines, subexpressions, splatting, redirections, variables, `-EncodedCommand`, `Start-Process`, invocation of `.ps1` and nested shells. cmd needs separate treatment of caret escapes, `%VAR%`/delayed `!VAR!`, `call`, `start`, `&`, `&&`, `||`, pipes and redirections. Windows executable lookup, `.cmd` npm shims, case-insensitive tool names and path canonicalization affect git, package, secret and hook-file checks. Git Bash remains a third dialect and can launch PowerShell/cmd; inspecting only its outer command is insufficient.

Pass a trusted shell/dialect identifier from each adapter, launch provider executables via fixed argv, and recursively inspect known interpreter wrappers. Deny unsupported/ambiguous shell constructs when a guard rule applies; preserve fail-closed missing/timeout/malformed hooks for every provider. Do not claim Python string splitting can implement PowerShell or cmd. Arbitrary Python/.NET/binary code can bypass command heuristics: OS enforcement and upstream branch protection remain necessary. The guard remains a safety net, never a sandbox. If practical PowerShell/cmd coverage cannot be achieved, stop broad provider rollout rather than advertise Linux guard parity.

## Packages, updates, git, preview and files

Replace [acc-packages](../deploy/packages/acc-packages)' Linux catalog/actions and `deploy/accounts` systemd path units with typed, bounded Windows helper requests. The privileged service accepts approved registry coordinates/catalog IDs, not command lines, URLs or destination paths. Per-account npm tools use a private prefix as that user; system installs are separate. Test npm `.cmd` shims with an explicit launcher. Prefer pinned vendor artifacts/MSIs for unattended installs: winget availability in interactive Windows does not establish availability under a service account, offline operation or machine-scope behavior. No silent downgrade to shell downloads or ignored integrity checks.

Use Authenticode-signed, timestamped ACC installer and native helper binaries; protect signing keys outside builds, publish signed release manifests/hashes and an SBOM, validate artifact publisher/hash before elevation, and keep rollback/version identities consistent with existing updates. Test Defender/SmartScreen and enterprise application-control policy. Vendor self-updates must be disabled or explicitly coordinated with ACC qualification; retain the last qualified version. MSI files in use, pending reboots, interrupted updates and partial rollback require explicit states. Existing unsigned WSL setup does not establish this release chain.

Git for Windows supplies worktrees, CLI auth and HTTPS backup pushes. Adapt `/dev/null` global config/hooks suppression to qualified Windows-safe empty files/directories or supported null semantics; keep credential helpers disabled, redirects off, tokens out of argv/disk/logs and short-lived. Qualify custom CA files, worktree metadata, dirty/untracked archive, path case collisions, CRLF, symlinks and lock/rename behavior. Keep repositories on local NTFS for v1. Git history backup is not a backup of dirty files or the whole Windows machine. Restore to a fresh node and re-enroll with a new machine identity; never clone live node keys.

Live preview keeps existing reserved-port leases and `preview-v1` WSS framing, main-side origin isolation and browser authorization. Test native dev servers/HMR, cancellation, reused ports and resume after sleep. Binding a server to `0.0.0.0` can expose it outside ACC; enforce/qualify loopback binding and firewall policy rather than treating the tunnel as a network sandbox. No Windows firewall weakening is needed for the outbound node connection. Native services/Compose or Linux containers via Docker Desktop are outside node v1; do not install WSL as an implicit dependency. Files/Changes/artifacts ship only after the handle/ACL tests above pass.

## Native main: separate approval after the node

Python/ASGI accd, Caddy's Windows build and SQLite are plausible portable components. Run accd and Caddy as separate unprivileged Windows services, with least-privilege access to database, TLS and configuration; keep SQLite/WAL on local NTFS, not an SMB share. Audit Unix imports, signal handlers, subprocess/event-loop behavior and atomic writes across server modules. Use SQLite backup APIs/consistent snapshots; never copy a live database without its consistency protocol. Qualify migrations, crash recovery and restore of keys/catalog/projects.

Port **every** privileged helper, not just account/package creation: publishing/Caddy config and reload, certificates/trust, DNS credential storage, port/firewall operations, updates, backup/restore scheduling and services lifecycle. Service Control Manager and Windows task scheduling replace systemd units/timers. Preserve request validation, authorization, audit and idempotency. Do not run accd as Administrator to make these operations work. Git smart-HTTP receiver and its immutable-history `#!/bin/sh` update hook need Git for Windows hook qualification or a native equivalent before promising backup parity.

[Caddy supports Windows services](https://caddyserver.com/docs/running#windows-service) and can serve TLS on Windows, but automatic startup, internal-CA trust, DNS challenge modules, 80/443 firewall ownership and certificate/private-key ACLs need a Windows deployment design. Linux services brokers/rootless Docker and the approved Linux container runtime have no direct native equivalent; show unsupported operations explicitly. A native main managing Linux nodes could precede local Windows workload parity, but still needs all main control-plane helpers qualified. Node-only first preserves the existing dashboard, receiver, database, publishing and backups while the riskiest execution boundary is tested.

## Security claims and proposed UI wording

Current Linux per-person isolation is not same-person session isolation. Optional systemd limits and targeted AppArmor profiles should not be overstated. Native Windows can match cross-person separation with correct SID/ACL/token policy, but this has not been demonstrated. It cannot inherit Linux mount/PID/network namespaces, seccomp, general AppArmor confinement, immutable container mounts or the approved private per-session rootfs just by using services and Jobs.

AppContainer is a possible capability boundary but arbitrary developer tools, credential stores, browsers and nested provider sandboxes need a dedicated compatibility project. Windows Sandbox offers a separate kernel/desktop with disposable state, edition/virtualization restrictions and guest file/terminal/network bridging; it does not supply durable per-account ACC sessions automatically. Neither is in the estimates for native node v1. Do not describe Windows containers/Docker Desktop as rootless Linux containers without WSL: those are different platforms and often introduce a VM/WSL dependency.

Following [DESIGN](design/DESIGN.md), proposed copy uses existing Badge/Note/confirmation primitives and words as well as color; no UI is changed here:

- Machine: **Windows native · Experimental**, with last successful qualification version/time.
- Session: **Not isolated**. Detail: “Runs as a dedicated Windows account. Sessions for this person share access. ACC's per-session container protection is unavailable.”
- Provider protection separately: **Codex sandbox: verified [mode]**, **Sandbox unavailable**, or **Protection unknown**. Report the observed mode/version and test evidence; never let it replace the ACC session-isolation label.
- Guard capability: **PowerShell checks qualified** / **Command checks unavailable** with a reason. Unqualified mandatory guard support disables launch; it is not a dismissible warning.
- Native launch requires the approved owner-only per-project exception and audit reason; organization policy may prohibit it. Unknown/offline capabilities stay unknown/stale, not healthy. No silent fallback from containers, failed sandbox setup, or WSL mode to native mode.

Admins and a compromised broker remain trusted; hooks/events from a compromised node remain that node's word. Same-account agents can reach each other's credentials/processes unless a stronger separate boundary is delivered. Jobs do not prevent network exfiltration. Do not offer this pilot as suitable for hostile multi-tenant/company workloads requiring container isolation.

## Phases and acceptance gates

Estimates assume one experienced engineer, existing Linux main retained and supported Windows 11 x64 on local NTFS. ARM64, Windows Server/10, enterprise fleet rollout and a general Windows sandbox are additional scope. Security review and Windows test access are dependencies, not assurances hidden in the range.

| Phase | Engineer-weeks | Required exit evidence |
|---|---:|---|
| 0. Feasibility spike | 1–2 | On owner test PC, boot-time service ConPTY works without login; separate SID/profile login; native CLI hooks and Codex sandbox mode demonstrated. Review named-pipe identity and guard feasibility. Stop/redesign on failed mandatory boundary. |
| 1. Native runtime and transport | 3–4 | Terminal host, snapshot/reattach, Jobs, named pipes, account provisioning, native layout/capabilities, ACL/handle file layer; negative tests against two accounts. Linux contracts unchanged. |
| 2. First provider vertical slices | 3–5 | Claude, Codex, Copilot each complete install → login → launch → guard refusal → Needs you → reconnect → stop → same-conversation resume; PowerShell/cmd/Bash policy coverage and real hooks qualified. |
| 3. Node release and pilot | 3–5 | Signed install/update/rollback/uninstall, approved packages, Files/Changes, Git backup/restore, Preview/HMR, no-login boot, reboot/sleep and owner acceptance; accurate UI warnings/Guide. |
| Node first-cohort subtotal | **10–16** | All gates above; otherwise remains experimental/unavailable. |
| 4. Remaining providers | +3–5 | Cursor, Antigravity, OpenCode same full path, including OpenCode missing-plugin refusal. Individual provider enablement only after qualification. |
| 5. Native main (new decision) | +6–10 | accd/Caddy/SQLite, every privileged helper, receiver hooks, TLS/DNS, migrations, full backup/restore and LAN browser acceptance; no administrator dashboard process. |

Total with all six providers and native main: **19–31 engineer-weeks**, excluding optional general sandbox development. Approving the proposal should authorize only the chosen phase/budget; it does not imply parity or authorize configuration changes in this document task.

## Test plan for an owner Windows machine

Use an owner-designated disposable test account/machine (for example **windows-test-pc**) after separate implementation authorization. Do not change it as part of this design task. Record OS build/edition, CPU architecture, filesystem, CLI versions, security software/policies and exact sandbox mode. Start with current supported Windows 11 x64; test another build and ARM64 only when added to scope. Use synthetic repositories/accounts/secrets and retain redacted evidence.

1. **Component tests:** native paths/argv/config and hook JSON golden cases; guard adversarial corpora for PowerShell/cmd/Bash and nested interpreters; malformed hooks fail closed; bounded spool/VT parsing and snapshot race tests. Run existing Linux tests with `PYTHONPATH=src` to catch regressions; Windows CI exercises actual Windows APIs rather than Linux mocks.
2. **Identity attacks:** two person SIDs plus same-person sessions; deny cross-account files/pipes/process access, node key reads, service modification and package-helper spoofing. Attempt pipe squatting/remote access, SID spoofing, reparse/hard-link/ADS/UNC escapes, ACL inheritance/reset and open-time swaps. Explicitly demonstrate expected same-account sharing so claims stay honest. Check no privileged token/handle inheritance or breakaway children; test CPU/memory/process/disk exhaustion.
3. **Terminal lifecycle:** multiple viewers/read-only enforcement, single input authority, Unicode/emoji/IME/paste, resize/alternate screen, large scrollback and slow clients. Close dashboard, restart main/broker, disconnect/reconnect network; terminal PID stays alive. Crash terminal host, reboot and power-cycle: honest recovery and exactly one resume of the original conversation, no replayed package requests. Test sign-out, locked desktop, no-login boot and sleep/wake.
4. **Real provider qualification:** first cohort then later cohort; device/manual auth, refresh under service profile, workspace trust, pre/post-tool/permission/stop hooks, model/MCP/skills, interrupt, prompt detection, transcript discovery and resume. Change/disable hooks/plugins and deny guard execution: no unchecked tools. For Codex verify read/write/network denial and nested build/browser tools under each supported mode; no automatic full-access fallback.
5. **Full dashboard path in a browser:** enroll/revoke native node; select host; launch/login/permission prompt, reconnect terminal, edit/read files, inspect Changes/artifacts, reserve preview port and exercise HTTP/HMR WebSockets; manual Git backup, rewritten-ref retention, download/restore bundle. Verify cross-person authorization and proposed labels on desktop/mobile, keyboard/focus and screen reader. Capture synthetic-data screenshots for the Guide when UI exists.
6. **Operations:** clean machine without WSL installed; install twice, failed elevation, revoked enrollment, expired CA/code, certificate rotation, offline package action, interrupted signed update, rollback, reboot-required result and uninstall retaining data. Restore onto another machine with new node key. Prove current Linux/WSL installation/attach tests still pass. For a future native main add migrations, database crash/restore, receiver ref protections and Caddy/TLS/firewall/privileged-helper negative tests.

These are future acceptance gates. This documentation task has not run Windows APIs, real native provider sessions or a browser through a native ACC path.

## Phase 0 results — windows-test-pc (2026-10-24, owner run 2026-10-25)

On 2026-10-24 the phase 0 spike ran on the owner's test PC **windows-test-pc** as an agent session inside its WSL node. It was told to stop rather than elevate, and it did. On 2026-10-25 the owner ran the scripts in [`scripts/windows-phase0/`](../scripts/windows-phase0/README.md) directly, elevated for install and uninstall and unelevated for collection. **Gates 1 and 5 pass on windows-test-pc. Gate 2 passed its criteria at the time, but its token held `SeImpersonatePrivilege` and `SeCreateGlobalPrivilege`, which now fail it (finding 3); it needs a re-run. Gates 3, 4 and 6 are outside this probe and not yet run, so phase 0 is not complete.**

**Machine:** Windows 11 Pro for Workstations 26H2, build 26300.9711, x64; AMD Ryzen 9 7950X3D; NTFS on C:. Microsoft Defender running with real-time and tamper protection; AppLocker stopped; Smart App Control off. Claude Code 2.1.308 is bundled with the Claude desktop app but is not on PATH and not signed in. Codex 0.163.0-alpha is signed in with `sandbox = "elevated"`, hooks enabled, and its `CodexSandboxOffline`/`CodexSandboxOnline` users already exist.

| Gate | Result (2026-10-25, boot 16:06 UTC, install `eef69fbd`) |
|---|---|
| 1. Boot-time service hosts ConPTY without login | **PASS.** After a restart with nobody signed in, the Session 0 service ran `cmd.exe` and a long-running `ping` through ConPTY. Output was captured and the child was alive after 10 seconds. The process snapshot showed `winlogon.exe` and no shell process outside Session 0. |
| 2. Separate SID/profile, non-interactive service logon, and a token with no impersonation or global-object privilege: every privilege present (enabled or not) on the allowlist `SeChangeNotifyPrivilege`, `SeShutdownPrivilege`, `SeUndockPrivilege`, `SeIncreaseWorkingSetPrivilege`, `SeTimeZonePrivilege`, and the service's required privileges exactly `SeChangeNotifyPrivilege` | **Re-run needed.** The identity part passed: a separate standard SID in Session 0, with its own profile, hive and environment. The token held `SeImpersonatePrivilege` and `SeCreateGlobalPrivilege`, which fail the stricter criteria added after the run (finding 3). |
| 3. Native CLI hooks fire (Claude Code, Codex) | Not run (outside this probe). Claude Code is not signed in on Windows. |
| 4. Codex Windows sandbox read/write/network denial | Not run (outside this probe). |
| 5. Named-pipe client SID check and squatting resistance | **PASS.** The pipe's server PID is bound to SCM. The owner SID was refused and the service SID accepted. Explicit local DACL; a second first-instance pipe was refused. |
| 6. Guard feasibility | Not run (outside this probe). |

### Findings from the owner run (2026-10-25)

1. **Six problems appeared only on real Windows.** Each was fixed on main with a regression test:
   - `[IO.File]::Replace` with `$null` (#285);
   - `New-LocalUser -Description` over 48 characters (#286);
   - `TimeoutException` ambiguous between `System` and `System.ServiceProcess` (#287);
   - session states (#288);
   - `WTSEnumerateSessionsW` failing for the standard probe account with error 259 "No more data is available", although it succeeds for the owner (#289). The signed-in check now uses the process snapshot, with `winlogon.exe` as a positive control.

   The sixth was in the owner commands, not the scripts: a multi-line paste into PowerShell keeps running after a `throw`. Owner instructions must wrap steps in `& { … }`.
2. **"Nobody is signed in" is often false on home PCs.** On windows-test-pc, Windows' *"Use my sign-in info to automatically finish setting up after an update"* (Automatic Restart Sign-On) signed the owner in at boot and then showed the lock screen. `quser` reported a logon at boot time. A password-less auto sign-in account does the same. The probe correctly failed Gate 1 until the setting was turned off. **The native node must not assume the owner is signed out after a reboot.** Agent work has to keep running beside an active owner session (a separate SID already provides this), and the UI must not imply that a restart signs anyone out.
3. **The service token holds `SeImpersonatePrivilege` and `SeCreateGlobalPrivilege` (both enabled).** The installer grants only the service logon right. These come from the built-in `SERVICE` group under the default *Impersonate a client after authentication* and *Create global objects* policies. **`SeImpersonatePrivilege` is a known local-to-SYSTEM escalation path**, so an agent process must not inherit it. Required before any agent runs under this account:
   - set the service's required privileges (`SERVICE_CONFIG_REQUIRED_PRIVILEGES_INFO`) to `SeChangeNotifyPrivilege` only, so SCM strips the rest. **Done in the probe:** Install sets it before the first start, reads it back and records it in the manifest; deleting the service in Uninstall removes it;
   - start agent children with a restricted token that has no impersonation or global-object privilege (still open: the probe runs no agent);
   - add both to the Gate 2 pass criteria. **Done:** Gate 2 fails on either privilege, on any privilege outside the allowlist (present counts, enabled or not, since a process can enable its own), and when the configured required privileges differ. The allowlist and its reasons are in the [probe README](../scripts/windows-phase0/README.md#what-the-gates-prove).

   The other privileges were ordinary and disabled: Shutdown, Undock, IncreaseWorkingSet and TimeZone. ChangeNotify was enabled.
4. **Uninstall needs a restart in the middle.** After the service stops, Windows keeps the account's profile hive loaded until the next restart. Uninstall then (correctly) refuses to delete it, and a second run after a restart completes. The product uninstaller should schedule profile removal for the next boot rather than ask the owner to run it twice.
5. **Fast Startup was on.** Owner procedures must say **Restart**, not Shut down, so that a boot really happens.

The probe was removed after the run. Uninstall reported user, service, profile, rights and folder absent, with 0 firewall changes.

### Finding: a WSL node is not isolated from the Windows account that runs it

Three things combine on windows-test-pc, and the first two are WSL defaults:

1. **WSL interop** lets any process in the distro, including agent sessions, start Windows programs (`powershell.exe`, `cmd.exe`) as the Windows user.
2. **Automount** exposes `C:` under `/mnt/c` readable by the agent accounts, so agent sessions can read the owner's Windows profile, including CLI logins such as `.codex\auth.json`. The spike confirmed it was readable and did not open it.
3. **UAC set to never prompt** (`ConsentPromptBehaviorAdmin=0`) let a probe get a full administrator token silently, so anything running as the user can become administrator.

Linux file permissions do not help while interop is on: a Windows program started through interop runs with the full rights of the Windows user.

**Recommendations:**
- Restore UAC to at least the default level on any PC that runs a node. This does not affect development.
- For nodes dedicated to Rynhult, isolation is **implemented as an option**: `deploy/node/install.sh --wsl-isolate` disables interop and restricts automount in `/etc/wsl.conf`; `--wsl-disable-automount` is a separate explicit choice. Interactive installs default to No, updates never change it, and `wsl --shutdown` is required. Host warns on reported interop or agent drive access; owners may dismiss that warning per machine with an audited acceptance. See the Guide's "Isolation for dedicated WSL nodes" section.
- Where agents need Windows tools (Windows development), run the WSL node under a **separate, non-administrator Windows user**. Its Windows tools then run as that user and cannot read the owner's profile. WSL only runs while that user is signed in (fast user switching keeps it running).
- The native Windows node in this document is the long-term answer: per-SID agent accounts, Codex's sandbox and no interop bridge.

**Owner decision (2026-10-24):** windows-test-pc stays as is for now, as an accepted risk on a lab machine. Only coordinator-launched, watched sessions run there, never on untrusted repositories.

### Next steps for phase 0

- Gates 1 and 5: done on windows-test-pc. Gate 2: re-run on windows-test-pc with the required-privileges install and the stricter criteria (install, collect, restart, collect, uninstall). Before any agent runs under a Windows agent account, agent children also need a restricted token without those privileges.
- Run gates 3 and 4 after Claude Code is signed in on Windows, with synthetic repositories and no hook-trust bypass.
- Gate 6 (guard feasibility) remains a design exercise; see "Guard semantics" above.

## Questions for owner approval

1. Approve **node-only first**, Windows 11 x64/local NTFS, with Linux/WSL main retained? Authorize phase 0 only (1–2 weeks), or the gated 10–16 week first cohort?
2. Accept the **owner-only per-project native exception**, **Not isolated** warning and same-person sharing? Confirm organization policy can block native launches; stronger general Windows isolation would need a separate design/budget.
3. Are Claude, Codex and Copilot the mandatory first cohort? Which additional providers/native Windows toolchains justify phase 4, and is Git Bash an acceptable optional native dependency?
4. Confirm a test machine such as windows-test-pc, Windows edition/build, permission for dedicated service users during implementation, provider subscriptions and time for reboot/sign-out/security tests. What workloads must work for acceptance?
5. Who holds the Windows code-signing identity/key, and what update/reboot/maintenance policy is acceptable? Is deferred updating while sessions run acceptable?
6. Defer native main until the node gates pass, then reconsider the extra 6–10 weeks and unsupported Linux container/services features? If a container-equivalent boundary is mandatory immediately, retain WSL/Linux instead of releasing native mode.
