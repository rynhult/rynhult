# Updates

Approved by the owner, 2026-10-22. The main ACC checks GitHub once a day; additional machines only use their main
ACC. Updates are offered to owners, never members. Automatic installation is off until an owner chooses a daily
local time in Settings. Every main update makes a full backup first.

## Releases and identity
- Settings → Updates names an `owner/repository`, initially derived from a github.com `ACC_SOURCE_URL`. Empty
  disables checking. Installation is bound to root-owned `ACC_UPDATE_REPOSITORY` (default: the same repository
  derived from `ACC_SOURCE_URL`), so an acc-writable preference cannot redirect root to another code source. An optional token is read from a secrets file; no API returns it and no log records it.
- Check the latest stable release, excluding drafts and prereleases. Compare semver versions; never downgrade.
  Keep the latest version, publication time, plain-text notes, check time and error. Notify owners once per version.
- Tagged CI builds `acc-<version>.tar.gz` using `acc.bundle`, with `SHA256SUMS` and a `BUILD` identity manifest. The
  small manifest also detects changed source under the same stable version; equal version with a different build
  can update, older versions cannot. Main and node installers share
  archive validation: matching SHA-256, one expected top folder, regular files and directories only, no absolute
  paths, dot traversal, links or devices. Validation and extraction both stream with cumulative limits: 100,000 raw headers (extensions included), 2 GiB effective file data, 64 KiB per extended header, 8 MiB total extended-header bodies and applied PAX fields and 16 nested extensions. Metadata sizes are checked before parsing their bodies, sparse files are refused, and decompression buffers and total physical bytes are bounded. A full validation pass completes before extraction; the same checked file descriptor and limits are used for the extraction pass.
- A build is a SHA-256 of the bundled source paths, executable bits and bytes, independent of timestamps and
  packaging. Exclude the identity file itself and generated assets. Preserve the identity in release archives.
  Report version **and build** in health, enrollment and node status. Old nodes have an unknown build.

## Main update transaction
`accd` cannot install. It atomically writes an action and version to `updates/requests`; a root-owned `acc-update`
helper, started by a systemd path unit, revalidates the request and resolves the release itself from trusted
configuration. Requests never contain a URL, path or command. Paths are configurable through the environment for
isolated tests. Helpers serialize transactions and record every step and final result for the dashboard.

1. Refuse a git checkout: “This ACC runs from a git checkout: update it with git pull and deploy/install.sh”.
2. Run `acc-backup` with a dedicated transaction backup destination (database and secrets); failure stops here.
3. Download the release and checksums over HTTPS, verify, and extract beside the installed source.
4. Keep the previous tree, place the new tree at `/srv/acc/app` (release setup currently copies its source there),
   and run the new tree's `deploy/setup.sh --yes`, the same main-install entry point the wizard uses. It keeps
   `/etc/default/accd`, ensures the version's prerequisites, and calls `deploy/install.sh`. Keep the existing virtualenv with
   the previous tree; each version installs its own environment.
5. Require health to report the expected version and build within a bounded interval. If install or health fails,
   stop accd, restore the pre-install database (including removal of WAL/SHM), restore the previous source, run its
   installer and report rollback success or failure. Restoring the database whenever installation started also
   covers migrations that committed before an install failed.

Dashboard restart disconnects terminals briefly. Agents run in tmux under independent broker services; accd restart
alone leaves them running. Installers also refresh brokers: the existing `KillMode=process` explicitly preserves tmux across broker restarts, so there is no running-session wait.

## Owners' controls
Settings → Updates shows current version/build, release notes as text, last check/error, **Check now**, **Back up
and update**, progress/result, and automatic installation with a daily time. Confirmation explains the backup and
restart. A small update mark and one Inbox item per new version reach owners only. Audit `update.check`,
`update.request`, `update.result`, with versions and never tokens.

## Additional machines
Machines report build identity with version. A different or unknown build shows **Update available**; owners may
confirm **Update** or **Update all**. Each machine has **Follow the main ACC automatically**, initially off.
After a main upgrade, followers synchronize immediately, including on reconnect.

The signed link carries the main's version, build and bundle SHA-256. A Linux/WSL node queues a root-helper request;
the helper downloads only `/api/nodes/bundle` from its enrolled primary, using its enrolled TLS trust (rechecking a private CA against the fingerprint in the root-owned enrollment config), verifies the
hash and archive, runs the new tree's `deploy/node/install.sh`, and restarts acc-node. Persist the result for the
next signed-link connection. Nodes never contact GitHub. macOS keeps its LaunchAgents model without a root
helper: show **Update this Mac with the new installer**, linked from the main's installer information.

## Verification
Stub GitHub, downloads, backups, installers, health and service commands; run helpers only under temporary roots.
Cover versions, stable-release selection, errors/rate limits, archive attacks, request validation, step order,
rollback, owner boundaries, node messages/results, and browser controls. Release CI and a real system upgrade
remain deployment checks, never tests against the development host.
