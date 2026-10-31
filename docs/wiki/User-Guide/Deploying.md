# Deploying

A deployment path for a small team: one Linux server, a browser on each person's
computer or phone, and optional additional machines for agent sessions.
Use the linked install pages for exact commands, platform requirements and screenshots.

**Draft for the planned public release candidate, v0.15.0-rc.2.** This page does
not announce that downloads are available. Use the reviewed source supplied for
your candidate; do not substitute an unverified archive or an older installer.

## Before you start

Have an administrator who can recover the server from its console, a private
network or VPN, and somewhere separate to keep backups and recovery material.
Choose a stable HTTPS name, for example `rynhult.example.com`, that all your
devices and additional machines can resolve and reach.

| Part | Recommended starting point |
|---|---|
| Main Rynhult | Ubuntu 24.04+ or Debian 13+, systemd, amd64 or arm64, reliable storage and power |
| People | A browser on Windows, macOS or Linux; the phone web app uses the same server |
| Additional machines | Linux, a macOS node, or Windows 11 through WSL2 |
| Remote connection | Tailscale or another administrator-managed VPN |
| Control-state backups | A separate Linux backup receiver, optionally a second S3-compatible target |
| Workload backups | A separate plan for repositories, uncommitted files, application databases and volumes |

The main is the Linux server, not a desktop app. Native Windows, standalone
macOS/Linux desktop apps and native iOS/Android apps are later work, not
candidate downloads promised here. A Mac node and a Windows WSL node are
existing session machines, not those deferred apps.
See [platform scope](/User-Guide/Install#v10-platform-scope).

Start with SQLite. PostgreSQL and [manual high availability](/User-Guide/High-Availability)
need a separate database/files/secrets recovery plan; the SQLite capture and
**Back up and update** paths do not back up PostgreSQL. There is no automatic failover.

## 1. Prepare private access and HTTPS

1. Connect the server and users' devices to your LAN or VPN. With Tailscale,
   authorize the devices and grant only the needed users access to the server.
   If you use a subnet router, its advertised route also needs administrator
   approval. Sharing a device does not make an otherwise unreachable LAN
   address reachable.
2. Make your chosen name resolve to an address reachable from those devices.
   Test from another computer and from the phone with Wi-Fi off and the VPN on.
   The same name must match the server's certificate. A name resolving to a LAN
   address may work at home but fail for a remote teammate.
3. Choose certificate trust in [Install](/User-Guide/Install#main-rynhult-on-linux):
   a Cloudflare zone with DNS-01 issuance, or Caddy's private certificate
   authority. DNS-01 proves domain ownership through DNS; a publicly trusted
   certificate does **not** make the dashboard publicly accessible.
   Cloudflare mode needs the reviewed Caddy module/build described there.
4. With a private authority, install its public root certificate on every
   client that needs it, following [Connect your phone](/User-Guide/Connect-Your-Phone).
   Verify it through your administrator. Never distribute its private key or
   turn certificate verification off.

Use the VPN path for remote work. **Direct public remote access is a later,
separately qualified Preview**, off by default; saving its settings or pairing
a device does not enable public routing. Do not open the dashboard to the
internet or put it behind an unqualified tunnel as a shortcut.
[Remote access](/User-Guide/Remote-Access) explains the boundary.
Other VPNs must deliver traffic through the private addresses allowed by the
installed HTTPS edge; have the administrator check routing and policy rather
than removing that policy.

## 2. Install and prove the first session

1. Follow [Main Rynhult on Linux](/User-Guide/Install#main-rynhult-on-linux)
   from the reviewed candidate source. Keep local console access and the
   install log private.
2. Complete [Getting started](/User-Guide/Getting-Started). The first setup
   token is private; never put it in a ticket or shared terminal recording.
3. On [Providers](/User-Guide/Host-and-Providers#providers), check the named
   **Machine** and **Agent account** before signing in. Browser users and
   operating-system agent accounts are different identities. Agent accounts
   should not have sudo, Docker or administrator access.
4. Create a small [project](/User-Guide/Projects) or [workspace](/User-Guide/Workspaces),
   then start a [session](/User-Guide/New-Session). Confirm its machine, terminal,
   activity and a harmless file edit. Sign in to GitHub separately if the
   project needs it. A green dashboard alone does not prove a session can launch.

**What you should see:** a working browser session on the intended machine and
account, without a certificate bypass. If it fails, start with
[Troubleshooting](/User-Guide/Troubleshooting), not a permission reset.

## 3. Add teammates and phones

An owner follows [People and sign-in](/User-Guide/People-and-Sign-in) to invite
each person with the intended role and agent account. Send one-use invitation
links privately. Use member access unless the person needs installation-wide
owner controls. Sign in to providers for the account that will actually run
that person's sessions.

Follow [Connect your phone](/User-Guide/Connect-Your-Phone) for network access,
certificate trust and installing the web app. Test a notification on the actual
phone after granting permission. Push delivery depends on that device and
browser; it is not a substitute for checking machine and backup health.

## 4. Add session machines

Use **Host → Machines → Add machine** on this main, then the matching
[Linux](/User-Guide/Install#additional-machines),
[macOS](/User-Guide/Install-macOS) or
[Windows WSL](/User-Guide/Install-Windows#add-this-pc-before-v10-no-installer)
instructions. Candidate builds offer source commands where a release installer
is unavailable. Enrollment codes work once and expire after ten minutes.
Nodes connect outward to the main; the main cannot wake or dial an offline node.

For Windows, initialize the intended Ubuntu distribution, enable systemd if
needed, and review [optional WSL isolation](/User-Guide/Install-Windows#isolation-for-dedicated-wsl-nodes)
before running agent sessions. Without isolation they may run Windows programs
and access your Windows files. The optional keep-running task does not prevent
Windows sleep or keep the node online after sign-out.

On a Mac, sessions have the signed-in user's access; do not assume a separate
Linux-style restricted account. Follow the Mac prerequisite and sign-in steps.

Select the right [machine role](/User-Guide/Nodes#roles-and-offline-machines):
**Always-on** for a server expected to remain available, **Workstation** for a
laptop or PC that sleeps. Set the offline threshold and plan known downtime.
Use **Check machines now**, then launch a small session on each new machine.
Do not treat a successful enrollment as proof that its broker and provider work.

## 5. Make recovery independent of the main

1. Set up [Backups](/User-Guide/Backups) on a separate Linux machine. This is
   sometimes called a *vault node*: here that means an encrypted backup receiver,
   not the [Keys](/User-Guide/Keys) API-key vault. Follow the exact owner setup
   commands and **Check setup**; copying the commands does not run them.
2. Keep the receiver's root/maintenance access independent of the main. Its
   append-only writer lets the main add backups without deleting old ones.
   Update that receiver from an independently trusted owner source, not through
   main-controlled root automation. Save an appropriate disk ceiling.
3. Optionally add [S3-compatible storage](/User-Guide/Backups#s3-compatible-storage)
   as another target. Use dedicated credentials. **Backed up** does not mean
   immutable: **Protected** requires the observed retention described there.
   An ordinary S3 bucket or local target is not automatically ransomware-proof.
4. Complete a verified capture. Export a recovery kit containing the target,
   keep copies offline and separately from the repository, and record the
   backup ID **and snapshot ID**, capture time and identity fingerprint off-main.
   The kit plus repository can reveal your secrets; treat both as sensitive.
5. Only then enable scheduled backups. The defaults are daily captures and weekly
   full restore verification; choose hourly captures if you need a shorter recovery
   point. New targets' schedules and independent S3 retention maintenance start off;
   enabling one does not enable the other.
   Schedule maintenance independently and do not give deletion credentials to
   the main just to save space.
6. Follow the [quarterly recovery drill](/User-Guide/Backups#test-recovery-every-quarter)
   on isolated storage. Automatic verification is useful, but not a replacement
   for recovering when the original main is unavailable. Restored data stays
   quarantined; do not remove its hold to try starting it.

Control-state backup is **not a backup of all work**. Separately protect
uncommitted workspaces, provider conversations/logins, application data,
containers and volumes. Review [retention and exclusions](/User-Guide/Backups#retention-and-exclusions).
Receiver root or a compromised storage administrator can still destroy data;
keep an independent offline or immutable copy where that risk matters.

## 6. Operate and update

Check **Host**, **Inbox** and the age of the **last verified** backup regularly.
For a receiver with unavoidable nightly shutdowns, configure
[planned downtime and catch-up](/User-Guide/Backups#backup-age-and-planned-downtime)
and keep another available target. Acknowledging an outage is not repairing it.
Do not rely only on notifications while no one is watching them.

Follow [Updating Rynhult](/User-Guide/Updating) for the install type you use.
Release candidates are not offered by the stable updater; use the documented
manual candidate/source update path. Check for active backup work first:
deployment refuses while capture, upload or maintenance holds its lease.
Wait rather than clearing locks or killing the backup.

After updating, verify the running version, browser access, a small session and
a fresh verified backup. Sync nodes from their main; if an older node cannot
update itself, use its [Host recovery command](/User-Guide/Nodes#update-an-older-machine)
without re-enrolling. Preserve matching database/secrets and the previous source
for the documented rollback, not a mix of old data and new keys.

For failures, use [Troubleshooting](/User-Guide/Troubleshooting).
For administrator service/log details, use [Operations](/Internal/Operations).
Neither this draft nor a passing documentation check qualifies a live deployment.
