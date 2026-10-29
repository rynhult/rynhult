# Telemetry collector operator runbook

Status: **deployed as a pre-release staging service** since 2026-10-26, on Akamai/Linode in Frankfurt (EU), serving
privacy notice 0.2 (#335); this repository holds notice 0.4 (`deploy/telemetry/privacy.html`), which the next deploy
serves. Data received before the v1 launch is test data and
is deleted at launch. The client is not yet qualified end to end against it. The project owner hosts and controls
`telemetry.rynhult.dev`. No third-party analytics or error service is involved.
The client contract and consent remain unchanged. This service must run separately from ACC,
its secrets, release infrastructure and user workloads.

## Deployment boundary

Use a dedicated Ubuntu 24.04+ VM with systemd, Python 3.12+, and Caddy 2.6+.
The collector runs as `acc-telemetry` with no login shell. Only its private state and runtime
directories are writable; systemd makes the rest of its filesystem read-only. Caddy runs under
its packaged `caddy` account and terminates TLS. The collector has no TCP listener or outbound
network access (`IPAddressDeny=any`, Unix sockets only): Caddy reaches its Unix socket. The
collector has its own `acc-telemetry` group; Caddy joins it through `SupplementaryGroups` in its
drop-in, so a compromised collector gains nothing of Caddy's (TLS keys). The socket directory is
accessible only to those two accounts. Those accounts and root are trusted; do not grant other
users membership in `acc-telemetry` or put untrusted services on this VM.

Caddy replaces incoming forwarding headers with the actual peer address. Uvicorn trusts forwarding
headers only because its sole listener is the protected socket. Do not expose that socket, add
workers, enable debug/access logs, put a CDN/proxy in front, or copy these forwarding settings to a
TCP listener. One worker holds bounded IP/key limits; restarts reset these counters. A distributed
attack can create signing keys, so signatures do not establish genuine installations.

Before launch: publish a controller name/contact and privacy notice, confirm the hosting agreement
and region, require MFA for operator SSH access, enable encrypted storage, and arrange monitoring
of disk space, TLS expiry and availability. Monitor at least hourly; alert before 80% disk use and
on a stopped/restarting service or `Collector retention failed`. Budget a small dedicated data
volume and monitor its growth: daily registration limits are not a global disk quota.
For the staging service, the controller name, contact and privacy notice are published (#335). This
repository doesn't record whether MFA, encrypted storage and monitoring are done; confirm them before the v1
launch. The owner confirmed (2026-10-27) that the staging collector has **no backups** beyond the VM's own disk, and
notice 0.4 says so; it keeps the data-processing agreement with Akamai.

## Install and first start (target VM only)

These are future operator commands, **not actions taken by this task**. Start with a freshly
provisioned dedicated VM and a reviewed checkout of this commit in your operator home directory.
Run commands from that checkout. Every privileged step is explicit below. The installation
script creates the account, root-owned code/venv, private data directory, unit and Caddy resource
limit drop-in, and reloads systemd. Dependencies install from `requirements.txt`, which pins every
package including transitive ones by SHA-256, as wheels only (`--require-hashes --only-binary :all:
--no-deps`), so no download is unverified and no package build script runs as root. Rerunning the
script is safe. Regenerate the lock deliberately (the command is in its first line) and review it. It does not start services or overwrite the Caddyfile.

1. Install prerequisites on the target VM. Package installation may start packaged Caddy;
   stop it before configuring this dedicated host. Review the installed packages as usual.

   ```sh
   set -eu
   sudo apt-get update
   sudo apt-get install --yes python3 python3-venv caddy curl
   sudo systemctl stop caddy
   sudo sh deploy/telemetry/install.sh
   ```

2. Point the domain's A/AAAA records at this VM. At the provider firewall, allow TCP 80 and 443
   publicly (ACME and HTTPS), and SSH only from your administration network. Do not open any
   collector application port. Remove an AAAA record if IPv6 does not reach this VM. No ACC host
   publishing or machine configuration is part of this task.

3. Install the privacy notice and the dedicated-host Caddyfile. The notice is
   `deploy/telemetry/privacy.html`: controller, contact, legal basis (consent), what is collected,
   keys, connection handling, retention, rights and hosting (Frankfurt, EU), marked as a pre-release
   staging service whose data is all deleted at the v1 launch. It is one
   self-contained page (no scripts or external resources). The Caddyfile's Content-Security-Policy
   allows its inline stylesheet by SHA-256 hash, so after editing the stylesheet update that hash
   (`tests/test_telemetry_privacy_notice.py` fails until they match). These commands replace the
   VM's Caddy configuration; do not use them on a shared host.

   ```sh
   set -eu
   sudo install -d -m 0755 /var/www/acc-telemetry
   sudo install -m 0644 deploy/telemetry/privacy.html /var/www/acc-telemetry/index.html
   sudo install -m 0644 deploy/telemetry/Caddyfile /etc/caddy/Caddyfile
   sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   sudo systemd-analyze verify /etc/systemd/system/acc-telemetry.service
   sudo systemctl enable --now acc-telemetry.service
   sudo systemctl enable --now caddy.service
   ```

4. Verify HTTPS from outside the VM, the privacy page, schema, a signed synthetic send/deletion
   using a disposable installation, and a 16,385-byte request returning 413. Inspect the VM to
   confirm no application TCP listener, 0700 data directory/0600 database, empty capabilities,
   resource limits and read-only filesystem restrictions. Check `systemctl status acc-telemetry`
   and `systemctl show acc-telemetry -p MemoryMax -p CPUQuotaPerSecUSec -p TasksMax`.
   Confirm no request body, address, URL query or signature is retained in any journal, provider
   flow/access log or monitoring agent. Do not enable client sending before qualification.

   ```sh
   curl --fail https://telemetry.rynhult.dev/
   curl --fail https://telemetry.rynhult.dev/v1/schemas
   sudo journalctl -u acc-telemetry.service --since '1 hour ago'
   ```

Caddy access logging is disabled; its default runtime logger discards records because proxy error
records can contain addresses. Uvicorn's log configuration suppresses request and exception logs;
only the collector's generic retention error is enabled. This sacrifices detailed proxy diagnostics.
Use availability/resource monitoring without request capture. Never turn on verbose logging against
real traffic. Caddy manages ACME certificates; preserve its private state with the VM's normal secure
certificate backup procedure, separate from collector data.

The configuration uses Caddy's documented [body size cap](https://caddyserver.com/docs/caddyfile/directives/request_body)
and [global log/timeouts options](https://caddyserver.com/docs/caddyfile/options).

## Limits, retention and export

The closed, canonical JSON schemas reject unknown fields at every model boundary and unknown
counter names. Authentication signs timestamp, path and exact bytes. Limits: 4,096 bytes usage;
16,384 bytes reports/deletion; Caddy additionally caps all request bodies at 16,384 bytes, headers at
8,192 bytes and body reads at 15 seconds. Compressed bodies are not decoded or accepted as JSON.
Per IP: 60 requests per clock hour, 20 new keys per UTC day. Per authenticated key: 60 requests
per clock hour across endpoints, independent of IP and raw-data deletion; at most two retained
usage days and ten reports received in a rolling day. Retries for an existing usage day/report
UUID are idempotent. IPv6 clients share one per-address bucket per /64. The collector refuses
new data (429) once the live database reaches `ACC_COLLECTOR_MAX_BYTES` (default 1 GiB, set it in a
unit drop-in to fit the volume); deletion and retention keep working. All three in-memory maps have at most 10,000 entries each; full maps refuse
new entries. Addresses remain only in memory until the current hour/day ends (or process exits).
The collector worker is limited to 256 MB, half a CPU, 32 tasks and 256 file descriptors; Caddy
has separate limits. Resource exhaustion can cause temporary refusal rather than unbounded growth.

The ASGI lifespan runs retention at startup and hourly, even without traffic; database errors
emit a generic error and retry in 60 seconds. Writes also run retention. Raw usage expires after
90 days from receipt, reports after 180 days; exact cutoff timestamps are retained until the next
pass. Monthly counts cover distinct signing key ids per OS, provider and feature. Months finalize
after the seven-day late-arrival window (00:00 UTC on the eighth), then become immutable and
contain no ids. Retain the last 13 completed calendar-month labels (the cutoff month inclusive).
Raw-data deletion cannot subtract from finalized unlinked totals. Downtime or deleting raw rows
before finalization can reduce counts; do not claim these are a census. Rotation and fabricated
keys mean the threshold counts pseudonymous installations, not verified people.

Run the local maintenance/export command as the service user. No admin HTTP endpoint exists:

```sh
sudo -u acc-telemetry env PYTHONPATH=/opt/acc-telemetry \
  /opt/acc-telemetry/venv/bin/python -m acc.telemetry_collector_ops \
  --db /var/lib/acc-telemetry/telemetry.sqlite retain
sudo -u acc-telemetry env PYTHONPATH=/opt/acc-telemetry \
  /opt/acc-telemetry/venv/bin/python -m acc.telemetry_collector_ops \
  --db /var/lib/acc-telemetry/telemetry.sqlite export
```

Export omits every figure below ten installations, including months with no eligible figures;
there are no grand totals, cross-tabulations or suppression counts to subtract. Do not publish
raw monthly rows, combine exports into smaller inferred groups, or publish intermediate raw-data
queries. Public statistics publication remains a later owner decision.

## Rotation and upgrade

ACC generates separate usage/report keys and rotates each every 90 days. The collector accepts
new keys without linking them to retired keys. It never accepts an alias/previous-key field.
ACC keeps retired private keys for deletion; a signed old-key deletion can erase only that key's
raw rows. To erase an installation across rotations, send deletion with every retained key in
both streams (the existing ACC deletion action does this). Lost private keys cannot be recovered
by the collector. An operator request needs the installation key id(s), not a hostname or email.

Before upgrading, take a backup, stop the worker, check out the reviewed version and rerun the
installer. Do not change wire schemas without a consent/version review. Keep the prior reviewed
checkout for rollback; the current change adds no database columns.

```sh
set -eu
sudo systemctl stop acc-telemetry.service
sudo sh deploy/telemetry/install.sh
sudo systemctl start acc-telemetry.service
```

A rollback uses the same stop/install/start sequence with the prior checkout. Review data/schema
compatibility first. Never restore a pre-deletion snapshot merely to roll code back. If host/admin
credentials are compromised, rotate those credentials and Caddy TLS material; the collector holds
public installation keys only and has no shared client signing secret to rotate.

## Back up and restore

**Staging: no backups.** The staging collector has no backups beyond the VM's own disk, and privacy notice 0.4
says so. Don't turn on provider backups or snapshots, or run the commands below, without first publishing a notice
that describes them (where, how long, encrypted).

Use the SQLite online backup API, never `cp` of a live database. Backups contain sensitive report
text and must be encrypted off-host with access restricted to operators. Keep the backup
destination in the EU: the privacy notice says data isn't stored outside it. Keep for **at most 30
days**; configure the backup destination's expiry and verify it. Use timestamped filenames, never
reuse one: backup refuses overwrite/symlinks. The example directory must be private and owned by
the collector. Keep encryption/repository credentials outside the collector's filesystem.

```sh
sudo install -d -o acc-telemetry -g acc-telemetry -m 0700 /var/lib/acc-telemetry/backups
sudo -u acc-telemetry env PYTHONPATH=/opt/acc-telemetry \
  /opt/acc-telemetry/venv/bin/python -m acc.telemetry_collector_ops \
  --db /var/lib/acc-telemetry/telemetry.sqlite backup \
  /var/lib/acc-telemetry/backups/2026-10-24.sqlite
```

Encrypt it off-host with your backup tool, verify the copy, then remove the local plaintext file:
local copies are outside the destination's expiry. `sudo rm /var/lib/acc-telemetry/backups/2026-10-24.sqlite`.

For erasure requests, delete affected backup generations within 30 days (or sanitize them with
the same deletion command). The collector keeps an erasure ledger: the key id (and report id, for
named reports) of every self-service and operator erasure, without payloads, for 45 days, longer
than any backup may exist. Write it with the `erasures` command; restore replays it. The ledger
travels inside later backups too. The rule stays: **never restore a raw snapshot to production
unless all erasures since its creation are known and replayed**; if neither the live database nor a
newer backup is available to supply the ledger for the whole interval, discard that snapshot. No restore procedure may resurrect erased data.
A backup is for disaster recovery only when this requirement is met. Consider using no raw-data
backups if maintaining this separate history is impractical.

Restore to a new filename while stopped. Copy the encrypted snapshot into the private state
directory after decrypting it using your backup tool; ensure service ownership and mode 0600.
Every plaintext copy (the decrypted file, the copy under `backups/`, `erasures.txt`) is removed
by the steps below.
Write `erasures.txt` from the live database (or, if it is lost, the newest available backup) with
the `erasures` command, and append any verified operator erasures recorded elsewhere. Each line
is `KEY_ID` or `KEY_ID REPORT_UUID`. The restore command requires this file, applies
all listed erasures and current retention, and runs SQLite integrity checking before use.

```sh
set -eu
sudo systemctl stop acc-telemetry.service
sudo install -o acc-telemetry -g acc-telemetry -m 0600 recovered.sqlite /var/lib/acc-telemetry/backups/recovered.sqlite
rm recovered.sqlite
sudo -u acc-telemetry env PYTHONPATH=/opt/acc-telemetry \
  /opt/acc-telemetry/venv/bin/python -m acc.telemetry_collector_ops \
  --db /var/lib/acc-telemetry/telemetry.sqlite erasures /var/lib/acc-telemetry/erasures.txt
sudo -u acc-telemetry env PYTHONPATH=/opt/acc-telemetry \
  /opt/acc-telemetry/venv/bin/python -m acc.telemetry_collector_ops \
  --db /var/lib/acc-telemetry/restored.sqlite restore \
  /var/lib/acc-telemetry/backups/recovered.sqlite \
  --deletions /var/lib/acc-telemetry/erasures.txt
sudo mv /var/lib/acc-telemetry/telemetry.sqlite /var/lib/acc-telemetry/retired.sqlite
sudo rm -f /var/lib/acc-telemetry/telemetry.sqlite-journal
sudo mv /var/lib/acc-telemetry/restored.sqlite /var/lib/acc-telemetry/telemetry.sqlite
sudo rm /var/lib/acc-telemetry/retired.sqlite
sudo rm /var/lib/acc-telemetry/backups/recovered.sqlite
sudo rm /var/lib/acc-telemetry/erasures.txt
sudo systemctl start acc-telemetry.service
```

Stop on any failed command; never swap in a failed restore. The old rollback journal is removed
while stopped so it cannot replay against the replacement database. Do not retain the retired database. Secure deletion uses SQLite DELETE journaling; old WAL databases
are refused. To migrate one, stop every reader/writer, checkpoint/truncate WAL using SQLite, switch
to DELETE mode and securely dispose of old journals/snapshots before restarting. SQL deletion and
file removal cannot guarantee physical-media erasure on snapshots, SSDs or copy-on-write storage.

## Delete on request

Prefer ACC's signed **Request deletion**: it authenticates key ownership and returns a receipt.
For an operator-assisted request, verify control through the published contact procedure and get
every usage/report installation key id, including retired ones. Do not accept an email/hostname
as proof. A report id alone needs manual ownership verification before operator-assisted removal.
The following erases all raw rows and public-key registration for one key:

```sh
sudo -u acc-telemetry env PYTHONPATH=/opt/acc-telemetry \
  /opt/acc-telemetry/venv/bin/python -m acc.telemetry_collector_ops \
  --db /var/lib/acc-telemetry/telemetry.sqlite delete -- INSTALLATION_KEY_ID
```

Repeat for each verified id (the collector's erasure ledger records it), and delete/sanitize
backup generations. Confirm completion within 30 days. Finalized unlinked totals cannot be located
by installation key. Erasure is not a ban: an installation that continues sending can create new
rows; turn off sharing in ACC first. Operator access/audit records should record action and receipt,
without report text, connection addresses or copies of signed headers.

Erase one report (when its owner's control of the key id is verified) the same way, with the ledger:

```sh
sudo -u acc-telemetry env PYTHONPATH=/opt/acc-telemetry \
  /opt/acc-telemetry/venv/bin/python -m acc.telemetry_collector_ops \
  --db /var/lib/acc-telemetry/telemetry.sqlite delete-report -- INSTALLATION_KEY_ID REPORT_UUID
```

## Access and portability on request

A key id isn't secret: it is in every payload the owner previews, and in anything they paste. Verify that the request
comes from the installation's owner first, through the published contact procedure, and get every usage and report key
id, current and retired. Then write everything held for each id (its public-key registration, every payload and its
erasure records; monthly totals carry no key id) to a new private JSON file and send it to the requester:

```sh
sudo -u acc-telemetry env PYTHONPATH=/opt/acc-telemetry \
  /opt/acc-telemetry/venv/bin/python -m acc.telemetry_collector_ops \
  --db /var/lib/acc-telemetry/telemetry.sqlite access -- INSTALLATION_KEY_ID /var/lib/acc-telemetry/access-KEY.json
```

The command refuses to overwrite an existing file. Send the copy over an encrypted channel, then delete the local file.
Answer within one month.

## Version 1 launch: delete the staging data

The privacy notice promises that everything received before the v1 launch is deleted at launch. On launch day, before
the collector accepts v1 data:

1. Stop ingestion: `sudo systemctl stop caddy acc-telemetry`.
2. Delete the database and every copy of it: `/var/lib/acc-telemetry/telemetry.sqlite` and any
   `telemetry.sqlite-journal`, everything under `/var/lib/acc-telemetry/backups/`, `erasures.txt`, any `access-*.json`,
   and every off-host backup generation taken before launch. The monthly totals and the erasure ledger live in the
   same database and go with it.
3. Clear the journal, so no pre-launch log lines remain: `sudo journalctl --rotate`, then
   `sudo journalctl --vacuum-time=1s`. Vacuuming applies to the whole journal, not one unit; on this VM, which runs
   only the collector, that is what you want.
4. Start the collector, which creates an empty database: `sudo systemctl start acc-telemetry caddy`.
5. Publish the privacy notice without the staging banner (a new version and date), and record the wipe's date and
   what was deleted in the operations log.

## Incident response

1. Stop ingestion with `sudo systemctl stop caddy acc-telemetry`. Retention/deletion pauses while
   stopped: record the outage and run maintenance before reopening. For a flood, ask the provider
   to filter traffic without creating address logs; no automatic third-party analytics integration.
2. Isolate the VM, revoke operator access, and assess whether report bodies/backups were exposed.
   Do not copy bodies or IPs to tickets/agent prompts. Treat all submitted text as hostile plain text.
3. For disk pressure, expire old backups and run retention; investigate generic maintenance errors
   using a synthetic isolated copy. Never enable verbose logging on public traffic. A corrupt DB
   needs a qualified restore, not disabling storage security checks or switching to WAL.
4. Rebuild from reviewed source on a clean VM after compromise; restore only qualified data with
   erasures replayed. Rotate administrator/TLS/backup credentials as appropriate. Tell the controller
   promptly so they can assess notification obligations using the published incident procedure.
5. Before reopening, verify TLS, permissions, deletion, retention, export threshold, rate limits and
   logging again. Document times, affected data classes and remediation without sensitive payloads.
