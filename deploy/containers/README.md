# Session qualification image (local only)

This image is **not qualified**. It targets Linux amd64 and the launcher's
`network=none` contract. No registry publication, signing service, automatic
update, host CLI upgrade or session default change is included.

## Reviewed inputs

- Playwright Python 1.56.0 Noble, amd64 manifest
  `sha256:508b37a524219d210a67a7190beb079efda72553e0dcb9ef21d4dae8f8a7686b`, referenced as
  `playwright/python:v1.56.0-noble-amd64@sha256:…`. The digest is what the build pulls; the release tag beside it
  lets Dependabot propose the next `-noble-amd64` release instead of the stale floating `latest`.
  `scripts/check_pins.py` refuses a base without an amd64 release tag.
  The manifest is the pin for **all inherited system package versions**, Python,
  git, Chromium and its libraries. No apt resolver runs during this build.
  [Upstream recipe](https://github.com/microsoft/playwright-python/blob/v1.56.0/utils/docker/Dockerfile.noble),
  [registry manifest](https://mcr.microsoft.com/v2/playwright/python/manifests/v1.56.0-noble).
- Node 22.21.1 linux-x64 tarball, SHA-256
  `680d3f30b24a7ff24b98db5e96f294c0070f8f9078df658da1bce1b9c9873c88`,
  from the [release checksums](https://nodejs.org/dist/v22.21.1/SHASUMS256.txt).
  The build checks the digest before extracting.
- Claude Code **2.1.120**, Codex **0.164.0**, Copilot CLI **1.0.102**.
  `session/package-lock.json` fixes every resolved npm package version and
  SHA-512 integrity, including native optional packages. `npm ci --ignore-scripts`
  verifies integrity and cannot resolve new versions. Claude's executable points
  directly to its pinned glibc native package, avoiding its install script.
  Missing native packages fail the build's CLI version checks.
- PyYAML **6.0.3**, needed by the imported ACC provider registry, is pinned to
  its CPython 3.12 Linux amd64 wheel SHA-256 in `session/requirements.txt`.
  `pip --no-deps --require-hashes --only-binary=:all:` verifies it without
  resolving dependencies. [Release metadata](https://pypi.org/pypi/PyYAML/6.0.3/json).
  A build-time provider import catches missing Python dependencies.
- ACC's guard, hook, request and statusline scripts, provider adapters, and fixed
  qualification scripts come from the reviewed checkout. The build context
  excludes Git metadata, credentials, dependencies and Python bytecode.

The multi-stage build assembles a selected rootfs into `FROM scratch`: shell/core
tools, git, Python/ACC providers, the three CLIs, Node and Chromium plus their ELF
dependencies. It excludes npm/pip, package databases, build tools, caches, docs,
test trees, extra browsers/locales and unused voice/webview add-ons. License files
are retained. Copilot's native launcher extracts executable dependencies into a
writable cache; instead this image extracts its integrity-checked package during
the build and runs that immutable package with pinned Node. No writable code cache
is needed. This profile does not include voice or desktop webview features.
The final stage has no RUN, because a RUN there deleted `/run/.containerenv` (see
docs/CONTAINERS.md). `check_rootfs.py` checks `/runtime` in the assembled stage, and
the `checked` stage runs the CLIs inside the image as it will run; the final stage
copies one unchanged file from it so the build cannot skip it.

The local rootless build measured **1,579,270,314 bytes (1.47 GiB)** via Podman
`image inspect .Size`. That is the image's uncompressed engine size, not compressed
registry transfer size. It remains a substantial footprint; no numeric production
size target is asserted met. `/opt/acc/rootfs-size.json` records selected file bytes.
`/opt/acc/system-packages.txt` and `python-packages.txt` describe the pinned builder's
package provenance (not a claim that every builder package is in the final rootfs).
`/opt/acc/SHA256SUMS` records selected ACC/CLI bytes. Base layers and npm/Python/Node
artifacts are checksum-verified. Refreshing pins requires review and requalification.

HOME, Codex hooks/trust, Claude managed settings and Copilot machine-policy hooks
are root-owned image files with read-only parents. Claude/Copilot mutable state
uses separate tmpfs config directories; Codex state uses explicit tmpfs links.
Qualification requests disableAllHooks in user settings and requires the real
machine-policy guard to remain effective. The launcher additionally
freezes hash-checked session inputs and mounts them read-only; only event/status
outputs get individual writable binds. This boundary deliberately exposes startup
incompatibilities rather than making hooks writable to obtain a pass.

## Build and review locally

The owner selected security review 8 SR8-3 option A: **the dedicated system account
`rynhult-build`, never an agent/session account**. The image is the trust root for every account's containers, so it is built by an account that runs no sessions (security review 8, SR8-3). Right before a build, confirm the account is quiet: `ps -u rynhult-build` shows nothing but the build. Keep the existing quiet-account
census as defense in depth. Run these commands from the owner's retained administrative
console, outside an ACC session's inherited `NoNewPrivileges`. No account is created
by an installer or a session, and no image is built as root.

### One-time account setup

Use a reviewed, root-owned checkout whose ancestors are not writable by agents or
other users. The builder needs read/traverse access to that checkout; never add it
to an ACC/agent group to obtain access. The example path is `/srv/acc/reviewed/phase1`.
Prerequisites installed separately by the owner: shadow-utils (`useradd`, `usermod`,
`newuidmap`, `newgidmap`), systemd (`loginctl`, `systemctl`), `crontab`, Python 3,
Podman/rootless overlay prerequisites and `/usr/sbin/nologin`. `/etc/subuid` and
`/etc/subgid` must exist and be readable to the root helper. Do not install or
upgrade host tools from an agent session to satisfy this runbook.

```sh
sudo /srv/acc/reviewed/phase1/deploy/containers/setup-build-account --dry-run
sudo /srv/acc/reviewed/phase1/deploy/containers/setup-build-account
# A repeat validates the account and makes no changes if it still matches:
sudo /srv/acc/reviewed/phase1/deploy/containers/setup-build-account --dry-run
sudo id rynhult-build
sudo stat -c '%U %G %a %n' /var/lib/rynhult-build
sudo grep '^rynhult-build:' /etc/subuid /etc/subgid
sudo test ! -e /var/lib/systemd/linger/rynhult-build
sudo systemctl is-active "user@$(id -u rynhult-build).service"
```

Expected: a system UID and a private `rynhult-build` primary group, no supplementary
groups; home `rynhult-build rynhult-build 700`; the same printed, non-overlapping
subordinate UID/GID range (at least 65,536 IDs) in both files; no linger entry;
`inactive`/`unknown` for the user manager (systemctl's expected nonzero exit).
The helper allocates the first free range starting at 100000 across **both** files,
locks password login and sets the nologin shell. It creates no broker, session,
service or user unit. It verifies no crontab (even an empty installed one), user
timers, queued `at` jobs where atq is installed, or active user manager. A matching
account with lingering on has lingering disabled; an active manager must first
be reviewed/stopped by the owner. No timers/jobs are silently deleted. Different
identity/home/group/password properties or partial subordinate mappings **refuse**;
never adopt an existing agent account. Home ancestors must be root-owned, protected
and ACL-free; an existing home must be a real 0700 directory without ACLs. No user
startup files are sourced.

Dry-run performs read-only inspection and prints every proposed mutating command,
including the selected range. It creates no files. Apply stops on the first error
and post-verifies the account. Interrupted setup has no automatic rollback: retain
the output, inspect the exact account/home/range entries, and have the owner remove
only the partial resources from that attempt before retrying. Do not run concurrent
account/range administration while provisioning. Do not enable lingering, run sessions,
add credentials or configure timers/cron for this account afterwards.

### Build and save without a user manager

```sh
sudo -u rynhult-build -- /usr/bin/env -i PATH=/usr/bin:/bin \
  HOME=/var/lib/rynhult-build LANG=C.UTF-8 \
  /srv/acc/reviewed/phase1/deploy/containers/build-image \
  /var/lib/rynhult-build/build-20261026-1
```

Choose a new output directory for every attempt. The helper builds **and saves**
`acc-session.oci.tar`, its checksum, `image.txt` and `review/`. It pulls only the pinned
public inputs and never pushes. Use `sudo -u` (or `runuser -u`), not `sudo -iu`: no
login shell, PAM login, user manager or lingering is required. The helper creates a
0700 `XDG_RUNTIME_DIR` inside its private throwaway store and passes
`--cgroup-manager=cgroupfs` on **every** Podman invocation, including cleanup.
It does not use `/run/user/UID`, the user bus, or systemd cgroup delegation.
Real Podman/cgroupfs operation still requires owner qualification on the target
kernel/filesystem; a fake-tool test does not establish that it works there.

### Transfer the reviewed archive into root custody

Keep the output private to the builder. From the owner console read/review
`image.txt`, `acc-session.oci.tar.sha256` and the inventories, record the full image
ID and archive SHA-256 independently, and review the inputs below. Do not source
a builder-owned file or derive expected pins again from the copied archive.
With the helper's protected `/var/lib` ancestor check satisfied, create a **new**
root-only review directory (mkdir must refuse an existing name):

```sh
sudo mkdir -m 0700 /var/lib/rynhult-image-review-20261026-1
# Never follows a link: each folder below the builder's home, and the archive itself, must be real; the archive a regular
# file owned by rynhult-build. The copy is new, root's, mode 0600.
sudo /usr/bin/python3 -I /srv/acc/reviewed/phase1/deploy/containers/take_archive.py \
  /var/lib/rynhult-build/build-20261026-1/acc-session.oci.tar \
  /var/lib/rynhult-image-review-20261026-1/reviewed.oci.tar
sudo /usr/bin/python3 -I /srv/acc/reviewed/phase1/deploy/containers/verify_image_archive.py \
  /var/lib/rynhult-image-review-20261026-1/reviewed.oci.tar \
  REPLACE_WITH_REVIEWED_64_HEX_ARCHIVE_SHA256 \
  sha256:REPLACE_WITH_REVIEWED_64_HEX_CONFIG_DIGEST
sudo stat -c '%U %G %a %n' /var/lib/rynhult-image-review-20261026-1/reviewed.oci.tar
```

Custody writes through the opened, ownership-checked destination directory; swapping its pathname afterwards
cannot redirect the copy. Short writes are completed, and a write that makes no progress refuses. Always verify
the copied bytes against independent pins, including after an interrupted or failed copy.

Expected: archive verification succeeds and the copy is `root root 600`. On any
failure stop; keep the directory protected for review, do not import or publish.
Continue with [protected installation](ROOT-IMAGE-STORE.md), setting its
`REVIEWED_ARCHIVE` to this verified root-held copy. That procedure makes another
protected copy, re-verifies the pinned bytes and layers, imports as root, and
publishes a separate root-owned execution tree. Never chown the builder's tree or
Podman store to root and treat it as reviewed. Retain the builder output and
root-held archive for audit/rollback; this setup helper does not remove accounts,
archives or subordinate entries on uninstall.

**The account must be quiet** (security review 8, SR8-3). Any other process of the building account could change the
image's layers, the build context or the saved archive while it is built. `build-image` runs
`deploy/containers/build_census.py`, which refuses:

- **when it starts**, before it creates anything, if the account has lingering enabled
  (`/var/lib/systemd/linger/ACCOUNT`), timer units in `~/.config/systemd/user` or `~/.local/share/systemd/user`, a
  crontab (`crontab -l`) or queued `at` jobs (`atq`). These checks are best effort: one whose tool is missing is
  skipped. A user manager that is already running is refused by the census below.
- **when it starts, right before `podman save`, and right before it writes the checksum the owner pins**, if the
  account has any process outside the build's own process tree. That includes processes running as one of its
  subordinate UIDs (`/etc/subuid`), which run inside a user namespace it made. The one exception is Podman's pause
  process, recognised by its executable (`/proc/PID/exe` is the system's `catatonit`) and its exact `catatonit -P`
  command line.

Each refusal names every foreign process: PID, a fixed label (login session, system service, user manager, user
manager unit, Podman pause scope, session bus, other, or "starts the build") and its escaped cgroup. A refusal after
`podman save` removes the archive, `image.txt` and any checksum, so nothing is left to pin. The dedicated build account must never have agent sessions or a broker; if these are found, stop and investigate the account's use before rebuilding. Have the owner review/remove any unattended starts.

**Limits:**
- A process that starts and ends entirely between two checks isn't seen. The settings check removes the usual ways
  one starts unattended; a dedicated build account with no sessions, broker or login removes the rest.
- `ACC_BUILD_CENSUS`, like `ACC_BUILD_PODMAN`, is the invoker's choice, for tests. `env -i` above passes neither.

The helper builds in a **private throwaway store** (`mktemp -d /tmp/acc-ib.XXXXXXXX`, passed to every Podman call as
`--root`/`--runroot`/`--tmpdir`, with `CONTAINERS_CONF=/dev/null` and `deploy/containers/build-storage.conf`). The
blob-info cache (`XDG_DATA_HOME`), image-copy scratch files (`TMPDIR`) and private runtime (`XDG_RUNTIME_DIR`) go there too. The helper removes the store on exit, success or failure, and says whether
the build succeeded. If removal fails, it prints the leftover path and the exact command to remove it; the build's
result and exit status are unchanged. It never uses the account's own store: a layer left there with the ID of a
session image layer is found before the protected copy, and the launcher refuses every launch. That happened in
qualification attempt 11, through the `checked` build stage, whose first layer is the final image's first layer.

`build-storage.conf` used by hand, without the helper, only moves the graphroot (to `~/.local/share/acc-image-build`).
The runroot, libpod's state and the blob-info cache stay the account's defaults. That leaves no image layer where the
launcher looks, but it is not isolated: build with the helper.

The folder then holds `acc-session.oci.tar`, its `acc-session.oci.tar.sha256`, `image.txt` (`ID architecture OS`) and
`review/` with `/opt/acc/system-packages.txt`, `python-packages.txt` and `rootfs-size.json`. The review inventory comes
from a created, never started, container. Record the image ID and archive checksum. Review the npm lock, native package
licenses and the rest from the archive's layer tar, never by executing image code.

If an older build used the account's own store, check it before qualifying, as the agent account:
`python3 -I scripts/container_qualification/image_store.py --check-store sha256:FULL_REVIEWED_IMAGE_ID`. It prints the
exact commands that remove only the images and containers holding a layer of the pinned image, using
`deploy/containers/account-store.conf` (the account's own store, no additional store) and the run root and tmp dir
Podman recorded for that store. If it can't read them, or they aren't plain paths in the account's runtime directory or
home, it names what to remove and prints no command.
Review installed versions, licenses, native binaries and the entire Containerfile;
compare `git diff`/commit and lock integrity with upstream releases. Retain the
local archive for review/rollback. Do not put tokens or personal homes into builds.

The rootfs assembly strips ACLs, group/other write and set-id bits from every
entry except the `/tmp` and `/var/tmp` mount points (1777) and refuses to build if any remain: the pinned base
ships Chromium 0777, and a build folder with default ACLs would otherwise turn
new files' group bits into rwx (security review CT-02). Inspect with
`podman image mount` or the archive's layer tar, never by executing image code.

Buildkit/Podman versions, compression and filesystem timestamps may affect OCI
bytes. The same pinned inputs are reproducible; byte-identical archives across
engines are not asserted. The **resulting local image ID**, never its build tag,
goes into the separate protected qualification policy. Continue with
[the qualification runbook](../../scripts/container_qualification/README.md#phase-1-through-the-trusted-launcher).

A real local rootless build and two-generation user-manager run succeeded in
starting this image; qualification is **NO-GO**. All three provider hooks and Codex trust passed, including hostile user settings.
Owner-selected Option A now uses the container itself as sandbox under an immutable
launcher contract: Codex danger-full-access, Chrome --no-sandbox, no nested bubblewrap
requirement. Native defaults remain unchanged. All eleven local image/runtime checks
pass, including refused outside-mount writes; trusted-launcher authority is still skipped.
Proc masks, seccomp, dropped capabilities, network none and immutable inputs are unchanged. The registered
broker/root launcher was not exercised. See the session handoff for exact evidence.
Build/pull restrictions are never grounds for replacing pins with tags.

## Protected session image installation (CT-05)

Builds remain unprivileged and separate from installation. Session launches use
the root-owned read-only `additionalimagestores` profile, never an account-local
image. Follow [the exact owner commands](ROOT-IMAGE-STORE.md) to copy and verify
the checksum-pinned OCI archive, verify all layer digests, import as root, pin the
expected image ID in root policy, publish an independent pinned execution tree,
and capture the host write/unshare proof. The launcher stages the held tree
read-only and executes with `--rootfs` so a same-ID image imported after inspection
cannot substitute for it. Old installations without the tree fail closed.
No production qualification claim is made until the owner runs those checks.

## Planned public distribution

The intended registry identity is `joshuafulton/rynhult`. No image is published by
this tree or by publication dry runs. Keep qualification images local and pinned
by reviewed digest; public distribution remains held until fully featured v1.0
and separate owner approval.


Phase-2 container teams require rebuilding this recipe with `acc-mcp-team` and
its immutable `acc_env.py` reader retained by `slim_rootfs.py`. Claude's managed
settings explicitly disable its inner sandbox; the entrypoint requires the
verified outer-container contract before starting any payload. Do not reuse an
old image/rootfs pin. The additional owner qualification checklist is in
[CONTAINERS.md](../../docs/CONTAINERS.md#additional-owner-root-qualification-gate).
