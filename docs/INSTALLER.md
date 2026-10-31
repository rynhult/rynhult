# One-step install (M12)

Approved by the maintainer, 2026-10-22: one installer per system. On Linux it's a terminal wizard (TUI); on Windows and macOS it's
a full installer. Each installs everything ACC needs, then asks: **main ACC** (standalone, or the primary of
several machines) or **an additional machine** for an existing ACC. Installers ship unsigned for now (the source is
open source under the MIT License, see LICENSE).

## What each system can be

| | Main ACC | Additional machine |
|---|---|---|
| Linux (Debian/Ubuntu, systemd) | yes: `deploy/setup.sh` (as today) | yes: `deploy/node/install.sh` |
| Windows 11 | yes, inside WSL2 (Ubuntu 24.04 with systemd) | yes, inside WSL2 (as today) |
| macOS | **not yet**: ACC's main services need systemd and separate agent accounts; a port is a later milestone | yes: LaunchAgents (as today) |

Everything already installed by `setup.sh` and the node installers stays theirs (Caddy, uv, Python packages, tmux,
git, agent CLIs). ACC uses SQLite, so there is no database server to install. The new pieces are front ends, a way to
get the code onto the machine, and making a private certificate automatic.

## The pieces and their contracts

### 1. Linux: `deploy/acc-install` (bash + whiptail)
- Run as root, from a checkout or from the bootstrap below. Installs `whiptail` if it's missing (it ships with
  Debian and Ubuntu).
- First screen: **Main ACC** / **Additional machine** / Cancel. Without a terminal, or with `--yes`, it takes its
  answers from flags and the environment (CI and scripted installs).
- Main: asks `setup.sh`'s questions in dialogs (the same variables, the same defaults), then runs
  `deploy/setup.sh --yes` with them in the environment and shows its log. Re-running changes nothing that's already
  set up, as with setup.sh.
- Additional machine: one input, "Paste the command from Host → Machines → Add machine" (the existing
  `acc-node enroll --primary … (--trust system | --ca-fingerprint HEX) --code … --name …` line). It's parsed, never
  executed as given: `--primary` must be https, `--name` must match the node name pattern, the code and
  fingerprint must have their shapes. Then it runs `deploy/node/install.sh` and `acc-node enroll` with exactly
  those values.
- Flags: `--main`, `--node 'acc-node enroll …'`, `--yes`, `--unqualified-distro` (explicit test-install opt-in for recognised non-apt systems), `--dry-run` (passed through to the installers).
- Output and the last lines of a failed step go to `/var/log/acc-install.log` (root only, 0600, new on every run).
  Tests drive a real run with `ACC_INSTALL_ROOT=<temporary folder>` (refused for real or effective UID 0).
  The folder must be canonical, owned by the unprivileged test user, mode 0700 and under `/tmp`; traversal,
  symlinked ancestors and linked log/config/CLI paths are refused before any installer step. With the variable unset,
  the normal paths and root requirement apply. In root gates, only the child running the wizard drops privileges.
  In test mode: the log, the existing-setup check (`etc/default/accd`) and `acc-node` are looked for there, and the
  installers it runs are stubs (`tests/test_acc_install_root.py`).

### 2. The main ACC serves what a machine needs (accd)
- `GET /api/nodes/bundle`: a `tar.gz` of this ACC's own source at its running version (built at install time by
  `deploy/install.sh` into `data/bundle/acc-<version>.tar.gz`, from the installed tree without `.git`, `.venv`,
  `node_modules`, data or secrets). Public, like a release download, and rate-limited. It is also the source
  download for this exact version, so everyone using it over the network can get the
  source of what runs (section 13); Settings links it next to the repository, and a fork that changes ACC serves
  its own changes the same way.
