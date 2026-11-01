# Per-session containers

**Status: approved by the owner, 2026-10-22, with the answers in decision 0008 (private decision record).
The trusted launcher is built (2026-10-23, [below](#trusted-launcher-built-2026-10-23)). The scoped phase-1
qualification passed once, on 2026-10-26 ([attempt 19](#phase-1-qualification-result-2026-10-26)), but the remaining
qualification gates are still open, so production isolation is not claimed.** Containers ship in the release
candidate as a **Preview** ([release status](#release-status)): **off** unless an owner sets `ACC_CONTAINERS=1` or
turns *Containers per session* on in Admin features (after verifying their sign-in again), on only for projects an owner switches to containers, and only on a host that has recorded a passed qualification.
Every other session launches exactly as before.

## Release status

**Owner decision, 2026-10-26: containers ship in the RC as a Preview.**
- **Off by default.** `ACC_CONTAINERS` is off, and every project's switch starts off.
- **On per project, by an owner.** Only owners see and change a project's **Containers** switch. Members can't.
- **Labelled Preview** wherever they appear: the project's **Containers** card, **Settings → Container limits**,
  New session and session Setup, the Guide and this document.
- **Only on a qualified host.** Switching a project on is refused (409) until this host's root-owned qualification
  record names a passing report for this machine:
  1. Run the [owner qualification kit](../scripts/containers-qualification/README.md) and the runbook's
     [phase-1 step](#qualification-run) on this host.
  2. Record its passing report as root: `sudo /srv/acc/app/.venv/bin/python -I -m acc.container_host_record record report.json`.

     That writes `/etc/acc/containers/host-qualified.json` (root-owned, 0644). It holds the report's sha256, its
     status and this machine's `/etc/machine-id`. A failing report records nothing.
  3. `python -I -m acc.container_host_record check` prints what Rynhult concludes.

  Rynhult refuses a record that isn't a root-owned regular file in root-owned folders, names a failing report, or
  comes from another machine, and the project card says why. Switching a project off never needs the record. The
  owner's switch-on is audited with the report's sha256 (`project.containers`).
- **New container sessions need it too.** While the record doesn't pass, a new session in a project switched to
  containers is refused (409, "this project runs sessions in containers, a Preview that needs a qualified host: …")
  and audited as `container.launch_refused` (`host_not_qualified`). Nothing is made for it: no checkout and no
  launcher request. Sessions already running are left alone. Every new process, including resumes, restarts,
  retries and restored sessions, needs a current passing record. Internal creators cannot silently fall back to native
  sessions in a container-enabled project; helper launches are refused until their container integration is supported.
  Refused enables and launches are audited.

  The root recording step is an owner's attestation that the supplied report was produced on this host; it is not a
  signed report validator. Keep the original report as evidence, rerun qualification after relevant host/image/policy
  changes, and never record a report copied from another host. Copying the protected record itself is refused by the
  machine-id check. Use the isolated Python command above from a root-controlled installation.
- **Full support** follows once a **second host** has passed the qualification. Until then the Preview label stays,
  and the open gates under [phase-1 qualification result](#phase-1-qualification-result-2026-10-26) still apply.

Designs the approved proposal: every agent session gets a rootless container, installed by
default; agents never receive a host Docker socket. Owner direction: "Yeah, docker
or something else should be installed and available. We already provide more
sandboxing than our main 'competitors'."

## Recommendation and current boundary

Use **rootless Podman + crun**, one container per session, on Linux and inside
WSL2. Install the runtime and a pinned default image with ACC, not on first agent
request. Use the same Linux images in a dedicated Podman machine on macOS.
Keep the owner's existing rootless Docker for owner-approved project services;
it is not the session runtime. Offer a qualified gVisor profile when reducing
host-kernel exposure matters more than compatibility.

Today, [account creation](../deploy/accounts/acc-accounts) provisions one
unprivileged agent account per person; [acc-agent-add](../deploy/broker/acc-agent-add)
sets workspace roots, run folders, hooks and optional account-wide limits.
The [broker](../src/acc/broker/server.py) authenticates its Unix-socket caller,
then [launch](../src/acc/broker/ops.py) writes session files and starts the native
CLI in that account's shared tmux server. [Attach](../src/acc/broker/attach.py)
streams its PTY. Sessions belonging to one person can still reach one another's
homes, processes, run files and credentials. macOS currently uses the signed-in
user for both node and sessions ([MULTIHOST](MULTIHOST.md)).

[acc-guard](../bin/acc-guard) parses tool requests: useful safety checks, **not
a sandbox**. Launch-file hashes fail closed on changes, but their account owns
the files. Codex separately uses its selected
[sandbox/approval mode](../src/acc/providers/codex.py). Its
[AppArmor profile](../deploy/apparmor/acc-codex-bwrap) permits user namespaces for
the bundled bubblewrap binary; `flags=(unconfined)` is not a general confinement
policy. Owner-selected Option A now uses the ACC container as the sandbox: disable
Codex's and Chrome's inner sandboxes only under the immutable launcher contract.
Native sessions retain their own sandboxes; see decision 0043.
Do not disable Ubuntu's user-namespace restriction globally.

[setup.sh](../deploy/setup.sh) currently installs Docker packages/rootless extras,
subordinate IDs and linger for the **owner**, unless `ACC_DOCKER=no`, and starts
the owner's user daemon. That neither installs a per-session boundary nor gives
agent accounts Docker access. It remains separate from the proposed default-on
session-runtime installation. See [current security](wiki/Internal/Security.md).

## Options compared

Times are planning estimates for **warm local storage**, excluding pulls, model
startup and CLI authentication; not ACC measurements. All options need explicit
mount, network and resource policies.

| Option | Isolation | Warm start | Images and operations | GPU | Linux / WSL2 / macOS |
|---|---|---|---|---|---|
| **Rootless Podman** | User, mount, PID, IPC and network namespaces; shared host kernel | About 0.2–2 s | OCI images, layers, daemonless; manage subordinate IDs, storage, networking and cgroups | Linux NVIDIA CDI / selected AMD devices, driver qualification needed | Native Linux; inside WSL2; Linux VM on Mac |
| **Rootless Docker** | Similar shared-kernel boundary; daemon also unprivileged | About 0.2–2 s with daemon ready | Same OCI images; mature Compose, but daemon/socket lifecycle per account; do not reuse services daemon | Linux toolkit/CDI integration; WSL NVIDIA stack needs testing | Native Linux/WSL2; Mac needs Linux VM. Docker Desktop is not proof the guest daemon is rootless |
| **bubblewrap / systemd-nspawn-style namespaces, no image** | Shared kernel; only as strong as the namespace, mount, seccomp and LSM policy. nspawn is not automatically rootless | Tens–hundreds of ms; provisioning extra | No registry; curated root filesystem or host tool mounts. Dependency drift and mount-policy maintenance become ACC's job | Direct selected devices, broad driver attack surface | Linux; WSL kernel/distro dependent; Linux VM on Mac |
| **gVisor (`runsc`)** | Userspace kernel interposes most Linux syscalls; less direct host-kernel exposure, not a VM | Roughly 0.5–3 s | OCI images; extra runtime, syscall/I/O compatibility and nested-sandbox qualification | Supported NVIDIA paths exist; not arbitrary device passthrough | Qualify Linux first; WSL conditional; Mac only inside Linux VM |
| **Kata Containers** | Separate guest kernel / hardware virtualization per sandbox | Roughly 1–5 s | OCI plus guest kernel/rootfs and hypervisor lifecycle; higher RAM and operational cost. Not a drop-in rootless Podman promise | VFIO/device assignment is specialist work, often privileged provisioning | Linux virtualization; WSL nested virtualization conditional; no native Mac runtime |
| **Firecracker microVM** | Minimal KVM VMM and separate guest kernel | VMM boot can be subsecond; budget 1–5 s for a useful ACC guest | Build/patch kernel and disk images; implement guest agent, filesystem transfer, networking, snapshots and recovery | No general GPU passthrough; unsuitable default for local GPU models | Linux/KVM; WSL conditional on nested KVM; not native macOS |

Podman avoids adding another long-lived daemon and socket while retaining
reproducible toolchains. Namespace-only execution saves image space but would
recreate much of a container engine. Kata/Firecracker are disproportionate for
the default personal development machine.

**Platform fallback:** Linux: a separate, broker-controlled rootless Docker
daemon for each agent account if Podman is unavailable. WSL2: the same fallback
inside the systemd-enabled distro, with projects on its Linux filesystem, not
`/mnt/c`; if required kernel features are absent, select an enrolled Linux node.
macOS: Podman machine, with rootless guest execution explicitly checked; fallback
to an ACC-dedicated Linux VM running rootless Docker, or an enrolled Linux node.
Disable default whole-home VM sharing and share only ACC's workspace staging.
Do not silently use a rootful guest daemon or native host execution. Unsupported
providers fail with a reason; a macOS-only tool needs remote execution or the
owner's explicit native opt-out, not an emulated Linux replacement.

Runtime references: [Podman](https://docs.podman.io/en/latest/markdown/podman.1.html),
[Podman machine](https://docs.podman.io/en/latest/markdown/podman-machine.1.html),
[Docker rootless](https://docs.docker.com/engine/security/rootless/),
[gVisor compatibility](https://gvisor.dev/docs/user_guide/compatibility/),
[Kata](https://katacontainers.io/learn/),
[Firecracker](https://firecracker-microvm.github.io/).

## Launch, control and lifecycle

```text
accd / authenticated node link
  -> per-person broker (trusted host control plane)
     -> fixed-spec rootless launcher -> session container: CLI + tools + dev apps
     -> session-bound event collector -> host spool -> existing ingest
     -> reserved-port relay -> container network namespace
  Files / Changes / artifacts -> broker -> that session's persistent checkout
```

Keep existing broker authorization and the services-role method restriction.
The launcher accepts validated session IDs and server-selected image digests,
mounts, limits and ports, **not** arbitrary OCI flags, host paths or commands
from hook events. Neither runtime API sockets nor its management endpoint,
storage, node keys, broker sockets, tmux sockets or service-account home are
mounted into a session. Rootless is not a reason to expose a socket.

Use private PID/IPC/network namespaces, private `/proc`, tmpfs `/tmp` and `/run`,
a read-only root filesystem, drop capabilities, seccomp and an applicable
AppArmor/SELinux policy. Run the CLI as non-root with no-new-privileges; map its
UID to the agent account for workspace ownership, subordinate IDs for other
container users. Provision non-overlapping subordinate ranges per account.
Do not mount host `/home`, `/srv/acc`, `/run/user`, SSH agents or devices wholesale.

The existing [broker unit](../deploy/broker/acc-broker@.service) has
`NoNewPrivileges=yes`: simply invoking Podman there can prevent
`newuidmap/newgidmap` from working. Provision a separate trusted launcher service
that can establish rootless mappings before applying payload restrictions;
retain the broker's restriction. Establish cgroup-v2 delegation there and enforce
**both** per-session budgets and the person's aggregate CPU/memory/PID budget,
rather than losing the current broker-cgroup limits when work moves to a user
service. Add disk/inode, spool and cache quotas. No privileged agent container.

Extend broker `ping`/layout and provider inventory to report container paths,
runtime capabilities and image CLI versions, not host-installed binaries.
Compile launch files against that layout; version mismatches fail explicitly.

Move tmux **inside each container**; broker tmux operations become targeted
runtime exec/attach operations, preserving the existing framed PTY API, resize,
read-only attach, literal send-text, interrupt and prompt detection. Do not
leave a host tmux command prompt reachable through the terminal. Container
monitoring survives broker restarts; a launcher-owned record binds session,
container ID, native conversation ID, image digest and policy version.
Reconcile actual container exit/OOM separately from CLI hook events.

Stop kills the whole session cgroup, nested dev workloads and port relays, drains
events and removes secret tmpfs mounts. Resume recreates the pinned container
with fresh authorized keys and the same private CLI state/native ID; missing
state or image gives **Recovery needed**, never a new conversation. On node
disconnect do not assume processes stopped: queue cleanup and reconcile on
reconnect, as [vault cleanup](VAULT.md) already does. Archive retains dirty
checkouts and commits; cleanup never force-removes them.

## What a session sees

| Surface | Proposed contract |
|---|---|
| **Worktree, read-write** | Exactly its checkout at the broker-reported session path, including `.acc/artifacts`; no project parent or sibling mount. Resolve and validate mount sources without following agent-controlled symlinks. Files written here remain after the container exits. |
| **Git metadata** | Today's `git worktree add` writes a `.git` pointer to shared metadata outside the worktree ([ops](../src/acc/broker/ops.py)). Mounting the repository's entire `.git` read-write would expose sibling refs, config and worktree administration. For isolated sessions, replace that implementation with a session-private standalone checkout at the same logical worktree path, seeded from a broker-created bundle with no hardlinks or object alternates. It owns its `.git`, index and refs. Import commits through validated bundles into that session's branch, never by mounting shared git administration. Adapt Changes, archive and git backup to include these private refs; preserve local-only/no-remote workspaces. |
| **CLI home/config** | A private persistent home/state volume for this session only: its transcripts, resume data, selected skills and sanitized preferences. Never bind the person's whole home or provider directory. Catalog symlinks must resolve to session-selected read-only content, not the host catalog. |
| **Sign-ins** | Keep the person's canonical provider/`gh` sign-in store outside sessions. Run the CLI's own device/browser login in a short-lived credential-management container, controlled by the broker's existing [login flow](../src/acc/broker/logins.py); on Mac do not expose Keychain or its socket. An adapter copies only the selected CLI's required credential fields into this session's private tmpfs credential view, not histories or other CLIs' tokens. Never embed tokens in images, engine environment metadata, argv or logs. |
| **Credential refresh** | Prefer broker-managed refresh and short-lived session copies where the provider supports it; serialize canonical refresh and never copy untrusted session config back wholesale. Providers whose CLIs require rotating refresh-token writes need a qualified adapter or a separate per-session sign-in. Do not share one writable auth directory. Same-person sessions may still use the same provider identity/quota: filesystem isolation does not create independent tokens or prevent account-wide revocation. |
| **`session.env` / MCP keys** | Keep the [vault's](VAULT.md) current project/person/other-machines authorization and duplicate-variable refusal. Deliver the 0600 file and secret-filled MCP files to session-only tmpfs through a private broker channel; source before CLI exec, not runtime `--env` options. No values in durable launch specs or layers. Resume reauthorizes. Stop/exit/recovery/archive tears down mounts and clears host staging with retry. A running session can necessarily read/exfiltrate its own keys; redaction and the guard remain. |
| **Guard and hooks** | Pinned `acc-guard`, `acc-hook`, `acc-request`, statusline and dependencies are immutable image files. Policy, launch settings and every provider's effective hook file are broker-generated **read-only bind mounts**, with read-only parents, not files the agent can chmod/unlink/replace. Keep hash checks. Put mutable CLI state elsewhere. No mount capability or writable code dependency can replace these files. Qualification must show real pre-tool blocks/post-tool events and Codex hook trust. Under owner-selected Option A, the immutable launcher contract selects Codex danger-full-access and Chrome --no-sandbox; the outer container restrictions remain mandatory, including refused writes outside session mounts. Native sessions retain their sandboxes (decision 0043). |
| **Run folder and spool** | Preserve compiler-visible run paths but split immutable inputs, secret tmpfs and writable outputs. Replace direct append to `events.jsonl` with a bounded session-only collector endpoint; patched hooks/guard send there and the host collector writes the existing spool format. It binds the session ID itself and exposes no general broker method. The agent cannot edit the host spool or another session's endpoint. Own-session events remain untrusted, since an agent can invoke the sink itself; keep defensive ingest and human approvals. |

Immutable hook **files** are enforceable; universal hook **invocation** by an
arbitrary program is not. Do not claim containers solve shell-parser bypasses,
provider bugs or a deliberately spawned hookless CLI. A provider that cannot
retain mandatory ACC hook configuration is not qualified for guarded mode;
show an explicit unsupported state rather than silently running without it.
An already-approved interpreter can still run arbitrary code *inside* the
container. Enforced reach comes from the outer boundary, not the guard.

**Files, history and host-side Git:** [Files](../src/acc/server/api/files.py)
already selects the session's cwd/account/host; keep its descriptor-based path
checks, protected files, revision checks and redaction. Host-side checkout
mounts keep Files, wiki and artifacts available even while stopped. Route
transcript/history/usage lookup to the session's state, not `Path.home()` of the
broker. On Mac, keep checkout sharing narrow and test file notifications and
ownership across the VM. Any broker operation parsing an agent-writable
repository must disable hooks, filters, external diff/credential commands and
unsafe transports or run its Git subprocess in an equally restricted container:
otherwise crafted Git config can regain host execution.

### Network, preview, stacks and GPU

**Network:** isolated per-session networking, default outbound Internet access
for model APIs, Git, package registries and approved MCPs. Default-deny unsolicited
inbound and access to host/VM gateway, LAN/private/link-local/metadata addresses,
ACC control services and sibling container networks (IPv4 and IPv6); explicitly
grant selected local-model or project-service endpoints by project policy.
No host networking or automatic host-loopback mapping. A trusted networking
helper must enforce these rules outside agent control, including rootless
userspace-network paths; provisioning any required host rules is installer work,
not an agent capability. Internet access is not an exfiltration defense.

Optional per-project egress allowlists use a host-controlled DNS/HTTP CONNECT
gateway plus packet filtering that blocks direct TCP/UDP/DNS/QUIC bypasses.
Validate resolved addresses on every connect, including rebinding/private
addresses; pin permitted destinations, not just environment proxy variables.
Document provider CDN/auth endpoints and denied connections. Session tools
cannot modify policy. This is an opt-in restriction with compatibility costs,
not a guarantee about what data an allowed API receives.

**Reserved ports:** retain dev 8000–8099 / prod 8100–8199 from
[ports.py](../src/acc/server/api/ports.py), no "free port" discovery.
The broker leases a project's reservation to **one live session or services
stack**, refusing conflicts even between sessions of the same project.
A host-controlled relay binds `127.0.0.1:<reserved>` and reaches that session's
network namespace; support container-loopback listeners rather than requiring
LAN binds. Unreserved listeners are session-private. Verify the lease, target
identity and reservation on reconnect/reassignment; close old connections on
release so a stale preview cannot reach another project.

The [preview gateway](../src/acc/server/preview.py) can retain literal host
loopback upstreams, isolated origins, login-bound credentials and WebSockets/HMR.
[Remote preview](../src/acc/server/api/preview.py) is currently unavailable:
containers do not implicitly add node-to-primary forwarding. Keep that limit.
Owner-approved publishing remains a separate action; expose only its approved
reserved-port route, not an automatic `0.0.0.0` publish of every dev server.

**Project stacks / Docker-in-container:** keep the existing services broker and
compose-digest approval for durable deployments ([security](wiki/Internal/Security.md)).
Agents still edit Compose files but never receive the owner's socket,
`/var/run/docker.sock`, a Podman socket or an unrestricted TCP Docker API.
For disposable Compose/Testcontainers development, qualify an **opt-in,
session-private nested rootless dockerd** with its own data volume and socket
inside the session boundary. Docker's CLI may reach *that* daemon only; it can
mount only session-visible paths, inherits resource/egress limits, and stops
with the session. Nested published ports go through the same reservation relay.
No Docker-in-Docker daemon by default.

Nesting may require extra subordinate mappings, storage drivers and narrowly
reviewed namespace/seccomp permissions; an ordinary drop-all/no-new-privileges
payload is not assumed to support it. Qualify a trusted daemon initialization
path separately from the restricted agent payload. If nesting needs
`--privileged`, host capabilities, an unconfined profile or disabling the outer
boundary, **refuse** it: use an isolated Linux VM dev-stack profile or ask for
the existing owner-approved services workflow. This compatibility gate must
not delay the safe default or be hidden as working Compose support.

**GPU:** prefer an explicit allowlisted local-model HTTP endpoint without any
device mount. For in-session inference, owner-approved Linux profiles pass only
selected NVIDIA CDI devices or AMD render/KFD nodes, required driver libraries
and device-group access; never all `/dev`, host IPC or privileged mode.
GPU memory is not reliably isolated by ordinary cgroup memory limits; bound
concurrency and disclose shared-driver/VRAM risks. Qualify WSL2 NVIDIA separately;
no general AMD/WSL promise. macOS Linux VMs cannot provide normal Apple Metal
passthrough: use a deliberately exposed host inference endpoint or Linux GPU
node. gVisor GPU support is configuration-specific, not presumed.

## Images, approved tools and caches

Build one common **toolchain base**, with thin per-CLI images to avoid loading
unrelated CLIs and credentials. Include Git, Python, Node, shell, tmux, common
build/search tools, approved MCP runtimes, browser sandbox dependencies and
immutable ACC helpers. Publish amd64/arm64 variants from reviewed build recipes
and lockfiles. Record base/CLI/tool versions, SBOM and source revision; scan and
sign images, verify signatures and pin the full platform digest at launch.
Never build a secret or host home into an image.

Use a scheduled security rebuild plus explicit reviewed releases, not `latest`
or CLI self-updates inside a session. New sessions pick the approved digest;
existing sessions keep theirs. Retain referenced images and compatible state
for resume/rollback; urgent revocation stops at an owner-visible update decision.
Pre-pull defaults at installation/update; offline launch uses verified cached
digests or fails clearly. No runtime fallback to a mutable tag.

[Tools on approval](TOOLS.md) keeps its authorization/audit rules and registry-only
requests, but the destination becomes **image or session**, not the host account.
An approved reusable tool builds a new derived image in an isolated builder;
an approved session-only tool installs into a private persistent tool layer,
never the immutable helper/hook mounts. apt image additions run as root **inside
the unprivileged build container**, not host root. Account managers run as the
session user. Do not forward these requests to today's host package helper.
Host-only managers (such as Homebrew, snap or flatpak) fail explicitly for a
Linux session image; request an equivalent image package, not a host install.
Show image/session scope and resolved version in Tools/Inbox; a new image takes
effect on explicit restart/new launch, not by mutating a running root filesystem.
Workspace dependencies remain free to install locally; approval exemptions do
not grant host privileges or writable hook files. An approved package can still
execute malicious code within its destination.

Share verified read-only OCI layers per account; use quotas and reference-aware
GC. Registry blobs may be deduplicated by a trusted host cache. Writable package,
compiler and browser caches are session-private by default, optionally
project-scoped only with owner consent (cache poisoning is shared write access).
Never cache auth files, secret MCP config, `session.env` or secret-bearing layers.
Separate private persistent tool/state volumes from disposable rootfs and tmpfs;
exclude credentials/run mounts from snapshots and ordinary backups. Do not
export the whole writable layer as a reusable image.

## What changes for people

Expect seconds of warm launch overhead, minutes for a first large image pull,
extra disk for images/private Git state, and VM RAM/battery cost on Mac.
Linux bind-mount I/O should stay close to native; Mac VM file sharing, WSL
Windows-drive mounts, nested Docker and gVisor's syscall-heavy workloads may
cost substantially more. Benchmark launch, build, watcher and browser workloads
before promising performance.

Host-installed tools, home dotfiles, host virtualenvs, absolute host symlinks,
SSH agents, host-loopback databases and Metal tooling no longer appear by
accident. Provide toolchain presets and explicit approved imports/endpoints;
never "fix" compatibility by mounting a whole home. Existing sign-ins need
adapter migration or a per-session login. Files, Changes, keys, terminal and
reserved-port Preview keep their workflows; Tools shows the new destination.

Containers default on for **every qualified agent**, including explicit helper
sessions, not just Codex. An owner may authorize a per-project **native
execution** exception with a visible warning, reason and audit record; members
cannot opt out. It affects only new sessions. No automatic fallback on failure.
During mixed-mode migration, a native session under the same account can read
container backing files and defeat cross-session protection: do not label that
account protected. Require all its native sessions to stop (or a separate
container-only execution account) before claiming isolation.

Never live-move a running process. Inventory and label legacy sessions; let
them finish or stop them with permission, preserve dirty/untracked files and
native conversation state, then seed a private checkout and resume on the same
conversation ID. Missing/incompatible state stays Recovery needed. Roll back
the runtime/image without discarding work; native rollback needs explicit owner
consent and invalidates the protected-account claim.

## Threat model

Adds a kernel-enforced limit to a prompt-injected agent's filesystem, process,
socket and network reach, including **other sessions belonging to the same
person**. Separate homes, Git metadata, spools and networks matter as much as
the container itself. Per-person accounts still separate owners; guardrails,
branch protection, approvals and backups still serve different purposes.

Does **not** protect delivered credentials, permitted source files or writable
project data from their own agent; guarantee trustworthy hook events; stop
all resource exhaustion; or constrain a trusted host admin/compromised broker.
Shared egress endpoints, project caches and GPU drivers add explicit exceptions.
No competitive isolation claim is established by this design.

crun/runc containers share the host kernel: kernel/runtime exploits can escape,
and rootless mainly reduces the privilege gained, not the possibility.
**Recommend gVisor for higher-risk untrusted repositories/agents** once the
CLI/browser/nested-sandbox workload passes qualification; make incompatible
features fail explicitly rather than silently dropping to crun. For a policy
requiring a separate kernel, use a separately qualified Kata/VM boundary;
Firecracker is later work, not a stronger-isolation switch promised in v1.

## Effort, phases and owner decisions

Estimate **7–11 engineer-weeks** for one experienced engineer, plus owner
qualification time; not a delivery commitment. Optional nested Docker and
hardened-runtime support add roughly 2–4 weeks. Implementation was authorized
by decision 0008; phase exit gates still apply.

| Phase | Estimate | Exit gate |
|---|---|---|
| 0. Approve and prototype | 1 week | Decide defaults; prove rootless UID mapping under systemd/AppArmor, the selected container sandbox boundary, credentials and private Git checkout on Linux. Stop if mandatory hooks cannot be retained. |
| 1. Linux control/data plane | 2–3 weeks | Launcher, per-session tmux, durable lifecycle, mounts, state/secret delivery, collector, safe host-side Git, checkout import/backup, resource limits and restart/recovery behavior. |
| 2. Integrations | 2–3 weeks | Credential adapters, immutable image hooks and mandatory private/LAN egress denial; Files/Changes/history/artifacts, leased ports and Preview/HMR, approved-tool images/layers, image signing/cache/update/rollback. The first adapter slice below is implemented with fakes, not runtime-qualified. |
| 3. Platforms and rollout | 2–4 weeks | WSL2 and Mac VM qualification, every provider's real login/resume/hooks, installer defaults, opt-out warnings, migration and docs. Default-on only after these gates pass. |

Required acceptance evidence: two same-account sessions cannot read/write each
other's workspace/state/env/spool or reach their processes/sockets/dev ports;
the runtime/host sockets and node keys are unreachable; arbitrary interpreters
cannot replace hook files or policy; real tools are blocked by the guard when
expected. Test symlink/config-based host escapes, direct egress/DNS/IPv6 bypass,
resource/disk exhaustion, token refresh races, collector forgery/limits, dirty
archive and backup of private refs. Cover PTY resize/read-only/reconnect, broker
restart, crash/OOM/reboot, node disconnect, image rollback, approved installs and
port release/reuse with HTTP/WebSockets. These are **future gates**, not tests
performed for this document.

Owner approval is needed for:

1. Podman/crun default, rootless Docker fallback, automatic default image download
   size and the 7–11 week core scope; registry/signing-key custody and security
   update/revocation policy.
2. Private standalone Git checkouts instead of shared linked metadata; import,
   backup and extra disk cost. Default per-session and aggregate resource limits.
3. Credential-copy versus separate-login policy per provider; which CLIs must
   qualify at first release, and who handles provider refresh-token restrictions.
4. Default private/LAN egress denial and explicit local-model/service grants;
   whether optional egress allowlists and nested Docker are first-release gates.
5. Mac VM footprint and loss of native/Metal tools; Linux-node fallback. Which
   GPU profiles and hardened gVisor workloads require qualification.
6. Whether native exceptions are allowed at all; acceptable mixed-mode warning
   versus separate execution accounts, and when legacy sessions must be stopped.


## Phase 0 results

2026-10-23: **NO-GO for phase 1; phase 0 qualification remains incomplete.**
The prototype is [container_prototype.py](../src/acc/broker/container_prototype.py),
with an explicit `enabled=False` feature flag. Nothing imports it into today's
broker launch path. The [qualification instructions](../scripts/container_qualification/README.md)
and [runner](../scripts/qualify_containers.py) are rerunnable as the current agent
account. No production launcher, service, installer or machine policy changed.

### Evidence and limits

The current agent account has a 65,536-ID subordinate UID/GID range. Installed
versions are Podman 4.9.3 and crun 1.14.1. In this session the process is inside
the systemd broker unit, has `NoNewPrivs=1`, and AppArmor context `unconfined`.
`podman info` using fresh private storage fails with exit 125:
`newuidmap: write to uid_map failed: Operation not permitted`.
The mapping request includes the account UID and its subordinate range.
Setuid `newuidmap`/`newgidmap` binaries are present. This is consistent with the
design's predicted inherited no-new-privileges problem, but a control run outside
that restriction was unavailable, so it is not proof of the sole cause.
`systemctl --user` also reports no user bus. No service was started to bypass it.

The host currently reports `apparmor_restrict_unprivileged_userns=0`.
We did not change it. Consequently this environment cannot establish operation
under Ubuntu's restricted AppArmor user-namespace configuration even if the
mapping failure is resolved. Installation alone does not qualify the runtime.
No rootful runtime, other account's store or live ACC endpoint was used.

| Gate | Result |
|---|---|
| Rootless UID mapping under systemd/AppArmor | Blocked before any container starts; dedicated launcher context and restricted AppArmor still required. |
| Private standalone checkout | Works on host: selected committed HEAD only, bundle transfer, private `.git`, no hardlinks/alternates/remotes/sibling refs. Source hooks and inherited Git config suppressed. Dirty file migration/import/backup are not implemented. |
| Credential transport | Synthetic ACC-issued token envelope tested; bounded stdin transport and a tmpfs payload are implemented. Container delivery was not executed. No real sign-in files were read; actual Claude/Codex/Copilot narrowing, refresh and return-through-ACC adapters remain unqualified. |
| Mandatory guard and hooks | Not qualified. Existing `acc-hook`/guard write file spools; the required host collector is absent. Optional payload tests direct helpers with read-only parents, not real CLI invocation or immutable effective provider settings. No provider may be marked supported from these results. |
| Nested Codex bubblewrap / Chrome | Not reached. Optional payload contains generic bubblewrap and sandbox-enabled headless Chrome probes; actual Codex sandbox/trust and Chrome renderer sandbox evidence remain required. No sandbox was disabled. |
| Network / GPU / fallback | Offline probe uses `network=none`. Internet plus private/LAN denial, project grants, NVIDIA Linux/WSL and rootless Docker fallback remain unqualified. |

### Measured overheads

One recorded run of the runner (local warm filesystem, no image pull):

| Measurement | Observed |
|---|---|
| Failed `podman info` wall time | 23.43 ms |
| Peak child RSS during that runtime probe | 37,120 KiB (36.25 MiB); runtime/helper process, **not container memory** |
| Private committed checkout of this ACC repository | 2,042.58 ms; 97,310,744 logical file bytes (includes private Git state) |
| Successful container startup / steady or peak container memory | Unavailable: no container started. JSON reports null, not zero. |

These are single samples, not performance promises or native/container comparison
benchmarks. Another failed probe observed 21.82 ms and 137,848 KiB child RSS;
startup/helper variation reinforces the need for repeated measurements. The
runner removes its private storage and checkout; review output is available in
the session Artifacts as `containers-phase0.json`.

### Required next step and design clarifications

Retain the separate trusted launcher from the approved design; do not call
Podman from the restricted broker or weaken the broker. An owner needs to qualify
that context with delegation and the intended AppArmor policy, then rerun the
nested sandbox and real-provider gates. This task did not authorize machine
configuration changes, so that prerequisite remains outside the prototype.
A mandatory hook retention failure is a hard stop; a missing qualification is
also a no-go, not permission to launch unguarded. The runner never returns go.

Resolve the phase table's “optional egress policy” wording as follows: **blocking
private/LAN destinations with explicit project grants is mandatory**; Internet
allowlists remain later optional work. NVIDIA Linux/WSL remains a first-release
gate. Podman/crun remains the default and separately qualified rootless Docker
the fallback. Native execution is an owner-only per-project exception with a
**not isolated** badge, never a runtime error fallback. Preserve narrowed or
short-lived provider copies where supported, returned only through ACC; the
transport helper is not a provider adapter. No owner decision is reversed.

## Trusted launcher (built 2026-10-23)

Built from the approved note [CONTAINER-LAUNCHER](CONTAINER-LAUNCHER.md) and decision
[0038](decisions/0038-trusted-container-launcher.md), with the owner's answers of 2026-10-23: the fixed launch
contract; limits configurable per agent account (members' defaults 2 CPUs, 4 GiB memory, 512 processes, 20 GiB disk
per session and 4 sessions at once; owners unlimited unless limits are set); audit kept as long as the main audit log.

| Piece | Where | What it does |
|---|---|---|
| Root helper | [deploy/containers/acc-containers](../deploy/containers/acc-containers) | `acc-containers.socket` (`/run/acc-containers.sock`, `Accept=yes`): one root process per request. Validates, reserves capacity, runs `systemd-run --uid=<agent> -p Delegate=yes` with the limits, cleans up, audits |
| Reconcile | `acc-containers-reconcile.timer` (every minute) | Stops containers whose grant was revoked or changed, or that use more disk than their limit; retries failed cleanups; reports `acc-ct-*` units without a record (never deletes them) |
| Pinned images | `/etc/acc/containers/policy.json` (root, 0644; [example](../deploy/containers/policy.example.json)) | Policy id → `sha256:` image ID, network (`none` only) and each provider's command in the image. Absent by default: every launch is refused |
| Grants | `/srv/acc/containers/grants/<session>.json` (folder `acc:acc` 0750) | accd's authorization for one launch: account, generation, policy id, private checkout, run folder, command, guard hashes, limits |
| Records, audit | `state/` (root 0700), `audit/` (root:acc 0750) | Durable launch records; JSON lines accd copies into ACC's audit log every 10 s |
| Broker side | [acc.broker.containers](../src/acc/broker/containers.py), `ops.launch`/`kill` | Asks the helper with the fixed request; the tmux pane runs [acc.broker.container_attach](../src/acc/broker/container_attach.py) against the unit's relay socket. A refusal is the launch's error; there is no native fallback |
| accd side | [acc.server.containers](../src/acc/server/containers.py), `api/containers.py` | Limits (Settings), the project switch, grants, audit ingestion |
| AppArmor | [deploy/apparmor/acc-podman](../deploy/apparmor/acc-podman), [acc-podman-runtime](../deploy/apparmor/acc-podman-runtime) | `userns` for newuidmap, newgidmap and pasta (and podman/crun where the distribution has no profile). **Not** installed by `deploy/install.sh` |

**Request.** Exactly `version, request_id, operation (launch|status|stop), session_id, generation, policy_id`; any
other field, a wrong type or more than 4 KiB is refused. The broker never supplies argv, paths, mounts, environment,
images, units or systemd properties.

**Authority.** The caller's uid must be the grant's account and its pid the `MainPID` of
`acc-broker@<account>.service`: agent processes share the uid but are never the broker's main process (decision
0012 explains why this, plus the grant written only by `acc`, is the protected control-plane channel the note asks
for). The grant must be a regular file owned by `acc`, not group/world-writable, in a folder `acc` owns. Its
generation and policy id must equal the request's. Account checks: name `*-agent`, non-root uid/gid, no
administrative group (sudo, adm, docker, lxd, acc, ...), root-owned protected broker settings with
`ACC_BROKER_ROLE=agents`, and a subordinate uid/gid range of at least 65,536 that starts at 100,000 or above and
overlaps no other account's.

**Mounts.** Only the session's private checkout (under the account's `ACC_ALLOWED_ROOTS`, in a `.worktrees` folder,
with a `.git` folder of its own: a worktree's shared administration is refused) and its run folder
(`ACC_RUN_DIR/<session>`), each at the same path inside. Both are opened from `/` without following a link at any
level, must be owned by the account and not world-writable, may not be or contain a protected folder (`/`, `/etc`,
`/home`, `/run`, `/srv`, `/srv/acc`, the account's roots or home, ...) and may not overlap. Root holds the opened objects through startup. PID 1 binds `/proc/<helper>/fd/<fd>` non-recursively into a
root-owned, private `/run/acc-container-inputs/<session-unit>` tmpfs before changing uid. Podman mounts those stable staging
paths at the granted container destinations. Their original identities are also re-checked after startup; a swap
stops the unit. Podman's own storage, runroot and temporary folders are a separate fresh tmpfs owned by the account at
`/run/acc-ct/<digest of the unit name>`, in the same private mount namespace: Podman refuses a runroot longer than 50
characters. Both mount-point parents must be missing, which lets PID 1 create them as root, or be root-owned real
folders that nobody else can write, as must their parents; otherwise the launch is refused before systemd starts
anything. Paths with `,`, `:`, `=` or newlines are refused (no mount option
injection). Podman runs as the agent, so a swapped path could never reach more than that account can already read.

**Container.** `podman run --cgroups=split --sdnotify=conmon --detach --rm --pull=never --read-only --cap-drop=ALL
--security-opt=no-new-privileges --userns=keep-id`, private pid/ipc/uts, the policy's network, `--pids-limit`,
tmpfs `/tmp`, home and `/run/credentials`, a fixed `/bin/sh` entrypoint that reads the run folder's `session.env`
(0600) and execs the policy's provider command with the grant's arguments. The unit has `Delegate=yes`, `Type=notify`,
`KillMode=mixed` and the session's `CPUQuota`, `MemoryMax` (swap 0) and `TasksMax`; the account's slice
(`acc-containers-<uid>.slice`) gets sessions × each limit. Disk is enforced by the reconcile timer (the checkout and
run folder's allocated blocks, checked every minute); `/tmp` is capped by its tmpfs size.

**Lifecycle.** Exact retries (same request id) are idempotent; another request for the same generation conflicts; an
older generation is stale. A per-session lock covers launch, stop and reconciliation through all unit changes;
cleanup timeouts leave a durable `cleanup_failed` record. Normal container exit uses Podman's `--rm`. The container, conmon
and Podman's storage exist only inside the session's unit (its cgroup, private mount namespace and runtime tmpfs), so a
unit that stopped proves absence. A unit that doesn't stop is quarantined and holds capacity until an owner reviews it.
The account's own Podman store is never consulted: anything running as the account can write it, so a same-named
container there proves nothing and never blocks a session. The helper never deletes a container by name or label. Stopping revokes the grant; archiving returns a clean branch through the broker.

**What outlives the unit.** Rootless Podman's pause process (catatonit) left the unit in qualification attempt 10:
Podman had the account's user manager move it into a `podman-pause-*.scope`.

**How it escaped** (Podman 4.9.3, containers/common 0.57, godbus 5.1 sources):
- **The move happens regardless of the cgroup manager.** When Podman creates a new rootless user namespace,
  `SetupRootless` calls `utils.MovePauseProcessToScope`. That calls `RunUnderSystemdScope` with
  `cgroups.UserConnection(uid)`, which dials `godbus.SessionBusPrivateNoAutoStartup`. None of this checks
  `--cgroup-manager`, so `cgroupfs` didn't prevent it.
- **The private runtime dir didn't hide the bus.** godbus takes `DBUS_SESSION_BUS_ADDRESS` first (the unit has none).
  Otherwise it discovers `/run/user/<uid>/bus`, with the uid from `user.Current()`, not from `XDG_RUNTIME_DIR`. The
  unit shared the host's `/run`, and an account with a running user manager (for example lingering) has that socket.
  So Podman asked the account's user manager to start the scope with the pause PID, and it adopted the process, which
  belongs to its own uid. Builds without a user manager log "Failed to add pause process to systemd sandbox cgroup:
  dbus: couldn't determine address of session bus".
- **What escaped:** only catatonit (one process), and with it the unit's mount namespace: the runtime tmpfs with
  Podman's overlay storage and `pause.pid`. That tmpfs memory and the process were then outside the unit's
  CPU, memory and tasks limits and its cleanup.
- **What stays in the unit:** conmon isn't moved (`--cgroups=split` and systemd's `INVOCATION_ID` both turn its
  placement off), and the container goes into a sub-cgroup of the unit (`cgroupfs`, split).

**Prevention:** the unit now gets `TemporaryFileSystem=/run/user:ro`, an empty read-only `/run/user` in its private
mount namespace. The scope move fails with a warning, and catatonit stays in the unit, under its limits, and ends with
it. The cleanup below stays as the backstop.

**The launcher's own Podman checks** (qualification attempt 13). Outside the unit, the launcher also runs Podman as
the account: `image inspect` at launch and `container exists` at cleanup, with `XDG_RUNTIME_DIR=/run/user/<uid>`.
When the account had no pause process yet, each made one and had it moved into a `podman-pause-*.scope`, which also
socket-activated the account's session bus. Both stayed as processes of the account outside every unit, which the
qualification census counts as possible forgers. These calls now get `DBUS_SESSION_BUS_ADDRESS=disabled:`. godbus
uses that value as given, and `disabled` is no transport it knows, so the move fails and the bus isn't started. The new
catatonit stays in the launcher request's own cgroup, which no account process can enter. Before the request answers,
the launcher ends every process of the account in that cgroup the way `podman system migrate` does (SIGKILL through a
pidfd, after re-checking the uid and cgroup), and removes the account's `pause.pid` if it names one of them. No folder
on the way is followed through a link. A pause process the account already had is only joined; it is elsewhere and
left alone. This end runs only when that cgroup is one of the launcher's own units (`acc-launcher.slice/acc-containers@…`
or `acc-containers-reconcile.service`): on the legacy cgroup hierarchy every process reads `/`, and a `--reconcile` run
by hand shares a root shell's session, so there "the request's cgroup" holds processes the launcher didn't start. A
process the launcher can't end is never dropped: it is audited (`container.account_leftover`, PID and a fixed label)
and refuses the request, `account_leftover`. A cleanup records that as `cleanup_failed`, and reconcile retries it.

**A `pause.pid` Podman can't join** (qualification attempt 18b; the clearing below fired at attempt 19's generation 2,
which [passed](#phase-1-qualification-result-2026-10-26)). Generation 2's `image inspect` failed with
"podman exited 125: cannot re-exec process to join the existing user namespace", so the launch was refused as
`untrusted_image_store`. In Podman 4.9.3, a `pause.pid` naming a process that can't be joined is removed only by
`TryJoinPauseProcess`. When the `alive` file is missing from Podman's tmp dir, runtime setup refreshes first and skips
that step:
- it starts a new pause process, which publishes `pause.pid` with `rename_noreplace`;
- the existing file wins, so Podman joins the PID it names;
- opening that process's `/proc/PID/ns/user` fails, and Podman stops with that error.

`podman system migrate` goes through the same setup, so it can't recover from this, and it would SIGKILL whatever PID
the file names.

So before each Podman call as the account, the launcher reads `/run/user/<uid>/libpod/tmp/pause.pid` without following
a link:
- **The account's live, non-zombie catatonit:** it is joined and left alone.
- **No process, an exited one, or another uid's or non-catatonit process:** the file is removed. Before removing it,
  the launcher checks it is still the same file it read. Nothing is signalled. The audit line
  `container.pause_state_cleared` names the PID and why.
- **Can't be verified** (a link on the way, not a regular file, not the account's own file, or not removable): the
  launch is refused as `stale_podman_state` and audited, and Podman isn't run.

**Cleanup backstop.** If a pause process does outlive the unit, it keeps the unit's mount namespace, and with it the
runtime tmpfs and `pause.pid`. The host sees only an empty `/run/acc-ct/<id>`, so a later
`podman system migrate` can't reach it.

So once the unit has stopped, on a stop and after a failed start, cleanup does the following:
1. It looks for every process whose real uid is the account and whose mount table still has that generation's runtime
   tmpfs. Only namespaces copied from that unit have it.
2. It ends each one as `podman system migrate` ends the pause process (SIGKILL), through a pidfd, after re-checking
   it. Nothing runs inside that namespace, because the account can shape it. It never does this while another active
   record uses the same runtime dir.
3. If anything is still alive, cleanup fails (`cleanup_failed`, capacity held, retried every minute). The audit and
   the stop answer name the PID with a fixed label ("catatonit" or "another process of the account"; the
   account-chosen process name is never copied), and the qualification report's `cleanup_verified` is false, with
   that text in `cleanup_detail`.
4. Only then is the empty mount-point folder removed. Reconcile also removes empty `/run/acc-ct/<id>` folders that no
   active record and no listed `acc-ct-*` unit uses. A unit without a record is left for an owner, folder included:
   removing a folder that is a mount point in another namespace detaches that mount there. If the unit list can't be
   read, nothing is swept.

**Next qualification run: `ProtectProc=`.** Host-side processes in the unit (Podman, conmon, catatonit) can still
open `/proc/<pid>/root` of the account's other processes, which is same-uid ptrace-read access. The agent inside the
container can't, because it has its own PID namespace. `ProtectProc=invisible` or `ptraceable` on the unit would remove
that route. It isn't set, because systemd documents that it "cannot be used for services that need to access
metainformation about other users' processes". Container processes other than the keep-id user run as the account's
subordinate UIDs, which are other users in the unit's `/proc`. conmon runs outside Podman's user namespace, and
`podman exec`/crun open `/proc/<container pid>/ns/*`. Record, on the qualification host, with a test unit carrying
`ProtectProc=ptraceable` and then `invisible`:
- start, attach, `exec` and stop of the session image;
- conmon's exit-status handling;
- whether the guard's `/proc/self` checks pass.

Add it only if all of that works unchanged.

**Host git in a container's checkout.** New private checkouts get a broker marker outside their mount. Replacing
`.git` cannot turn off protection. Host reads run against a no-follow snapshot of Git metadata in a temporary
folder outside container mounts, with no shared administration, alternates, hooks, fsmonitor, global/system
config or unsafe config keys. Snapshot work is capped at 256 MiB, 100,000 entries, 64 levels and 10 seconds.
Branch return fetches this checked snapshot too. Host Git writes are refused; commit and push from the session's
terminal. These limits may refuse large repositories. Legacy private checkouts created before the marker existed
must be recreated before qualification; do not resume an unmarked checkout with rewritten administration.

**Fixed image behavior.** Image volumes are ignored; image healthchecks (`--no-healthcheck`, which also keeps Podman
from trying to add a systemd timer), automatic writable rootfs tmpfs and Podman file logging are disabled. A private cgroup namespace is explicit. OCI hooks are selected only from the
root-owned `/etc/acc/containers/hooks.d` directory (empty on a fresh install). Runtime/storage config files are
bypassed with explicit `CONTAINERS_CONF`, `CONTAINERS_CONF_OVERRIDE` and `CONTAINERS_STORAGE_CONF` settings;
subscription mounts are disabled, events have no file backend, and crun is selected at `/usr/bin/crun`. Network must be `none`; `pasta` is
refused until the separate egress policy is qualified. The image remains untrusted payload; its hash is not proof
that its code or hooks are safe.

**Bounded helper.** Its processes share `acc-launcher.slice` (256 MiB, 4352 tasks, one CPU); each service has
128 MiB and one CPU. A launch request's service gets 256 tasks: it runs Podman as the account for the image check, two
Go processes of about one thread per CPU each, and 32 tasks rejected a fork on a 20-CPU host (qualification attempt
14). The reconcile service runs no Podman and keeps 32. When the launcher's Podman check hits the limit, the request is
refused as `launcher_task_limit` and audited, even if Podman got through: a rejected fork may leave its state half made.
A process it left behind is named in the same refusal. The slice's task limit admits every connection the socket
allows (16) at 256 tasks, plus reconcile's 32, so it is never the limit that refuses. Kernels with hierarchical pids
events count a rejection in the cgroup whose own limit refused it, so only the request's own unit counter is ever
read, and only inside the launcher's own units.
The services run with `UMask=0077`, so every mode others need is set explicitly: the attach root 0755, audit files and a
`statusline.json` the launcher creates 0640 (accd reads both as the `acc` group). This is separate from payload budgets. The root disk walker uses descriptors,
never follows links, and stops with `disk_scan_failed` on races, 100,000 entries, 64 levels or a two-second budget
per mount. Disk remains polling, not a hard quota: a session can exhaust disk between scans. Audit stores at most
16 MiB per day of lifecycle events and 64 KiB per refusal bucket per day (64 fixed uid buckets) of refusals for 30 days. Saturation emits
`container.audit_saturated` to the journal; excess lines are intentionally dropped. Refusals cannot fill the
lifecycle stream. accd imports independent stream cursors even with `ACC_CONTAINERS` off. Runtime errors never
include command arguments or provider diagnostics in audit or responses. Run-folder materialization uses held
directory descriptors and refuses links before any launch files or environment are written.

**What the pinned image must hold.** Only the checkout and run folder are mounted, so the image must contain each
provider CLI at the path the policy names and ACC's `acc-hook`, `acc-guard` and `acc-statusline` at
`/srv/acc/bin/` (the paths sessions' hook settings name), plus Python 3 for them. A session whose guard is missing
must fail qualification: some CLIs treat a hook that can't run as a non-blocking error.

**Terminal attach (option A, built, not yet qualified).** The container lives in the launcher unit's private mount
namespace and root-owned user namespace, which the broker can't enter (CT-05), so `podman attach` from the broker can't
reach it. Instead:
- **The relay:** after the identity drop, the execution guard forks a relay as the account inside the unit. It listens
  on `/run/acc-ct-attach/<digest>/attach.sock`.
- **The folder:** the launcher makes it for the account (0700) before the unit starts and removes it at cleanup,
  without following anything left in it. It is never mounted into the container. Anything the account leaves there, or
  adds during removal, is audited as a leftover and never fails the cleanup.
- **The attach root** (`/run/acc-ct-attach`) must be root's real folder, 0755: the guard reaches the folder below it as
  the account. A narrower mode (an earlier launcher made it 0700 under its unit's umask: attempt 14) is set to 0755 on
  the held folder; a link, another owner, or group or other write is refused, never repaired.
- **What it relays:** one client at a time, only the account (the peer's uid is checked). It connects to conmon's
  attach socket (through a held folder, since the bundle path is longer than a socket address) and its `ctl` FIFO for
  resize.
  - Resize messages longer than 64 bytes or malformed are dropped.
  - A client that stops reading is dropped after 30 seconds.
  - Nothing one connection does ends the relay.
- **The pane:** it runs `acc.broker.container_attach` with the same frames as the broker's PTY attach. Every key goes
  to the agent, and the pane ends when the container does. The relay outlives a broker restart.
  - The pane starts in the session's checkout, which the agent writes, so Python runs in isolated mode (`-I`) and
    imports only from Rynhult's own source. It never uses the working folder or `PYTHON*` variables.
  - Before sending anything, the pane checks that the socket's listener is the account and runs in the session's unit
    (its cgroup or below), which the account can't move a process into. Otherwise it refuses.

Until this is qualified on a real machine
(attach, resize, broker restart and reattach, two sessions, and the container can't see the socket), accd refuses
container sessions with 409 unless `ACC_CONTAINERS_ATTACH_QUALIFIED=1`. That covers new sessions, and also resumes,
restarts and retries of container sessions that already exist, which fail with the same message. The relay assumes Podman 4.9's conmon
layout: `overlay-containers/<id>/userdata/{attach,ctl}` under the runroot, `SOCK_SEQPACKET` output with a stream
byte, and `1 <rows> <cols>` resize messages. Qualification must confirm that.

**Known qualification risks for the launcher itself.** Timeouts are set so a launch answers within accd's 60 s (the unit
has 40 s to report ready).

**Not covered by phase-1 qualification:** real provider login/resume with the adapters below, approved
project-service egress grants, real Orchestrator helper qualification, other machines and NVIDIA. Explanation
helpers stay native. The adapters remain off outside their own qualification window. An implementation with
fake transports is not runtime qualification.

## Phase 2 first slice: credential, hook and egress adapters

**Off and unqualified.** Phase 1 has shipped; its qualification does not qualify these adapters.
Keep the adapters off until the rebuilt image, credential delivery and egress boundaries have their own qualification.

Implemented behind the existing control-plane switch (`ACC_CONTAINERS=1` or Admin features) and project opt-in, with a second explicit
opt-in in the **root-owned pinned-image policy**. The account broker must also have `ACC_CONTAINERS=1` in its
protected service environment. Neither the installer nor the example policy enables these adapters. There is no
new API route, session-supplied mount, sudoers permission, runtime socket, root daemon or policy override.
The existing trusted launcher is extended; this slice was tested only as an ordinary user with fakes.

The owner must build/review and pin a **new image digest** containing the adapter runtime before qualification.
The old image cannot consume these mounts. In the selected image entry, `credentials: "readonly-v1"` enables
narrowing. The optional `egress` object has exactly `mode: "internet-v1"`, a nonempty `deny_hosts` list and a
nonempty `deny_networks` list. The owner lists **all** control-plane names (including public aliases), host public
addresses, connected LAN prefixes (including globally addressed IPv6 LANs), infrastructure endpoints and their
public/NAT addresses. Hostname exclusions cover subdomains.
`deny_hosts` matches the name a session asks the gateway for, never the TLS server name inside the connection:
a session can reach an excluded host through any other name or address that leads to it. Only `deny_networks`
is a boundary. A control plane behind a CDN or tunnel, whose addresses are shared, can't be excluded this way: keep
egress off for such a host (final RC security audit, F-1).
Empty exclusions, unsupported adapter modes and any network other than `none` are refused. Existing offline
policies without either key keep their earlier behavior and never read canonical sign-in files.

### Credential boundary

The broker reads only its own account's selected provider file, through held directories without following links.
It refuses foreign ownership, group/other permissions on the credential file, writable credential directories,
hardlinks, special files, malformed/oversized input and unsupported layouts. It does **not** read or mount the
owner's home, copy a home tree, or enumerate other providers. The supported first-slice formats are:

| Provider | Canonical source in the enrolled agent account | Delivered view |
|---|---|---|
| Claude | `.claude/.credentials.json` | Inference access token, expiry and inference scope only; no refresh token. Less than five minutes remaining refuses launch. |
| Codex | `.codex/auth.json` | Explicit `apikey` mode and `OPENAI_API_KEY` only. Rotating ChatGPT/OAuth sign-ins refuse; they need a separately qualified adapter. |
| Copilot | `.copilot/config.json`, including leading JSONC comment lines | Exactly the selected GitHub.com account and its token; no other accounts, hooks, trust settings or preferences. Enterprise/unknown layouts refuse. |

These formats require real-provider qualification against the pinned versions. In particular, access-only Claude
renewal and Copilot's read-only config may require a fresh generation before the CLI can continue; a fixture does
not establish provider refresh support. API keys/GitHub tokens retain their original authority and expiry. A
session/generation binding authenticates delivery, **not** provider-side revocation or a newly issued short-lived key.
With HTTPS egress the session can use them from anywhere: a Copilot token is the person's GitHub.com OAuth token, so
egress plus Copilot gives the agent GitHub API access as that person, beyond the guard's git command rules.
No refresh token is shared and no provider file is written back. Reauthenticate outside the container, then recreate
the session if renewal is needed. An unsupported sign-in fails explicitly, with no native fallback.

The broker holds the snapshot in memory and exposes a session-only Unix bootstrap endpoint. The launcher checks
its peer UID **and broker MainPID**, provider, session, generation and policy digest, then independently validates
the narrow payload. Bootstrap requires root via `SO_PEERCRED` and hands the credential over once. The launcher
seals a memfd against writes/growth/shrinkage and binds that file read-only at `/run/acc-credentials/auth.json`.
No provider snapshot token enters OCI environment/argv, audit metadata, a launch grant, image layer or persistent
root input snapshot.
Existing explicit `session.env` delivery remains unchanged. The image links only the selected provider's private
credential view to this read-only mount; mutable provider state remains private tmpfs. No host directory is mounted.

### Hooks in the pinned image

Claude managed settings and Copilot machine policy stay outside mutable provider state. Codex's immutable hook
configuration and forced `features.hooks=true` remain in force. The image includes `acc-guard`, `acc-hook`, the
bound-output statusline helper, `acc-request` and its fixed tool-coordinate validator. Events still reach only the
session's bound output files; no network/control socket is needed to collect them. This does not claim that every
arbitrary child CLI invokes a hook. Repeat the hostile-project hook qualification with the rebuilt image.

### LAN denial without direct container networking

The launcher still enforces **`--network=none`**, including its actual loopback-only Option A check. No slirp/pasta,
host, bridge or custom-network mode is enabled. For an opted-in image, a small image-local bridge listens only on
`127.0.0.1:18765` in that isolated namespace and transports CONNECT requests to the session gateway in the broker.
It receives no provider/session secrets in its process environment. The mounted Unix endpoint offers only the
bounded HTTPS gateway (plus the root-only one-shot bootstrap), never broker/runtime management commands.
The container can bypass the loopback bridge, but cannot bypass the host-side destination checks.

Every CONNECT must name port 443. The gateway resolves each request, refuses **all** answers if any is non-public
or excluded, and connects to an approved numeric address without a second resolution. It denies IPv4/IPv6 private,
loopback, link-local/metadata (and Azure's public platform address `168.63.129.16`), shared/CGNAT, multicast, reserved/site-local and IPv4 transition/translation ranges,
as well as the explicit control-plane/public-host exclusions. Because the gateway dials from the host's network
namespace, it also denies the host's own addresses and every directly connected network (on-link IPv4 routes in
every routing table and the host's local addresses and ranges, both from `fib_trie`; IPv6 interface prefixes and on-link IPv6 routes of /8 or longer), re-read for each
connection; unreadable or oversized routing state denies everything. This covers globally addressed IPv6 LANs and a VPS's own
public subnet even when the owner's list misses them. Prefixes reached through a router (other VLANs, a
network-specific NAT64 prefix, hairpin NAT to the host's public address) still need the owner's exclusions.
This split is the owner's decision (2026-10-25). Built in, always: RFC 1918, link-local and metadata, CGNAT, the
other special-use and transition ranges, the host and its on-link networks. The owner configures, in
`deny_networks`: every routed network. For example, a host on VLAN 10 (`192.0.2.0/24`) whose router also serves a
publicly numbered VLAN 20 (`2001:db8:20::/64`) must list `2001:db8:20::/64`. The gateway only sees a default
route to it and treats it as Internet. Direct TCP, UDP, DNS, QUIC and alternative proxies
cannot bypass `network=none`. HTTP, SSH, arbitrary ports and project-service exceptions are unsupported here.
HTTPS-capable clients may use the fixed proxy; clients that ignore it simply fail to connect.

Connections are bounded per session and broker, with bounded headers, connection/idle/total time and transfer bytes.
The gateway neither terminates TLS nor logs its contents. Stop/secret cleanup closes its listener and live streams;
a stale generation's stop cannot close the new generation. Launch failure closes the gateway without fallback.
A broker crash/restart removes the only working egress process, so an existing container loses egress; stop/recreate
it with a new generation. Automatic gateway recovery without weakening session identity is still unqualified.

Tests use synthetic sign-ins, fake DNS/dials and Unix/socketpair transports, plus simulated root metadata and fake
systemd/Podman commands. They cover narrowing, source-file attacks, wrong session/generation/provider/policy/peer,
sealed read-only views, hook layout, LAN/IPv6/rebinding denial, transport shutdown and no credential bytes in
persistent snapshots, argv or audit. No image was built, runtime/service installed, root command run, real token read
or external provider contacted for this slice. Root namespace/store qualification, real CLI auth/hooks, all public
infrastructure exclusions, transport under systemd, simultaneous-session isolation and restart/reboot behavior
remain owner acceptance gates. Keep default-off until that evidence exists.

## Qualification run

For a fresh Linux qualification window, use the reviewed
[owner qualification kit](../scripts/containers-qualification/README.md): read-only
preflight, a journaled setup, root-captured JSON evidence and guarded uninstall.
It refuses existing kit/image-store state and leaves the feature flag off.
The commands below remain the manual reference for already-provisioned hosts;
do not combine their mutations with a kit-owned run.

The owner runs these as root in an agreed window, from a retained root console. Substitute the enrolled agent
account for `alex-agent` and this checkout's path for `/srv/acc/app`. Nothing here is done by `deploy/install.sh`
except installing the helper and its units.

1. **Install the helper** (an ordinary update): `sudo /srv/acc/app/deploy/install.sh`. Check
   `systemctl status acc-containers.socket acc-containers-reconcile.timer`.
2. **Record the current state:**
   ```sh
   loginctl show-user alex-agent -p Linger
   sysctl -n kernel.apparmor_restrict_unprivileged_userns
   grep -E '^alex-agent:' /etc/subuid /etc/subgid
   ls /etc/apparmor.d/podman /etc/apparmor.d/crun /etc/sysctl.d/ | cat
   cat /proc/sys/kernel/yama/ptrace_scope   # must be 1 or more (decision 0038)
   ```
3. **Linger for the agent account** (its user manager and `/run/user/<uid>`, which Podman needs):
   ```sh
   sudo loginctl enable-linger alex-agent
   loginctl show-user alex-agent -p Linger -p State
   sudo systemctl status user@$(id -u alex-agent).service
   ```
4. **Baseline qualification, outside the broker's NoNewPrivileges** (as the agent account, from the checkout):
   ```sh
   sudo -u alex-agent -H bash -c 'cd /srv/acc/app && PYTHONPATH=src /srv/acc/app/.venv/bin/python \
     scripts/qualify_containers.py --enable-prototype --user-manager'
   ```
   The report must show `"launcher": "user-manager"`, `"no_new_privs": "0"` and `rootless_info_exit: 0`. Save it.
   Add `--image sha256:<64 hex> --image-archive /path/reviewed.oci.tar` for the payload gates.
5. **AppArmor profile, then the sysctl:**
   ```sh
   sudo install -m 0644 /srv/acc/app/deploy/apparmor/acc-podman /etc/apparmor.d/acc-podman
   sudo apparmor_parser -r /etc/apparmor.d/acc-podman
   # only if /etc/apparmor.d/podman or /etc/apparmor.d/crun is missing:
   #   sudo install -m 0644 /srv/acc/app/deploy/apparmor/acc-podman-runtime /etc/apparmor.d/ && sudo apparmor_parser -r /etc/apparmor.d/acc-podman-runtime
   sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=1
   ```
   Then rerun step 4 and check `sudo journalctl -k --since '15 minutes ago' | grep -i apparmor` for denials.
   Smoke-test the owner's Docker stacks, Chrome (the Playwright MCP) and Codex's sandbox.
6. **Phase-1 qualification through the launcher:** follow the
   [owner runbook](../scripts/container_qualification/README.md#phase-1-through-the-trusted-launcher)
   and [pinned image build/review](../deploy/containers/README.md). Build only with `deploy/containers/build-image`,
   as the dedicated `rynhult-build` system account (provisioned by the owner with
   `setup-build-account`, never used for sessions). It uses a private 0700 runtime
   directory and cgroupfs, with no user manager or lingering, and a private throwaway store, never the agent account's own store (a same-ID layer there shadows the
   protected image; qualification attempt 11). It refuses to start, and stops before saving or writing the checksum,
   while the building account runs any other process or can start one unattended (security review 8, SR8-3). Use the separate
   `qualification-phase1` policy and the non-root `acc` driver; **leave `ACC_CONTAINERS` off**.
   Every mandatory row must pass. The launcher must include the protected input snapshot change.
   This scoped session stop/recreate run does not replace the review's broker-restart,
   simultaneous-session, staging-race, credentials or disk-exhaustion gates.
7. **Keep or roll back.** On success, persist the sysctl in a reviewed `/etc/sysctl.d/` file and verify after a
   reboot. On failure: `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=<recorded value>`,
   stop test workloads, then `sudo apparmor_parser -R /etc/apparmor.d/acc-podman && sudo rm /etc/apparmor.d/acc-podman`
   (likewise `acc-podman-runtime` if loaded), remove `ACC_CONTAINERS=1` and restart accd, and `sudo loginctl disable-linger alex-agent` if it
   was off before and nothing depends on it. Preserve the reports and logs.


## Security review before merge (2026-10-23)

See `docs/reviews/2026-10-23-security-review-container-launcher.md` in the internal checkout for proofs, fixes and qualification
limits. Owner qualification must additionally verify descriptor-backed `BindPaths` on the installed systemd,
non-recursive source mounts and private staging, actual cgroup-v2 individual/aggregate accounting, normal `--rm`
cleanup, failed cleanup quarantine, the empty OCI hooks directory, and host Git reads after hostile metadata
changes. These tests do not run systemd, Podman, root or AppArmor on this machine.

The MainPID check assumes a protected broker and Yama ptrace scope at least 1. The helper does not enforce the
sysctl. Native programs already running as the agent account can invoke their own runtimes or user manager;
launcher budgets cover launcher-managed units, not every process belonging to that uid. The AppArmor `userns,`
profiles are executable namespace permissions with `flags=(unconfined)`, not payload confinement or protection
against native same-uid code. Do not treat the sysctl or these profiles as closing the remaining phase 0 gates.

Rootless Podman may join an existing pause mount namespace. Staging names are unique to the session and generation,
so an old namespace cannot alias another session's inputs. If that namespace cannot see the new staging path, the
launch must refuse. Multiple-session operation may need further runtime integration and remains an enablement
blocker until the owner demonstrates it. No fallback to mutable source paths is permitted.


## Phase-1 qualification harness (2026-10-23)

The [phase-1 driver](../scripts/container_qualification/driver.py) uses the actual
registered broker and trusted launcher without changing the default feature flag.
The [local image recipe](../deploy/containers/Containerfile) pins a Linux amd64
base digest, Node checksum and every resolved CLI npm package version/integrity.
Its final `FROM scratch` stage has **no RUN step**, only COPY, ENV, USER, WORKDIR, ENTRYPOINT and CMD:
- **Why:** during a RUN, Podman mounts its own `/run/.containerenv` (and `/etc/hosts`, `/etc/hostname`,
  `/etc/resolv.conf`) over the image. The commit can record those as deleted, which removes the mount targets the
  read-only rootfs needs. Qualification attempt 10 failed this way: crun couldn't open `rootfs/run/.containerenv`.
- **Where the checks run now:** `deploy/containers/check_rootfs.py` checks `/runtime` in the assembled stage and must
  leave that tree unchanged. It runs the runtime's files with the build stage's loader, libraries and interpreters, so
  the full checks (state folders, the provider import, the CLI `--version` calls) also run in a separate `checked`
  stage: the image as it will run. The final stage copies one unchanged file (`/opt/acc/rootfs-size.json`) from that
  stage, because Podman skips stages nothing uses.
- **The guard:** `verify_image_archive.py` reads every layer before deciding and applies them as overlay would:
  whiteouts, opaque folders, and a non-directory hiding what is below it. Each `slim_rootfs` mount target must end up
  as the right kind (folder, file, or `etc/mtab` linking to `../proc/self/mounts`). It refuses a layer that loses or
  replaces one and names the layer and path. It also refuses an image that lacks one, and layers an importer could
  read differently: `..`, duplicate entries, an entry below a non-directory, or data after a header tarfile stops
  at. It also refuses device nodes and other special files, set-id bits, and world-writable entries (except the
  `slim_rootfs.STICKY` folders, `/tmp` and `/var/tmp`, at mode 1777), and layers over 4 GiB uncompressed in total. It reads the mount targets and `STICKY` from `slim_rootfs.py` by
  parsing literals, never by running it, because root runs the verifier.
It includes Claude Code 2.1.308, Codex 0.164.0, Copilot 1.0.102, git, ACC helpers
and Chromium. A rootless local build and two-generation user-manager run were performed; no registry push occurred.

Real CLI tool calls are driven through an offline loopback model fixture. All
mandatory rows have pass/fail/skip reasons; GO requires every row, with failures
retained across stop/recreation. Immutable effective settings are mandatory and
are protected by image-baked read-only home/settings and root-owned, hash-verified input snapshots.
Only the exact events/statusline files are writable binds; mutable CLI state uses explicit tmpfs links.
The local run passes all three real provider hooks, Codex trust (without bypass), active
policy/hook mutation rejection, network, limits and cleanup. Claude uses immutable
machine-policy hooks with separate writable state; hostile user hook-disable settings do not
disable the guard. Copilot's hooks are described below. Under owner-selected Option A, the container is the sandbox: live Chrome uses --no-sandbox
only under the immutable launcher contract, and nested bubblewrap is not applicable.

**Root's files inside the session container.** The launcher's container is rootless, and host root is never mapped in
a rootless user namespace, so the protected rootfs and the launcher's contract show there as the kernel's overflow
uid (65534), not 0. The image's policy reader and its Codex and Chrome wrappers therefore take uid 0, or the overflow
uid when the process is in a user namespace and that uid isn't its own, as root's (attempt 15 found the contract
unverified and both wrappers exiting with "unsafe container policy" while they required 0). The payload runs as uid
1000, the launcher's `CONTAINER_UID`, and the driver reads that constant from the installed launcher and expects
exactly that uid in its report. The wrappers carry the policy's own `root_uids()`, copied into them when the image is
built, so the two ownership rules can't drift.

**Copilot's hooks in the session container** (attempt 16 ran the forbidden command with no hook firing). Copilot
1.0.102 skips machine-policy hooks (`/etc/github-copilot/policy.d`) that uid 0 doesn't own, so in the rootless
container it skipped them; the image no longer ships that copy. It reads hooks from `$COPILOT_HOME/hooks` only, and
rewrites `config.json` in `$COPILOT_HOME` at every start, so `$COPILOT_HOME` is the session's tmpfs
(`/tmp/acc-state/copilot`) and its `hooks` is a link to the image's read-only `/home/acc/.copilot/hooks`. In a folder
it trusts, a repository's `.github/copilot/settings.json` or `settings.local.json` with `disableAllHooks` switches
every user-level hook off. The image's `copilot` is a wrapper made from the same checked template as the Codex and
Chrome wrappers (`install_wrappers.py`); before every start `sandbox_policy.copilot_problem` checks:
- `COPILOT_HOME` is the session folder and no `--config-dir` moves it;
- the image's hooks file is root's, read-only all the way up, alone in its folder, and runs the guard before tools;
- the link is that link (it makes it on the first start);
- the session's `settings.json`, `settings.local.json` and `config.json`, and both repository files in the working
  folder and every folder above it, set no hook key other than `hooks`, `disableAllHooks: false` or an empty
  `disabledHooks`. A file that isn't plain JSON, has duplicate keys or is a link is refused.

On any problem Copilot doesn't start: the wrapper says why and exits 78, so the session ends at once (fail closed).
Copilot loads hooks once, at start (checked: removing the hooks folder or writing the repository file mid-session
doesn't change the running CLI), and the guard refuses an agent's writes of hook settings to the two repository files.
Only `copilot --version` skips the check. The qualification probe requires the refusals and then the guard's denial
and the post-tool event in a trusted repository.

**`acc-hook` uses only the image's commands.** Since the rename it ran `dirname`, which the image doesn't have
(`slim_rootfs.COMMANDS`), so it failed to source `acc-env.sh` and exited 2 with a message before writing anything.
Codex reads that as a blocked prompt and made no model request (attempt 16's `codex_hooks`); Claude lost every
post-tool event (`claude_hooks`). It now finds its folder with shell expansion, and a test runs it with only those
commands on `PATH`.
A local run has no registered broker, so it skips the trusted-launcher row. That row first passed on the
qualification host in attempt 19.
**The harness doesn't qualify production rollout, and neither does attempt 19's scoped GO**
([below](#phase-1-qualification-result-2026-10-26)). See decision
[0042](decisions/0042-container-qualification-evidence.md) and the runbook for
exact evidence and remaining security-review gates.


## Phase-1 qualification result (2026-10-26)

**Attempt 19 recorded a scoped phase-1 GO.** It does not qualify production.
- The report says `status: scoped-phase1-pass`, `go_phase_1: true` and `production_qualified: false`.
- Its sha256 is `5419ac6388ef32335eb6bcc1cf1e4362a93abaa5ddba6929b754fe56486a6afe`. The owner keeps it with the gate
  artifacts as `cq19/report.json`.
- The run took 22:54:09 to 22:55:28 UTC. The owner ran it on the qualification host with the
  [owner qualification kit](../scripts/containers-qualification/README.md), whose preflight is in the report, and the
  runbook's [phase-1 step](#qualification-run). `ACC_CONTAINERS` stayed off.

**What it ran on.** This is the scope the result holds for:
- **Host:** one Linux amd64 qualification host with Podman 4.9.3, crun 1.14.1 and systemd 255 (Ubuntu).
- **Account:** one non-administrative enrolled agent account, through its registered broker and the `acc` control account's driver.
- **Session:** one session with two container generations. Each took about 12.3 s.
- **Limits:** 2 CPUs, 4096 MiB memory, 512 PIDs, 4 GiB disk and 2 sessions.
- **Configuration:** network `none`, a synthetic model transport and no credentials. The separate `qualification-phase1` policy.
- **Image:** `sha256:287aa7c3838c6186b112385d021095966d169dc29a98c5e06ee0e845f00828d3`, from the reviewed archive
  `ec74683f526d796f75eb936bd96ac4fa7ff42edc869bfa81c00b978a474bb42b`, held in the protected image store.
- **Reviewed sources:** the report records the sha256 of every reviewed source it checked (`review_sha256`).

**What passed:**
- **Preflight:** all 50 checks. They cover:
  - the non-root account and its NoNewPrivs;
  - the protected executables and folders;
  - an installed launcher and execution guard that match the reviewed source;
  - the AppArmor profiles, the user-namespace sysctl, subordinate IDs, cgroup v2, linger and the user manager;
  - an account with no other processes before the run, and production containers off.
- **Every mandatory row of the phase-1 report (13 of 13):**
  - `trusted_launcher`: the registered broker authorized the fixed launcher unit.
  - `claude_hooks`, `codex_hooks`, `copilot_hooks`: each real CLI was run in a repository whose settings try to turn
    hooks off. Each gave the exact custom denial of the forbidden command (whose marker stayed absent), ran the allowed
    command and sent its post-tool event. Copilot refused the repository's hook-disabling settings before any model
    request.
  - `codex_hook_trust`: real non-interactive Codex hooks ran with no trust-bypass flag.
  - `codex_bundled_bwrap`: not applicable, because the container is the sandbox
    ([Option A](#owner-selected-option-a-phase-1)).
  - `chrome_sandbox`: a live Chrome renderer with `--no-sandbox` kept the outer NoNewPrivs and seccomp.
  - `network_none`: loopback only, with no external IPv4 or IPv6 route.
  - `per_session_limits`: the kernel's cgroup-v2 CPU, memory, swap and PID controls were checked for the unit and the
    aggregate slice.
  - `restart_stop_cleanup`: the launcher stopped the whole unit and removed the container after each of the two
    generations (`cleanup_verified: true`).
  - `immutable_inputs`: the kernel refused write, chmod and folder replacement for every effective hook and policy file.
  - `container_boundary`: writes outside the mounts failed with EROFS. The read-only root, dropped capabilities,
    NoNewPrivs and seccomp were verified.
  - `image_store`: a root-captured host probe found direct and `podman unshare` writes denied and keep-id layers protected.
- **Settle step:** the pause process and session bus ended, and only the broker and the user manager's `init.scope`
  remained.
- **Stale pause.pid:** the launcher cleared it at generation 2. The launcher's audit, not the report, records this as
  `container.pause_state_cleared`. Attempt 18b had failed on this (see "A `pause.pid` Podman can't join" under the
  [trusted launcher](#trusted-launcher-built-2026-10-23)).

**What this doesn't show.** Production qualification still needs every one of these, and `ACC_CONTAINERS` stays off
until then:
- **The runbook's gates:**
  - broker restart and reboot;
  - simultaneous sessions, including the two-session adversarial staging race;
  - disk exhaustion and hard disk quotas;
  - the host collector, which the image-store preflight row names as still required for runtime immutability.
- **The security-review items** ([above](#security-review-before-merge-2026-10-23)):
  - descriptor-backed `BindPaths` on the installed systemd;
  - quarantine of a failed cleanup;
  - host Git reads after hostile metadata changes;
  - multiple-session operation with a shared pause namespace.
- **Credentials:**
  - live sign-in;
  - refresh;
  - the provider adapters that deliver narrowed ACC-issued copies. These are not built.
- **Terminal attach:** attach, resize, broker restart and reattach, two sessions, and a socket the container can't
  see ([terminal attach](#trusted-launcher-built-2026-10-23)). Until then accd needs
  `ACC_CONTAINERS_ATTACH_QUALIFIED=1`.
- **`ProtectProc=`:** recording it on the qualification host, as described under the launcher.
- **Networking:** private/LAN egress denial with project grants (this run had no network). Rootless Docker fallback.
- **Scope beyond this run:**
  - every provider's real login and resume;
  - Orchestrator helpers;
  - other machines, WSL2, Mac VMs and NVIDIA;
  - other hosts, distributions and Podman versions;
  - the [phase 3](#effort-phases-and-owner-decisions) rollout gates.

One pass on one host is evidence for this configuration, not a guarantee of repeatability. A later qualification run
with a changed reviewed source, image or host needs a new report.


## Owner-selected Option A (phase 1)

Decision [0043](decisions/0043-container-is-the-session-sandbox.md) selects the
container as the sandbox. The root launcher generates a private frozen contract
and binds it read-only at `/run/acc-container/contract.json`. Image-only wrappers
at the pinned executable paths verify that contract, readonly root/parent, NNP,
seccomp and zero bounding capabilities before selecting Codex danger-full-access
and Chrome --no-sandbox. Environment/repository markers cannot select this policy.
No native compiler or host executable is changed; absence of a verified contract
preserves original arguments.

Security review of phase 1 (docs/reviews/2026-10-23-security-review-containers-phase-1.md) added:
the gate also observes loopback as the only interface, so a copied contract in a
networked container keeps the inner sandboxes on; every Codex command line ends
with `-c features.hooks=true`, because a repository's `.codex/config.toml` could
otherwise turn hooks off; image entries carry no group/other write, set-id bit or
ACL; and `container.launch` audits `sandbox=container`.

**CT-05 implementation:** session images now come from the root-owned read-only
`/var/lib/acc/session-images` additional image store. The launcher rejects image
resolution to account-local layers, including an account-local copy of the pinned
ID or of any of its layers. On that refusal the launcher's journal (never the audit) names the failing layer path,
whether it is in the account's own store, and the fix. The host probe and `image_store.py --check-store` find such
layers before a launch and print the exact removal commands, with the run root and tmp dir Podman recorded in the
store's database (`libpod/db.sql`; Podman refuses any other). When those can't be read (an older BoltDB store, or a
recorded value that isn't a plain absolute path), the check names what to remove and prints no command.
When the launcher's own `podman image inspect` fails, the refusal and its `container.refused` audit say how: Podman's
exit code and its last error line (printable text only, at most 240 characters, every path but Podman and the
protected store replaced by `<path>`), a timeout, or an answer that isn't exactly one image. Qualification attempt 17
lost that reason. The owner verifies the reviewed archive checksum and every layer/config
digest before importing and records the expected ID in root-owned policy. Follow
[the exact installation and host probe commands](../deploy/containers/ROOT-IMAGE-STORE.md).
Qualification requires fresh root-captured proof that direct host writes and
`podman unshare` writes fail, and that keep-id uses protected runtime lowerdirs.
The implementation is covered by non-root tests; owner installation and runtime
qualification remain required before production use.

Historical nested-sandbox gates above are superseded by this explicit policy.
No proc masks, seccomp, capabilities, network or immutable input checks were
relaxed. Qualification labels bubblewrap not applicable, observes a live Chrome
renderer with --no-sandbox and outer NNP/seccomp, and requires a new mandatory
container_boundary test: outside-mount writes under /etc and /usr must fail EROFS.
The local two-generation run passes all image/runtime rows; trusted launcher
remains skipped until the owner executes the protected control-account driver.
Security review precedes merge, and the global container flag stays off.

## Phase 2: inner sandboxes and Orchestrator helpers (2026-10-25)

Implemented behind the existing, default-off `ACC_CONTAINERS` gate and project
switch; tested with fake launchers/providers. **No new runtime qualification or
production GO.** Decision [0043](decisions/0043-container-is-the-session-sandbox.md)
still applies. The image entrypoint now refuses an unverified launcher contract
before running a provider. Native providers and their sandbox settings are unchanged.

| Provider inside the pinned image | Explicit policy | Reason |
|---|---|---|
| Codex | Inner sandbox disabled: verified image wrapper selects `danger-full-access`; approval settings and mandatory hooks remain | Nested bubblewrap's proc mounts conflict with retained OCI proc masks. The outer container is the approved boundary. |
| Claude Code | Inner Bash sandbox disabled: immutable `/etc/claude-code/managed-settings.json` sets `sandbox.enabled=false`; mandatory managed hooks remain | Nested Linux bubblewrap is not qualified under the retained proc masks/seccomp/drop-all policy. Do not silently retry a failed nested sandbox or relax the outer boundary. |
| Chromium | Existing verified wrapper selects `--no-sandbox` | Decision 0043; retained outer seccomp/NNP/capability checks remain mandatory. |

Claude's [sandbox configuration](https://code.claude.com/docs/en/sandboxing) and
[managed settings precedence](https://code.claude.com/docs/en/settings) explain the
upstream knobs; the pinned version's actual behavior must still be qualified.
This slice does not claim either provider's native sandbox works nested. Enabling
it later needs a separate reviewed profile and qualification of bubblewrap,
socat/proxy dependencies, nested user/mount namespaces, seccomp syscall rules,
proc mounts and AppArmor on the target kernel. Do not add `--privileged`,
`seccomp=unconfined`, capabilities, unmasked proc paths or a host sysctl workaround.
The existing root-owned execution namespace bootstrap, subordinate mappings,
keep-id payload namespace, default seccomp, NNP and zero capabilities stay required.

The reviewed image recipe must contain the pinned Claude/Codex packages and
checksums, Python and its pinned dependencies, Node where required, Git, shells,
Chromium, immutable effective managed hooks, `sandbox_policy.py`, `acc-session`,
and **`acc-mcp-team` with `acc_env.py`** alongside the existing guard/hook/request
helpers. The slim runtime explicitly retains the team helper. Rebuild, inspect,
review and install a **new digest and protected rootfs**; an existing phase-1
image is not sufficient. No host CLI installation is inherited. Bubblewrap and
socat are not prerequisites for this disabled-inner-sandbox profile.

A container Orchestrator grant allows `machines=["local"]` only. Helpers and
helpers of helpers inherit container execution even if the project's switch is
later turned off. Each starts from its parent's committed HEAD in a new standalone
repository, through the same broker, grants, image policy and launcher as ordinary
container sessions. Parent metadata is copied into a bounded no-follow snapshot
before host Git reads or bundles it. No shared Git administration or native launch
fallback is introduced. Turning `ACC_CONTAINERS` off refuses new helpers. A native
lead whose project switches to containers must start a new container team first.
Team depth/total/concurrency/budget/permission rules remain in force.

The launcher holds the account-owned, private `run/<session>/team` directory by
descriptor and binds it **read-only inside** the container. The broker atomically
publishes replies there after launch; the frozen launch snapshot has only an
empty mountpoint. Only `events.jsonl` and `statusline.json` retain writable output
binds. No extra host workspace, broker/runtime socket or credentials are exposed.
Symlinks and group/other-accessible reply directories refuse launch.

For container teams, `acc-team merge` prepares an at-most-8-MiB Git bundle from
the stopped helper's safe metadata snapshot, under the lead's reply directory.
It returns `prepared=true`, `merged=false`, bundle path, branch, commit and
instructions. The lead runs `git fetch <bundle> <branch>` and `git merge FETCH_HEAD`
**inside its container**, verifies the result, then calls `finish`. Conflicts are
resolved or aborted there. Preparation never reports a completed merge or archives
the helper. The bundle is streamed and capped in memory, so an oversized branch
never reaches the disk. There is one bundle per helper branch (`merge-<branch>.bundle`):
preparing again replaces it, `finish` removes it, and archiving a container session
removes every merge bundle in its reply directory. Native teams retain automatic
merges; a native helper whose project switched to containers later gets a refusal
that names its branch for a manual merge, never a host merge. Archive still returns clean
helper branches to the project; dirty checkouts are retained. Explanation helpers
remain native and are outside this project-team integration.

### Additional owner root qualification gate

In addition to every existing phase-1 row, retain evidence from the registered
broker/trusted launcher, with the newly reviewed image/rootfs:

1. Record the digest, source revision, CLI versions and managed Claude sandbox
   setting. Run real Claude and Codex tool calls with hostile repository settings;
   verify Claude's inner sandbox is explicitly off, Codex selects the container
   policy, and both still invoke mandatory pre/post hooks. Record disabled-with-
   reason, never a passing nested-sandbox result. Launch without a valid contract
   must refuse at the entrypoint; changing environment/repository markers must
   not enable the policy. Existing outside-mount EROFS, network, seccomp, NNP,
   capabilities and immutable-hook mutation rows must pass again.
2. Through each real provider's **stdio acc-team MCP**, start a local helper and
   a descendant. Verify separate checkouts/Git administration, namespaces,
   container IDs, grants, cgroups, aggregate capacity and protected image mounts.
   Verify replies created after launch arrive, survive resume/recreation, and
   cannot be written or redirected by the payload. No broker/runtime socket may
   appear. A forged reply directory/link must refuse before runtime startup.
3. Commit in a helper, stop it, prepare a bundle, import/merge in the lead
   container, then finish. Check conflicts, changed branches, oversized/unsafe
   metadata, dirty archive, retained branch and no host Git hooks/filters executed.
   Test permissions still belong to the person in Inbox.
4. Refuse missing image/helper, launcher failure, remote helper placement,
   exhausted capacity and disabled `ACC_CONTAINERS`, without a host CLI process.
   Stop the team and prove descendant cgroups/credentials are gone. Test broker
   restart and stopped-helper resume with fresh generation/reply mounts.

These are owner-run qualification requirements, not results from this machine.
Credentials, networking, persistent provider state and other existing unqualified
phase-1/platform rows remain blockers. Only related fake tests ran for this slice.
