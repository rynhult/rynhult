# Upgrade from rc.1

Use this procedure for an existing **0.15.0-rc.1** installation when rc.2 is
published. This is an in-place upgrade, not a new enrollment. Upgrade the main
first, check it, then update nodes one at a time. rc.1's release checker cannot
offer prereleases; repeatedly pressing **Check now** does not fix that.

The commands below use the standard installation paths. Stop if your paths or
service names differ. Schedule a quiet period: pause schedules and teams, finish
or stop running work, and keep people out during upgrade and rollback. Dashboard
and node connections restart; tmux agents normally survive, but do not rely on
an active team continuing through a rollback. Keep a second administrator shell
open. No step below requires sharing a token, recovery code or raw log.

**PostgreSQL:** do not use the bundle transaction below. It supports only the
default SQLite database and refuses a configured PostgreSQL URL, even if an old
`acc.db` remains. Have your database administrator take a consistent PostgreSQL
backup plus the matching secrets/configuration, test restoring it, and arrange
schema rollback before changing source. The git installer can run PostgreSQL
migrations; its SQLite pre-migration copy is not a PostgreSQL backup.

## 1. Verify and stage the new release

Download `acc-0.15.0-rc.2.tar.gz`, `BUILD`, `SHA256SUMS` and `SHA256SUMS.asc`
from the published rc.2 release into a new folder. Follow
[Verify a source release](/User-Guide/Install#verify-a-source-release): compare
both full fingerprints with your independently trusted copy before importing
the release key, then verify the signature and every listed checksum:

```sh
gpg --verify SHA256SUMS.asc SHA256SUMS
sha256sum -c SHA256SUMS
```

Stop on any failure or missing file. Do not substitute GitHub's automatically
generated source archive; use the signed `acc-…tar.gz` asset. rc.1's updater does
not itself verify release signatures. macOS uses `shasum -a 256 -c SHA256SUMS`.

On a Linux main, freeze those verified public files into a new root-owned
folder, verify that copy again, and unpack the reviewed upgrade kit there:

```sh
sudo mkdir /srv/acc/upgrade-rc2
sudo install -m 0644 acc-0.15.0-rc.2.tar.gz BUILD SHA256SUMS SHA256SUMS.asc /srv/acc/upgrade-rc2/
cd /srv/acc/upgrade-rc2
gpg --verify SHA256SUMS.asc SHA256SUMS
sha256sum -c SHA256SUMS
sudo tar --extract --gzip --file acc-0.15.0-rc.2.tar.gz --no-same-owner
sudo chown -R root:root acc-0.15.0-rc.2
sudo chmod -R go-w acc-0.15.0-rc.2
```

`mkdir` deliberately refuses an existing staging directory. Inspect and choose
a new directory if a previous attempt left one; never unpack over an installed
app or delete recovery files to make a retry pass. Use only a root-protected
parent, not a project or agent-writable workspace. The helper runs in Python
isolated mode and refuses an unprotected code tree.

## 2. Back up before changing anything

For the main's default SQLite install:

```sh
sudo systemctl start acc-backup.service
sudo systemctl show acc-backup.service --property=Result
sudo mkdir -m 0700 /srv/acc/pre-rc2-config
sudo cp -a /etc/default/accd /srv/acc/pre-rc2-config/
sudo cp -a /etc/caddy /srv/acc/pre-rc2-config/
```

The service loads your installed backup settings, including encryption. Confirm
its result is `success` and the backup includes `acc.db` and `secrets/`
or `secrets.tar.enc` with the matching vault key. Keep the encryption passphrase
separately if encryption is enabled. Retain any device-authority recovery backup
and its separate passphrase too. The configuration copies above are private;
never attach them to an issue. The updater's retained backup is local recovery,
not a replacement for an off-machine backup. Project workspaces are not reverted
by the updater; back them up separately as needed.

## 3a. Main installed from a source bundle

Do **not** rerun `setup.sh` from the new extraction onto an existing old app;
the stale-checkout guard intentionally refuses that. Use rc.2's explicit local
upgrade entry point instead:

```sh
sudo /usr/bin/python3 -I /srv/acc/upgrade-rc2/acc-0.15.0-rc.2/deploy/updates/acc-update \
  --offline --bundle /srv/acc/upgrade-rc2/acc-0.15.0-rc.2.tar.gz \
  --version 0.15.0-rc.2 --yes
```

This uses the same capture exclusion, backup, source swap, installation,
migrations, health check and failure rollback as the normal updater. It verifies
the local signature and archive checksum again and makes no release-discovery
request. Installing dependencies may still need package registries. It retains
the previous source beside `/srv/acc/app` and a full backup under
`/srv/acc/updates/backups/`. The final message includes a **rollback ID**. Record
it privately. Do not run two update mechanisms at once.

Check the command's exit status. A failed install or version/build health check
attempts to restore the previous source and database, reinstall it and check its
health. A failed backup or verification never swaps source. If rollback fails,
stop and preserve the source directories, state file and backup for recovery;
do not retry blindly. Settings → Updates shows the last transaction result.

## 3b. Main installed from git

The bundle helper refuses a `.git` installation. After the backup above, use
your existing reviewed upstream/branch and its usual fast-forward update:

```sh
cd /srv/acc/app
sudo git rev-parse HEAD
sudo git status --short
sudo git pull --ff-only
sudo bash deploy/install.sh
```

Record the first command's commit for rollback. Stop if there are local changes,
if the pull cannot fast-forward, or if the updated source is not the intended
release. A signed release checksum authenticates the source asset, **not** this
git pull; independently review your upstream and commit. The installer applies
migrations and restarts services. For a schema-changing git rollback, stop all
database users, restore the pre-update database and matching secrets from step 2
without leaving failed WAL/SHM files beside it, restore the recorded source
commit, then rerun its installer. Follow
[Upgraded by hand](/User-Guide/Updating#troubleshooting) for the recovery cautions.
Do not run older code against a migrated database.

## 4. Check the main before proceeding

In **Settings → Updates**, confirm **0.15.0-rc.2**, the published `BUILD`, and the
successful last result. Then choose **Release channel: Release candidates**
for future signed RC updates. RC installs default to this channel; **Stable**
never offers a prerelease. Automatic installation remains stable-only; choose
**Back up and update** yourself for an RC. No newer release being available
does not mean the manual installation failed.
From the main's administrator shell:

```sh
sudo curl --fail --silent --show-error --unix-socket /run/accd.sock http://localhost/api/health
sudo systemctl is-active accd.service
sudo systemctl is-active accd.socket
```

Health must report the intended version and build. Reopen the browser, check
projects, Inbox, teams and backup targets, and resume scheduled work deliberately.

## 5. Linux and WSL nodes

An rc.1 node's old helper can reject a correct new bundle because its tree
exclusion rules differ. **Do one manual reinstall from the verified rc.2 bundle**
to replace that helper; do not depend on Host's **Update** button for this first
hop. On WSL use the node's Linux shell. Verify the release files on that machine
as in step 1, unpack into a fresh directory outside `/opt/acc/app`, then run:

```sh
sudo mkdir -m 0700 /var/backups/acc-node-before-rc2
sudo cp -a /opt/acc/app /etc/acc-node.env /etc/acc/broker /var/lib/acc-node /var/backups/acc-node-before-rc2/
tar --extract --gzip --file acc-0.15.0-rc.2.tar.gz --no-same-owner
sudo bash acc-0.15.0-rc.2/deploy/node/install.sh --update --install-prereqs
sudo systemctl is-active acc-node.service
```

This preserves the node configuration, enrollment and configured agent accounts;
`--update` never creates a new default agent account. Do not supply an enrollment
code or remove `/etc/acc-node.env`. Keep a private copy of the installed source,
`/etc/acc-node.env`, `/etc/acc/broker` and `/var/lib/acc-node` before this
manual operation. It does not have the main bundle transaction's automatic
rollback. After this one-time helper refresh, Host's **Update** and automatic
following can synchronize the main's exact installed version/build, including
RCs, through the enrolled-primary trust path. Nodes do not independently choose
a newer GitHub release. macOS keeps its manual installer route.

## 6. macOS nodes

As the signed-in node user, **without sudo**, verify and unpack the same release
files and run the Mac installer from the fresh extraction:

```sh
(umask 077; mkdir "$HOME/acc-node-before-rc2")
cp -a "$HOME/Library/Application Support/ACC" "$HOME/acc-node-before-rc2/"
mkdir "$HOME/acc-node-before-rc2/LaunchAgents"
for plist in "$HOME/Library/LaunchAgents"/local.acc.*.plist; do
  if [ -f "$plist" ]; then cp -p "$plist" "$HOME/acc-node-before-rc2/LaunchAgents/"; fi
done
tar --extract --gzip --file acc-0.15.0-rc.2.tar.gz --no-same-owner
bash acc-0.15.0-rc.2/deploy/node/macos/install.sh
```

Beforehand, keep a private copy of `~/Library/Application Support/ACC` and the
node's `~/Library/LaunchAgents/local.acc.*` plists that exist on your Mac. Do not
remove the installed enrollment or use a new pairing code. The installer keeps
existing node settings, refreshes the app and restarts its LaunchAgents. Mac
nodes use this manual route; do not assume Linux's root update helper is present.

For either node type, wait for **Host → Machines** to show it connected with the
same version/build as the main. Check Providers and launch a small test session.
For manual node rollback, retain the failed files, restore the saved source and
configuration privately, and rerun the matching old installer (Linux/WSL:
`--update`; Mac: signed-in user). Do not overwrite newer project work or rotate
an enrollment merely to retry an update.

## Roll back a bundle main

Stop new work first. Rollback discards database changes made since the upgrade,
so preserve a current backup and agree the recovery point with other people.
The helper also takes a new backup before restoring the earlier database.
Use the rollback ID printed by the successful upgrade, replacing `<rollback-id>`:

```sh
sudo /usr/bin/python3 -I /srv/acc/upgrade-rc2/acc-0.15.0-rc.2/deploy/updates/acc-update \
  --offline --rollback <rollback-id> --yes
```

Only a matching current version/build and an unused retained transaction may be
reverted. It restores the old source and database, runs the old installer and
checks the old version/build health. Existing secrets are preserved by install;
if secrets were changed after upgrade or their pairing is uncertain, stop and
restore the complete matching backup using your independent recovery procedure.
External configuration, workspaces, external services and node updates are not
rolled back. Check `/api/health` and the browser again. Keep the staging kit,
previous source and backup until the release is accepted; remove them only after
a separate administrator review of recovery needs and disk space.
