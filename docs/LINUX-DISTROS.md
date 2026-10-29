# Linux distributions beyond Debian

The October 25 owner requirement (#293) covered main/node installers and the
desktop client. [Decision 0073](decisions/0073-v1-linux-server-scope.md) limits v1
to the Linux main server used from browsers, with existing Linux, macOS and WSL
nodes. The main stays qualified on **Ubuntu 24.04+ and Debian 13+ with systemd**.
Linux desktop applications, RPMs, PKGBUILDs and AppImages are **post-v1**; native
platforms will follow one at a time, in an order the owner has not chosen.

## v1 node remainder (2026-10-27, main-370 audit)

| Path | Current status | v1 treatment |
|---|---|---|
| Main on Ubuntu 24.04+ / Debian 13+, systemd | Qualified main distributions | Keep existing regression gates |
| Debian/Ubuntu Linux node, systemd and Python 3.12+ | Existing node installer path | Keep existing capabilities and limits |
| Fedora/RHEL node (dnf) | Recognised, explicit test-install opt-in; not qualified | Node installer/provider-helper tests and independent machine qualification |
| openSUSE node (zypper) | Recognised, explicit test-install opt-in; not qualified | Same; repository/LSM/version-specific qualification |
| Arch node (pacman) | Recognised, explicit test-install opt-in; not qualified | Same; preserve rolling repository consistency, qualify derivatives separately |
| macOS node / Linux node in Windows WSL | Existing node paths | Retain current capabilities and limits; not native desktop applications |
| Desktop .deb, RPM, Arch package, AppImage | Experimental desktop artifacts/proofs | Park further delivery/qualification after v1 |

**No additional distribution becomes supported by this audit.** A package manager
is not a distribution/version qualification record. Fedora evidence does not
qualify RHEL; Tumbleweed does not qualify every openSUSE release; Arch does not
qualify its derivatives. Direct main test installs on these managers remain
experimental too; expanding the qualified main is outside this lane.

All node package-manager paths from distros 1–4 are already in `main-370`
(`fb596fe19`), including #337 (`db832e878`). There is no node-only patch to recover
from `linux-rpm-qualification`: its node installer, package-manager map, SELinux
label helper and provider package helper are identical to main-370. Do not replay
the old stack wholesale over newer security fixes.

Here, **node packages** means prerequisites plus the root-owned `acc-packages`
provider helper and its node service drop-ins. A node uses `/opt/acc/app`, its own
agent accounts and an outbound connection to the main. It does not install the
Tauri desktop app, GUI libraries, Caddy or a dashboard. Existing desktop RPMs
are not node RPMs; no native node RPM/PKGBUILD is introduced or required by this
slice. Keep using the source/enrollment installation route.

This slice fixes two node SELinux ordering gaps: missing `semanage`/`restorecon`
are independent prerequisites even if Python/tmux/git/uv already exist; persistent
labels are restored **after** installing the executable payload and creating
the runtime, updater and package-request directories, before starting account
brokers or request consumers. Node updater/package state gets scoped `var_lib_t`
contexts; runtime sockets retain `var_run_t`. An unexpected `selinuxenabled` exit
refuses installation rather than skipping labels. Dry-run tests cover those boundaries, no-prerequisite
refusal and repeated multi-account update plans. The unqualified-distribution
opt-in and enforcing mode are unchanged.

### Qualification still required for additional Linux nodes

Use disposable machines, never a production node or main. Retain exact distro
release, architecture, package inventory, kernel/systemd/LSM state and source hash.
For each Fedora, RHEL, openSUSE and Arch target separately:

1. Confirm systemd and the required Python 3.12+ are available at the installer's
   interpreter path. Without a suitable interpreter, stop; do not replace the
   system interpreter or assume installing the package named `python3` upgrades it.
   Confirm Node.js 22+ before testing npm-installed providers. A recognised manager
   does not guarantee these versions in its configured repositories.
2. First run the node dry run, proving default refusal without
   `--unqualified-distro`, then the opt-in prerequisite plan. Install only in that
   disposable machine after administrator review, keeping distro repositories and
   trust keys. No new upstream repository or Arch partial upgrade is implied.
3. On an enforcing SELinux guest, ensure `selinuxenabled` is installed (the
   installer uses that detector), then prove labels and actual service transitions,
   inspect AVCs after fresh install and update, and test missing-tool refusal.
   On any LSM, retain the actual active policy; never disable it to pass.
4. Enroll against a separate disposable main on a qualified distribution. Prove
   authenticated reconnect, per-account socket/peer-credential isolation, refusal
   across accounts, session hook/PTY operation and stop through fixture APIs.
5. Exercise the node provider helper with fake pinned artifacts and refusal paths;
   retain signature/hash and root-owned path checks. Separate real provider
   auth/inference qualification from fake-provider tests.
6. Update and rerun with multiple configured accounts, proving roots, identities
   and enrollment remain intact. Test disconnected/reconnect behavior and cleanup
   in the guest; stop only the guest's owned sessions and units.

The existing desktop container proofs do **not** satisfy these node steps. No
real distro/systemd/SELinux guest was run for this slice; its tests simulate
managers and dependency availability and only execute installer dry runs.

### Parked desktop work (retain branches; do not revert shipped fixes)

| Branch / commits | Disposition |
|---|---|
| `linux-rpm-qualification`: `790d58d66`, `5403e9eae` | Unmerged desktop libexec launcher and Fedora/Tumbleweed RPM/systemd/SELinux proof. Park for later desktop review/replay against then-current main; not node qualification. |
| `linux-desktop-qualification`: `ee744608a`, `c78889841`; `linux-arch-qualification`: `333742628`, `71c710781`, `b7d266110`; reviewed replay `linux-arch-qualification-r`: `a348a6d54`, `6ff521c7c`, `5feac047f`, handoff `8d946d0aa` | Historical desktop account/PTY/uninstall/container evidence. Reviewed equivalents shipped in #337; retain fixes, defer further desktop qualification. |
| `linux-distros-4`: `80ba79091`, `542457823`; `linux-distros-4-r`: `e540170fb`, `de69dc3c7`, handoff `ff1ec3066` | Mixed historical stack: installer/pacman work already shipped in #337; desktop PKGBUILD delivery is post-v1. No wholesale replay. |
| `linux-distros-1/2/3`, `linux-stack-review`, `linux-stack-r`, `linux-stack-lows` | Foundational mixed stacks and reviewed hardening already represented on main, including #337. Retain shipped protections; no further desktop work for v1. |
| `rc-linux-packages`: `81c512c1a`, `8b8d9fdc8`, `662ae0a60` | Desktop runtime/build, prerelease format, metadata and maintainer work. Park desktop delivery. The installed-main settings-read diagnostic from `8b8d9fdc8` is already on main; do not lose it. |

The sections below retain historical implementation and desktop qualification
records. They are not a supported-platform list or v1 native-app release gate.

Machine labels in the qualification narrative below are anonymized. The
recorded results and unsupported-distribution boundaries are unchanged.

Historical additional-distribution proposal (not current scheduling):

The proposed additional set is Fedora/RHEL (dnf, RPM),
openSUSE (zypper, RPM), Arch (pacman, PKGBUILD), and AppImage as a portable fallback. The list itself awaits the
owner's confirmation. **No additional distribution is listed as supported yet.** Each needs its own qualification
first. Arch is the first qualification target; desktop RPM, Arch and AppImage
release qualification belongs to the later Linux desktop app, not the v1 server gate. Package-manager recognition or an
unsigned developer build is not release qualification.

## Phase 1 (this change)

- **Detection.** `deploy/lib/pkg.sh` detects the package manager: apt, then dnf, zypper and pacman. It also maps
  package names and builds the install command. The apt branch produces exactly the commands it did before (a test
  compares them), so Debian and Ubuntu behave byte for byte the same.
- **Node installer** (`deploy/node/install.sh`):
  - On dnf, zypper or pacman it refuses unless given `--unqualified-distro`, which allows a test install. With
    it, it installs prerequisites and the distribution's own Node.js through that manager.
  - A system with none of the four is refused clearly.
- **Main installer** (`deploy/setup.sh`, `deploy/install.sh`): refuses anything but apt and names what it found.
  Phase 2 lifts this.
- **Desktop:** `desktop/rpm/rynhult-desktop.spec` with `desktop/package-rpm.sh` repackages the Tauri `.deb` as an
  RPM, with the same files at the same paths.

Verified in rootless Podman containers (Fedora 42, openSUSE Tumbleweed, Arch Linux, 2026-10-25); details in the
handoff:
- The node installer's dry run refuses, then plans each manager's commands.
- Those planned installs succeed for real inside the containers: tmux, git, Node.js 22, 24 and 26 respectively.
- The RPM builds from a real Tauri `.deb` and installs with dnf and zypper.

Nothing ran on acc-host.

## Phase 2 (the four findings, and AppImage)

- **Architecture without dpkg:** `setup.sh` maps the kernel's `x86_64`/`aarch64` to `amd64`/`arm64`. Debian, which
  has dpkg, is unchanged.
- **setfacl / acl declared and checked:**
  - `deploy/install.sh` stops before changing anything when `setfacl` is missing, naming the package for that
    manager. Without it, `acc-agent-share` would silently grant nothing. `setup.sh` already installs `acl` on apt.
  - The desktop `.deb` depends on `acl`, and the RPM requires `/usr/bin/setfacl` (a file dependency, the same on
    every RPM distribution).
  - `desktop/setup.py` already refuses without it, which also covers the AppImage.
- **Bytecode compiled at build time:**
  - `desktop/package.sh` compiles every shipped module, including the bundled runtime's standard library and
    site-packages, with `-f` and `unchecked-hash`. The `.deb`, the RPM (repackaged from it) and the AppImage (built
    from the same data) all carry it.
  - Checked on a real `.deb`'s runtime: the first run as a writing user wrote 129 `.pyc` files before the change and
    none after. `compileall` exits 0, and the size is unchanged (160 MB).
- **No spaces in package names:**
  - A Linux-only packaging config (`desktop/src-tauri/tauri.package.linux.conf.json`) sets the product name to
    `rynhult-linux-spike`. Its desktop template keeps `Name=Rynhult Linux spike`.
  - Resources move to `/usr/lib/rynhult-linux-spike` and the desktop file to `rynhult-linux-spike.desktop`. The
    `.deb` package name was already `rynhult-linux-spike`.
  - The window title is set in code and is unchanged; the Mac build doesn't use the Linux config.
  - The RPM no longer needs its desktop-file provides workaround.
- **AppImage recipe:**
  - `desktop/appimage/fetch-tools.sh` fetches AppRun, linuxdeploy (Tauri's release `07333c6`) and
    linuxdeploy-plugin-appimage (`1-alpha-20250213-1`, a tagged release instead of Tauri's default "continuous").
    It checks each against its digest: GitHub's published digest for the first two, a first-seen one for the plugin
    (re-verify in release review).
  - It applies the same 3-byte magic blanking Tauri applies to linuxdeploy, so `tools.sha256` stays stable.
  - `package.sh --appimage` sets `bundle.useLocalToolsDir`, so the tools live under the checkout's target folder,
    not a shared cache. It refuses unless the pins match, and checks them again after the build.
  - The AppImage needs the build host's WebKitGTK 4.1 helper processes, which Tauri copies in. It is a distribution
    format, not isolation.

Not re-verified in containers: from mid-afternoon, `kernel.apparmor_restrict_unprivileged_userns` was 1 on the build
host, so rootless Podman (and even `unshare --user`) is refused. The RPM with the new names, a full AppImage build and
the container dry runs are therefore pending. A full desktop build was also not run, to keep host load down.

## Phase 3 (the coordinator's decisions, SELinux, systemd units)

The main installer now takes Fedora/RHEL (dnf) and openSUSE (zypper) for a **test install** with
`deploy/setup.sh --unqualified-distro`. The flag passes `ACC_UNQUALIFIED_DISTRO` to `deploy/install.sh`. Arch, and any
other manager, are still refused for the main installer. Debian and Ubuntu are unchanged: setup's and the node
installer's dry runs are byte-identical, and the apt code is kept verbatim inside its branches.

- **Claude Code without apt** (decision: the official native installer, pinned):
  - `acc-packages` uses method `claude-native` wherever apt-get is absent. It installs Anthropic's native release
    2.1.308, from `downloads.claude.ai/claude-code-releases/<version>/<platform>/claude`, checked against the SHA-256
    Anthropic publishes in that version's `manifest.json` (`linux-x64`, `linux-arm64`).
  - The download is capped at 512 MiB. It installs into `/opt/acc-claude/versions/<v>`, linked atomically from
    `/usr/local/bin/claude`, like restic.
  - The binary is root-owned, so its own per-user updater can't replace it. A new version is a Rynhult change, as for
    the other provider pins. Setup installs it right after Rynhult; elsewhere, an owner does from Host → Providers.
- **Project stacks with Podman** (decision: Podman on Fedora/openSUSE):
  - Setup installs `podman` and `docker-compose` (the compose v2 provider), sets the owner's subordinate IDs and
    linger, and enables the owner's `podman.socket` user unit.
  - The services broker runs stacks through `acc.broker.ops.compose_command()`. Rootless Docker stays first, so
    Debian is unchanged. Podman is used when the owner has a Podman socket and no Docker socket, as
    `podman compose`; `ps`, `config --format json` (the approval digest) and `up` behave the same. An explicit
    `DOCKER_HOST` is kept.
  - Owners see an engine problem in the project's Runtime note, like any broker error.
- **SELinux** (Fedora/RHEL enforcing, openSUSE Tumbleweed): see "SELinux" below. The installers label Rynhult's
  paths and **never** run `setenforce` or add permissive domains.
- **systemd units:** see "systemd units" below. A distribution's Caddy unit is overridden by one more drop-in, off
  Debian only.

Package names were checked against Fedora 43/44 (mdapi) and Tumbleweed's published repository (2026-10-25). Fedora
has podman 5.8, docker-compose, caddy 2.10, gh, policycoreutils-python-utils, shadow-utils and sqlite. Tumbleweed has
podman 6.1, docker-compose, caddy 2.11, gpg2, shadow, sqlite3, nodejs-default and npm-default. `npm`, `nodejs` (on
Fedora 44) and `python3` (on Tumbleweed) are virtual names that the package managers resolve, as the phase 1
containers showed for `npm` and `python3`.

### SELinux

`deploy/lib/selinux-labels.sh main|node` runs where `selinuxenabled` succeeds. Setup and the node installer add
`policycoreutils-python-utils` for `semanage` there. It adds persistent rules with
`semanage fcontext -a` (or `-m` on a rerun) and applies them with `restorecon -R`:

| Path | Type | Why |
|---|---|---|
| `/srv/acc/app/.venv/bin`, `/srv/acc/bin` (node: `/opt/acc/app/.venv/bin`, `/opt/acc/bin`) | `bin_t` | systemd and people start these; a service started from `var_t` (`/srv`'s default) would be refused |
| `/srv/acc/data`, `/srv/acc/git` (node: `/var/lib/acc-node`) | `var_lib_t` | State |
| `/srv/acc/run`, `/srv/acc/run-root`, `/run/acc-broker` | `var_run_t` | Session spools and the brokers' sockets |
| `/run/accd.sock` | `httpd_var_run_t` | A confined Caddy (`httpd_t`) may write to accd's socket |

Policy that labels can't give: Fedora's `caddy` package runs Caddy confined as `httpd_t`, and `httpd_t` may not
connect to a socket served by `unconfined_service_t` (accd). The module `deploy/selinux/rynhult.te` allows exactly
that (`allow httpd_t unconfined_service_t:unix_stream_socket connectto`). An owner loads it during qualification:
`checkmodule -M -m -o rynhult.mod rynhult.te && semodule_package -o rynhult.pp -m rynhult.mod && semodule -i
rynhult.pp`. The Cloudflare Caddy build in `/usr/local/bin` (`bin_t`) runs unconfined and doesn't need it.

Expected to need no module, but to be confirmed in the qualification VM's audit log:
- the brokers and accd (`unconfined_service_t`, from `bin_t`);
- rootless Podman under the owner's user manager;
- the container launcher's crun (`container_t`).

Check with `ausearch -m AVC -ts recent`. Fix a denial with a label or a reviewed module rule, never with
`setenforce 0` or `semanage permissive -a`.

### systemd units

| | Debian/Ubuntu | Fedora | openSUSE Tumbleweed |
|---|---|---|---|
| Vendor units | `/lib/systemd/system` (= `/usr/lib/...` with merged /usr) | `/usr/lib/systemd/system` | `/usr/lib/systemd/system` |
| Rynhult's units and drop-ins | `/etc/systemd/system` | same | same |
| Caddy unit (vendor) | `ExecStart=/usr/bin/caddy run --environ --config /etc/caddy/Caddyfile` | adds `ExecStartPre=/usr/bin/caddy validate ...` | `ExecStart=... run --resume --environ ...` |
| Caddy user | `caddy`, home `/var/lib/caddy` | sysusers: `caddy`, `/var/lib/caddy` | sysusers: `caddy`, `/var/lib/caddy` |
| Podman user socket | (Docker) | `podman.socket` in `/usr/lib/systemd/user` | same |

Rynhult writes only to `/etc/systemd/system`, and `deploy/lib/unit-aliases.py` already searches both vendor
directories. Off Debian, `install.sh` adds `caddy.service.d/distro.conf`, which clears `ExecStartPre` and sets
`ExecStart` to the chosen Caddy, run with `--environ --config /etc/caddy/Caddyfile`:
- On Fedora, the validation would use `/usr/bin/caddy`, which lacks the DNS module of a Cloudflare build, and refuse
  to start.
- On openSUSE, `--resume` would prefer Caddy's autosave to the Caddyfile.

Caddy's data path (`/var/lib/caddy/.local/share/caddy`, whose local CA root `install.sh` copies) is the same on all
three, because none of the units sets the XDG paths. Fedora's unit adds `CAP_NET_ADMIN`; the hardening drop-in
already resets capabilities to `CAP_NET_BIND_SERVICE`.

## Audit: Debian assumptions

**Historical audit (before phases 2–4), not the current implementation matrix.**
The table retains the original assumptions and proposed work; "planned" and
"when the main installer gains other managers" describe that earlier point.
See [phase 3](#phase-3-the-coordinators-decisions-selinux-systemd-units) and
[phase 4](#phase-4-arch-first-automated-desktop-qualification-passed) for implemented paths.
The Arch PKGBUILD is implemented; its full current package dependencies are in
[Desktop local package](#desktop-local-package).

| Where | Assumption | Plan |
|---|---|---|
| `deploy/setup.sh` | `apt-get` for base packages, with Debian names | Map names per manager (table below); refresh only where needed |
| `deploy/setup.sh` | `dpkg --print-architecture` (`amd64`/`arm64`); the fallback `uname -m` gave `x86_64`/`aarch64`, which the architecture check refused | Fixed in phase 2: normalised before the check |
| `deploy/setup.sh` | Docker CE from Docker's apt repository (key fingerprint pinned) | Fedora/RHEL: Docker's dnf repository, with its signing key pinned. openSUSE and Arch: the distribution's `docker` plus rootless extras. Or decide on Podman for project stacks (see below) |
| `deploy/setup.sh` | Caddy from Cloudsmith's apt repository (pinned key) | Fedora: the `@caddy/caddy` Copr; openSUSE and Arch: the distribution's package, if new enough for the site config; the Cloudflare build is unchanged (`/usr/local/bin/caddy`) |
| `deploy/setup.sh`, `deploy/node/install.sh`, `deploy/packages/acc-packages`, `deploy/lib/claude-apt-repo.sh` | Claude Code from Anthropic's signed apt repository, held with `apt-mark` | Phase 3: Anthropic's native release, pinned with its published SHA-256 (`claude-native`), wherever apt is absent |
| `deploy/lib/install-node.sh` | NodeSource's apt repository | Current Fedora, Tumbleweed and Arch ship Node.js 22 or newer: use theirs (done in the node installer). RHEL: AppStream module streams |
| `deploy/install.sh` | `dpkg -s` / `apt-mark hold claude-code` | Goes with the Claude Code decision; a no-op elsewhere |
| `deploy/install.sh` | AppArmor profiles for Chrome and Codex's bubblewrap, loaded when AppArmor is present | Fedora/RHEL use SELinux: no AppArmor profiles, but file contexts for executables under `/srv/acc` (below). openSUSE moved to SELinux with Tumbleweed (2025); Leap 15 has AppArmor. Arch: neither by default |
| `deploy/acc-install` | `whiptail` from apt (already guarded by `command -v apt-get`) | `newt` on dnf/zypper, `libnewt` on Arch |
| `deploy/packages/acc-packages` | apt for packaged CLIs (Claude Code, Ollama) | A manager-neutral backend once the Claude Code channel is decided |
| `deploy/broker/acc-agent-share`, `desktop/setup.py` | `setfacl` (the `acl` package). Present on Debian/Ubuntu; **missing from the Fedora and Tumbleweed base images** | Phase 2: install.sh checks it before changing anything; the desktop `.deb`/RPM declare it. Add `acl` to the base packages when the main installer gains other managers |
| `useradd ... --shell /usr/sbin/nologin` (main, node, telemetry) | `/usr/sbin/nologin` | Present on all three images checked (Arch through its merged `/usr/sbin`) |
| Units in `/etc/systemd/system` | Portable | Unchanged. `loginctl enable-linger` and `usermod --add-subuids` exist everywhere (shadow 4.x) |
| `desktop/package.sh` | Tauri `.deb` only | RPM (phase 1) and an AppImage recipe (phase 2); PKGBUILD planned below |
| `deploy/containers/Containerfile` | Debian-based pinned image | Unchanged: the container image is independent of the host distribution |

### Package names

**Historical package-name mapping, not a complete prerequisite or dependency list.**
Use the installers' current checks and the package recipes for required packages;
the [phase 4 desktop recipe](#desktop-local-package) lists the full Arch GUI dependencies.

| Debian | Fedora/RHEL (dnf) | openSUSE (zypper) | Arch (pacman) |
|---|---|---|---|
| python3, python3-venv | python3 (venv included) | python3 (venv included) | python |
| gnupg | gnupg2 | gpg2 | gnupg |
| sqlite3 | sqlite | sqlite3 | sqlite |
| uidmap | shadow-utils | shadow | shadow |
| dbus-user-session | dbus-daemon or dbus-broker | dbus-1 | dbus |
| gh | gh | gh | github-cli |
| nodejs (NodeSource) | nodejs, npm | nodejs-default, npm-default | nodejs, npm |
| whiptail | newt | newt | libnewt |
| tmux, git, curl, ca-certificates, jq, acl, rsync, smartmontools | same | same | same |

`deploy/lib/pkg.sh` maps only what the node installer installs today (python3, gnupg, nodejs, npm); phase 2 adds
the rest with the main installer. On Arch, a list refresh without a full upgrade (`pacman -Sy`) is a partial
upgrade, which Arch doesn't support. So the installer never refreshes there: run `pacman -Syu` first on a stale
system. Phase 2 should say this when an install fails.

## Per-distribution plan

**Historical proposal, not current implementation or qualified support.** In
particular, the Arch desktop dependency shorthand below is incomplete; use the
[implemented phase 4 recipe and full dependency list](#desktop-local-package).
Phase 3 records the chosen Podman stack path; the Docker/Podman alternatives
below preserve the earlier design discussion. No distribution becomes supported
by appearing in this table.

| | Fedora / RHEL 9+ | openSUSE Tumbleweed / Leap | Arch |
|---|---|---|---|
| Node installer | dnf (done, unqualified) | zypper (done, unqualified) | pacman (done, unqualified) |
| Main installer | dnf; Docker's dnf repository or Podman; Caddy Copr | zypper; distribution Docker or Podman; Caddy package | pacman; extra Docker or Podman; Caddy package |
| Desktop client | RPM (spec added) | RPM (spec added) | PKGBUILD repackaging the same tree, `depends=(webkit2gtk-4.1 gtk3)` |
| Mandatory access control | SELinux enforcing: label `/srv/acc/app/.venv/bin` and `/srv/acc/bin` as `bin_t`, so systemd may execute them, and check rootless Podman/Docker under `container_t` | Tumbleweed: SELinux (as Fedora); Leap 15: AppArmor (existing profiles) | None by default |
| Signatures | Signed RPM plus a repository `.repo` file with a pinned key | Signed RPM; an OBS project is optional | A signed package or AUR; AUR is unsigned by design, so offer a signed release artifact |

**Containers.** The trusted container launcher (CONTAINERS.md) already uses Podman with crun, which is native on
Fedora and openSUSE. Project stacks use rootless Docker today. Fedora could use Podman's Docker-compatible socket
instead of adding Docker CE; that's a product decision for phase 2, not a packaging detail.

**AppImage fallback.** Recipe in phase 2 above: pinned, checksummed tools in a local tools folder, so Tauri never
downloads them. An AppImage is a distribution format, not isolation. It needs FUSE (or
`--appimage-extract-and-run`), and it shouldn't self-update package-manager-owned files.

## Desktop RPM

`desktop/package-rpm.sh [the .deb]` extracts the `.deb` built by `desktop/package.sh` and runs `rpmbuild` with
`desktop/rpm/rynhult-desktop.spec`. The RPM takes the `.deb`'s package name and version and contains the same files
at the same paths, including the resource folder the host binary looks for (`/usr/lib/<product name>/`).

- **Requirements** come from the host binary's ELF libraries (WebKitGTK 4.1, GTK 3), named however each
  distribution names them. The private Python runtime is excluded from that scan, and the package provides nothing
  to the system.
- **Payload:** rpmbuild's post-install scripts are off. Nothing is stripped again, the system Python never
  recompiles the bundled files, and the tool shebangs are left alone.
- **Findings from the container builds** (a real 0.0.0 `.deb`, Fedora 42 and Tumbleweed, RPM 4.20):
  - Requires came out as WebKitGTK 4.1, JavaScriptCore 4.1, GTK 3, libsoup 3, cairo, D-Bus and glibc. Installs
    resolved them with dnf and zypper, with no missing libraries.
  - `/usr/bin/python3`, `/usr/bin/env` and `/bin/sh` are also required, by the bundled agent tools' shebangs. That
    matches the desktop setup, which needs python3 too.
  - The space in Tauri's product name ("Rynhult Linux spike.desktop") split into bogus provides such as `Linux`.
    Fixed in phase 2: Linux packages now use `rynhult-linux-spike`.
  - After the bundled Python first ran as root, `rpm -V` showed three of the runtime's own standard-library `.pyc`
    files rewritten, in the `.deb` too. Fixed in phase 2: the build compiles the whole runtime.
- **Status:** unsigned, a spike like the `.deb`. Debian versions with a revision or epoch (`-`, `:`) are refused,
  since an RPM version can't hold them.

## Qualification before listing a distribution

For each distribution, on a disposable VM, not a container, because systemd, SELinux and user namespaces matter:

1. Main install from scratch, an update, and an uninstall.
2. Node enrollment, sessions as agent accounts, and broker restarts.
3. Rootless containers, both the launcher and project stacks.
4. Caddy in both certificate modes.
5. Desktop package install, first run and agent account setup.
6. SELinux enforcing with no denials, or AppArmor profiles loaded.
7. Package signatures verified with a pinned key.
8. A reboot.

Record the versions, then list the distribution in the Guide.

## Phase 4: Arch first (automated desktop qualification passed)

The main setup and direct installer now accept pacman with the same explicit test-only opt-in as Fedora and
openSUSE: `sudo deploy/setup.sh --unqualified-distro` (or `ACC_UNQUALIFIED_DISTRO=1` for the direct installer).
The node already accepts `deploy/node/install.sh --unqualified-distro --install-prereqs` with its usual enrollment
arguments. Neither role is qualified yet. No AUR helper, added repository, kernel change or security-mode change is
performed. Update the entire disposable test system first (`pacman -Syu`); never refresh with `pacman -Sy` alone.
The bootstrap now installs a missing rsync using the detected manager before copying the checkout.

### Desktop local package

Build the Linux x86-64 `.deb` with `desktop/build-runtime.sh`, the desktop web build and `desktop/package.sh` as
usual. This PKGBUILD repackages that trusted local artifact; it does not build Tauri from source or download an
unsigned binary from a moving URL. The helper prints the recipe and payload SHA-256 hashes, refuses an existing
output directory, checks the Debian name/version/architecture, and retains executable modes and bundled bytecode.
The recipe disables stripping/debug splitting so makepkg cannot rewrite the bundled runtime. It includes the
project license and third-party notices. Review the recipe before building it as an ordinary user:

```sh
desktop/package-arch.sh path/to/rynhult-linux-spike.deb desktop/package/arch
cd desktop/package/arch
cat PKGBUILD
sha256sum PKGBUILD rynhult-desktop.tar.gz
makepkg --cleanbuild
sudo pacman -U ./rynhult-linux-spike-*.pkg.tar.zst
# After closing the desktop:
sudo pacman -R rynhult-linux-spike
```

Dependencies: `acl`, `webkit2gtk-4.1`, `gtk3`, `librsvg` (no AppIndicator: the app has no tray icon). Dependencies are resolved from the
system's configured signed repositories. Package removal removes packaged files, not the user's desktop profile or
separately provisioned execution broker. Use the desktop's documented setup/uninstall path for the latter.

### Rootless container qualification (2026-10-25)

**Passed** on a disposable rootless Arch Linux guest with systemd 262.1 as PID 1.
The PKGBUILD was built with makepkg as unprivileged guest UID2000 and installed
with pacman; `pacman -Qkk` reported **5,862 files, zero altered**, both before and
after the session checks. The bundled main and both fake providers exercised real
owner/agent account switching, socket activation, ACLs, synthetic paste/device
auth, hook ingestion and PTY WebSocket streaming. Both sessions and fake auth
survived an actual owner PAM/user-manager logout/login cycle. Cross-user file,
socket and broker peer-credential access was refused. The broker is a system
service with `User=agent`; the owner's `systemd --user` manager is what logs out
and restarts, not an agent user-unit service.

Uninstall found and fixed a stale runtime socket: systemd defaults to
`RemoveOnStop=no`, so the installed helper now explicitly unlinks only the
stopped owner's endpoint. A fresh guest repeated qualification and uninstall
successfully. Both owners' units/socket/manifests and all packaged files were
removed; accounts stayed locked/expired and work, fake credentials and profiles
were retained as designed. The fixture first stopped sessions through the API,
stopped the owned service units and closed the recorded dedicated tmux server
through its socket. It intercepted the helper's UID-kill primitive **only after
proving the UID had no processes**, so no UID/pattern kill ran. Forced termination
of still-running agents is not qualified by this result.

Repeat instructions and a pinned-base Containerfile are in
[`desktop/tests/arch-phase1.md`](../desktop/tests/arch-phase1.md). The probe refuses
host/rootful/non-systemd execution before provisioning. No host directories or
sockets are mounted; the qualification guest has network none. The image build
uses a full `pacman -Syu`, not a partial upgrade. The base digest pins the base,
not the rolling repository state: retain the exact package inventory as well.

Arch-specific findings: use `systemctl --version` (`systemd` is not an executable
on Arch's normal PATH), and allocate an explicit distinct builder UID before
provisioning the desktop owners. Official repositories supplied the declared GUI
dependencies; the native binary's `ldd` output had no missing libraries. GTK/WebKit
rendering was **not** run. A container shares the host kernel; this does not
qualify the tester's derivative, display/login manager, kernel, cgroup delegation,
LSM, real vendor CLI auth/inference, signatures, or the main/node installers.
No distribution is promoted to supported by this automated desktop result.
The 2026-10-26 review hardened setup destinations, uninstall identity/error
handling and AppImage cache custody, and added bundled license/notices files.
Those changes have unit/fixture coverage; repeat the disposable systemd procedure
with a freshly rebuilt package before treating the historical result as evidence
for that new artifact.
Retain fresh package hashes, version inventories and cleanup evidence when
following the public qualification procedure linked above.

### Release-candidate package check (2026-10-27)

Built on acc-host (Ubuntu 24.04, no containers, no installs) from `main` at `3b8f145d1`.

- **`.deb`:** built after one fix. `build-runtime.sh` deleted `libpython3.12.so.1.0` but kept its
  `libpython3.12.so` link, and Tauri refused to bundle the dangling link. It now deletes the link too, and refuses
  any dangling link in the runtime.
  - Package: `rynhult-linux-spike` 0.0.0, amd64, 74 MB, 6,025 entries. Depends: `acl, libwebkit2gtk-4.1-0,
    libgtk-3-0`. No maintainer scripts.
  - Contents: the binary, the desktop entry (now `Categories=Development;`, previously empty) and the icon under
    `/usr`. Everything else is in `/usr/lib/rynhult-linux-spike`, including `docs/LICENSE` and
    `docs/THIRD_PARTY_NOTICES.md`.
  - Checks: every `.py` has bytecode; no dangling links; nothing unreadable; the binary's 15 libraries all resolve.
- **AppImage:** built with the pinned tools, and its resources are file-for-file the `.deb`'s. It can't run
  execution setup, because its resources aren't root-owned; the Guide now says so.
- **Arch:** `package-arch.sh` rendered the PKGBUILD and payload, with licences in `/usr/share/licenses`. `makepkg`
  isn't on this host.
- **RPM:** not built; `rpmbuild` isn't on this host. The spec and file list were reviewed against the `.deb`.
- **Installers:** the simulated apt/dnf/zypper/pacman dry-run tests pass. On acc-host itself (a live main) the dry runs
  can't read root-only files, so they need a fresh machine.
- **Follow-up fixes (same branch):**
  - Packages take the project version from `pyproject.toml`, and `package.sh` refuses one that isn't
    `major.minor.patch[-prerelease]`. A release candidate gets each format's pre-release form, so it sorts before the
    release: the `.deb` gets `0.15.0~rc.1` (repacked from Tauri's `0.15.0-rc.1`, which Debian would read as revision
    `rc.1`, i.e. newer than `1.0.0`). The RPM keeps the `~`; Arch's pkgver is `0.15.0rc.1`.
  - Identifier `dev.rynhult.desktop`.
  - The `.deb` installs `/usr/share/doc/rynhult-linux-spike/copyright` (machine-readable, MIT, from `LICENSE`).
  - The unused `libayatana-appindicator` dependency is gone from the Arch recipe.
  - `setup.sh --dry-run` on an installed machine without root says it can't read `/etc/default/accd` and to use
    sudo.
- **Gaps:**
  - The maintainer (the `.deb` Maintainer, the RPM changelog, the PKGBUILD comment) is a marked placeholder,
    `MAINTAINER PLACEHOLDER ... <maintainer-placeholder@example.invalid>`: the owner sets it.
  - Nothing is signed.
  - The RPM and Arch packages still need building and qualifying on their distributions.

### Common derivatives

- **EndeavourOS** uses Arch repositories plus its own small repository and keyring; preserve their configured order
  and keys. See [the EndeavourOS mirror documentation](https://github.com/endeavouros-team/mirrors/blob/master/how-to-add-mirror.md).
- **Manjaro** has distinct stable/testing/unstable branches and its own kernel packages. Build and resolve GUI
  dependencies on the tester's branch, rather than copying Arch packages or adding Arch repositories to Manjaro.
  See [Manjaro branches](https://wiki.manjaro.org/index.php?title=Switching_Branches).
- **CachyOS** provides optimized repositories and customized kernels. Record CPU ISA/repository selection and exact
  kernel; do not assume a container validates those kernel features. See [optimized repositories](https://wiki.cachyos.org/features/optimized_repos/)
  and [kernel variants](https://wiki.cachyos.org/features/kernel/).
- Do not assume AppArmor or SELinux is installed, enabled or enforcing on any derivative. Inspect the actual booted
  kernel LSM list, userspace tools and loaded policy. Installing AppArmor alone does not enable its boot-time LSM
  configuration ([Arch AppArmor](https://wiki.archlinux.org/title/AppArmor)). Absence of both means no MAC protection;
  it must not be reported as confined. Qualify cgroup v2/delegation, user namespaces, subordinate IDs, linger, Podman
  socket and broker ACLs on the tester's real system. Do not disable a configured LSM to pass qualification.
