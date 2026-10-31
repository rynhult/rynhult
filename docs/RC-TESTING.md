# Testing Rynhult 0.15.0-rc.2

Thank you for testing. This page is for anyone testing the second public release candidate. The short version you
tick through is [RC-TESTING-CHECKLIST.md](RC-TESTING-CHECKLIST.md).

## What rc.2 is

Rynhult runs coding agents (Claude Code, Codex, GitHub Copilot CLI, Cursor, Antigravity, OpenCode) on your own
machines and lets you watch and steer them from a browser or your phone. rc.2 is the second **release candidate** of
the next server release, numbered 0.15.0, with the v1 scope: the main Rynhult with its dashboard, plus existing Linux, macOS and
Windows-through-WSL nodes. Browser access on any client platform, including the phone web app, is part of v1.

Native applications come after v1.0, one at a time, in an order not chosen yet.
The candidates are native Windows, macOS standalone, Linux desktop, iOS and Android. rc.2 does not start native Windows work. Desktop/mobile code in the source is experimental groundwork,
not part of the v1 app release; testing it is not required for this server candidate.
See [the scope decision](decisions/0073-v1-linux-server-scope.md).

- It is a test build. Install it on a machine you can wipe, ideally a virtual machine, not on a machine you depend on.
- Get the source assets from the public `v0.15.0-rc.2` prerelease at
  https://github.com/rynhult/rynhult/releases/tag/v0.15.0-rc.2 when published.
  An installed Rynhult never offers prereleases as automatic updates.
- After the one-time upgrade from rc.1, **Settings → Updates → Release channel**
  can offer the next signed candidate when set to **Release candidates**. RC
  installs default to that channel. Stable never offers prereleases; automatic
  installation remains stable-only. Choose **Back up and update** yourself for an RC.
- rc.2 is a **server** release candidate: the download is the source tarball only. There are no Windows or Mac
  installers and no desktop or phone apps. Run the main Rynhult on Linux. Add Windows (WSL2) and Mac machines from
  source (below), and use the dashboard in a browser or install it as an app from the browser's menu.

Orchestrator v2 is **on by default**, not a Preview. You still grant a bounded team when launching With helpers;
turning the feature on does not start a team or approve execution of branch code. See the [Orchestrator Guide](wiki/User-Guide/Orchestrator.md).

## Preview and off by default

**Off by default** means the feature is in rc.2 but stays off until an owner turns it on. **Preview** means it works
but hasn't been through 1.0's qualification. Leave these alone unless you want to test one on purpose, and say so in
your reports.

| Feature | Status |
|---|---|
| Agent sessions in containers | Preview, off |
| DNS providers other than Cloudflare (Azure DNS, Route 53, Google Cloud DNS, DigitalOcean, Hetzner) | Preview |
| Git backups over a public address for roaming machines | Preview, off |
| Remote access without a VPN | Preview, off |
| Company sign-in (OpenID Connect) | Optional pilot, off |
| Group machine accounts | Off |
| S3 retention maintenance on a separate host | Off |
| Bringing a backup up on a replacement main | Off; no switch in the dashboard |
| Automatic failover | Dry run only: it shows what it would do |
| Usage statistics and bug reports | Off until you opt in (see below) |
| Installing updates automatically | Off |

## Before you install

- A Linux machine with systemd, amd64 or arm64, and an account for yourself with `sudo`.
- The download: `acc-0.15.0-rc.2.tar.gz`, `BUILD`, `SHA256SUMS` and `SHA256SUMS.asc`, in the same folder.
- Optional: a domain on Cloudflare with an API token (**Zone → DNS → Edit**). Without one, Rynhult makes its own
  certificate, and you add its root to your devices (Guide → Install → *Trust the certificate on your devices*).

