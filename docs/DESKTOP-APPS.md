# Rynhult desktop apps: macOS, Windows and Linux

Date: 2026-10-24. **Status: Tauri direction approved; Linux phase 1 session source slice built; no desktop release.**

## Release scope (owner decision, 2026-10-27)

**Desktop apps are outside v1.0.** v1 is the self-hosted Linux server, used
through browsers on any client platform, including the phone web app, with
existing Linux/macOS/Windows-WSL nodes retained. Native apps follow v1, one
platform at a time to reduce total cost, in an order not chosen yet. The candidates
are native Windows, macOS standalone desktop, Linux desktop, iOS and Android. Native Windows no longer starts
immediately after rc.1. See [decision 0073](decisions/0073-v1-linux-server-scope.md).

Desktop/mobile code and the recorded spikes below already exist on main; they
are experimental groundwork, not v1 application deliverables. Existing node
installers are separate and remain supported. Native app packaging, signing and
device qualification are post-v1 gates, not blockers for the Linux-server release.
The dated technical evidence below is retained; the original phase numbering
and estimates are not the current delivery order.

Qualification machine labels below are anonymized, without changing the
recorded results or claiming additional qualification.

Owner requirement (2026-10-24): “MacOS, Windows, and Linux desktop apps should have a standalone mode that allows them to work within the app without connecting to a dedicated server.” Standalone therefore includes a real local Rynhult main, persistent projects/history and local agent execution for one person on one machine. No dedicated server, Rynhult cloud account or push relay is required. Internet-dependent providers still require their own connectivity and accounts.

## Recommendation and existing boundaries

Bundle the existing Python main and built React/xterm UI with a small desktop host. First spike Tauri 2/system WebViews against three small native hosts; choose only after local IPC, terminal, accessibility and remote client identity work on each OS. Keep one domain model, API and UI. Do not rewrite the main in Rust, Swift, C# or JavaScript to obtain an installer. Native execution portability, helper privileges and agent isolation are the expensive work, regardless of shell.

Inputs read: PLAN (the private roadmap), [MOBILE-APPS](MOBILE-APPS.md), [WINDOWS-NATIVE](WINDOWS-NATIVE.md), [CONTAINERS](CONTAINERS.md), [REMOTE-ACCESS](REMOTE-ACCESS.md), [DESIGN](design/DESIGN.md), current installers under `deploy/macos`, `deploy/node/macos`, `deploy/windows` and `deploy/node/windows`, and the Rynhult brand-kit README, palette and icon masters in the reviewed Rynhult brand kit. The quoted standalone requirement above is authoritative for this proposal. Existing accepted decisions, including 0020 (private decision record), still apply.

Current macOS setup is an additional-machine installer: Homebrew dependencies, a user LaunchAgent, and sessions running as the signed-in user. Its package signs the setup app ad hoc, not with a release publisher identity. Windows Setup is an elevated Inno/PowerShell wizard installing Ubuntu/WSL2 with systemd; its main role changes mirrored networking/firewall settings. Neither is the proposed standalone desktop product. Do not repurpose these installers silently or suggest today's Mac package runs a main.

WINDOWS-NATIVE proposes node-first and leaves main later. The new requirement makes standalone native Windows main an explicit destination; retaining node-first as an internal dependency is sensible, but a client-only release cannot satisfy it. Its strict dedicated-account boundary remains the proposed release policy. CONTAINERS has a built trusted Linux launcher, but qualification is still incomplete and production isolation is not claimed. Desktop packaging does not remove that gate. These recommendations are pending owner choices, not a new accepted ADR.

## Modes, identity and everyday behavior

| Mode | Authority and execution | Visible behavior |
| --- | --- | --- |
| Standalone | Local Python main, local SQLite, local broker/terminal host; one local person is owner. | First launch offers **Use on this computer**. No server URL required. Persistent **This computer · Standalone** context and separately reported execution protection. |
| Server client | Dedicated main remains authoritative; desktop is a client, not a second main for its data. | **Connect to a server**; sign in and pair where required. Connecting never uploads local projects automatically. Local main can remain stopped. |
| Client plus enrolled desktop node | Dedicated main controls sessions running on the desktop through the node protocol. | Separate **Use this computer for server sessions** review/enrollment. Client device identity and node identity are distinct. |
| Move standalone work | Explicit snapshot/import/cutover into a dedicated main; optionally retain the desktop as its node. | **Move work to a server** previews projects, destinations, secrets exclusions and unsupported records. Local source remains retained and read-only after cutover. |

Keep separate profiles for local and each server: database/state directory, origin, person ID, cookies/session, device keys and optional cache. Server identity is its verified installation identity plus origin/trust binding, not a friendly name. Switching profiles cancels requests, closes terminal writers and clears rendered sensitive data before loading another profile. Never silently fall back from a disconnected server to local execution. Show selected authority on launch/approval/migration confirmations and notifications. An unavailable server is not an empty project list.

Standalone creates a local owner through a one-use native bootstrap over authenticated IPC. It does not expose an unauthenticated owner-creation HTTP endpoint. OS-session ownership plus app unlock replaces repeated web sign-in only for this explicitly local profile; this is new auth work, not today's cookie behavior. Require OS reauthentication for secret export, migration, account/helper changes and local lock-policy changes. Lock the app on workstation lock; running sessions continue, and already supplied agent credentials cannot be recalled by locking the window. Offer explicit stop/secret-cleanup controls. Server mode always retains normal person sign-in and recent-verification rules.

Initial standalone scope: Projects/workspaces, qualified local providers, terminal, Files/Changes, Inbox, catalog, budgets and local export/restore. Capability negotiation hides or explains unavailable actions. Public publishing, system services/Compose, multi-person administration, HA and Linux-only backup receivers are not implied by a bundled main. Existing schedules require an awake, running main; missed work follows reviewed schedule semantics, never an unbounded wake-up replay. A supported local-model adapter can work offline, but model availability is separate from having a local main.

## Runtime and shell choices

```text
Signed desktop host + bundled React/xterm
  ├─ local authenticated transport → Python local main → broker/terminal host
  │                                  ├─ private SQLite/catalog/audit/vault
  │                                  └─ qualified execution account/container
  └─ scoped TLS transport → dedicated main → its brokers / enrolled nodes
```

The local main is a supervised companion process, not Python embedded in the renderer. Separate main, broker and terminal lifetimes: closing a window detaches a viewer; restarting the main does not duplicate a session. Fence launches with installation/session/generation IDs and reconcile actual processes before recovery. Unix can retain tmux where qualified. Windows needs the durable ConPTY host, VT snapshot/replay, single-writer and Job lifecycle described in WINDOWS-NATIVE; pywinpty alone is not persistence. After terminal-host crash/reboot, mark **Recovery needed**, retain files/transcripts and explicitly resume the same provider conversation where supported. Never advertise a surviving process across reboot.

| Architecture | macOS | Windows without WSL | Linux | Solo-maintainer trade-off |
| --- | --- | --- | --- | --- |
| Tauri + Python sidecar + React (first spike) | WKWebView, Rust host and signed Python/helper executables. | WebView2 plus native Python/runtime port. | WebKitGTK plus packaged Python; distro ABI/support matrix required. | One shell framework and UI; Rust/plugins and three WebView differences remain. Remote mTLS and local-origin cookies/IPC need proof. |
| Small OS hosts + Python + React | Swift/AppKit or SwiftUI host. | C# host/WebView2. | GTK/WebKit host. | Direct OS control, can reuse Swift mobile identity concepts; three hosts/toolchains and bridge implementations to maintain. Fallback if framework transport limits are material. |
| Electron + Python + React | Bundled Chromium/Node on each OS. | Same renderer reduces WebView variation. | Bundled renderer reduces distro WebKit differences. | Familiar web tooling; larger download/RAM and another security runtime. Disable Node integration in renderer, use sandbox/context isolation and a narrow preload bridge. It still needs the Python/runtime port. |
| Full native UI/main rewrite | Swift UI/runtime. | WinUI/.NET runtime. | GTK/Qt or another UI/runtime. | Reimplement auth, providers, migrations, terminal and authorization; widest regression surface. Defer for a solo maintainer unless measured failures justify it. |

