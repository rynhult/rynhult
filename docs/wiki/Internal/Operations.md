# Operations

## What runs where
| Piece | Runs as | Unit |
|---|---|---|
| accd (API, supervisor, dashboard) | `acc` | `accd.service`, listening on `accd.socket` (`/run/accd.sock`, Caddy only) |
| broker, one per agent account (tmux, terminals) | that account | `acc-broker@<account>.service` from `acc-broker@<account>.socket` (`/run/acc-broker/<account>.sock`, accd only) |
| services broker (Docker stacks) | the projects' owner | `acc-broker@<owner>.service`, `ACC_BROKER_ROLE=services` |
| publishing helper | root | `acc-publish.path` → `acc-publish.service` |
| account helper (new agent accounts from People) | root | `acc-accounts.path` → `acc-accounts.service` |
| container launcher (projects switched to containers) | root; containers run as the agent account | `acc-containers.socket` (`/run/acc-containers.sock`) → `acc-containers@.service` per request; `acc-containers-reconcile.timer` (every minute); sessions run as `acc-ct-<session>-g<n>.service` in `acc-containers-<uid>.slice` |
| packages helper (agent CLIs, Ollama) | root | `acc-packages.path` → `acc-packages.service`; `acc-packages-daily.timer` (05:15). Linux nodes run it too (files group `acc-node`, a drop-in sets `ACC_PACKAGES_GROUP`); acc-node writes its requests |
| host snapshot | root | `acc-hoststate.timer` (every minute) |
| backups | root (mirrors other users' files as them) | `acc-backup.timer` (daily) |
| root's admin session (optional) | root | `acc-root-session.service`, spool in `/srv/acc/run-root` |
| reverse proxy | `caddy` | `caddy.service` |

## Publishing validation failures

If Publish reports that Caddy refused a route, an administrator can inspect
`journalctl -u acc-publish.service` (or its `rynhult-publish.service` alias).
Validation failures log a single-line reason capped at 2,048 characters, with known
credential values and token-like strings redacted; the API retains its generic refusal.
Treat the journal as private operational data even after redaction.

Caddy validation provisions log writers. The publishing helper uses a private
throwaway `/var/lib/caddy` mount for them and keeps its validation state in
`/var/lib/acc-publish`; it cannot modify the running Caddy service's real logs,
storage or certificates. The main installer replaces the unit and reloads systemd
on upgrade. A separately deployed helper needs the updated unit and a systemd
configuration reload before its next run.

## Install
User steps for every system and both roles: [Install](/User-Guide/Install) (design: `docs/INSTALLER.md`). On Debian or Ubuntu (systemd, Python 3.12+), from a clone of the repository:
```
sudo deploy/acc-install         # wizard (whiptail): Main Rynhult or Additional machine
sudo deploy/acc-install --main --yes        # no questions: setup.sh's ACC_* variables from the environment
sudo deploy/setup.sh            # what --main runs; asks in plain prompts; --yes, --dry-run (--help)
```
`acc-install --node '<the enrollment line from Add machine>'` parses that line strictly (`parse_enroll_command`: `rynhult-node enroll` or legacy `acc-node enroll`, an `https://host[:port]` primary, one trust mode, code, name and fingerprint of their shapes, none of the shell's special characters) and never runs it as given: it runs `deploy/node/install.sh --install-prereqs`, then the node enrollment command with the parsed values. `--dry-run` passes through to both installers; output goes to `/var/log/acc-install.log` (0600, replaced on each run, never written through a link). A failed step stops with its exit status, the log's last lines (secrets redacted) and the log's path.
1. `setup.sh` installs the dependencies (git, tmux, Python, uv, Node.js 22+, gh, a current Caddy with the Cloudflare DNS module, Claude Code from Anthropic's signed apt repository (added by `deploy/lib/claude-apt-repo.sh`, shared with the node installer and `acc-packages`, which checks the key's pinned fingerprint first), rootless Docker for the owner), copies the checkout to `/srv/acc/app` and writes `/etc/default/accd` (see [Configuration](/Internal/Configuration)).
2. It then runs `deploy/install.sh`, which creates the `acc` user and `/srv/acc`, the agent account (`<owner>-agent`, no sudo, adm, lxd or docker, via `acc-agent-add`), the units and helpers, and renders the Caddy site: a wildcard for `ACC_WILDCARD_ZONE` with a Cloudflare token, otherwise ACC alone on Caddy's own certificates. In that second case it copies Caddy's root (`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`) to `/srv/acc/data/node-ca.pem` on every run and adds `ACC_NODE_CA_FILE` to `/etc/default/accd` if it isn't set (then restarts accd), so Add machine shows `--ca-fingerprint` and **Settings → Certificate** offers the root to devices.
3. It also builds the source bundle `/srv/acc/data/bundle/acc-<version>.tar.gz` (`python3 -I src/acc/bundle.py`: the tree without `.git`, `.venv`, `node_modules`, build output, data, secrets or caches; regular files only, root-owned) in a folder of root's and installs it there (root:acc 0640), removing other versions.
4. Open ACC with the one-time setup token, sign the agent account in from **Providers**, and optionally add a GitHub token to `/srv/acc/data/secrets`.

Nothing is specific to one machine: disks, GPU and network are detected at run time. The firewall is left alone (allow 80/443; keep 8000–8199 to the LAN).

## Nodes (other machines)
A node runs brokers and `acc-node`, never accd or Caddy (design: `docs/MULTIHOST.md`; user steps: [Install](/User-Guide/Install#additional-machines) and [Nodes](/User-Guide/Nodes)). Every installer is idempotent and has a dry run.

The main ACC serves what a new Linux machine needs, both public and sharing one limit of 30 requests per 10 minutes per client address:
- `GET /api/nodes/bundle`: the source bundle above (also the source download for this version; `/api/auth/state` reports it as `source_download`).
- `GET /api/nodes/install.sh`: the bootstrap (`src/acc/server/node_bootstrap.sh` with the version filled in). Its arguments are the enroll values. With `--ca-fingerprint` it fetches `/api/nodes/ca` unverified, keeps only the first certificate and compares its SHA-256; then it downloads the bundle (verified against that CA, or the system CAs), checks it is a gzip tar whose members are regular files and folders under `acc-<version>/`, unpacks it to `/opt/acc/src-<version>` (root's, no group or other write) and runs `deploy/acc-install --yes --node "acc-node enroll …"`. `--dry-run` unpacks into a temporary folder and runs the wizard's dry run.

Add machine's Linux line pipes the bootstrap to `sudo sh` with system trust. With a private CA it is an `sh -c` that does the CA check above as the signed-in user before fetching the bootstrap with `--cacert`.

| System | Installer | What it sets up |
|---|---|---|
| Linux (Debian/Ubuntu) | `sudo deploy/node/install.sh [--agent NAME] [--roots A:B] [--install-prereqs] [--dry-run]` | ACC in `/opt/acc` (uv); user `acc-node`; the agent account via `acc-agent-add` with `ACC_AGENT_CLIENT=acc-node ACC_AGENT_PREFIX=/opt/acc`; `acc-broker@` units with node drop-ins (socket group `acc-node`, `ExecStart` from `/opt/acc`); `acc-node.service` (starts once `/etc/acc-node.env` exists); `/usr/local/bin/acc-node` |
| Windows 11 | `deploy\node\windows\install.ps1 [-Agent NAME] [-Distro Ubuntu-24.04] [-WhatIf]` | WSL2 Ubuntu 24.04, `systemd=true` in `/etc/wsl.conf`, the Linux installer inside WSL (`wsl-node.sh` copies the checkout to `/root/acc-src`), Scheduled Task **Rynhult node (WSL)** at logon (`wsl --exec sleep infinity`) |
| Windows 11 (installer) | `Rynhult-Setup-<version>.exe` (`deploy/windows/acc.iss`, Inno Setup), which runs `deploy\windows\install-acc.ps1 -Role Main\|Node [-DryRun] [-Uninstall [-RemoveDistro]]` | Both roles: what the row above sets up (shared code in `deploy/windows/AccWindows.psm1`). Main: `networkingMode=mirrored` in `%UserProfile%\.wslconfig`, Hyper-V firewall rule `ACC-WSL-Web` (TCP 80/443), the owner's Linux account (`wsl-node.sh owner`), ACC copied to `/srv/acc/app` and `deploy/setup.sh --yes` with answers through `WSLENV` (`wsl-node.sh main`), task **ACC (WSL)**. Node: enrollment with strictly parsed values, task **Rynhult node (WSL)** (an update removes the older **ACC node (WSL)**). Restart answers live in administrator-protected `%ProgramFiles%\ACC\private\resume.json` (Cloudflare token DPAPI-encrypted); HKCU RunOnce **ACC Setup** resumes after sign-in and requests elevation, exit 3010. Logs: `%ProgramFiles%\ACC\logs`. Legacy profile resume files are not used. CI: `.github/workflows/installers-windows.yml` |
| macOS | `deploy/node/macos/install.sh [--roots A:B] [--dry-run] [--render-dir DIR]` | Homebrew python/tmux/git/uv; ACC in `~/Library/Application Support/ACC`; LaunchAgents `local.acc.broker` and `local.acc.node` (from `acc.plist.in`) as the signed-in user; logs in `~/Library/Logs/ACC` |

`sudo acc-node enroll …` (Linux) enrolls as root into a private staging folder under `/run`, has the `acc-node` user store the key and CA in `/var/lib/acc-node` (`acc-node adopt`, so root never works on paths `acc-node` controls), writes `/etc/acc-node.env` and starts the service. The Linux installer runs only a root-owned uv outside home folders and makes `/opt/acc/app` root's with no group/other write bits before running anything from it. On a Mac the wrapper sets `ACC_NODE_CONFIG`, `ACC_NODE_STATE_DIR` and `ACC_NODE_SOCKET_DIR` to the folders above, and the node's LaunchAgent starts when the config file appears. CI: `.github/workflows/node-packages.yml` (branches `node/*`, or run by hand).

Windows/Mac release installers are built only for v1.0.0 and later tags.
Pre-v1 **Add machine** tabs explain their absence. Ordinary older Linux/WSL nodes
instead use the copyable [in-place recovery commands](/User-Guide/Nodes#update-an-older-machine)
on their **Host → Machines** row: verified main source, root `--update`, no new
enrollment or secrets in the command. PowerShell uses an inner-double-quote-free
WSL command; an in-Ubuntu root form is also offered. A Mac without an installer
needs the next installer release.

Dedicated WSL nodes may explicitly opt into interop/drive restrictions; normal
updates never change them. See [isolation, restart and undo](/User-Guide/Install-Windows#isolation-for-dedicated-wsl-nodes).
Backup receivers retain their stricter [independent-update boundary](#enrolled-linux-backup-receiver).

## Update
```
sudo git -C /srv/acc/app pull && sudo /srv/acc/app/deploy/install.sh
```
Versions follow semantic versioning (`pyproject.toml`, shown in **Settings**, tagged `vX.Y.Z`); see `CHANGELOG.md`.

### Web assets across updates

`deploy/install.sh` delegates its web build to `deploy/lib/build-assets.sh`.
The unprivileged build still starts clean. Root's `copy-web.py
--keep-previous-assets` stages and validates that output before replacing the
served static directory. It publishes only the new HTML, manifest and service
worker, plus the current **and immediately previous** build's `assets/` files.
This lets an already open tab finish loading old hashed JS, CSS and fonts after
one deployment. Shared names use the new build's file; a second deployment
discards assets used only by the older generation.

The root-written `.vite/asset-files.json` records only the new build's own asset
names, not the carried files. The first upgrade without an inventory uses the
existing asset set once. Inventory traversal paths, symlinks and non-regular
artifacts are refused; validation failure leaves the served build intact.
The MCP copy mode does not retain an older generation.

The root updater's source bundles omit generated files. Before swapping source
trees, it copies the installed static tree into the incoming tree, keeping the
old UI available during installation. The installer then performs the same
one-generation rollover. An update launched by an **older root helper** cannot
retroactively get this pre-swap step on its first upgrade; missing chunks still
get the [explicit reload banner](/User-Guide/Troubleshooting#a-page-is-loading-or-couldnt-be-shown).
Running `npm run build` directly remains a clean developer build, not this
retaining deployment path. Retention is not a guarantee that old UI code works
with arbitrarily changed APIs.

## Deploy a change
```
sudo bash deploy/lib/build-assets.sh "$PWD" web       # npm runs as acc-build; root installs artifacts
systemctl restart 'acc-broker@*.service'            # every account's broker; running sessions survive
systemctl restart accd                               # migrations run on start
```

Setup and updates share the root-tool trust check; a home-installed uv is ignored. Missing system uv is installed from the pinned checksummed release. The non-login `acc-build` account builds web assets and stages MCP npm packages in disposable folders; dependency code never runs as root.

If installation reports an unsafe data path, stop and inspect ownership and symlinks offline. Do not blindly chown a reported path: it may point outside ACC. The data tree and secrets must belong to `acc`; `data/bundle` and its files belong to root. Existing keys are retained. The installer does not repair suspicious paths automatically.

For Cloudflare HTTPS, preinstall a reviewed root-owned Caddy with the DNS module or pass `ACC_CADDY_SHA256` with the approved architecture-specific download hash. A failed checksum leaves the installed binary untouched. Both HTTPS modes install the Unix admin socket and runtime directory; malformed configuration or a missing/TCP admin listener fails before reload.

## MCP servers
The GitHub MCP wrapper's executable is installed by `deploy/lib/install-github-mcp.sh` (pinned v2.0.2, committed SHA-256 sums for amd64/arm64). Updates fail if the download cannot be verified.

The npm-based MCP servers are staged as `acc-build` with `npm ci --ignore-scripts` and installed once from `deploy/mcp/package-lock.json` into `/srv/acc/tools/mcp` (root-owned). To update one, change its version in `deploy/mcp/package.json`, regenerate the lock (`npm install --package-lock-only --ignore-scripts`), scan it with osv-scanner, then rerun the MCP step of `deploy/install.sh`.

## Back up and restore
**Implemented:** encrypted local/mounted-directory repositories and enrolled Linux append-only receivers,
signed capture manifests, owner-held recovery kits and isolated offline restore with startup hold.
See [Backups](/User-Guide/Backups). S3 is available with conditional immutable-retention checks; other receiver platforms follow later. The existing daily backup and updater rollback procedure below
remain separate; their optional secrets-only CBC encryption is not whole-backup authenticated encryption.

Backups run daily: the database (an online, integrity-checked copy), secrets and dashboard catalog additions go to a different drive, and 14 days are kept.

**A backup is as sensitive as the machine.** Each day's folder holds `acc.db` next to `secrets/`: `vault.key` (which decrypts every key in the vault), the GitHub, Cloudflare and API tokens and the push key. Anyone who can read the backup drive can read all of them. Keep the drive as private as the machine, or encrypt the secrets:

- **Encrypt the secrets with a passphrase:** put a long passphrase in a file only root can read, on a different drive from the backups, and name it in `/etc/default/acc-backup`:
  ```
  sudo install -m 0600 -o root -g root /dev/null /etc/acc-backup.passphrase
  sudo sh -c 'openssl rand -base64 32 > /etc/acc-backup.passphrase'
  echo 'ACC_BACKUP_PASSPHRASE_FILE=/etc/acc-backup.passphrase' | sudo tee -a /etc/default/acc-backup
  ```
  From the next run, each day has `secrets.tar.enc` instead of `secrets/`; the plain files never reach the backup drive. Keep a copy of the passphrase elsewhere too (a password manager): without it the backup's secrets can't be restored.
- What this doesn't cover: `acc.db` itself still holds model endpoints' API keys and sign-in (TOTP) secrets in plain text, and the git backups hold the projects' code.

To restore:
1. `systemctl stop accd` (sessions keep running).
2. Move the current `acc.db` aside, then copy the backup in as `acc`, mode 600.
3. If `/srv/acc/data/secrets/vault.key` is missing or was replaced, copy it back from the **same** backup (`secrets/vault.key`, owner `acc`, mode 600). The key vault's values can only be decrypted with the key they were stored with. If that day has `secrets.tar.enc`, unpack it first, as root:
   ```
   mkdir -m 700 /root/restore && cd /root/restore
   openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 -pass file:/etc/acc-backup.passphrase \
     -in /var/backups/acc/<day>/secrets.tar.enc | tar -x
   ```
   then copy what you need from `secrets/` and delete `/root/restore`.
4. `systemctl start accd`.

The same backup holds `git/`: every project's git backup (bare repositories from `/srv/acc/git`), with files unchanged since the day before hard-linked to that day's copy.

### Git backups
- **Where:** `/srv/acc/git/<project id>.git` on the main ACC (acc 0700). Pushed by each project's machine every 15 minutes when there are new commits, and after a session stops. Design: `docs/MULTIHOST.md` ("Git backup").
- **Address:** `ACC_BACKUP_URL`, else the first `https://` origin. Every machine (this one too) must resolve it and trust its certificate.
- **Restore a project from it:** download the bundle from the project's **Backup** card, then `git clone <project>.bundle <folder>`. As root on the main ACC: `sudo -u acc git clone /srv/acc/git/<id>.git /tmp/restore` (the project id is in the project page's address).
- **Restore after losing the machine:** copy `git/` from the daily backup back to `/srv/acc/git` (owner `acc`, mode 0700 for the folder). Each repository records the project it belongs to (slug and creation time). If you restore an older database next to newer git backups, a repository whose id now names a different project is moved to `/srv/acc/git/orphaned/<id>-<slug>-<time>.git` on that project's next backup instead of being shown or pushed to; clone from there to recover it.
- **A branch shows up as `backup/<date-time>/<name>`:** it was rewritten upstream; the old history stays under the original name. Nothing to do unless you want to clean up (as acc: `git --git-dir /srv/acc/git/<id>.git update-ref -d refs/heads/backup/…`; pushes can't delete).

## Runbook
| Situation | Do |
|---|---|
| Dashboard down | `systemctl status accd accd.socket`, then `journalctl -u accd`. Check it directly as root: `curl --unix-socket /run/accd.sock http://acc/api/health` |
| *the vault's key … is missing* (Settings → Keys, or a launch fails) | `vault.key` was lost while keys exist. Restore it from the backup (step 3 above). Without it the values are gone: delete the keys and add them again |
| Sessions unavailable (broker unreachable) | `systemctl status acc-broker@<account>.socket acc-broker@<account>.service` |
| Limit an agent account | `rynhult-agent-add <account> --cpu 400% --memory 16G --tasks 2048` (a drop-in on its broker unit, so it covers everything its sessions run; `--no-limits` removes it). **Host → Agent accounts** shows use against the limits |
| Add an agent account | `rynhult-agent-add <account>`, then share folders it should work in: `rynhult-agent-share <account> <owner> <folder>`, and log its CLIs in from **Providers** |
| A session won't resume | Its **Activity** tab shows why. Check the CLI login on Providers, then **Resume** |
| Publishing failed | The request's detail shows Caddy's or the DNS provider's error. DNS is rolled back automatically |
| Disk filling up | **Host** shows which mount. Projects' containers and images are the usual cause |
| After a reboot | Nothing to do: units start, ACC resumes running sessions and brings autostart projects up |
| Codex stops for approval on every command, or *bwrap: loopback: Failed RTM_NEWADDR* | See [Codex sandbox and bubblewrap](#codex-sandbox-and-bubblewrap) |

## Codex sandbox and bubblewrap
Codex runs every command inside bubblewrap (`bwrap`), which needs unprivileged user namespaces. Ubuntu 24.04 restricts
them (`kernel.apparmor_restrict_unprivileged_userns=1`). Without an AppArmor profile that allows them for the `bwrap`
Codex runs, every sandboxed command fails with *bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted*. Codex
then fails closed: in workspace-write or on-request mode it stops for approval on everything, and never runs a command
unsandboxed by itself.

**Why there are two profiles.** Codex runs one of two copies of bubblewrap:

| Profile | Covers | When Codex uses it |
|---|---|---|
| `acc-codex-bwrap` | Codex's bundled copy under `/usr/lib/node_modules/@openai/codex/…/codex-resources/bwrap` | The distribution's `bubblewrap` package isn't installed |
| `acc-system-bwrap` | The distribution's `/usr/bin/bwrap` | Codex 0.163 and later prefer it once the package is installed |

The package often arrives as a dependency, for example of `libwebkit2gtk-4.1-dev` (the desktop build dependencies) or
Flatpak. That's why `deploy/install.sh` installs `acc-system-bwrap` on restricted hosts even before `/usr/bin/bwrap`
exists. It leaves an installed profile unchanged when it says the same thing (comments and whitespace aside), and
loads both.

**The trade-off.**
- `acc-codex-bwrap` covers a binary only Codex uses.
- `/usr/bin/bwrap` is a general-purpose tool: with `acc-system-bwrap`, **any local account** can create user
  namespaces through it. That's how Ubuntu before 24.04, Fedora and Arch behave by default. It widens the kernel
  attack surface that user namespaces expose (namespaced capabilities, more kernel code reachable), but it gives no
  account new privileges outside its namespace.
- Everything else stays restricted.
- If that isn't acceptable on a host, remove the `bubblewrap` package (Codex goes back to its bundled copy) or remove
  `/etc/apparmor.d/acc-system-bwrap` and unload it (`sudo apparmor_parser -R /etc/apparmor.d/acc-system-bwrap`).

**How to check.**
- `deploy/install.sh` runs `codex sandbox -- /bin/true` as the agent account (30-second timeout) when Codex is
  installed. If that fails, it prints a warning naming the bubblewrap Codex used and its profile. The install doesn't
  fail.
- By hand, as the agent account: `codex sandbox -- /bin/echo ok` should print `ok`. On Codex 0.163 the form is
  `codex sandbox -- <command>`; `codex sandbox linux` isn't valid.
- `sudo aa-status | grep bwrap` lists the loaded profiles, and `sysctl kernel.apparmor_restrict_unprivileged_userns`
  shows whether the restriction is on.
- To see which `bwrap` Codex runs: `strace -f -e trace=execve codex sandbox -- /bin/true 2>&1 | grep bwrap`.

## Built-in catalog restore

Owners use **Catalog → Restore built-ins**. Installed `catalog/` stays read-only. Precedence is installed
built-ins, `catalog-local/restored/<generation>/` items, then custom items in `catalog-local/local.json`;
unchanged profile overlays apply last. `restored/active.json` switches generations atomically. Metadata
is ACC-only (0600); skill files and the pointer are readable by agent brokers. Never put secrets in skill
files. Existing MCP environment values and vault references survive restore.

Preview/apply endpoints are owner-only and use expiring single-use previews. Applying checks for stale
state, serializes dashboard writes, validates the complete catalog and rolls the pointer back on failure.
Successful changes audit `catalog.restore` with item IDs and source version. Profiles are never reset.
A custom ID wins; owners must choose another unused ID for a separate built-in copy. Previous successful
generations remain on disk so running broker skill links remain valid; failed generations are removed.
Latest-source checks use the Updates repository and `data/secrets/update_token`, verify the complete
release exactly as the updater does, and allow at most one check per minute (including failures).


## Offline replacement recovery

Practice only in a disposable, independently isolated replacement environment:
no NIC/uplink, inherited host mounts or production helpers. A system container
or a fresh WSL2 guest can be a drill environment, but isolating it does not fence
the original main. An unfenced live-main backup must stop offline, without a
dashboard or hold removal. The saved kit authenticates the restore; it is not a
dashboard login, and the restore CLI does not prompt for its password.

Keep databases, keys and the kit on owner-approved encrypted storage inside the
guest's trust boundary. Deletion is not secure erasure. Record exact disposable
resources before creating them, and remove only those resources on completion
or abort. Host-specific execution plans and evidence stay private. This portable
procedure is not evidence that replacement recovery has been qualified.

1. Fence the old main: power it off or isolate it from machines, helpers and endpoint control, and prevent restart.
   DNS movement alone is insufficient. Record evidence independently; without fencing stay offline.
2. Keep replacement accd/socket activation, brokers, timers, publishing and autostart disabled. Obtain matching
   ACC version/build and pinned restic from trusted release sources, independently of the backup repository.
3. Retrieve the owner's kit from the password manager or offline copy. Compare repository/installation IDs,
   public-key fingerprint, the explicit backup ID and its snapshot ID against the off-main drill record.
   Do not trust “latest”, and do not trust a backup ID alone: a compromised main can reuse one.
   Mount/copy ciphertext at the kit path, or edit only `repository` in a private kit copy for a changed mount path;
   preserve the IDs, passwords and signing public key.
4. As an unprivileged account, run `python -m acc.backup_server restore --kit /secure/acc-recovery-kit.json
   --target-id TARGET_ID --backup-id BACKUP_ID --snapshot-id SNAPSHOT_ID --destination /secure/example-replacement`
   on one line.
   The destination must not exist. Scratch and final trees are private; allow capacity for archive and validation
   copies, preferably on an encrypted filesystem. Failure creates no final installation.
5. Inspect the validated tree: `data/acc.db`, `data/secrets`, `catalog-local`, `git` and `review/review.json`.
   The matching DB/key are one restore unit. `data/RESTORE-HOLD` must stay in place. Starting ACC with this data
   directory returns quarantine HTTP 503 before any ordinary startup. No migrations, hooks, source, units or
   environment files from the backup execute. Trusted git hooks/configuration replace captured executable settings.
6. Review independent records for post-snapshot enrollments/revocations, grants, pending approvals/helper intents,
   credentials. The tool revokes captured browser sessions and invites and records a restore audit. Revalidate
   authority before any future activation. Local sessions are unavailable until their original workspaces/transcripts are recovered separately.
   Root-owned TLS/private-CA keys need independent escrow; review stable origin, DNS, accounts and paths manually.
7. Ordinary restore stops here. The off-by-default replacement controller is available only after independent
   fencing/authority review; follow [replacement activation](/User-Guide/Backups#replacement-activation-candidate).
   It never starts services, and no live replacement drill is claimed.
   Do not remove the hold to “try” reconciliation. Preserve it until the replacement-control procedure is qualified.
   Record the drill outside ACC, then attest it in **Host → Backups → Record restore drill** on the original main.

Never run manual git repack/GC/ref maintenance concurrently with capture without holding the tree's
`.git-backup.lock` (the parent of the configured git directory). ACC receive, ref updates and orphan moves already
use this lease. An interrupted updater leaves `data/.update-in-progress`; review the transaction before clearing it.
A failed upload leaves its private `data/backup-service/staging/<backup-id>` generation for review. Only the newest
complete one (signed manifest, latest `finished_at`) is kept, and none once no target is enabled: the next capture removes older ones (audit `backup.staging_discarded`, with count and bytes), because each is
a plaintext copy of data that is still live and that the newer capture supersedes. Before this, an hourly failing
target kept one generation per hour until the 20 GiB cumulative payload budget made every capture fail, for every
target. A kept generation never costs a capture: if the budget is short with it, the worker removes it and captures again. A removal that fails is logged at WARNING and the capture still runs. Failed jobs record the fixed `Refused` reason (restic's exit status, never its stderr) or the exception type,
and the worker logs the same line at WARNING.


## Container qualification evidence

Use the [owner qualification runbook](/Internal/Containers-Qualification-Runbook)
for the reviewed kit's account, build, archive/pin, policy, preflight, setup,
collection and between-attempt rollback order. Its kit/launcher deployment
prerequisites must be met first; it does not enable production containers.

For a new Linux qualification window, the source checkout includes
`scripts/containers-qualification/README.md`. Its owner kit checks prerequisites
without changing them, journals reviewed setup, collects root-captured JSON
evidence and restores recorded changes with a guarded uninstall. It refuses
existing kit/image-store state and unknown changes; it never enables containers.
Review its manifest and source hashes from a protected checkout before root use.
Pending operations require owner review rather than automatic destructive recovery.

Keep containers disabled for ordinary sessions. The source runbook
`scripts/container_qualification/README.md` describes the separate local image build,
root-owned qualification policy and non-root control-account command. The driver uses
existing broker RPCs and protected grants; it never bypasses launcher peer authentication.
No service restart or global flag change is needed. All mandatory rows require pass for GO;
missing or skipped checks remain NO-GO. The effective hooks/policy and their parents must be
immutable; writable configuration fails even when binaries are in a pinned read-only image.
The driver retains private checkouts for review, attempts stop on partial launches and
revokes temporary grants so reconciliation can retry cleanup. Review exact reported units;
do not remove unknown containers by labels. Session recreation, synthetic model protocol
and kernel control-file checks do not certify broker/host restart, provider credentials,
hard disk quotas, multi-session staging attacks or production rollout. Decision 0042 in
the source checkout records the boundary.
## Enrolled Linux backup receiver

Prepare and check the target in **Host → Backups → Set up**; the owner runs the generated root commands.
See [Backups](/User-Guide/Backups#before-you-start). Enrollment alone grants no receiver capability.
No root shell, SSH identity or maintenance broker is given to ACC. Target port 18762 binds loopback only;
transport uses the existing enrolled TLS machine link with a root-owned installation/target binding.

On the target the administrator can inspect `systemctl status acc-backup-receiver.service`. Preserve
`--append-only --private-repos`, the root-owned auth file and the dedicated non-login account. Never make
`/var/lib/acc-backup` accessible to `acc-node` or give the main retention-admin credentials. Restrict target
root and maintain an independent immutable/offline copy: append-only does not constrain target root.

For independent offline recovery, stop the receiver during the owner-administered consistent ciphertext
export, copy `/var/lib/acc-backup/TARGET_ID` to protected storage, then restart it. The exported repository
is decrypted and validated on the trusted replacement with the kit and `--repository`; no dead-main link
or node enrollment key is required. Retention runs on a trusted independent maintenance host under its own
administrator credentials; coordinate downtime, run a full restic check, then restic forget/prune preserving
all retained references. ACC's ordinary API refuses remote prune and holds no maintenance credential.

Backup target setup stops the main-controlled root update/package triggers and blocks their root services
with `ConditionPathExists=!/etc/acc/backup-target.json` drop-ins. The node rejects update/system-tool requests,
and the root updater independently rejects pending requests. Owner review of independently trusted source
is required for subsequent target updates. Keep these conditions and the binding in place; re-enabling
main-controlled root updates would let a compromised main erase history. Broker accounts must have no sudo,
Docker or root configuration write access. Existing root-helper jobs must finish before initial setup.


## PostgreSQL operations boundary

With `ACC_DATABASE_URL` selecting PostgreSQL, operate one ACC main and independent PostgreSQL backups/PITR.
The SQLite restore/capture procedures above and the legacy SQLite helper scripts do not protect PostgreSQL.
Main updater installation is also refused before download: its backup and rollback are SQLite-only. Disable
automatic installation and use an owner-managed PostgreSQL backup/update/recovery procedure instead.
ACC server capture refuses that backend. Disable existing ACC capture schedules before a switch. The owner
runbook in the source tree, `docs/POSTGRESQL.md`, covers the root-only offline read-only transfer and per-table verification.
The service environment file is `root:root` mode `0600`; PostgreSQL connections default to verified TLS, so configure
the database CA and hostname rather than disabling verification to work around a certificate error.
Keep all vault keys and files paired with the database. Activate a standby only through the explicit [manual HA procedure](Manual-Failover.md).

## Manual high availability

The [phase-2 owner runbook](Manual-Failover.md) covers standby provisioning,
protected owner API calls, planned demotion/promotion, independent crash fencing,
node reconnection and returning to the original main. Every HA process starts
standby. Match builds and versioned secrets/files before handover; keep external
helpers fenced until their pending operations are understood. Do not restore an
old shared PostgreSQL database as a main-host rollback. Automatic failover is not
implemented. See the [Guide](../User-Guide/High-Availability.md) for visible status.

## S3 backup operations

Owners configure S3 targets through **Host → Backups → Set up**; see [S3 setup and recovery](/User-Guide/Backups#s3-compatible-storage).
The writer is an infrastructure credential with provider `s3` and purpose `backup-writer`, sealed under the main’s
vault key. There are no project grants or reveal endpoint; DNS credential routes exclude these keys. Never log
process environments or the job’s private credential file. Reconciliation removes abandoned `backup-service/s3-job-*`
folders, while a private lease preserves files belonging to active owner operations. Writer replacement disables scheduling and clears verification evidence.

A protected target needs fresh authenticated capture/readback bracketed by matching complete key/version inventories
and explicit-version COMPLIANCE retention reads, with at least 30 days remaining at completion on every inventoried
version. Concurrent overwrites, missing version IDs and pre-upgrade unbound receipts do not qualify; a new successful
capture is required. The independent writer policy must deny non-lock version deletion and retention overrides,
and no independent prune or version cleanup may run during capture. Retention-only extension may overlap capture. Old shared chunks can reduce the minimum;
review the provider’s version inventory and extend retention independently or rotate repositories. Inspection is
bounded at 100,000 objects and fails closed. Versioning, governance and unsupported APIs never assert protection.
Keep maintenance credentials off the main. Coordinate downtime before any restic prune or provider version cleanup.
Retained probes and old transient-lock versions incur storage costs. Recover via independent account recovery,
version-aware ciphertext export and offline quarantine restore; the kit carries no S3 writer credential.

Use **Pause hourly backups** before independent prune or version cleanup, then wait for queued/running jobs to finish.
Pausing only disables future scheduled captures; it neither cancels work nor grants maintenance access.

Disable/revoke the main writer at the provider before independent S3 prune or version cleanup. A compromised main can ignore
the UI pause and delete transient locks; the provider-side credential boundary must quiesce it. For version-aware
recovery choose each key’s last version at a known-good timestamp, ignore later delete markers, and export only
validated restic paths into fresh private scratch before the authenticated offline restore.

### Independent retention job

`acc-s3-maintain` (also `python -m acc.backup_s3_maintenance`) runs only when an owner invokes it on a trusted
independent Linux host. No accd route, timer or main-vault maintenance credential exists. Its private target
file records all writer key IDs; its purpose-tagged 0600 identity must belong to a separate IAM principal.
Key-ID refusal cannot prove IAM separation. See the [exact policy and owner procedure](/User-Guide/Backups#independent-retention-maintenance).

Inventory conservatively includes all surviving restic object versions, so it covers deduplicated referenced
chunks and can retain orphans. Complete bounded dry-run planning precedes explicit apply. The job extends
COMPLIANCE locks below 35 days to 60 UTC days, never shortens/deletes, and verifies exact-version read-back.
A private job lease, 24-hour plan expiry, rate limiting, update batches, storage budget and fsynced intent/result
journal support safe retries. Inspect `job.json` and `audit.jsonl` independently; after a lost reply, resume the
same explicit apply. Partial exit code 2 needs another reviewed attempt; expired plans need a fresh dry-run.
No retention-only job report grants main-side protection. Wait for a new successful capture inventory.

Host Backups shows the last minimum deadline minus now minus 30 days as runway; enabled targets warn at seven
days. Unknown/future evidence is unverified. A stalled maintenance host can exhaust runway even while hourly
captures keep succeeding. Monitor the job outside the main, and inspect growing extra GiB-days before raising
the default 1,000 budget. Owner-supplied prices are estimates, not provider billing. Use the [rotation runbook](/User-Guide/Backups#rotate-to-a-fresh-s3-repository)
for a new prefix/key/kit; stop extending the retired prefix and preserve every old lock and recovery obligation.

## Runtime rename compatibility, phase 1

The Linux main/node installers add `rynhultd.*` and `rynhult-*` aliases to the
existing `accd.*` and `acc-*` systemd units, including sockets, paths, timers and
broker templates. Both names address the same job and listener. Existing units,
enablement and drop-ins remain authoritative; aliases do not start another job.
`deploy/lib/unit-aliases.py --check` preflights before installers replace units;
alias creation checks the complete known unit set again and refuses
masks, conflicting objects and new-name drop-ins, and refuses an alias name that
already exists (or has drop-ins) in another systemd unit directory (`/run`, `/usr/lib`,
`/lib`, `/usr/local/lib`), since an `/etc` link would shadow that unrelated unit.
Installers run it with `python3 -I`. It accepts both its own
relative aliases and the exact absolute targets created by systemd enablement. Command aliases likewise refuse
to overwrite existing destinations. Review conflicts manually before retrying.

Standalone helpers load the shared settings reader (`acc_env.py`) only from beside
themselves, or from the source checkout when none is installed there. The reader must
have the helper's owner and must not be writable by other users; otherwise the
helper stops with exit 2, which also makes `acc-guard` block the tool call rather
than letting it run unchecked. The root container launcher resolves broker settings
with the same precedence as the broker (`RYNHULT_*` wins).

Launchd has no safe label aliases. Mac jobs retain `local.acc.broker` and
`local.acc.node`, their enrollment and directories. A later label cutover requires
a drained node; do not load duplicate jobs. Windows task names also remain.

New nodes advertise `rename-compat-1` through the existing authenticated handshake.
The main filters and persists it in each machine's `info.capabilities`, exposed
by the existing machines API. A reconnect without the flag clears prior evidence.
An offline or older enrolled machine cannot establish fleet readiness. Keep both
names for at least two stable releases and until every enrolled machine reports
the compatible version/capability; there is no automatic cleanup in this phase.

A stage 2 rename failure must retain the database and roll back binaries/config
only. The general updater's existing database restore remains a separate recovery
contract: no physical rename/cutover is enabled by this phase. Qualify that updater
integration before enabling later stage 2 migration operations.

## Public names, phase B

`deploy/lib/public-aliases.py` adds reviewed public names alongside the existing
Linux/WSL commands and units; the Mac node installer adds user command aliases. Main and node installers run its read-only preflight
before their normal work and reconcile again after components are installed.
The update helpers already invoke these installers, so ordinary software updates
also acquire aliases. No new updater transaction, node migration or path cutover
is enabled. Normal updates retain their existing restart and database recovery
contracts; use `--aliases-only` for the additive operation while sessions run.

The Linux/WSL alias-only installer path invokes neither setup nor account helpers. It calls
only the isolated Python helper and `systemctl daemon-reload`. It never enables a
second timer/path/socket or starts a worker. Unit contents, old-name drop-ins,
wants/requires links, activation dependencies, runtime/state directories and
socket access policy stay authoritative under their old names. The optional
collector also has an alias; its separate installer still refuses a running
collector and never activates services.

The complete allowlists are `HOOKS`, `HELPERS`, `command_aliases()` in
`deploy/lib/public-aliases.py` and `UNITS` in `deploy/lib/unit-aliases.py`.
Public aliases point at legacy executables, retaining one `acc` implementation
namespace. Existing stage-one console-script links are accepted at their known
legacy install locations. Internal policy modules, environment bridges and the
container's internal executables keep their original names.

On Linux/WSL, phase B owns only the links listed in
`/etc/rynhult-phase-b.json` (root, 0600).
The helper locks the protected `/etc` directory inode, checkpoints intended
additions atomically with file and parent fsync, then creates links without
replacement. Reruns inspect actual objects and resume pending creations. Parents
are traversed through no-follow directory descriptors, requiring protected
ownership and modes; executables must be regular protected files. Masks, custom
units, vendor/runtime instances and new-name drop-ins block unit preflight.
Rollback validates the complete journal allowlist and every remaining link before
removing anything. It never restores a database or changes a legacy file.

Before rollback, drain consumers of new names; the helper cannot discover shell
scripts or live-process dependencies. Inspect `--rollback-aliases --dry-run`, then
run the same installer with `--rollback-aliases`. Missing owned links are accepted;
a replaced link is a conflict. Retain the journal with backups and do not copy it
between hosts or roles. A leftover `.tmp` journal after a power cut before atomic
activation requires operator review; no aliases from that checkpoint were created.

The repository has no sudoers or polkit rules naming these binaries. The only
`sudo -l` checks remain in account provisioning, and existing workspace ACLs,
socket ownership/modes, AppArmor paths and service sandboxes are unchanged. Do not
add alias paths to privilege allowlists to make a command work. On each host,
review external sudoers/polkit/AppArmor rules and binary ACLs before deployment;
these are outside repository discovery. Real loaded-unit and active-session
qualification still requires disposable systemd hosts and coordinator checks.

The Mac alias-only path runs as the signed-in user with the existing installation's
venv Python and Homebrew bin location. It bypasses logging/setup and every
`launchctl` operation. Its journal is
`~/Library/Application Support/ACC/rename-phase-b.json` (user, 0600), and the
same creation/rollback rules apply. Protected root-owned system ancestors and
protected user-owned app/bin directories are accepted; group/world-writable
parents are refused (root-owned sticky temporary ancestors are permitted).
No permission changes are made to Homebrew to accommodate an alias. The fixture
tests exercise paths with spaces, dry-run dispatch, rerun and rollback; native
macOS filesystem and launchd continuity still need platform qualification.

Windows Setup's WSL uninstall invokes journaled rollback after stopping the old
units and before deleting installed source. It detects only the protected
journal's main/node role and aborts on an alias conflict. Its dry-run prints that
step without running it. Drain sessions and new-name consumers before uninstall;
this does not migrate or rename native Windows tasks or WSL distributions.

Alias-only root dispatch uses absolute Python, systemctl and dirname paths, so a
caller-supplied PATH cannot select its helper loader. Mac dispatch rejects root
before looking up Homebrew. The main installer rejects unknown options before
host work; a misspelled alias/dry-run flag cannot fall through to an ordinary
installation.

The alias helper permits read-only Mac plans in root-run test fixtures. Its Python
privilege check still rejects root installation and rollback; the Mac installer
continues to require the signed-in user, including for its dry-run wrapper.

### Installer test root

The install-wizard tests copy the scripts into a temporary tree and replace setup, node installation and enrollment
with fakes. `ACC_INSTALL_ROOT` redirects only the wizard's log, existing-setup check and enrollment executable. It is
refused for real/effective UID 0 and requires an owned mode-0700 canonical folder under `/tmp`, without symlinks in
its root or log/config/CLI paths. It does not sandbox the downstream installers: tests must keep replacing those.
Root gates run the wizard child with unprivileged credentials and separately check the actual-root refusal.