First [download the public key and compare both full fingerprints](../SECURITY.md#verify-a-source-release).
Import it only after that comparison. Only `SHA256SUMS` is signed; the in-app
updater does not verify signatures yet.

Check the signature before checking the downloads:

```sh
gpg --verify SHA256SUMS.asc SHA256SUMS && sha256sum -c SHA256SUMS
```

Stop if the signature is missing or either check fails. After both succeed:

```sh
tar -xzf acc-0.15.0-rc.2.tar.gz
```

`sha256sum` must print `OK` for both `acc-0.15.0-rc.2.tar.gz` and `BUILD`. If it doesn't, stop and tell us. The archive unpacks into a
folder called `acc-0.15.0-rc.2`.

## Install the main Rynhult

### Ubuntu 24.04 or newer, Debian 13 or newer (supported)

```sh
sudo acc-0.15.0-rc.2/deploy/acc-install
```

Choose **Main Rynhult** and answer the questions; each shows a default you keep with Enter. The installer copies
itself to `/srv/acc/app` and takes a few minutes. Its log is `/var/log/acc-install.log`.

To see what it would do first, without changing anything: `acc-0.15.0-rc.2/deploy/acc-install --main --dry-run`.

### Fedora, Enterprise Linux, openSUSE and Arch (unqualified tests)

These distributions aren't qualified for 1.0. You can still try a test install, and reports are very welcome:

```sh
sudo acc-0.15.0-rc.2/deploy/acc-install --main --unqualified-distro
```

- The wizard forwards this explicit opt-in to setup. Minimal machines need curl, tar/gzip, git and CA certificates before bootstrap; see [prerequisites](wiki/User-Guide/Install.md#before-you-start).
- EL package sources are fingerprint-pinned. openSUSE's broad default sudo rule must exclude agent accounts; follow the administrator guidance in the [Install Guide](wiki/User-Guide/Install.md#agent-accounts-must-have-no-sudo-authorization).
- On Arch, update the whole system first (`sudo pacman -Syu`); never only `pacman -Sy`.
- Project stacks use Podman, and Claude Code is installed from Anthropic's pinned release instead of a package.
- On Fedora, SELinux stays enforcing. If something is refused, send us the output of `sudo ausearch -m AVC -ts recent`.
  Never turn SELinux off to get past it.

### First sign-in

When the installer finishes, it lists a DNS entry to add for Rynhult's name and how to read the one-time setup
token:

```sh
sudo cat /srv/acc/data/secrets/setup_token
```

Open Rynhult in a browser and paste the token. Scan the QR code with an authenticator app, choose a username and a
password of 12 characters or more, enter the 6-digit code, and select **Finish setup**.

### Additional machines

Add them from **Host → Machines → Add machine**:

- **Linux:** copy the command from the **Linux** tab and run it on the machine.
- **Windows (WSL2) and Mac:** there are no installers in rc.2. The tabs show commands that install from your main
  Rynhult's own source, with the steps around them.

## Upgrade from rc.1

Follow [Upgrade from rc.1](wiki/User-Guide/Upgrade-from-rc1.md) for the one-time
manual step: back up and verify signed rc.2 files, upgrade the main using the
source-bundle transaction or reviewed git route, verify its health/version/build,
then refresh node helpers one machine at a time. rc.1 cannot discover rc.2 in-app.
After this hop, select **Release candidates** for future signed in-app RC updates.

## Upgrade from 0.14

The database is upgraded when Rynhult starts; you don't run anything for it. You do move the new files into place
by hand, because the old 0.14 release checker cannot offer this candidate. These steps do what the built-in updater does.

1. **Back up the database and secrets:**

   ```sh
   sudo systemctl start acc-backup.service
   sudo ls /var/backups/acc/$(date +%F)
   ```

   If it fails with *Failed to set up mount namespacing: /var/backups/acc: No such file or directory*, your 0.14
   install never made a backup (a fresh 0.14 install didn't create that folder). Create it and run the backup again:
   `sudo install -d -m 0700 /var/backups/acc && sudo systemctl start acc-backup.service`.

   The listing must show `acc.db` and `secrets` (or `secrets.tar.enc`). If `/etc/default/acc-backup` sets
   `ACC_BACKUP_DIR`, look there instead. If you use PostgreSQL instead of the default
   SQLite, back it up with your own `pg_dump` first; this backup doesn't cover it.
2. **Check and unpack the download** (see *Before you install*), then put it beside the installed copy:

   ```sh
   sudo mkdir /srv/acc/app.rc2
   sudo tar -xzf acc-0.15.0-rc.2.tar.gz -C /srv/acc/app.rc2 --strip-components=1 --no-same-owner
   sudo chown -R root:root /srv/acc/app.rc2
   sudo chmod -R go-w /srv/acc/app.rc2
   ```

3. **Swap them**, keeping 0.14 beside it:

   ```sh
   sudo mv /srv/acc/app /srv/acc/app.0.14
   sudo mv /srv/acc/app.rc2 /srv/acc/app
   ```

4. **Run the new setup.** It keeps your settings in `/etc/default/accd` and asks nothing. The dashboard may be
   unavailable for a few minutes while it runs; sessions keep running.

   ```sh
   sudo /srv/acc/app/deploy/setup.sh --yes
   ```

5. Open Rynhult. **Settings → Updates** should show **Rynhult 0.15.0-rc.2**.
6. **Update your other machines one at a time.** An old Linux/WSL helper needs
   one manual installer rerun from the verified rc.2 source. Follow the
   [node upgrade steps](wiki/User-Guide/Upgrade-from-rc1.md#5-linux-and-wsl-nodes),
   keeping enrollment and agent accounts. After refreshing the helper, Host's
   **Update** can synchronize the main's exact build, including RCs. Macs use
   their signed-in user's installer, without sudo.

Don't run `deploy/install.sh` from the unpacked folder somewhere else: the services keep running `/srv/acc/app`,
so you'd get a mix of both versions.

### If the upgrade goes wrong

Stop and report what happened. Include only reviewed, non-secret excerpts from `/var/log/acc-install.log`,
not whole logs containing tokens or private details. To go back:

1. Stop Rynhult: `sudo systemctl stop accd.service accd.socket`.
2. Restore the database and secrets from the backup from step 1.
3. Put 0.14 back: `sudo mv /srv/acc/app /srv/acc/app.rc2-failed`, then `sudo mv /srv/acc/app.0.14 /srv/acc/app`.
4. Run 0.14's setup again, which also puts back its helpers and services: `sudo /srv/acc/app/deploy/setup.sh --yes`.

A schema downgrade isn't the way back: some downgrades drop data that only the newer schema had.

## Your first 30 minutes

Do these in order and note anything that's confusing, slow or wrong. Exact steps are in **Guide → Getting started**
inside Rynhult.

1. **Overview.** The **Get started** list at the top shows what's left to do.
2. **Sign in an agent.** **Providers**: sign Claude Code, Codex or another agent CLI in for your agent account.
3. **Add a project.** **New project**: clone a small repository of your own, or make an empty folder.
4. **Start a session.** **New session**: write a small task, then select **Launch**, or **Trust and launch** the
   first time. Watch the terminal; type into it.
5. **Close the browser and come back.** The session keeps running; open it from the sidebar.
6. **Look at the work.** On the session's page, open **Changes** and **Activity**.
7. **Answer a question.** When an agent asks something, it shows under **Needs you** and in **Inbox**. Answer it.
8. **Phone.** Open Rynhult on your phone, install it from the browser's menu, and turn on notifications
   (Guide → *Connect your phone*).
9. **Search.** Find a word from your session with **Search**.
10. **Version.** Check that **Settings → Updates** shows **Rynhult 0.15.0-rc.2** and a build.

## Report a bug

- **From inside Rynhult (optional):** if you opted in under **Settings → Usage and bug reports**, review
  and send the in-app report when one is available. Nothing is sent without your approval.
- **RC bugs:** [open a public issue](https://github.com/rynhult/rynhult/issues/new). No repository invitation or
  telemetry opt-in is needed. If you cannot file an issue, contact [support@rynhult.dev](mailto:support@rynhult.dev).
- **Questions and experiences:** use [Discussions](https://github.com/rynhult/rynhult/discussions).
- **Security problems:** privately, to [security@rynhult.dev](mailto:security@rynhult.dev). Never in a bug issue.
  See [SECURITY.md](../SECURITY.md).

Use the [RC issue template](rc/ISSUE_TEMPLATE.md). In each issue, say:

- the version and build from **Settings → Updates**;
- your distribution and version, and whether it's the main Rynhult or an additional machine;
- what you did, what you expected, and what happened;
- whether you turned on any Preview or off-by-default feature.

Leave out passwords, tokens, keys, real hostnames and addresses, and session transcripts. A screenshot is fine if
nothing private shows in it.

## Usage statistics and bug reports (optional)

Both are off until an owner turns them on. Neither is required to test Rynhult or report a public issue.
Read [the privacy note](PRIVACY.md) and the privacy notice linked in Settings before opting in.
Scrubbing is best effort; always review the exact report before sending.

1. Open **Settings** and find **Usage and bug reports**.
2. Select **Show exactly what would be sent**. This only shows it; nothing is sent.
3. Tick **Share usage statistics**, **Offer bug reports for review**, or both, then select **Save privacy choices**.

**Usage statistics** are one summary a day, with:

- the version (`0.15.0-rc.2`), the kind of install, and which operating systems your machines run;
- rough counts in ranges such as `2-3`: machines, people, projects, running sessions, keys;
- which agent CLIs and which of a fixed list of features are turned on;
- how often fixed events happened that day (sessions launched, searches, merges and so on), also as ranges;
- the UTC day, and an installation id that changes every 90 days.

It never contains prompts, transcripts, terminal output, file or project names, paths, hostnames, addresses,
usernames or emails, key names or exact counts.

**Bug reports** are Rynhult's own error details with a scrubbed error message, never terminal output or agent
replies. **No report sends itself:**

- each one waits under **Reports awaiting review** until you select **Send this report** or **Don't send**;
- **Remove message** deletes the message text first, if you'd rather not send it;
- unanswered reports expire after seven days.

During the release candidate, reports go to the project's staging collector, `telemetry.rynhult.dev`. Everything it
receives before 1.0 is deleted at launch. To stop, untick the choice and save. To also delete what was already sent,
use **Delete data already sent**.

## Uninstall

The clean way out is to wipe the test machine or virtual machine. Before you do, copy anything you want to keep
from `/srv/acc/workspaces` (your projects).

To remove Rynhult from a machine you keep, use the uninstaller. Run it first without options: it lists exactly what
it would remove and what it keeps, and changes nothing.

```sh
/srv/acc/app/deploy/uninstall.sh
sudo /srv/acc/app/deploy/uninstall.sh --yes
```

Run the first line, read the list, then run the second; it removes what the list showed:

- Rynhult's services and unit files, and its drop-ins for Caddy;
- its Caddy sites and settings files, and its AppArmor profiles;
- its helper commands and settings in `/etc`;
- `/srv/acc` except your data;
- the `acc` and `acc-build` accounts, and the agent accounts it created.

Rynhult adds no sudoers files.

It **keeps**:

- your projects (`/srv/acc/workspaces`), Rynhult's database and secrets (`/srv/acc/data`) and git backups
  (`/srv/acc/git`);
- `/var/backups/acc` and the agent accounts' home folders;
- the packages setup installed (Caddy, Node.js, Claude Code and the other agent CLIs) and their apt sources;
- your own account.

An account that still runs processes is skipped, not stopped: archive its sessions in Rynhult first, then run the
uninstaller again.

To delete your data too, add `--purge-data`. It asks you to type `delete my Rynhult data` before it changes anything:

```sh
sudo /srv/acc/app/deploy/uninstall.sh --yes --purge-data
```

The uninstaller is for a main Rynhult on Linux. Tell us anything it left behind: that's useful too.