[Tauri's distribution documentation](https://v2.tauri.app/distribute/) describes platform-specific bundling/signing; it does not qualify Rynhult sidecars or guarantee a particular artifact size. Planning allowances, **not measurements**: system-WebView shell + Python/dependencies + UI around 80–200 MB compressed per architecture, 200–500 MB installed; Electron variant around 180–350 MB compressed and 400–900 MB installed. Exclude provider CLIs, Git/tmux/toolchains and images. Budget an additional 1–5 GB download / 3–15 GB disk for an initial Linux agent image/toolchain; a Mac VM may reserve 2–4 GiB RAM while running and a growing 10–30 GB virtual disk. Measure actual signed artifacts, expanded size, cold start and RSS in phase 0; these ranges are capacity reservations, not comparative benchmarks.

Bundle a pinned Python interpreter, locked runtime dependencies and built UI per OS/architecture. No Python, Node or package manager should be required merely to open the app and manage projects; Node/Git/provider dependencies are separate execution prerequisites. Audit import-time POSIX assumptions, paths, signal handling, locks, SQLite/WAL, migration/backup atomicity, subprocess quoting and file containment. Linux systemd, `/srv/acc` and root helpers must become explicit runtime capabilities, not startup requirements. Never run the whole main elevated to bypass portability work. Keep app binaries separate from writable state, initially local APFS/NTFS/ext4-class filesystems; network/sync folders are unsupported for live SQLite.

## Isolation: what a standalone machine can truthfully promise

One local person still needs separation between trusted control plane and untrusted agents. “Single person” does not make an agent safe to run with the person's browser credentials, vault or app IPC. Account separation protects different identities; it does not isolate sessions sharing that account. Hooks, prompt approvals, launch hashes and Job/cgroup limits are useful controls, not a general filesystem/network sandbox.

| OS | Without repeated privilege prompts | Provisioning and limitations | Proposed launch policy |
| --- | --- | --- | --- |
| Linux | Once provisioned, constrained account/broker and trusted launcher services can start sessions without sudo prompts. Rootless Podman provides per-session namespaces on the shared kernel. | One-time administrator setup for separate agent identity, subordinate IDs, cgroup delegation, LSM policy and narrow helper. An unprivileged portable app cannot assume these are configured. Existing launcher expects system accounts/services; adapting it is work. | Prefer qualified containers, fixed images/mounts, private state, no runtime socket in payload. If unavailable, launch stays blocked unless the owner explicitly grants the existing per-project native exception. |
| macOS | App/main can run as desktop user; provisioned standard agent account plus constrained broker can separate owner files. Optional dedicated Linux VM with rootless sessions can run after setup. | Creating accounts/helper needs administrator approval once. TCC access, noninteractive profile/Keychain login and broker launch need qualification. Signing/notarization or App Sandbox on the UI does not sandbox arbitrary child CLIs. Native developer tools need capabilities incompatible with a blanket app sandbox assumption. | Default isolated route is qualified dedicated VM/container execution, with only explicit project shares and no whole-home mount. Native macOS tools need an owner-approved project exception and dedicated-account boundary. |
| Windows native, no WSL | Provisioned standard agent SID, service broker and terminal host; constrained installation helper handles approved system actions. Normal launches require no UAC. | One-time admin provisioning/service logon/profile and explicit NTFS/pipe ACLs; unattended provider auth must work. Jobs bound resources/process trees, not file/network access. AppContainer requires separate compatibility/security work. Windows Sandbox/Hyper-V are optional VM research with edition/setup/lifecycle costs. | Native dedicated-account sessions require explicit project exception, **Not isolated**. No automatic Docker/Podman install that introduces WSL. If stronger isolation is mandatory, block local launch and offer an approved Linux node or separately qualified VM option. |

[Podman documents that macOS and Windows require a Linux VM](https://docs.podman.io/en/latest/markdown/podman-machine.1.html). A VM's host-side process being unprivileged does not prove the guest engine/payload is rootless. Mac VM execution uses Linux tools, not native Metal/macOS APIs; Windows native mode cannot claim Linux container parity. A separate desktop login for all of Rynhult reduces access to the owner's usual home but leaves agents able to attack a main running under that same identity. Same-user execution may be used only in an explicitly approved development experiment, not as the released fallback to failed account setup.

Use existing Badge/Note primitives with text, details and evidence timestamp:

- **Container · Linux** or **Container · Linux VM** only after runtime policy and provider qualification; describe shared kernel/guest, mounted roots, egress and limits.
- **Not isolated · Separate agent account** for native exceptions: “Sessions for this person share access. Rynhult's per-session container protection is unavailable.” Show that account separation protects the desktop owner's private files only where permissions actually enforce it.
- **Protection unknown** for missing/stale runtime evidence; block a launch requiring isolation. Show provider-specific sandbox mode/version separately, never as a substitute for the session badge.

Preserve CONTAINERS policy: no silent native fallback, no host engine socket, mandatory private/LAN egress denial with explicit grants before internet-enabled qualification, private checkouts/CLI state, immutable effective hooks, secret cleanup and current Option A restrictions. Do not disable an inner CLI/browser sandbox merely because a VM exists. Show unavailable disk/network limits honestly; polling disk use is not a hard quota. Native exceptions remain owner-only, audited per project and subject to organization prohibition, including on a single-person installation.

## Local transport, secrets and release trust

Prefer a Unix-domain socket in a private runtime directory on macOS/Linux and a local named pipe with explicit DACL/SID validation on Windows. The desktop host adapts the existing API/event/terminal contracts through bounded typed messages; no generic native shell, arbitrary HTTP fetch or arbitrary file bridge. The browser renderer cannot open a Unix socket directly. Validate peer identity, endpoint owner, main generation and protocol; prevent socket/pipe squatting. [Microsoft's pipe guidance](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights) shows why default pipe permissions are insufficient. Reject remote pipe clients.

If local HTTP is materially simpler in the spike, bind only to loopback at literal `127.0.0.1` (and `::1` only if deliberately supported), with kernel-assigned port and no LAN listener. The host launches the exact bundled binary, passes a random 256-bit per-launch capability through an inherited private handle, and authenticates the readiness handshake. Never put the capability in argv, URLs, renderer storage, environment inherited by agents or logs. Native transport keeps it out of JS and authenticates every API/event/WebSocket channel. Strict Host/Origin checks, no wildcard CORS, CSRF for cookies and one-use terminal tickets remain required; another localhost browser cannot gain owner access. Authenticate before returning sensitive data; port secrecy is not authentication. Either transport must reject a stale/forged main and close channels on lock/profile change. Qualify a coherent bundled-origin/cookie model or implement a reviewed native session protocol; do not invent a trusted Origin header to skip authorization.

No local Caddy, DNS record, firewall exception, LAN discovery or client CA is needed for standalone. Agent-facing hooks get session-scoped credentials only. Remove desktop transport capabilities and main secrets from child inheritance. Bundled assets use strict CSP, no remote scripts/eval or service worker, and no bridge in external navigation, Markdown or project previews. Open external URLs in the system browser; previews use a separate unprivileged origin. Validate deep-link destinations after unlock and never execute a URL as a command.

| Store | What belongs there | Qualification / recovery |
| --- | --- | --- |
| macOS Keychain | Local vault wrapping key, server session and app client identity under scoped access controls. | Sign/update identity continuity, locked Keychain, prompts and dedicated-agent denial. See [Keychain services](https://developer.apple.com/documentation/security/keychain-services). |
| Windows Credential Manager / user-scoped DPAPI | Credential Manager for bounded credential items; DPAPI-wrapped private key/vault material in owner-only files where needed. | Load the correct user profile. Avoid machine-wide DPAPI for owner secrets; [CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) documents its user/machine binding. Same-user malware remains a threat. |
| Linux Secret Service | Wrapping key and client/session secrets, with app state encrypted separately. | Detect missing/locked service. Offer memory-only use or an explicitly passphrase-encrypted vault, never silent plaintext fallback. Test actual GNOME/KDE implementations; the [Secret Service specification](https://specifications.freedesktop.org/secret-service/latest/) is an API, not proof of application access isolation. |

Keep provider sign-ins in the qualified agent credential boundary, not copied from the owner's browser. Only selected session credentials enter payloads; credentials an agent receives remain readable by that agent. OS key stores do not protect an unlocked authorized process from all same-account malware. Reuse mobile's distinctions between device identity, person sign-in, app signing and notification registration. Remote client mode uses scoped trust/hostname verification and the mobile native-CSR proposal, with separate owner-approved enrollment implementation. Preserve mTLS before ACC for public access, then normal person auth; current REMOTE-ACCESS release gates remain. Do not copy a browser/device identity into a machine identity or relax private-only install/enrollment rules.

### Packaging, signing and updating

| Platform | Initial distribution recommendation | Signing/update implications |
| --- | --- | --- |
| macOS | Direct `.app`/DMG for supported Apple silicon first, Intel as separately tested artifact if needed; optional privileged setup package. Defer Mac App Store. | Developer ID, hardened runtime and minimal entitlements, sign nested Python/helpers before outer app, notarize and staple. Rehearse offline Gatekeeper launch and helper update. Apple's [notarization guidance](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution) is a release prerequisite, not evidence current packages meet it. |
| Windows | Signed per-user installer for client/basic main; separately reviewed elevated helper/account setup. Windows 11 x64 first; ARM64 separate qualification. | Authenticode sign/timestamp installer and executables, verify publisher/hash before elevation. Test Defender/SmartScreen without promising reputation. Do not turn off security checks to install. MSIX/store packaging is later compatibility work for services/CLIs. |
| Linux | One supported distro baseline first; signed `.deb` plus optional AppImage for unprivileged client use. | Repository signatures or signed artifact manifests, runtime ABI tests; AppImage is not isolation. Flatpak is a later client channel: host execution/portal escape permissions conflict with claiming sandboxed local agents. Do not self-update package-manager-owned files. |

Publisher/owner controls accounts, identifiers, signing and offline recovery keys, with restricted CI credentials; no contractor/agent is sole custodian. Verify artwork redistribution rights: the brand kit does not establish trademark/artwork licensing. Avoid exact fees/minimum OS promises until implementation rechecks platform policies.

Opt-in automatic **download**, explicit safe activation by default; signed release manifest binds channel, OS/architecture, shell, Python main, UI/bridge, schema and helpers. Verify signature/digest, reject replay/downgrade, stage atomically and retain last known-good binaries. Framework update signatures are additional to OS code signing; [Tauri's updater](https://v2.tauri.app/plugin/updater/) requires its own signing scheme. Protect signing roots and plan key rotation/revocation. Quiesce migrations under an exclusive lease and take a verified pre-upgrade data snapshot. Defer disruptive activation during sessions; terminal-host version mismatch blocks unsafe attach. Binary rollback cannot undo schema changes: restore a compatible snapshot into recovery mode, retain newer work separately, and require review rather than silently losing writes. Test interrupted download/install, insufficient disk and failed migration.

Agent CLIs have a separate approved version catalog and update transaction. Show version/source/qualification date, verify vendor artifacts and pin image digests; never run downloaded shell text as the owner. Coordinate or disable vendor self-updaters where supported; otherwise detect drift and withhold qualified status/launch until checked. New sessions adopt qualified versions; retain old versions needed for resume until safely migrated. Workspace dependency installation remains separate from host tool updates. Installation/admin work uses a narrow helper only when needed; ordinary CLI launch does not prompt for admin each time.

## Moving work to a server without losing it

Migration is a new feature, not a claim that today's backup/restore can merge installations. Support two destinations: a fresh main receiving a full logical import, and an existing main receiving selected projects/history with explicit identity/permission mapping. Never overwrite an existing server database with the standalone SQLite file. Distinguish import (historical data) from execution placement (server node, another node or this desktop enrolled as a node). Importing Windows projects does not make Linux tools able to resume them.

1. **Plan and preflight.** Authenticate to the verified destination as an authorized owner; compare API/schema/export versions, disk space, provider and target OS capabilities. Show all projects including local/no-remote research, dirty/untracked/ignored files, submodules/LFS, private session checkouts, wiki/artifacts, catalogs, schedules, audit/history and vault dependencies. Explicitly list exclusions and blocked path collisions, symlinks, executable bits or case/encoding differences. User chooses file exclusions; never presume ignored files are disposable or safe to upload.
2. **Quiesce and capture.** Stop launch/schedule/update writers, release terminal control and stop or explicitly checkpoint running providers. No live PTY/process migration. Consistently capture SQLite through its backup/snapshot protocol and files under a migration lease, plus logical versioned records, Git bundles/private refs, dirty bytes, transcript/native-ID state and manifests. Use current backup capture contracts where portable, but qualify new cross-OS/merge behavior. Missing/oversize/unreadable items fail capture visibly. A manifest proves byte transfer, not that excluded files were unimportant.
3. **Protect and transport.** Create an encrypted archive with authenticated integrity, per-file hashes, sizes and original installation/entity IDs. Encryption password/recovery material travels separately and is never logged. Upload over verified authenticated TLS or transfer the encrypted file offline. Bound unpack counts/size/depth; reject traversal, device files, hardlink/reparse escapes and archive bombs; do not run source Git hooks/config on import. Delete owned temporary plaintext scratch after use; explain encrypted archive retention separately.
4. **Import into quarantine.** Assign a migration ID and durable receipt; repeated upload/import is idempotent. Preserve original IDs as provenance and use a source→destination mapping for collisions. Map local owner to the chosen destination person explicitly; retain authorship/time, conversation IDs, session runs, audit source and links. Do not merge people by display name/email or overwrite destination policies/catalog. Imported schedules, publishing, grants and external side effects stay disabled. Retain imported audit as source history, with separate destination import events; no fabricated destination actions.
5. **Handle keys deliberately.** Export vault values only with explicit authorization, within encrypted transfer, then re-encrypt under destination keys and require fresh grants/consents. Never transfer Keychain/DPAPI blobs as portable secrets. Re-login providers when credentials are device/account-bound or vendor portability is unknown; preserve transcripts even if native resume is unsupported. Generate fresh desktop client and node keys on enrollment. Do not clone local IPC tokens, cookies, passkeys, device CA, signing/update keys, machine identities or push registrations. Backup archive/recovery keys remain separate; incomplete secrets can block affected launches without hiding projects/history.
6. **Verify before cutover.** Destination checks every manifest item/hash, record/reference counts, Git refs, dirty-file diff, artifact/history access and key mapping without logging values. Test a selected resumable conversation with no automatic tool action; record unsupported provider state as **History imported · Resume unavailable** and retain its files. User reviews the report and acknowledges limitations. A failed/lost acknowledgement is reconciled by migration ID/receipt, never blind import replay.
7. **Commit authority once.** Write a local durable read-only migration hold before destination activation; transfer/cutover IDs fence imported launches until the destination acknowledges activation. If interrupted, show pending and reconcile online; source cannot unfreeze itself automatically. Destination remains the sole writer after acceptance. Retain the source snapshot and encrypted export until the owner verifies backup/restore and explicitly deletes them. Going back requires stopping/fencing destination writes and a reverse export of new work; unlocking an old source snapshot alone would fork authority and lose later changes.

Optional **join as node** is a separate private/qualified enrollment after import. Disable the old local main's brokers and schedules for migrated projects; assign a fresh node identity and dedicated execution roots. Adopt existing desktop working files only after inventory/hash/path checks, rather than deleting/replacing a dirty checkout. Reauthorize provider/vault access and verify target paths/resume. Otherwise copy projects to a qualified server-side node; incompatible workloads remain archived and available locally read-only. Retaining unrelated standalone projects is possible in separate roots, never with both authorities writing the same checkout/provider state. No automatic two-way sync or concurrent-main merge is promised.

The release gate is preservation and demonstrable recovery: synthetic projects with unpushed branches, dirty/untracked/ignored files, no remote, LFS/submodules, private CLI state, revoked keys and ID collisions must survive. Test disk full, sleep/disconnect at every stage, repeated import, source/destination crash around activation, wrong password, tampered archive and failed rollback. Retain an accessible inventory of every unsupported item; “migration complete” requires byte/history verification and owner-reviewed capability limitations, not merely HTTP success.

## Offline, desktop lifecycle, accessibility and brand

Standalone opens and reads/edits local projects/history without internet, and supports Git/local tools already installed. Cloud inference, remote Git/issues, login/refresh and first-time CLI/image downloads report **Internet required** individually. Offline launch requires cached verified images/CLIs and an available qualified backend; a Mac VM must be installed in advance. Explicitly granted local-model endpoints are separate from cloud access. Keep actions to the running local main available while the internet is down; do not apply mobile's online-only rule to local authority.

For a disconnected dedicated main, reuse mobile's no-blind-replay policy: no queued launch/stop/approval/terminal input. Fetch fresh state before reacquiring a writer, reconcile event gaps and label ambiguous mutation results. Optional encrypted server summaries are off by default, bounded to 100 items/24 hours, partitioned by server/person and labelled with fetch time. No vault/terminal cache; purge on sign-out/detected revocation/expiry. Standalone's durable database/projects are primary data, not this disposable cache. App uninstall preserves them by default; a separate reviewed delete requires a recoverable export.

Close window detaches UI; with active local sessions, explain once that work continues and offer **Keep running** or **Stop and quit**. Explicit Quit shows affected sessions and consequences, with no silent forced deletion. With no active work, stop the local main/VM when quitting. Tray/menu-bar operation and start-at-login are opt-in; Linux must retain an in-window equivalent if its desktop has no tray. Display active authority, running/attention counts, open/lock/quit actions; tray presence is not proof a sleeping machine is executing. Default no wake lock; optional prevent-sleep-while-working needs consent and battery impact. On wake recheck main/process generation, credentials, capabilities and all streams. No always-on agents after OS logout promised in the per-user tier; managed boot services are separately qualified.

Idle targets to measure, not achieved results: under 1% of one CPU averaged over five minutes for shell+idle main, under 400 MiB combined RSS with no agents/VM, no periodic UI polling while hidden, bounded logs/scrollback/cache and timer coalescing. An active session needs event ingestion and bounded reconciliation even when hidden. Client-only mode should not start Python or the VM. Stop an unused VM after a visible grace period; never suspend a running workload to meet an idle metric. Publish VM/tool/image usage separately so app measurements do not conceal gigabytes of execution overhead. Test on battery, low-memory devices and extended offline use.

Local-main attention can produce native local notifications without APNs/FCM or relay. Client mode uses authenticated foreground/background connections only while awake; after full quit there is no guaranteed remote notification without a separately designed push path. Reuse mobile's generic default **Rynhult needs your attention**, opaque routing IDs, no actions that approve from a notification, and unlock plus authoritative fetch on tap. Ask permission at first useful alert, respect Focus/Do Not Disturb and deduplicate with active window/PWA where feasible. No delivery SLA; missed alerts remain in Inbox. APNs/Windows push/other transports, if later needed, require separate registration/privacy/revocation work, not a reused FCM token.

Follow DESIGN's dense desktop layout, Overview/Inbox/Sessions/Projects navigation, shared tokens/primitives and accessible text terminal. Preserve Charcoal `#10191F`, Slate `#263B46`, Frost `#B9CFE1`, Snow `#F0F3F3`, with contrast-adjusted Periwinkle interactions; status uses words/icons as well as colors. Use angular R master with half-mark-height clear space; generate proper `.icns`, multi-resolution `.ico`, Linux desktop and monochrome template tray assets, testing small-size legibility. Existing PWA icons are inputs, not proof of native suitability. Retain Inter/JetBrains Mono in web content and system fonts in native dialogs; do not add font downloads.

Accessibility gates: keyboard-only first-run/migration/lock flows, native menus and standard Cmd/Ctrl shortcuts without stealing terminal Ctrl-C, visible focus and an accessible terminal escape path; VoiceOver, NVDA and Orca reading order across host/WebView; 200% zoom/reflow, OS text size, high contrast/forced colors, reduced motion, IME/dead keys, selection/copy and multiple monitors/scaling. Avoid auto-focus that traps users in the terminal. Preserve screen-reader mode and text snapshots, including decision 0020's 20/minute/person limit and busy-audit warning. Native notification/connection announcements must not speak every terminal byte. Automated browser/axe tests supplement physical OS/assistive-technology testing. No screenshots of nonexistent app flows are presented as shipped UI.

## Phased delivery and estimates

The table retains the original work-breakdown phase numbers for historical
references and cost estimates, **not release order**. Remaining native delivery
is serial under decision 0073: Windows, macOS standalone, Linux desktop, then
iOS and Android. Already built Linux slices are reusable groundwork, not a
reason to ship the Linux desktop in v1 or reopen parallel native workstreams.

One engineer-week is five focused days. Estimates assume a solo maintainer familiar with this codebase, shared UI/main, first Claude/Codex cohort and access to physical Mac/Windows/Linux machines. **32–49 engineer-weeks base, plus 25–40% contingency**, owner review/device time and signing/release waits. These estimates cover sequential implementation; this task delivers only the design. Remaining remote-access/container qualification and optional VM/stronger Windows sandbox work are additional where not already available. Do not add WINDOWS-NATIVE's estimates wholesale: its runtime/provider work overlaps phase 3; full server-main publishing/helper parity exceeds this standalone scope.

| Phase | Effort | Exit evidence |
| --- | --- | --- |
| 0. Scope/security and shell spike | 2–3 weeks | Fresh local main without Linux deployment assumptions; compare Tauri/native transport, local auth/lock, events/terminal and remote TLS on each OS. Measure size/RSS. Reconcile absent afternoon decisions; choose support matrix and stop/re-estimate if boundaries fail. |
| 1. Linux standalone vertical slice | 4–6 weeks | Install → local project → qualified login/launch → attention/terminal → dirty work → quit/restart/recovery → export/restore. Auth/peer/privilege negative tests; container release gated on existing qualification, native exception explicit. Shared backend/browser regressions. |
| 2. macOS standalone | 5–8 weeks | Portable main, account/helper/TCC/Keychain, tmux recovery, optional qualified VM adapter; signed/notarized pilot. Native tool exceptions and VM limitations visible. Real sleep/lock/logout, VoiceOver, isolated credential tests. |
| 3. Native Windows standalone | 12–18 weeks | Broker/ConPTY/VT host, SID/service/pipe/NTFS and shell guard port from WINDOWS-NATIVE; portable main, local backup, first two providers. Clean machine with no WSL, native auth/hooks/resume, two-account attacks, signed updates and NVDA. Stop if dedicated-account/auth/guard contract cannot be met. |
| 4. Server client and migration | 5–8 weeks | Versioned scoped transport, private pairing/sign-in and separate node enrollment; quarantined idempotent import, verification, fenced cutover/recovery across all OS pairs and existing-main collisions. Public mode only after REMOTE-ACCESS gates. |
| 5. Release hardening and docs | 4–6 weeks | Signing/key recovery, interrupted upgrades/schema recovery, offline install media, battery/resource matrix, accessibility and full user paths; accurate Guide screenshots and supported capability/version matrix. Owner acceptance before broad release. |

A client preview may ship earlier within its post-v1 platform turn if labelled client-only; standalone remains mandatory for each final desktop product. Additional four providers, Intel/ARM variants, distro families, store channels, full server feature parity and a general Windows sandbox need separate estimates after spikes. Reserve initially 2–4 engineer-days/month for three desktop OS releases/dependencies plus yearly OS qualification and incidents; shared mobile/backend changes may increase this. A rewrite needs a new estimate, not a speculative promise of savings.

Share with mobile: React components/xterm/accessibility fixes, API capability/version schemas, events cursor/reconnect and uncertain-result logic, enrollment lifecycle/contracts, generic notification routing, redaction and cache rules. Swift Keychain/TLS pieces may be reusable on macOS after entitlement review; Android Kotlin code is not a Windows/Linux key-store implementation. Desktop local-main supervision, execution isolation, IPC/bootstrap, tray, migrations and signing/installers are additional. A common bridge schema can have different platform transports. Keep the proposed current/previous API contracts and six-month deprecation window consistent across clients if accepted; do not force desktop to wait for mobile push or a public relay.

Future implementation validation layers: pure unit tests for capabilities/paths/mapping/bridge envelopes; real OS integration for IPC/store/ACL/ConPTY/signing; API tests for auth/launch/import/cutover; browser tests for the shared complete user path; physical desktop tests for offline, sleep, assistive tech and update/recovery. Run Python with `PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q`; shared UI changes additionally require typecheck/build and relevant Playwright/axe tests. Capture sanitized test logs and fictional-data screenshots. No desktop app, native key store, installer, isolation boundary or migration has been implemented or qualified by this design task.

## Owner questions with recommendations

The standalone requirement is settled. These choices are pending; reconcile the missing afternoon answers before asking them again.

| Question | Recommended answer | Consequence |
| --- | --- | --- |
| Which shell and investment first? | Approve only the 2–3 week comparative spike first; prefer shared React/Python with Tauri if transport/accessibility gates pass. | Avoid a new main/UI rewrite; retain native-host fallback. |
| What does first standalone release include? | One local owner, projects/files/history, first Claude/Codex providers, local backup/export and honest capability limits. | No server required; publishing/HA/services parity remains separately funded. |
| Launch order and supported hardware? | Linux reference → Apple silicon Mac → Windows 11 x64 without WSL; qualify remote transport early on all three. | All three remain required; select actual minimum OS/distro versions from test machines, then fund additional architectures. |
| Permit one-time admin setup? | Yes for constrained account/helper/runtime provisioning, never for each ordinary launch or an elevated main. | Refusal leaves project management/client mode available, with local execution unavailable until safely provisioned. |
| Native execution and Mac VM costs? | Keep owner-only per-project native exceptions and dedicated agent identity; prefer qualified Linux containers/Mac VM. Windows native starts **Not isolated**. | If stronger Windows isolation is mandatory, fund a separate VM/sandbox phase rather than weaken labels. |
| What migration guarantee? | Include verified project/history transfer, secret rewrap by consent, retained source and fenced single-authority cutover in release scope. | Provider-native resume is promised only for qualified combinations; unsupported history/files stay accessible. No automatic two-way sync. |
| Which distribution and signing custodian? | Owner-held direct signed macOS/Windows distribution and one Linux package baseline; defer stores, keep separate updater and recovery keys. | Budget CI/signing accounts/test hardware; no account purchases/config changes authorized here. |
| What update policy? | Automatic checks/download optional; explicit safe activation, defer during work, retain compatible recovery snapshots and qualified CLI pins. | Security emergencies need a visible owner choice; no silent destructive downgrade or vendor drift. |
| Tray, login, notifications and sleep? | Opt-in tray/start-at-login, generic local notifications, no wake lock by default, explicit quit behavior. | Laptop sleep/quit may stop work; unattended always-on operation belongs on a dedicated main/node. |
| What effort/support budget? | Fund phased 32–49 weeks plus contingency, first two providers, 2–4 days/month maintenance; re-estimate after phase 0. | This is a substantial platform program for one maintainer, not an installer reskin. |

## Evidence boundary

Official platform/framework references above were consulted on 2026-10-24. They establish available mechanisms, not Rynhult compatibility, performance, store approval or security qualification. Architecture, size/resource budgets and effort are engineering proposals/inferences. Missing afternoon decisions, physical OS tests, signed artifact sizes, real CLI recovery across OS boundaries and existing-main import fidelity remain explicit implementation gates. This document changes no running configuration, exposure, installer or product permissions.

## Phase 0 results — Linux

### First slice: 2026-10-24

The owner authorized the 2–3 week comparative spike, preferring existing React
and Python in Tauri; first release is standalone, launch order Linux → Apple
silicon Mac → Windows 11 x64 without WSL, one-time admin setup only, signed
direct distribution before stores. This task instruction is the authority for
those answers. Decision 0050 now exists but contains mobile/remote/backup answers,
not desktop approval; the earlier statement that the file is absent is historical.
Other unanswered design questions above remain proposals.

**Go/no-go: NO-GO for building/qualifying on this machine now. Tauri’s Linux
architecture verdict is deferred, not rejected.** The required stop condition
was reached before implementation: Ubuntu 24.04.5 LTS has no
`libwebkit2gtk-4.1-dev` or GTK development headers and no `pkg-config`.
No Rust/Tauri installation or build was attempted after this discovery.
No standalone main, token bridge, shell, installer or performance result is claimed.

Exact missing direct Ubuntu prerequisites: `libwebkit2gtk-4.1-dev`,
`libxdo-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev`; additionally
`pkg-config` for discovery and `patchelf` for AppImage packaging.
Missing transitive headers: `libgtk-3-dev`, `libjavascriptcoregtk-4.1-dev`,
`libsoup-3.0-dev` (normally pulled in by WebKitGTK development dependencies).
`build-essential`, `libssl-dev`, `curl`, `wget`, `file` are installed.
The owner must provision packages outside this session before resuming.
Package names were checked against [official Tauri prerequisites](https://v2.tauri.app/start/prerequisites/)
and local `dpkg-query`; this was a prerequisite check, not a failed compilation.

| Spike gate | Evidence / result |
| --- | --- |
| Fresh standalone main without deployment assumptions | Not implemented; blocked before build. Existing installs unchanged. |
| Child lifecycle, isolated config state, authenticated IPC | Not implemented or verified; must include negative HTTP/event/WebSocket and shutdown tests. |
| Existing UI, events, terminal and remote TLS | Not exercised in an embedded WebView. Native-host comparison pending. |
| Startup time | Not measured; no executable. |
| Idle CPU under 1% of one core, five-minute average | Not measured; no shell/main process tree. |
| Combined idle RSS under 400 MiB | Not measured. |
| Compressed/installed package size | Not measured; no artifact. Planning allowances above remain estimates. |
| Keyboard and screen-reader exposure | Not verified. No display session; AT-SPI core not installed and Python AT-SPI binding absent. Needs physical Linux desktop/Orca testing. |
| Signing, update, isolation and first-release path | Not qualified by this prerequisite-only slice. |

`desktop/README.md` records exact prerequisites and the next test/build/measurement
sequence. No system package, machine configuration, service or existing server
behavior changed. No browser screenshot can represent an app that was not built.

macOS still needs a real Apple-silicon WKWebView build, portable bundled main,
private transport/authentication, Keychain, account/helper/TCC boundaries,
VoiceOver/keyboard, sleep/lock/recovery, signed/notarized artifacts and the same
resource measurements. Windows still needs Windows 11 x64 with no WSL,
WebView2/Python portability, named-pipe ACL/authentication, dedicated agent SID,
broker/ConPTY recovery, Credential Manager/DPAPI, NVDA/keyboard and signed
installer/update/resource evidence. Neither platform is qualified by Linux
prerequisite discovery. Continue the comparative spike after provisioning;
do not choose a framework or commit to release based on this blocked slice.


### Second slice: 2026-10-24 — Linux source proof

**NO-GO for qualifying/selecting the Tauri path on Linux yet.** The build and
companion architecture are viable enough to continue the comparative spike;
this is not evidence to reject Tauri. Required embedded WebKitGTK accessibility,
complete process-tree measurements and self-contained packaging are still open.
There is no standalone desktop release. The prerequisite-only first slice above
is historical: the owner has now installed the listed development packages.

The continuation starts from `acc/desktop-spike-linux-8442b440`, creates
`desktop-spike-linux-2`, and merges `main` (`12a13c29`). The proof is entirely
under `desktop/`; no existing-server source/default/installer behavior changes.
The Tauri host builds against the system WebKitGTK, starts the existing Python
main using a separate entry point, and is wired to navigate to the existing built
React UI. A kernel-selected loopback listener, fresh 256-bit app capability,
private inherited stdin, HMAC-authenticated readiness and a native-set HttpOnly
SameSite cookie form the outer transport boundary. All HTTP/event/assets and
WebSocket traffic passes the capability/Host/Origin guard before the existing
person authentication and CSRF checks. No token is passed in URLs, argv or the
child environment. State/helper/broker paths stay beneath a separate private
user-config profile. EOF requests graceful main shutdown; the host waits/reaps,
with bounded forced termination for a stalled child. Normal quit with a visible
window remains a physical-desktop check.

This source proof retains existing setup/login rather than inventing an
unauthenticated local-owner route. It deliberately starts no broker or agent;
projects can persist in SQLite, while broker-dependent execution/terminal/files
are unavailable. The window title states execution is unavailable. Native owner
bootstrap, lock/key storage, production capability negotiation, active-session
quit confirmation and release isolation remain future work. Host-scoped loopback
cookies, WebView storage and local-origin threats need explicit qualification.

The system-library gate now passes: WebKitGTK/JavaScriptCore 2.52.6, GTK 3.24.41,
libsoup 3.4.4, AppIndicator discovery 0.5.90 and librsvg 2.58.0. Build inputs:
Ubuntu 24.04.5 x86_64, Rust 1.99.1 (minimal home-local toolchain), pinned Tauri
2.12.5 / tauri-build 2.7.4 and Cargo.lock, existing React production assets.
An older Tauri pin resolved incompatible runtime-family versions; the successful
build uses the current pinned pair. No system packages or machine settings were
changed by this session.

Automatic approval review rejected installing the explicitly authorized
home-local Tauri CLI. It also rejected the command assembling the project-local
Python runtime. Neither action was retried; no live Inbox request/agent tool was
invoked. Cargo project dependencies were sufficient for a direct release binary
build, but no bundled interpreter or CLI-generated installer exists. A staging
copy of Python dependencies is not a self-contained app or measured package.

| Spike gate | Second-slice evidence / result |
| --- | --- |
| Fresh local main / separate state | PASS for source proof: real main, fresh SQLite, authenticated project creation/listing persists across two launches; private config state and cleared child environment. External Python still required. |
| Authenticated IPC and child lifecycle | PASS for tested boundaries: HTTP/assets/events capability denial, Host/Origin denial, WebSocket denial before accept, normal person-auth retained; Rust readiness forgery rejection and child EOF/timeout/reaping tests. Headless native startup reaches authenticated readiness and reaps the main after GTK fails. Visible-window quit and crash matrix pending. |
| Existing embedded UI / events / terminal / TLS | Host wiring compiles; actual WebKitGTK display unverified. Chromium renders the shared UI and navigates with keyboard. No execution broker, live terminal/event workflow, remote TLS or native-host comparison qualified. |
| Startup time | Main-only fresh-profile, warm-filesystem launch-to-ready measured separately below. Native headless host reaches authenticated main readiness, then cannot initialize GTK. No cold or warm launch-to-interactive result. |
| Idle CPU / RSS | Five-minute main-only measurement below. Complete shell/main/WebKit tree cannot be measured without a display; under 1% / 400 MiB gates remain OPEN. |
| Package size | Release shell binary measured below; bundled Python and signed installer do not exist, so compressed/installed **package** sizes remain OPEN. |
| Keyboard / screen-reader exposure | Supplemental Chromium Tab reaches skip link/navigation, Enter opens Projects, semantic heading/navigation tree captured. No display; no AT-SPI runtime/Orca/Python binding. Actual embedded keyboard, Orca reading order, terminal escape/IME/zoom remain OPEN. |
| Signing / updates / isolation / standalone release path | Not qualified; no signing, installer, broker or agent execution. |

Sanitized test logs, the Chromium-only screenshot/tree, headless native failure
log and measurement JSON are in this session's `.acc/artifacts/`. They must not
be presented as a screenshot or accessibility qualification of a native window.
`desktop/README.md` documents reproduction and exact limits.

Measured on this shared 20-logical-CPU Linux host (kernel 6.8.0-147, glibc 2.39),
using the existing Python 3.12 test interpreter and production React assets:
three fresh profiles with warm filesystem caches reached main readiness in
**2,259 / 2,356 / 2,858 ms**. These are not cold-cache or UI-interactive launches.
Over **305.50 seconds**, the idle Python companion tree averaged **0.246% of one
CPU core**, **129.20 MiB RSS**, with **129.20 MiB peak RSS**. Other test/build work
was running on this shared machine; these are preliminary samples, not a quiet
reference-machine benchmark. Shell/WebKit/provider/VM costs are excluded, so the
combined app resource gates remain open. The native headless failure-path run
reached authenticated main readiness in **1,729 ms**, then failed GTK initialization.

The stripped optimized host executable is **7,306,464 bytes (6.97 MiB)**;
gzip of that executable is **2,433,625 bytes (2.32 MiB)**. Existing built React
assets total **3,668,319 bytes (3.50 MiB)**. These are component measurements:
no interpreter/dependency payload, system-WebView libraries, catalog/Guide,
installer metadata or signatures are included in the executable figures.
**No compressed or installed standalone-package size is available.**

macOS still requires a real Apple-silicon WKWebView build, bundled portable
Python, private authentication/transport, Keychain, account/helper/TCC setup,
VoiceOver and keyboard, sleep/lock/quit/recovery, signed/notarized artifacts and
complete resource measurements. Windows still requires a Windows 11 x64 machine
without WSL, WebView2 and native Python portability, named-pipe ACL/authentication,
dedicated agent SID, broker/ConPTY recovery, Credential Manager/DPAPI, NVDA,
signed installer/update and equivalent measurements. Linux compilation does not
qualify either platform. Retain the approved launch order and comparative spike;
resolve Linux's remaining gates before selecting a framework.

Verification for this slice: direct locked release build succeeded; four Rust
supervision/readiness tests passed; desktop Python checks passed **9 tests with
one opt-in measurement skipped**, and the five-minute measurement passed
separately. The shared UI typecheck/production build and 28 existing
`test_auth`, `test_public_defaults` and `test_migration_portability` regressions
passed. The required full Python suite was attempted, then cleanly interrupted
to bound this spike: **318 passed, 2 skipped in 487 seconds**, with no failures
reported before interruption. **The remaining full suite is unverified.**
Ruff and wiki checks passed (wiki check warns that this account has no optional
denylist); Rust formatting automation was unavailable because the authorized
minimal toolchain does not include rustfmt. No remote/push/PR was attempted.


### Third slice: 2026-10-24 — real window, self-contained package

**Linux verdict: GO for the Tauri path, as the framework to carry into the macOS
and Windows turns. It is not a release.** This replaces the second slice's
no-go. Every Linux gate this machine can test now passes; memory passes only
narrowly (below). The remaining Linux
work is either product work Tauri doesn't decide (owner bootstrap, main watchdog,
signing, updates) or needs a real desktop login with Orca (listed below). None of
it has so far pointed away from Tauri. The small native-host alternative was
still not benchmarked; nothing measured here argues for it on Linux.

Branch `desktop-spike-linux-3` merges `desktop-spike-linux-2` and `main`. The
owner approved the Tauri CLI 2.12.4 in `~/.cargo` and a bundled Python inside
the worktree. Everything still lives under `desktop/`; installed servers are
unchanged.

What was built and run on this machine (Ubuntu 24.04.5 x86_64, kernel 6.8.0-147,
20 logical CPUs, shared with other work):

- **Debug and release hosts** (`cargo build`, `cargo tauri build`), Tauri 2.12.5.
  The four Rust supervision/readiness tests pass.
- **Bundled Python:** python-build-standalone CPython 3.12.15 via uv
  (SHA-256 checked against uv's manifest). Dependencies come from a hash-locked
  `desktop/requirements-linux.lock`, pinned to the versions the main is tested
  with. Tk/IDLE/tests/pip, the unused shared libpython and debug symbols are
  removed. Bytecode is precompiled in hash-checked mode. Timestamp `.pyc`
  files went stale when Tauri copied them, which doubled main startup.
- **A self-contained .deb** built by `desktop/package.sh`: host, bundled runtime,
  main, server package with built UI, catalog and Guide. It was extracted with
  `dpkg-deb -x` into a scratch folder and run from there, not installed. The main
  runs on the package's own interpreter (checked through `/proc/<pid>/exe`),
  never the system Python. The package needs only the system's WebKitGTK 4.1 and
  GTK 3.
- **The real window, headless:** Xvfb is not installed here. Tests use GTK's own
  Broadway display backend (`broadwayd`, already installed) with software
  rendering, plus a private D-Bus bus that carries AT-SPI and activates no
  services. Real key events go in through Broadway's web client in Chromium.
  `desktop/tests/test_native.py` (`DESKTOP_NATIVE_CHECK=1`) automates all of it.

| Gate | Result on this machine |
| --- | --- |
| Builds (debug, release, package) | **PASS.** Debug host 195,592,208 bytes (unstripped). Release host 7,292,600 bytes (2,432,378 gzip -9). |
| Self-contained package | **PASS.** `.deb` **66,702,946 bytes (63.6 MiB)**, installed size **158,139 KiB** (154 MiB; 120.9 MB of it the Python runtime, 19.4 MB the Guide with screenshots, 9.1 MB server package and UI, 2.8 MB catalog). Unsigned, not checked for reproducible builds. No AppImage: Tauri would download linuxdeploy into the account cache. |
| Whole-app startup | **PASS** (no budget was set). Six fresh profiles from the extracted .deb, warm file cache: main authenticated-ready **1,451–1,645 ms**, launch to the interactive setup screen (exposed over AT-SPI) **2,109–2,337 ms**. Broadway, software rendering. |
| Idle CPU < 1% of one core (5 min) | **PASS: 0.333%** over 300.4 s from the .deb on the setup screen (host 0.013, main 0.256, WebKit web 0.043, network 0.020). Covers the host, WebKit web and network processes and the main; the Broadway server is excluded. An earlier 300.8 s run gave 0.342%. |
| Idle memory < 400 MiB | **PASS, marginal: 375.9 MiB PSS mean** (377.6 peak), about 24 MiB of headroom: host 107, main 129, WebKit web 118, network 23. Summing each process's RSS gives 540.9 MiB, because the WebKit/GTK libraries shared by the host and web process are counted twice. PSS splits them. Software rendering on the setup screen only; GPU paths and a signed-in UI with live events will differ. Treat 400 MiB as a budget to defend, not a margin. |
| Keyboard | **PASS on Broadway.** Typing reaches the focused Setup token field. Tab moves to Continue; Shift+Tab walks back through Setup token to Copy setup command. One Tab past the last control leaves no focused accessible object (focus leaves the web content); the next Shift+Tab returns. |
| Accessibility tree (AT-SPI) | **PASS for exposure.** The frame, the header bar's Minimize/Maximize/Close buttons, and the WebKit document (`document web` "Rynhult") with heading "Set up Rynhult", entry "Setup token", push buttons "Continue" and "Copy setup command", and focusable/focused/showing states are all reachable over D-Bus. WebKitGTK answers `GetRoleName` with an empty string; numeric roles are correct, and those are what Orca uses. |
| Clean shutdown on quit | **PASS.** Pressing the window's Close button through AT-SPI exits the host with code 0 in about 0.5 s. The main logs completion of its lifespan shutdown, the host never needs its forced kill, and the WebKit processes are gone. SQLite `quick_check` is ok. |
| Clean shutdown on host crash / SIGTERM | **PASS.** After SIGKILL or SIGTERM of the host (no Tauri exit handler runs), the main sees EOF on its private stdin and finishes a graceful shutdown within about 0.45 s. WebKit processes exit with the host. Repeated on the release build and eight times on the .deb. |
| Main crash while the window is open | **GAP (release blocker, not a Tauri limit).** The host keeps running and the window shows stale UI. Release needs a watchdog plus a recovery screen or restart. |

Evidence (session Artifacts tab, ignored by Git) includes per-run JSON reports,
the AT-SPI tree and a screenshot of the native window drawn by Broadway. The
release-build and .deb results match. The window screenshot shows the setup
screen telling the person to run `sudo cat …/setup_token` on "the server".
That is wrong for a desktop app, and native owner bootstrap remains required.

What this machine could not verify, and why:

- **Orca speech and reading order, AT-SPI event delivery through the registry.**
  at-spi2-core (registry daemon, bus launcher), Orca and pyatspi are not
  installed, and this session may not install them. The tree was read directly
  from each application's root accessible instead; that shows exposure, not what
  a screen reader announces.
- **A real X11/Wayland session:** window-manager focus, GPU/DMA-BUF rendering
  (WebKit fell back to software; `/dev/dri` is not accessible to this account),
  HiDPI/zoom, IME, clipboard, tray, notifications, sleep/lock and log-out
  handling with a session manager.
- **Signed packages and updates,** an installed (not extracted) package under
  `/usr`, AppImage, and cold-cache startup.
- **Anything with execution:** the spike still starts no broker or agents, and
  terminals and events under load were not exercised.

Before a Linux release: native owner bootstrap (no server setup token), app
lock and key storage, a main watchdog and recovery, active-session quit
confirmation, the execution account/broker, signing and an update channel. Then
an owner-run pass on a real GNOME/KDE desktop with Orca.

**What macOS needs for its turn** (MacBook, Xcode installing now):

- Xcode command-line tools plus Rust (`rustup`, `aarch64-apple-darwin`) and the
  Tauri CLI 2.12.4, all account-local, as here.
- `build-runtime.sh` ported to the `aarch64-apple-darwin` python-build-standalone
  build, with a macOS lock (`--python-platform aarch64-apple-darwin`).
  Native-extension wheels need codesigning inside the bundle. The resource
  lookup already uses Tauri's `../Resources`.
- `cargo tauri build --bundles app,dmg`, and a Developer ID certificate plus a
  notarization profile from the owner. Without them, only unsigned local runs.
- The same lifecycle tests against WKWebView: Cmd+Q, closing the window, a host
  crash, sleep/wake. VoiceOver and the Accessibility Inspector cover keyboard
  and exposure; startup, idle CPU and memory are measured on battery as well.
- Keychain for the app capability or lock key, and the helper/TCC prompts for
  the execution account.

**What Windows needs for its turn** (windows-test-pc, Windows 11 x64, no WSL):

- Run it natively under a Windows desktop login, not from windows-test-pc's WSL node.
  `docs/WINDOWS-NATIVE.md` ("Phase 0 results — windows-test-pc") found that the node is
  not isolated from the Windows account: interop is on, `C:` is readable and UAC
  never prompts. Builds and installer tests there need the owner's elevation
  and, ideally, UAC restored to its default level.

- Visual Studio Build Tools (MSVC, Windows SDK), Rust `x86_64-pc-windows-msvc`,
  the Tauri CLI and the WebView2 runtime (Evergreen), installed by the owner.
- The `x86_64-pc-windows-msvc` python-build-standalone build and a Windows lock.
  The companion needs a replacement for Unix stdin EOF and `0700` modes: an
  inherited pipe handle, or a named pipe with an ACL for the user's SID, and an
  ACL'd profile in `%LOCALAPPDATA%`.
- `cargo tauri build --bundles nsis` or `msi`, with an owner-provided
  code-signing certificate.
- The same lifecycle tests: closing the window, killing the host in Task
  Manager, logoff/shutdown, sleep. NVDA and Accessibility Insights cover
  keyboard and exposure; the same resource measurements apply.
- Credential Manager/DPAPI for secrets, and a separate agent SID plus
  broker/ConPTY design for execution.

Verification for this slice is in the handoff note
`docs/handoffs/2026-10-24-a8e85a9e2b88-desktop-spike-linux-3.md`.

## macOS turn — prepared on Linux (#279 baseline)

2026-10-25 (owner approval 2026-10-24). Preparation from `desktop-spike-linux-3` on
`desktop-mac-prep`. **No Mac build, launch, accessibility or performance result
is claimed.** Linux's GO for Tauri is not a macOS verdict or a desktop release.
Reproduction and VoiceOver steps are in `desktop/README.md`.

| Surface | Prepared and Linux-checkable | Still needs the Mac |
| --- | --- | --- |
| Runtime | Native host selection for `cpython-3.12.15-macos-aarch64-none`; uv 0.13.1 downloaded/hash-verified PBS build 20261013 for layout inspection. Keep its required `libpython3.12.dylib`, prune Tk/tests/pip/headers, use `strip -x`, repair ad-hoc Mach-O signatures and precompile hash-based bytecode. Script dispatch/pruning/signature failures are fixture-tested. | Execute the interpreter and complete runtime assembly; Mach-O strip/sign behavior and actual dependency imports. Linux cannot run these arm64 Mach-O files. |
| Dependencies | Separate `requirements-macos-arm64.lock` generated with `uv pip compile --python-platform aarch64-apple-darwin --generate-hashes`, constrained to tested Linux versions. Wheels required except hashed pure-Python `http-ece==1.2.1`; its setuptools/wheel build dependencies are separately hash-locked. Public artifact bytes can be audited without installation. | Native installed dependency/main startup, CFFI/ctypes callbacks and complete runtime behavior. A dependency-install probe on Linux was denied; no approval tool or alternate installation was used. |
| Bundle | `.app` + `.dmg` configuration, provisional macOS 13.0 floor, loopback-only ATS allowance and hardened runtime with an empty entitlement plist. `package.sh` stages shared resources, rejects wrong native runtimes, signs nested code before the outer app, and supports explicit unsigned or signed/notarized paths. | Actual app/DMG generation, copied resource paths, native library loading, sealed nested signatures, Developer ID/notarization/stapling and offline Gatekeeper. |
| Rust | Same supervisor/HMAC transport modules type-check for `aarch64-apple-darwin` using the portable core feature. Five real-process Rust tests cover readiness, HMAC rejection/acceptance and graceful EOF/Drop cleanup on Linux. | Full host compilation/linking with Xcode/SDK and WKWebView APIs. The attempted full cross-check stopped at Objective-C compilation: Linux `cc` cannot handle Apple architecture/SDK flags. |
| Qualification harness | `desktop/tests/mac-smoke.sh` builds the unsigned app, starts fresh profiles, records main-ready/observed-tree idle measurements, requests native quit by exact PID and checks SIGTERM/SIGKILL EOF shutdown. The monitor's success/error cleanup is fixture-tested on Linux. Written VoiceOver/keyboard steps are ready. | Run the script on the coordinator's logged-in Apple-silicon Mac; prove native quit/close/sleep/lock behavior, VoiceOver speech and whole-app resource accounting. |

The declared macOS 13.0 floor is a spike baseline, not a tested support matrix.
Intel is deferred rather than silently compiling native dependencies: wheel-only
resolution with the current `cryptography==50.0.2` pin fails for
`x86_64-apple-darwin`. Its standalone Python archive existing is not enough.

There is **no unsigned-executable-memory, JIT or library-validation exception**
in the entitlement file. CPython is not itself a JIT; bundled native modules
must be signed with the same Developer ID. If a real callback path demonstrates
an executable-memory requirement, record that evidence and review a narrowly
scoped Python entitlement; do not grant it broadly to the host preemptively.
The app is not App Sandbox-isolated and still starts no brokers or agents.

`--unsigned` means no Developer ID/notarization; local ARM code still needs
ad-hoc signatures. Signed trials require owner-supplied environment variables:
`APPLE_SIGNING_IDENTITY` and either `APPLE_API_ISSUER`/`APPLE_API_KEY`/
`APPLE_API_KEY_PATH` or `APPLE_ID`/`APPLE_PASSWORD`/`APPLE_TEAM_ID`.
No credentials or signing assets are committed. No signing account/keychain,
TCC permission, Gatekeeper or machine configuration was changed on Linux.

Mac smoke startup is authenticated **main ready**, not UI interactive. Its
RSS/CPU sample covers the host and observed descendants; independent WebKit XPC
services may not be in that tree, and RSS is not Linux's PSS metric. No idle
resource gate can be declared passed from that lower-bound scope. Whole-app
measurement, on-battery runs and spoken accessibility still need owner evidence.

The phase 1 integration below now supplies native owner bootstrap, a single
shared lib-crate watchdog and execution provisioning. App lock/Keychain, recovery
UX, TCC qualification, active-session quit policy, signing/update qualification
and real desktop testing remain release gates. The Linux
package and existing installed-server behavior remain separate from this prep.

## Phase 1 Linux session slice — design (2026-10-25)

The desktop entry point alone composes the existing Python main (SQLite,
Supervisor, compiler, provider adapters, Files/history and terminal WebSocket),
React/xterm and `acc.broker.server`. Installed server entry points and defaults
are unchanged. The main runs as the desktop owner; the socket-activated broker
and tmux/Claude/Codex run as a newly created locked, unprivileged account.
A native capability-authenticated first navigation bootstraps one local owner
and a normal app session/CSRF cookie; no public owner-bootstrap API is added.
Existing phase-0 profiles containing other people are refused rather than adopted.

An administrator runs the packaged `desktop/setup.py install --owner <login>`
once, after reviewing its default dry-run JSON plan (execution requires
`--apply`). It creates `rynd-<owner uid>` with a locked password and private
home under `/var/lib/rynhult-desktop/<uid>/rynd-<uid>`, an owner-readable agent run
spool at `run/rynd-<uid>` (named-user default ACLs), an agent-owned projects root, root-owned configuration under
`/etc/rynhult-desktop/<uid>`, and uniquely named systemd socket/service units.
The socket under `/run/rynhult-desktop/<uid>` is owned by the owner's UID with
mode 0600, and the broker also checks the exact owner UID. The agent account is
also expired, so PAM refuses SSH keys, su, cron and at; only its unit runs it,
with NoNewPrivileges and ProtectHome (review 2026-10-25).
No sudoers rule, setuid binary, Docker group, network listener, general root
command executor or recurring elevation is installed. The broker executable,
Python and hooks come from the root-owned packaged resources; setup rejects
writable resources and existing accounts. The app discovers only its own
root-owned configuration, checks the account differs from its owner, and uses
the existing peer-credential broker client. Missing setup leaves execution
blocked. This session tests dry-run only and creates no host users or services.

This slice supports native execution only, requiring an audited owner approval
for each project, explicitly **Not isolated · Separate agent account**. Sessions
share that account's files and provider credentials. No container qualification
or silent same-user fallback is implied. The desktop exposes only Claude and
Codex launch; the existing Providers login flows authenticate the agent account.
CLIs are found in that account's `~/.local/bin` and system bin directories;
packaged session tools are also on the broker's PATH,
never the owner's PATH or credentials. The owner can explicitly install/update
an exact npm version of
`@anthropic-ai/claude-code` or `@openai/codex` from Providers. The shared broker
uses the existing registry-only tooling executor with an agent-home prefix.
All running sessions must stop first; launch and installation are serialized.
No system manager, host package install or automatic update is involved.
Setup requires Git, tmux, ACL tools, Python 3, Node and npm already installed.
Existing provider discovery/version/auth probes report
missing CLIs and login failures. Signed pinned CLI updates remain a release gate.

Uninstall runs the same setup program's `uninstall` plan with administrator
approval, locks and expires the account, disables the exact desktop
socket/service, removes the agent's crontab, kills its remaining processes
(KillMode=process otherwise leaves tmux running) and removes only its units and
root configuration. It preserves the locked account, projects, credentials,
spool and desktop profile so history/files can be recovered/exported. Account
and data deletion is a separate administrator operation after backup review.
Removing the app package alone does not delete work.

Out of scope: Mac/Windows account provisioning and terminal portability,
containers, public publishing, services/Compose, additional people/nodes,
schedules/teams, automatic CLI installation/updating and signed CLI qualification, OS lock/key-store
integration, signed distribution and new backup/migration formats. Existing
local history/export remains available; full local backup/restore qualification,
active-session quit UX and real desktop/assistive-technology qualification
remain release work. Closing this slice's app detaches; its dedicated broker
and tmux retain running work for reconnection, with existing reconciliation on
reopen. No session survives a reboot as a running process.

### Phase 1 evidence and qualification limits

The source slice reuses the server paths and passes the headless shared UI path
for fake Claude and Codex, including exact CLI installation with fake npm,
project creation, native consent, xterm, viewer reload and stop. A test-owned
companion additionally runs both fake providers in the actual Tauri/WebKitGTK
window and verifies rendered terminal bytes and `terminal.attach` audit entries.
Only fixtures simulate the account UID; production has no same-user test switch.
The native fixture requires a private 96-DPI GTK shim: Broadway's default screen
resolution is -1, which WebKit otherwise interprets as a negative device scale.
The shim is compiled under the fixture's temporary directory and is never
packaged. It changes no account/machine GTK settings.

Privileged setup is never applied on the development host. Automated Arch
qualification on 2026-10-25 used a disposable **rootless Podman guest with systemd
PID 1**, a fresh local PKGBUILD/makepkg/pacman package and fake provider CLIs only.
It passed real separate-account execution, socket activation/ACL and peer checks,
CLI synthetic login paths, hook ingestion, live PTY and owner PAM/user-manager
logout/login persistence, then clean uninstall preserving locked accounts/work.
The broker runs as a system service with `User=agent`; the owner user manager,
not an agent user unit, was exercised. The qualification also carried forward
the Debian PTY-import and run-root owner-read fixes, and fixed uninstall leaving
a stale socket by explicitly unlinking that owner's stopped runtime endpoint.
The UID-kill cleanup was intercepted as a proven-empty no-op after stopping owned
sessions/units/tmux; forced termination of running agents remains unverified.
See [Linux distributions](LINUX-DISTROS.md#rootless-container-qualification-2026-10-25)
and the repeatable `desktop/tests/arch-phase1.md` harness.

Physical desktop/derivative kernel and LSM behavior, real CLI downloads/auth and
inference, signatures, and verified backup/restore remain release gates. Real
CLI auth probes skip with a reason unless a disposable test account is explicitly
selected and already installed/logged in. No real inference was sent. Mac and
Windows provisioning/terminal work remains in the session handoff.

### Coordinator portability update (2026-10-25)

The coordinator reports a Windows 11 windows-test-pc Tauri build/run with 16 portability
fixes: LockFileEx locking, stdin-EOF shutdown, `build-runtime.ps1`, a per-user
43.6 MiB NSIS installer and real-UI idle measurements of 0.58% CPU / 280 MiB.
That patch will land after Mac prep and is not present in this checkout; these
are coordinator-reported results, not verification performed by this slice.
Preserve those fixes when merging. This slice keeps new Linux account lookup
behind a platform check and imports pwd/fcntl only on the relevant platform.
Its small profile-lease adapter uses a Windows byte-range lock pending
consolidation with the coordinator's LockFileEx code; Windows behavior is not
qualified here. The owner bootstrap/session-cookie flow is platform-neutral
and skips the server setup-token screen; retain it in the incoming hosts.
The desktop UI is built into `desktop/web-dist`, and packages must include
`main.py`, `execution.py`, `broker.py`, `setup.py` plus the platform runtime/UI.
Linux broker/setup remains Linux-only. The following macOS port adds its own
account/setup and launchd adapter; Windows execution remains unavailable.

## Phase 1 macOS session port — design (2026-10-25)

Use the shared desktop owner bootstrap, project-native consent, supervisor,
provider adapters, terminal and CLI tooling. Only desktop composition changes;
installed server defaults and the Linux installer remain unchanged. macOS uses
one root-owned system LaunchDaemon whose **UserName/GroupName are the new agent**,
not root. launchd creates the protected UNIX socket; the broker obtains its
listener through `launch_activate_socket` and uses the shared Broker handler and
macOS LOCAL_PEERCRED checks. It never binds a writable replacement socket.

Choose a **one-time Installer .pkg step** for authorization, rather than a
resident SMAppService root helper: this slice needs account/folder/daemon setup,
not privileged everyday commands. macOS Installer supplies the administrator
prompt and runs the fixed packaged postinstall. No deprecated
AuthorizationExecuteWithPrivileges, AppleScript shell elevation, sudoers rule or
general root RPC is added. SMAppService is appropriate if a later signed app
needs a maintained helper lifecycle; adding that persistent authority is outside
this slice. The developer package is unsigned and must be inspected before use;
release needs Developer ID Installer signing/notarization and real-device review.
See [Apple Service Management](https://developer.apple.com/documentation/servicemanagement/updating-your-app-package-installer-to-use-the-new-service-management-api).

Setup defaults to a JSON dry run, including on Linux. Explicit apply is macOS/root
only, from protected root-owned resources. It creates `rynd-<owner uid>` with a
fresh unused high UID/private GID, GeneratedUID, `IsHidden=1`, disabled password
and `/usr/bin/false` human login shell, without admin-group membership or a
Secure Token. The home is outside /Users, under
`/Library/Application Support/Rynhult Desktop/Owners/<uid>`. Projects and the
per-account spool grant the owner narrowly scoped inheritable macOS ACL access;
the agent home/credentials stay private. New agent directories discard inherited
ACLs before applying the intended grants; mode 0700 alone is insufficient. Configuration and the LaunchDaemon plist
are root-owned, and the persistent socket parent under its `Sockets/<uid>` directory is protected
(and fits macOS UNIX socket path limits). This survives /var/run cleanup at boot.
Setup adds one sshd drop-in, `/etc/ssh/sshd_config.d/100-rynhult-desktop-<uid>.conf` with
`DenyUsers rynd-<uid>`: a disabled password and false shell do not stop public-key logins or `-N`
forwarding with a key the agent plants in its own home. Setup refuses when `sshd_config` does not include
that folder (macOS 13+). The identity check also asks `dsmemberutil`, which resolves nested and
calculated groups, that the agent is outside `staff` (owner homes are 0750 group staff) and `admin`;
an unknown answer fails closed. No FileVault, TCC, firewall, loginwindow policy or system package is changed.
See [Apple's hidden-account guidance](https://support.apple.com/en-us/102099) and
[ACL inheritance](https://developer.apple.com/library/archive/documentation/OpenSource/Conceptual/ShellScripting/ShellScriptSecurity/ShellScriptSecurity.html).

The agent PATH includes its own CLI prefix, packaged tools, existing Homebrew
bins on Apple silicon/Intel and standard system bins. Setup does not run brew or
install dependencies. Existing Node/npm, Git and tmux must be accessible to the
agent. Exact Claude/Codex npm installation and login run through the same
unprivileged broker API as Linux. The broker explicitly selects /bin/sh for tmux
commands while the account's human login shell remains disabled. No owner CLI
credentials, PATH, browser session or desktop transport capability is inherited.

Default uninstall unloads the exact LaunchDaemon and removes its socket/config,
preserving account/work/credentials/profile. Like Linux, it also ends the agent: its tmux
server (stopped as the agent), its launchd user domain, its crontab and any remaining process
of its UID, and it refuses to report success while one remains. The sshd denial stays with the account. An explicit reviewed purge removes
only the recorded agent identity and its data after stopping its exact tmux
server and the rest of its processes, refusing if any remain, and removes the sshd denial; the owner profile and app
installation are separate. The MacBook smoke script must default to dry run,
require typed confirmation before creating an account and separately before
purging work, and exercise setup, peer checks, file ACLs and conditional
logged-in CLI version/PTY probes. Fake-provider sessions run in headless test
fixtures; no synthetic CLI is installed on the Mac by the smoke script.
No macOS account/service operation is performed on the Linux development host.

Out of scope: physical macOS qualification, real provider login/inference, TCC or
Keychain integration, app lock, signed updates, VM/container isolation and
qualified backup/restore. Native sessions remain **Not isolated · Separate agent
account**. The coordinator must qualify launchd activation, ACL inheritance,
provider credentials, terminal/reconnect and uninstall on the owner's MacBook.


### macOS implementation boundaries and coordinator verification

`desktop/mac_setup.py` owns the portable plan and root apply checks;
`mac_broker.py` consumes launchd's actual socket descriptors, validates the exact
UNIX listener/path/owner/mode and hands it to the shared Broker handlers. No
systemd environment spoofing or fd-3 replacement is used. The account shell stays
false while tmux is explicitly configured to run job commands through /bin/sh.
Project approval also binds the macOS account GUID: purge/recreation requires
fresh consent even at the same account name/path. The main checks the receipt's
GUID, UID/GID, hidden state, home/shell and absence
of admin membership before enabling execution. macOS ACL write grants on protected
resources/config are checked in addition to POSIX modes. The owner profile
also refuses explicit ACL grants even when its POSIX mode is 0700.

`desktop/build_mac_setup_pkg.py` prints a plan by default and builds only on macOS
with `--build`. Installer payload uses fixed root-owned Runtime resources;
postinstall binds owner name/UID/GID and the selected free agent UID/GID. Before
provisioning, a root-owned identity receipt is retained for failure recovery.
The Tauri Mac host selects this Runtime after installation, so the interpreter,
main, UI and broker stay together; its bundled window remains available before
setup and after full uninstall. Packaged Python agent tools use quoted shell
wrappers around bundled Python, avoiding Apple's older Python and shebang paths
containing spaces. No global Homebrew aliases are installed.

Default uninstall retains the identity receipt for a subsequent purge. A purge
verifies Directory Services GUIDs before deleting identities/data. Optional
`--remove-runtime` removes the fixed Runtime only after no owner receipts remain;
it does not delete the app/owner profile or manipulate Apple's Installer receipts.
The smoke script expands and compares the package's action/owner recipe and fixed
pre/postinstall before opening a private package copy in Installer. Preinstall
refuses runtime replacement while any owner receipt exists. This prevents accidental package/action
mismatch, **not malicious unsigned-package substitution**; signing and publisher
review remain release requirements. Partial account creation is not transactional;
a receipt remains, and incomplete-record recovery may need administrator review.

Provider login is deliberately not made to use the owner's Keychain. Anthropic
[documents headless subscription credential fallback and Console-login limits](https://code.claude.com/docs/en/authentication#credential-management).
These are provider mechanisms, not real-CLI results from this port. Keychain/TCC,
credential refresh and real inference still require the owner's MacBook.

The coordinator entry point is `desktop/tests/mac-phase1-smoke.sh` (default dry
run). Its probe is bound to the current owner's desktop manifest/socket, never a
server endpoint. It checks the dedicated broker, inherited project reads and
logged-in installed CLI version output over a PTY; absent auth skips clearly,
and it sends no inference. Full app sessions/keyboard/recovery, launchd activation,
real ACL enforcement and root apply/uninstall cannot be qualified on Linux.

Mac socket hardening follows the Linux security review: launchd creates the socket
as the owner UID with mode 0600 inside root-owned parents. No shared primary group
can connect; the broker independently verifies the owner peer UID. The run root
also stays root-owned (0711); the agent owns only its spool below that root.

## Phase 2 macOS standalone — Linux-built slice 1 (2026-10-25)

This slice starts phase 2 ("2. macOS standalone") with the parts that can be built and tested on Linux. **No Mac
result is claimed.** Everything that touches the system is behind the existing staff-group gate. The exact
commands for the Mac node demo-mac, to run later as a watched session, are in
[desktop/tests/mac-standalone-plan.md](../desktop/tests/mac-standalone-plan.md).

| Piece | Built and checked on Linux | Still needs the Mac |
| --- | --- | --- |
| Portable-main bundle (`desktop/build_mac_main.py build/verify`) | **Layout:** `RynhultMain/` holds `bin/rynhult-main` (a quoted launcher that copes with spaces and clears the inherited environment), `desktop/` (modules, `web-dist`, runtime), `src/acc`, `catalog`, `docs/wiki`, the agent tools and `MANIFEST.json` (SHA-256, size and mode per file). **Checked by `verify`:** completeness; any change since the build; arm64-only Mach-O (ELF, x86_64 and universal binaries without arm64 are refused, by header, never executed); nothing writable by group or others; symlinks stay inside; no file names the build, checkout or runtime folder; precompiled, relocatable bytecode. The build is all-or-nothing and never replaces a bundle. | Real runtime assembly, ad-hoc signatures, a headless start and EOF stop from a moved bundle; adopting it in `package.sh` and the setup package |
| Keychain adapter (`desktop/mac_keychain.py`) | `CredentialStore` interface; `MemoryStore` fake; `KeychainStore` over `/usr/bin/security`. Secrets are written through `security -i` on stdin, base64 only, and read back to confirm. Names come from a fixed character set, so callers can't add Keychain commands. Never as root or as another account. Tests check no secret reaches argv. | The real login Keychain, its access list and prompts. **Pilot limit:** items the `security` tool creates trust that tool. The signed app should move to Security.framework behind the same interface. |
| Start-at-login LaunchAgent (`desktop/mac_launchd.py`) | Owner-only plist in `~/Library/LaunchAgents`: the app's own executable from its Info.plist, only in /Applications or ~/Applications, Aqua sessions only, RunAtLoad, no KeepAlive, private logs and no inherited environment. Loaded with `launchctl bootstrap gui/<uid>`. Dry-run plan anywhere; install and uninstall only on macOS, as the owner, after the gate. | launchd accepting the keys, Login Items presentation, real login start |
| tmux recovery (`desktop/mac_tmux.py`, wired into `mac_broker.py`) | On macOS the broker sets TMUX_TMPDIR to `<run>/<agent>/tmux`, off `/private/tmp` (which reboot and periodic cleanup clear), and uses the short label `rd`, so even a 10-digit UID fits `sun_path` (≤101 bytes; the old label would be 104). At broker start, a live server's sessions are listed, a dead server's stale socket is removed (only the agent's socket in its 0700 folder), and a server left at the old path is reported. Tested with a real tmux server killed with SIGKILL. | macOS tmux honouring TMUX_TMPDIR with `-L`; the periodic cleanup settings; recovery under launchd as the agent |
| Gate (`desktop/mac_gate.py`) | `require_ready(owner_uid)` refuses off macOS, without provisioning, and unless the existing `mac_setup.verify_identity` passes. That check includes `dsmemberutil` proving the agent is outside staff and admin; an unknown answer fails closed. The Keychain and LaunchAgent entry points call it. The broker reaches tmux recovery only through `broker.configuration`, which runs the same check. | The staff answer itself (review finding 3, owner decision pending) |

The app (`package.sh`) and the setup package now ship the four runtime modules. Execution on macOS stays blocked
exactly as before. Nothing in this slice creates an account, a LaunchDaemon, a Keychain item outside the test plan,
or a TCC, firewall or system setting. App lock, TCC qualification, signed and notarized artifacts, the VM adapter
and VoiceOver remain phase 2 work.
