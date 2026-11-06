# Updating Rynhult

For an existing rc.1 deployment, follow [Upgrade from rc.1](/User-Guide/Upgrade-from-rc1)
for the signed in-place rc.2 procedure, main first and then nodes.

Owners can back up and update the [main Rynhult](/User-Guide/Glossary#main) from Settings, then sync the other machines to its exact source build.
Members do not see update controls or release notifications. Installing updates automatically is off by default.

**PostgreSQL installations:** the main Rynhult's **Back up and update** workflow and scheduled installation are refused
because their backup/rollback only supports the default SQLite database. Turn off **Install updates automatically**
and ask the installation owner to follow an independent PostgreSQL backup, update and recovery procedure.
A leftover SQLite database is not a backup of PostgreSQL. Updating additional machines is unaffected.

## Update the main Rynhult

The built-in updater uses the release channel selected in Settings → Updates.
Choose **Release candidates** to install the next signed RC in-app. Node **Update** then syncs the main's exact build.
Installations still on 0.15.0-rc.1 predate the channel: upgrade them once by hand with
[Upgrade from rc.1](/User-Guide/Upgrade-from-rc1), main first, then each node.

1. Open **Settings → Updates**. Check the current version and build, the latest release, its notes and the last
   check. Rynhult checks once a day; **Check now** checks immediately.

![Illustrative update controls on 0.15.0-rc.1, with an example future stable release](/.attachments/updates-settings.png)

2. **GitHub repository** defaults to the official `rynhult/rynhult`; no administrator edit is needed.
   Leave it empty and select **Save update settings** to turn checks off. A different repository needs
   administrator authorization as described below.
3. When **Update available** appears, select **Back up and update**, read the confirmation and confirm. Rynhult first
   backs up the database and secrets. A failed backup stops the update. Downloaded source must pass its release-signature, checksum
   and archive safety checks before installation starts.
4. The dashboard restarts. Terminals disconnect briefly; agents keep running in tmux under their brokers.
   Reconnect after the restart and check the step results. If installation or health checks fail, Rynhult attempts to
   restore the previous source and database and shows whether rollback succeeded.

A newer release creates one owner [Inbox](/User-Guide/Glossary#inbox) item per version; the sidebar also links to Updates while an update is
available. The build identifies the actual source, so machines with the same version can still need an update.

Source archives used for releases, installation and additional-machine downloads exclude host inventory, private handoffs, reviews, qualification material, rename/rollout inventories and private operational documents. They contain the public application source for that build. Keep private operational records in your own backups.

## rc.3 → rc.4

Use this procedure once rc.4 is published. Read the
rc.4 release notes (`docs/release-notes/0.15.0-rc.4.md`), including the Claude
sign-in change and migrations 0085–0087. Keep a second administrator shell open
and arrange a quiet period: pause schedules and teams and finish or stop work.

1. **Back up.** Follow [Upgrade from rc.1, backup step](/User-Guide/Upgrade-from-rc1#2-back-up-before-changing-anything)
   for the existing backup service, matching secrets and private host configuration.
   Check that capture succeeded and retain an off-machine copy. Back up project
   workspaces separately. PostgreSQL needs an independently tested backup and
   schema recovery plan; do not use the SQLite bundle transaction below.
2. **Upgrade.** In **Settings → Updates**, choose **Release candidates**, save and
   select **Check now**. Confirm the offered version is **0.15.0-rc.4** and its
   signature is verified, then select **Back up and update** and confirm. A failed
   backup or signature check stops installation. RC installation is manual even
   when automatic stable updates are enabled. Modified or untagged git installs
   follow [Troubleshooting](#troubleshooting); do not discard local work.
3. **Verify.** Reopen Settings → Updates and check **0.15.0-rc.4**, the published
   build and the successful transaction result. Check service health using the
   [main verification commands](/User-Guide/Upgrade-from-rc1#4-check-the-main-before-proceeding).
   Check projects, Inbox, layout saving and backup targets. Generate offline
   sign-in recovery codes in **Settings → Account → Security → Sign-in recovery codes**;
   migration 0086 does not enroll existing accounts. Review Claude sign-in choices
   and schedules: 0087 preserves existing credential-backed sessions and applicable
   schedules as API-key launches; new sessions default to subscription. Then sync
   Linux/WSL nodes with **Host → Machines → Update**, use the existing Mac installer
   route, and confirm matching version/build before deliberately resuming work.
4. **Rollback.** Failed installation or health checks attempt automatic source and
   database rollback; inspect the reported result. To undo a successful bundle
   update, use its retained rollback ID and the existing
   [bundle rollback procedure](/User-Guide/Upgrade-from-rc1#roll-back-a-bundle-main)
   with the verified rc.4 staging kit in place of rc.2. Keep the backup and prior
   source. Stop new work first: restoring the earlier database loses later database
   changes. Manual/git recovery follows [Troubleshooting](#troubleshooting): restore
   the pre-upgrade database and matching secrets before running old code. Never run
   rc.3 against the migrated database. Workspaces, host configuration and node
   updates are not reverted by the main transaction; recover them separately.

## Release signatures

After **Check now**, **Signature verified (Rynhult Releases, key …0DB5)** means the
release's checksum list was signed by a key trusted by this installed Rynhult.
The installation checks the signature again before using the checksum to unpack
any downloaded source. It uses the bundled public key, never a key supplied by
the download. The key suffix can change after an approved signing-key rotation.

A bad, unknown, expired or revoked signature stops the update. The result explains
why; ask the installation owner to check the release and trusted-key installation.
GnuPG must be installed on the main; missing verification tools also stop updates.

A missing signature disables **Back up and update**. For stable releases only, after independently
checking the release's source, an owner can choose **Update without signature…**
and confirm the separate warning. Rynhult cannot authenticate that release. This
exception is recorded in Audit and applies to that one update only; backup,
checksum and archive safety checks still run. It never overrides an invalid
signature. Pre-releases, automatic updates and Catalog restore do not take this exception.

Key replacements must arrive through a release trusted by the installed key or
an independently reviewed reinstall. An offline installation cannot learn a new
revocation until its trusted key bundle is updated. A signature authenticates the
release, not whether the server has shown you the newest release.

Additional machines sync a rebuilt bundle from their enrolled main, authenticated
by enrollment TLS and the main's checksum/build descriptor. This is a separate
trust path: a node does not independently verify the public release's signature
on that rebuilt archive. Source/git main installations retain this sync path.

## Schedule automatic installation

Under **Settings → Updates**, turn on **Install updates automatically**, choose **Daily time**, then select
**Save update settings**. The time uses the server's timezone, shown below the field. If the server was off at that
time, it checks when it returns. Automatic installation always makes the same full backup first.

## Sync additional machines

1. Open **Host → Machines**. **Update available** means a machine has a different source build from the main Rynhult.
   **unknown build** means an older [node](/User-Guide/Glossary#machine-node) has not reported a build identity yet.

![Machines shows build differences, update actions and the optional follow switch](/.attachments/updates-machines.png)

2. Select **Update** beside a Linux or Windows/WSL machine and confirm, or **Update all** to sync connected machines
   that need it. They download source from this main Rynhult using the TLS trust set at enrollment. They never check
   GitHub themselves. Offline machines must reconnect before a manual update can be sent.
3. Optionally turn on **Follow the main Rynhult automatically** for a machine. It is off initially. A follower syncs
   after the main changes build, including when the machine reconnects.
4. A Mac uses **Update this Mac with the new installer** when this build has an installer: download and run it, then finish Rynhult Setup
   as described in [Install on a Mac](/User-Guide/Install-macOS). Pre-v1 builds and release candidates have none here: the Mac's row shows a command to run in Terminal on the Mac instead, which reinstalls from this main's source and keeps the enrollment ([Update an older machine](/User-Guide/Nodes#update-an-older-machine)). There is no root update helper on a Mac.

An older Linux/WSL helper can reject a genuine new bundle with **node bundle version or build mismatch**.
It needs a **one-time manual reinstall from the updated main's source**, not another retry or the old
installed source. Use the [older-machine recovery steps](/User-Guide/Nodes#update-an-older-machine);
enrollment, accounts and certificate checks stay intact. Backup receivers require independently reviewed
updates instead of main-supplied root recovery commands.

After a successful helper-driven update and reconnect check, Linux/WSL keeps its two latest previous
installations and removes older helper-created copies. Failed updates keep recovery sources.
Host shows the helper's failure detail or a cleanup warning; a cleanup warning does not undo a healthy update.

## Troubleshooting

- **Git checkout:** a clean checkout with HEAD exactly at a release tag (`v*`) can use the normal release updater.
  Modified files, untracked files, commits after a tag and untagged checkouts keep the “This Rynhult runs from a git
  checkout: update it with git pull and deploy/install.sh” message. Save local work and update manually.
  The root helper also requires the checkout directory and its `.git` directory to be owned by root;
  symlinks and Git metadata files are refused.
  Release updates replace the checkout with verified release source; subsequent updates use that source installation.
- **Repository needs authorization:** an administrator sets `ACC_UPDATE_REPOSITORY=example/acc` in root-owned
  `/etc/default/accd` and restarts accd. The default authorizes `rynhult/rynhult`. An
  Rynhult preference can never redirect the root installer to another code source by itself.
- **Private repository:** the administrator puts the optional GitHub token in `data/secrets/update_token`, readable
  by the Rynhult service and root, with mode 0600. The token is never displayed by the dashboard or included in audit.
- **Repository not found:** check the repository name; private repositories need the token in `data/secrets/update_token`.
- **GitHub refused access:** check the token and its repository permissions.
- **Rate limited:** wait for GitHub's limit to reset, then select
  **Check now**. The last release details remain visible with the error.
- **Older node with unknown build:** [sessions](/User-Guide/Glossary#session) keep working. If its old node daemon does not understand update
  messages, use the copyable recovery commands on its **Host → Machines** row. They keep enrollment and install
  the helper without needing a desktop installer release; see [Update an older machine](/User-Guide/Nodes#update-an-older-machine).
- **Helper missing:** from the current source checkout on the main Rynhult, run:
  ```sh
  sudo deploy/install.sh
  ```
  On an additional Linux node, use this command instead:
  ```sh
  sudo deploy/node/install.sh
  ```
- **Archive refused:** release archives must fit the file, header and metadata limits. Refusal happens before extraction. Ask the release publisher to rebuild an ordinary source archive; do not bypass verification.
- **Update failed:** read the last result. A failed rollback needs an administrator: retained source is beside the
  installed app and the full pre-update backup is under `updates/backups/`. Restore the database and secrets from
  the same backup; keep the secrets passphrase if backup encryption is enabled.
- **Upgraded by hand (for example the next release candidate installed over the last):** `deploy/setup.sh` from a
  separate checkout refuses a different version already installed at `/srv/acc/app`. At the same version, setup continues from the installed copy only if its setup, main installer and Node installer files match the supplied files; differing installer code requires the manual upgrade below. To
  install the new one by hand, as root:
  1. Take a backup: `sudo acc-backup`.
  2. Stop Rynhult: `sudo systemctl stop accd.service accd.socket`.
  3. First check that `/srv/acc/app.previous` does not already exist, including as a broken link; if it does, stop
     and review that retained installation. Then move the installed version aside: `sudo mv /srv/acc/app /srv/acc/app.previous`.
  4. Run the new version's `sudo deploy/setup.sh`. It copies itself to `/srv/acc/app` and installs. Your data in
     `/srv/acc/data` is kept.

  Before Rynhult upgrades the database schema at its next start, it keeps a copy of the SQLite database beside it,
  named `acc.db.pre-migration-<revision>` (readable only by Rynhult's own account). An existing copy must be a private,
  regular SQLite file with valid integrity and the expected revision; otherwise the upgrade stops for review and
  does not replace it. If the new version fails:
  1. Stop both entry points: `sudo systemctl stop accd.service accd.socket`.
  2. Check that `/srv/acc/app.failed` does not exist, including as a broken link. Then retain the failed tree:
     `sudo mv /srv/acc/app /srv/acc/app.failed`.
  3. Restore the previous tree into the now-empty location: `sudo mv /srv/acc/app.previous /srv/acc/app`.
     Moving the previous folder onto an existing app folder would nest it inside the failed version instead.
  4. With all database users stopped, retain the failed `acc.db` and any `acc.db-wal` / `acc.db-shm` together in a
     private recovery folder. Restore the pre-migration copy as `acc.db`, owned by `acc`, mode `0600`, with its original
     `secrets/vault.key`. Never leave the failed database's WAL beside the restored copy. If secrets also changed or
     their pairing is uncertain, restore the complete backup from step 1 instead.
  5. Run `sudo /srv/acc/app/deploy/install.sh`.

  Remove old copies once the new version runs well; they hold the same data as the database. PostgreSQL gets no copy:
  back it up first.
- **Updater configuration is corrupt or unreadable:** checks, automatic installation and automatic machine
  following stop. Rynhult keeps the damaged file for diagnosis. Ask an administrator to repair its contents or
  permissions, or enter the intended repository, automatic setting and time and select **Save update settings**
  to replace it. Re-enable any machine follow switches afterward; damaged values are not recovered.


Source bundles served for installation and node downloads use the same exclusions as the public-source
exporter: private documentation folders, generated artifacts, secrets and environment files (including
case variants and `.env.local`) are omitted. A damaged Git checkout refuses bundle creation; repair
its Git metadata before retrying. Publishing new source still requires reviewing the exact exported tree.

## Rynhult name compatibility

This release accepts both `ACC_*` and `RYNHULT_*` environment settings. Use the
same suffix: `RYNHULT_LISTEN` corresponds to `ACC_LISTEN`. If both are set, the
Rynhult name wins, including an empty value. Keep one configuration file at its
existing location; an update needs no manual path or database changes.

On Linux, the installers add new service names as aliases: `rynhultd.service`
and `accd.service` address the same service, and the same applies to the node,
brokers and helper services and their sockets/timers/path triggers. Existing
service names keep working. If another program already installed a service
with one of the new names, the installer stops and names it instead of hiding it. On Mac, launchd labels stay unchanged until a later
maintenance operation; running a second job is unnecessary.

Cookies, the CSRF header, `acc.db`, service accounts and existing `/srv/acc` paths
remain permanent. Old and new names will coexist for at least two stable releases
and until every enrolled machine reports support. Offline machines extend that
window. Future path/default/label changes are separate from this compatibility
release. The owner-approved naming convention for new installs will be applied
in a later qualified phase.

Owners can use the read-only [rename readiness tool](/User-Guide/Rename-Readiness)
against an offline snapshot to review aliases and proposed future changes.
A failed future stage 2 migration must preserve the database while reverting
binaries/configuration.

### Add the public names without a software update

An administrator can install just the Linux/WSL command and unit aliases from a
reviewed checkout. On the main, first inspect the read-only plan:

```sh
sudo bash deploy/install.sh --dry-run
```

This plans phase B aliases and validates the installed folder mapping against
`/etc/default/accd`, optional `/etc/default/acc-backup`, broker settings, and any
existing folder associations. Run it with sudo to read root-private settings and
the alias journal. It remains read-only and does not simulate dependency
installation or a complete software update. A backup mirror inside the human owner’s home is permitted; a mirror inside an
agent’s home is refused. Agents cannot allow an owner’s mirror, its descendants,
or a parent folder containing it. Installation and dry run refuse protected and
mirror paths containing `.`, `..`, empty components (including repeated or trailing
slashes), or a symlink in any existing component; the error names the setting to fix.
A home equal to or inside a protected folder
is refused. After checking the plan, an
administrator deploys the aliases with:

```sh
sudo bash deploy/install.sh --aliases-only
```

On an existing Linux/WSL node, use `deploy/node/install.sh --aliases-only`, adding
`--dry-run` to inspect first. This changes links and reloads systemd definitions;
it does not restart the dashboard, brokers, agents or node daemon. A normal
software update still follows the update procedure above, with its existing
restart behavior. No machine is upgraded or re-enrolled by adding aliases.

On a Mac with an existing installation, run the following as the signed-in user,
without sudo:

```sh
bash deploy/node/macos/install.sh --aliases-only --dry-run
bash deploy/node/macos/install.sh --aliases-only
```

This adds command aliases in the existing app and Homebrew bin directories. It
never unloads or loads a LaunchAgent. Mac rollback uses the same installer with
`--rollback-aliases`, after new-name consumers have drained.

Public commands include `rynhult`, `rynhultd`, `rynhult-node`, `rynhult-restore`
and `rynhult-db-transfer`. Session helpers have matching names such as
`rynhult-request`, `rynhult-hook`, `rynhult-guard`, `rynhult-mcp-team`,
`rynhult-statusline` and `rynhult-wiki-check`. Installed administrator helpers
also accept the `rynhult-` prefix, for example `sudo rynhult-backup`. The same
permissions and arguments apply as for each existing `acc-*` command. Optional
components receive aliases only when their legacy executables are installed.
Old commands, hook configuration and service names continue to work unchanged.

If rolling back these additions, first stop consumers of the new names or keep
the harmless aliases until they drain. [Preview](/User-Guide/Glossary#preview) with `--rollback-aliases --dry-run`,
then an administrator runs `--rollback-aliases` on the appropriate installer.
This removes only links recorded as newly added by phase B. Pre-existing aliases,
legacy executables, units, data and configuration remain. A changed link blocks
rollback for manual review; never delete the conflicting object blindly.

### Release channel

In **Settings → Updates**, choose **Release channel: Stable** or **Release candidates**, save, then choose **Check now**. RC and beta installs default to Release candidates; stable installs default to Stable. You can explicitly choose either channel. Stable never offers pre-releases. Release candidates offers the highest newer stable or pre-release version: RC.2 can update to RC.3, then to the stable release. Drafts and downgrades are never offered.

RC updates use the same backup, checksum, archive validation and rollback path as stable updates, and always require a verified signature. **Update without signature** is never available for a pre-release. **Install updates automatically** installs stable releases only; choose **Back up and update** yourself for an RC.

A machine's **Update** action syncs its Linux/WSL node to this main's exact installed version and build, including RCs, using its enrolled main and trust. It never selects a newer GitHub release independently. Macs continue to use the installer or recovery command shown on Host.

### Scoped-approval database changes

The scoped-approval foundation adds migration 0082 without creating permissions
from older requests or settings. Keep the pre-upgrade backup. Once consent,
operation, batch-preview or per-request decision records exist, downgrade is refused so an older
server cannot ignore that authority or discard its audit evidence. Stop and fence
affected work, then restore the consistent pre-upgrade backup or forward-repair.
Never replace a saved permission with a global Security switch set to Off.