- `GET /api/nodes/install.sh`: the Linux bootstrap. It downloads the bundle from the same ACC, unpacks it to
  `/opt/acc/src-<version>`, and runs `deploy/acc-install --node "<command>"`. It takes the enroll values as
  arguments, except the code: with `--code -` it stays on stdin, which acc-install reads and pipes on to
  `acc-node enroll --code -`, so it is on no command line (process list, sudo's log).
- The Add machine dialog gets tabs, **Linux / Windows / Mac**, each showing one thing to copy:
  - **Linux**, with system trust: `echo C | sh -c '…; curl … "$1/api/nodes/install.sh" -o "$t/install.sh";
    sudo sh "$t/install.sh" --primary "$1" --trust system --code - --name "$2"' - https://ACC N`. The code is
    piped in (echo is a shell builtin) and stays on stdin through sudo and the bootstrap.
  - **Linux**, with a private CA: a short `sh -c` that first fetches `/api/nodes/ca` without verification, checks
    its SHA-256 against the fingerprint written into the command, and stops if it differs. Only then does it fetch
    the bootstrap with `--cacert`. The fingerprint is the trust anchor, as in `acc-node enroll`. The code is
    piped in the same way.
  - **Windows / Mac**: a link to the installer download and the enroll command to paste into its wizard.
- The enroll handshake already sends the node's version. The Machines card warns when a machine's version differs
  from the main ACC's.

### 3. Windows: `ACC-Setup-<version>.exe` (Inno Setup, built in CI)
- Built on the GitHub Windows runner by `.github/workflows/installers-windows.yml` on tags, by hand and on
  `installer/**` branches, and attached to the GitHub release. It bundles the ACC source (as a release: no `.git`,
  `.venv`, `node_modules` or data). `deploy/windows/acc.iss` only asks; `deploy/windows/install-acc.ps1` does the
  work (also usable alone, with `-DryRun`), sharing `deploy/windows/AccWindows.psm1` with
  `deploy/node/windows/install.ps1`. Answers reach it as environment variables, never on a command line.
- Wizard pages:
  1. Main ACC or Additional machine.
  2. Main: hostname, owner name, agent CLIs (the same questions as setup.sh; Cloudflare is optional).
     Additional machine: "Paste the command from Add machine" (parsed as in 1).
  3. Progress: install or enable WSL (it may ask for one restart, then continue at the next sign-in), Ubuntu 24.04
     with systemd, and **mirrored networking** in `%UserProfile%\.wslconfig` (main role only, so phones and other
     PCs reach it). Then setup.sh or the node install inside WSL.
- Uninstall removes the scheduled task and the ACC files in WSL, but never the WSL distribution or its data, unless
  a box is ticked.

### 4. macOS: `ACC-<version>.pkg` (pkgbuild/productbuild, built in CI)
- Installs the ACC source to `/Library/Application Support/ACC/src`. A post-install step then runs as the signed-in
  user: an AppleScript dialog asks for the command from Add machine (parsed as in 1) and runs
  `deploy/node/macos/install.sh` and the enroll. If cancelled, it can be done later from an "ACC Setup" app it
  leaves in /Applications.
- It says plainly that a Mac is an additional machine only, and that sessions run as the signed-in user (see
  docs/MULTIHOST.md, Known limits).
- Built: `deploy/macos/build-pkg.sh` (payload, applet, pkgbuild/productbuild), `deploy/macos/acc-setup.sh` (the
  parser and every step ACC Setup runs; `--dry-run`), `deploy/macos/check-pkg.sh`; CI
  `.github/workflows/installers-macos.yml`. The parser's examples are in `tests/data/enroll-commands.txt`, for the
  Linux and Windows parsers to share.

### 5. Private certificate, automatic (Caddy's CA)
- When `setup.sh` sets ACC up without a Cloudflare zone (Caddy signs with its own local CA), `install.sh` copies
  Caddy's root certificate (`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`) to
  `data/node-ca.pem` (0644) and sets `ACC_NODE_CA_FILE` in `/etc/default/accd`. Add machine then shows
  `--ca-fingerprint` with nothing configured by hand. The root is valid for 10 years, and a renewal is copied the same
  way on the next install or update.
- **Settings → Certificate** (owners): which kind of certificate ACC uses (public / Caddy's own / a file), its
  fingerprint and expiry, and downloads that make a device trust it:
  - `acc-root.crt` (Windows, Linux, Android);
  - `acc-root.mobileconfig` (iPhone, iPad, Mac: an Apple configuration profile holding only the root);
  - short instructions per system.

## Order and checks
1. Pieces 2 and 5 (server side) and 1 (Linux), then 3 and 4. Each has its own CI job: Linux shellcheck plus a dry run;
   Windows builds the .exe and runs it in silent mode with `/DRYRUN`; macOS builds the .pkg and checks its
   payload and scripts.
2. Guide: one **Install** page replacing the separate instructions, with the three systems and both roles.
3. Live check: a Linux VM on acc-host enrolls through the one-liner. The Mac and Windows PC follow
   `docs/handoffs/2026-10-22-mac-windows-node-test.md`, using the new installers once they're built.


## Privileged installer verification (D1/D3/D5/D6/D20/X4/Q12)
The Linux main installer and setup use a fixed system PATH and share the node installer's root-tool checks for uv; home executables are never used as root. npm work runs as `acc-build` in temporary staging, and root copies build output without following links. Both Caddy modes install the private Unix admin directive and runtime directory drop-in, and validate the adapted JSON before reload.

The installer refuses links and wrong owners in the data tree, creates the vault key as `acc` with noclobber, and installs root-owned source bundles through pinned directory descriptors. ACL walks and agent-home chmod run as the file owner. The AppArmor Chrome tree is created root 0755.

GitHub MCP v2.0.2 is provisioned with committed amd64/arm64 release checksums. NodeSource and Docker apt primary-key fingerprints are pinned. A new Cloudflare Caddy download requires `ACC_CADDY_SHA256` from an independently approved build/release; setup cannot securely infer a checksum for a dynamic build. A preinstalled reviewed root-owned Caddy with the module can be reused. See `deploy/caddy/README.md` and the Guide's Install page for operator steps.
