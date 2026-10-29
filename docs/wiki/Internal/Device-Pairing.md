# Device Pairing

Device pairing and public access default to off. The owner apply/off flow uses
a separate uninstalled, root-controlled helper; it never installs itself. The uninstalled root signer owns a
dedicated client-only CA; ACC stores public leaf certificates and metadata.

Migration 0050 adds `device` and `device_pairing` after the shipped main head 0049. Pairing capabilities
are 256-bit random secrets, hashed in the database, valid five minutes. A claim
consumes the link and creates a separate receiving-device receipt. An owner must
confirm a claimed request after recent sign-in verification and typing the
six-digit code shown only on the receiving device. The code derives from both hashed pairing
and receipt capabilities, so it is bound to the winning claim. The pairing list and
detail never return it (otherwise the owner page could post the code back without
anyone reading the device); the confirm API refuses a missing code, and a wrong one
cancels the pairing (`device.pairing_mismatch`) before signing. A phishing relay that
already holds a leaked link can still show its own claim's code to the person; the
code protects against careless confirmation, not a person who reads out an
attacker's screen. Members can start
pairing and see their own records; only owners can confirm issuance.

Pairing state moves `pending → claimed → signing → ready → downloaded` (or
`cancelled`) only through compare-and-set updates, so two racing claims or
confirmations cannot both pass a status check on PostgreSQL's read-committed
isolation. Confirmation commits `signing` before calling the signer, so the
database write lock is not held across the signer's 15-second deadline. After
signing it re-reads policy consistency, the device and person; a revocation, person removal or
pairing-off or a pending public-policy failure during signing discards the identity. A signer failure returns the
request to `claimed`. Renewal queries the original device again after signing,
so a concurrent removal returns a conflict without restoring the deleted row. Device names reject Unicode control, format (bidi and
zero-width), private-use and line/paragraph-separator characters.

The fixed root helper protocol is documented in `deploy/device-ca/README.md`.
The helper refuses to sign when its state directory or files are group/other
accessible, not owned by it, or symlinked, and keeps its own append-only
`issued.log` (device ID, serial, expiry; no keys), refusing more than 20
issuances per trailing hour. A compromised accd can still request leaves for any
well-formed device ID, but not unboundedly and not without a root-held record.
The socket client accepts no path or command. Provisioning/installing the helper
is a separate owner action. The initial signer is a dedicated online root;
offline-root/intermediate provisioning is deferred. Import bundles and passwords
remain only in process memory, actively expire after five minutes, and disappear
on one download or server restart. Download acknowledgement means delivery,
not proof that an OS installed the identity; ACC cannot observe local import.

Manual renewal requires LAN access, an unexpired active device record, recent
sign-in verification and owner confirmation. It replaces the certificate on the
same device ID with zero overlap. Public certificate-proven renewal is deferred
until trusted edge identity forwarding exists. Last seen stays unknown until that
boundary is implemented; a download does not establish a device connection.

Revocation removes the leaf from rendered policy, ends all sign-ins for the person,
closes their registered terminal connections and removes push subscriptions.
Existing preview authorization follows session revocation. When public access is
enabled, revoke/remove and person-removal routes reconcile the registry after
commit; a 15-second worker also detects expiry and registry changes. Apply is
acknowledged only after the root helper verifies the loaded configuration. A policy download uses only the
public `device-ca.pem` and active unexpired leaves of people who still have an
account (a row left behind by any removal path stays out of the gate). It emits the exact Caddy 2.6.2
JSON leaf verifier qualified in Phase 1, including an explicit empty verifier.

The preparation fragment still has no listener, proxy routes or deployment path.
The separate exact review/apply flow composes the complete root-owned base.
Identity-header integrity, stream revocation,
physical-device import/PWA qualification and outside-network probes remain gates
before remote access may ship. Remote previews remain private.

Removing a person also revokes their device rows, cancels pairing capabilities
and destroys their pending in-memory imports. Expired rows are reported as expired
and excluded from policy even if no background job has updated their stored state.

All pairing routes, including authenticated pending-status/password reads, require
a private peer and reject the configured public hostname, case variants and its
trailing-dot DNS alias. TCP requests cannot spoof a private source through
forwarding headers. Registry views remain normal signed-in person/owner resources.

## Uninstalled edge helper contract

The next apply layer is implemented in `src/acc/remote_access_helper.py`; its
closed protocol and deployment prerequisites are in
`deploy/remote-access/README.md`. It accepts review/apply/off/status only. Root
provisions a complete private-only native JSON base, a public hostname allow list,
a device CA public certificate and a qualification flag that defaults to false.
No request selects a path, command, upstream, raw configuration or admin socket.

A review returns only the public change (TLS policy, route, server settings,
base revision) and the canonical SHA-256 revision of the complete rendered JSON;
the root base, which can hold DNS issuer credentials, never leaves the helper.
Enabling skips automatic HTTPS for the public hostname only. The public route
keeps Caddy's own X-Forwarded-For (accd's client address) and strips Forwarded,
X-Real-Ip and X-Acc-Device-*.
Apply independently renders that revision, verifies that Caddy runs the current
or previous generation this helper wrote (so OFF still works after a failed or
interrupted reload),
validates a candidate, atomically writes it, reloads and verifies the entire loaded
configuration before acknowledging success. A stale reviewed revision or unrelated
Caddy change is refused. State is durable before reload; failure attempts the OFF
base instead of restoring a revoked identity. OFF omits the public route and TLS
policy completely, retaining the private recovery route and other sites in the
reviewed base. It needs neither the CA nor a hostname nor qualification.

Root ownership, fixed paths, no-follow regular files, shared DNS/publication lock,
bounded input/output, kernel peer identity, operation deadlines and sanitized errors
follow the DNS helper pattern. This helper is **not installed**. Deployment must
provide an immutable Python environment and confinement, pin/requalify its Caddy
binary, integrate base composition with DNS/publication and arrange restart-safe
loading before the qualification flag may be enabled. No service or machine change
is part of this slice. The release remains NO-GO pending the remaining stream,
edge and physical-device gates.

## Durable public intent and recovery

`remote_access_runtime` is a separate Setting containing desired enable intent,
last helper acknowledgement, pending status and the registry digest. No schema
migration is needed. Owners alone may GET exact review and POST apply/off; API
tokens are refused. Apply requires recent verification and the private peer/host
guard. OFF requires only an authenticated owner plus the normal CSRF/Origin
checks, even without a CA, hostname, pairing setting or recent verification.

The service serializes owner changes and reconciliation. It persists pending
intent before contacting the helper, never holds a DB transaction across IPC,
and clears enable intent after an uncertain apply. It tries OFF and retains
visible pending state until verified; the worker retries OFF after restart.
While enabled, the worker checks helper status and registry every 15 seconds;
a healthy apply has a 30-second client deadline, under the one-minute propagation
target. Failed infrastructure cannot promise a one-minute apply: status stays
pending/unknown, issuance is frozen while pending, and private recovery is retained.
A restart/drift never silently re-enables exposure. HA enablement is refused in
this slice, so no standby starts a public-policy worker.

The status panel distinguishes On, Off and Unknown, and labels counts/revision as
the last verified acknowledgement. Pairing remains private-only; node and preview
routes are denied by the fixed public browser route. Registry removal has the
same cleanup as revoke, then deletes the row and audits `device.remove`.

## Operational drills and authority recovery

`tests/test_remote_access_readiness.py` combines the real signer, current device
registry, authenticated owner API, edge helper and disposable Caddy. Revoke and
Remove each refuse new TLS 1.2/1.3 connections within 60 seconds without adding
an upstream request, while a second identity and private recovery remain usable.
The affected person's browser session is also refused. This is a local healthy
helper bound, not proof of established-connection termination or internet timing.

The restore drill snapshots test-owned signer state before revocation, loses the
original signer, and copies the snapshot into a fresh private directory. A fresh
signer instance issues a leaf chaining to the exact original CA. Old active and
new identities reach disposable Caddy; the identity revoked after the snapshot
does not. Recovery retains the current database, disables public routing first,
and requires an explicit owner review/apply. Signer recovery alone cannot enable
public routing. That fixture represents an already decrypted backup; the
encrypted tool below is tested separately.

Before deployment, the operator must separately qualify encrypted backup storage,
password recovery, independent CA fingerprint verification, root-only permissions
and a restart-safe restore. Include the signer's issuance ledger as well as its
key and certificate so the rate-limit history survives. Never put these in the
application database, normal application backup, Git, wiki or artifacts. Preserve
the current device registry, revocations and audit trail independently. A stale
database backup is not evidence of active membership: if intervening revocations
cannot be reconstructed, stay OFF, invalidate affected identities and re-pair
through private access. Do not replay an old enabled edge snapshot.

### Encrypted authority backup and restore

`deploy/device-ca/acc-device-ca-recovery.py` is a root-only owner tool, installed
beside the signer (not by this change). It is unreachable from accd and from the
signer's stdin contract, refuses non-root callers and prints only the public CA
fingerprint or a fixed refusal.

- `backup <new absolute file>`: reads `ca.key`, `ca.pem` and the issuance ledger
  through the signer's private-file checks and writes one 0600 file created
  exclusively: `ACC-DEVICE-CA-BACKUP-1` header, 16-byte salt, 12-byte nonce, then
  AES-256-GCM (header as associated data) under scrypt (n=2^17, r=8, p=1) of the
  first line of `/etc/acc-device-ca/backup-passphrase` (root, 0600, at least 16
  characters). That file is outside every backup set; keep a second copy offline.
- `restore <file> <fingerprint>`: needs the CA's SHA-256 fingerprint recorded
  independently when it was created. Wrong passphrase, altered file, other
  fingerprint, a key not matching the certificate, or a non-client-only CA are
  refused before anything is written. It creates the fixed authority directory
  (0700) and files (0600) exclusively: it never merges into or replaces one.

`deploy/backup/acc-backup` (root, daily) calls `backup` into the day's folder as
`device-ca.enc` when `/var/lib/acc-device-ca` exists, and otherwise reports
`not provisioned` or `NOT backed up`. It never copies the authority in plain
form. The system Python needs `cryptography` (python3-cryptography) for the tool.

Restore drill (`tests/test_device_ca_machine.py`): initialize, issue, back up,
delete the authority, refuse every wrong input, restore, check the ledger and
modes, and prove old and new leaves chain to the same CA. After a production
restore, follow the recovery rules above: keep public access OFF, keep the
current registry's revocations, review and apply explicitly. If `acc.db` was
restored too, its registry predates the backup: revoke again every device and
remove again every machine revoked or removed since that date (check the audit
log or the owner's notes) before turning public access on.

### Planned authority rollover (no maintenance window)

For a scheduled key change only (decision record 0062 in `docs/decisions`).
After a suspected compromise use the CA key rotation plan below instead, and never keep a compromised authority
as "previous". Run as root, from private access:

1. `acc-device-ca-recovery rollover stage` prints the new authority's fingerprint. Record it offline.
2. Install the edge's authority files: the staged `/var/lib/acc-device-ca/next/ca.pem` as
   `/etc/acc-remote-access/device-ca.pem`, and the current `ca.pem` as `device-ca-previous.pem` (root, 0644).
   The next Apply trusts both, and nobody notices.
3. `acc-device-ca-recovery rollover promote`. It refuses until step 2 matches exactly. New identities now come from
   the new authority, and promote **deletes the old authority's key**: nothing issues from it again and there is no
   abort path, so keeping it would only add risk. Only the old certificate stays in `previous/`, so old leaves still
   verify. Take an encrypted backup straight away (`backup <file>`).
4. Machines on the old authority replace their identity over their link. accd asks at most 10 machines an hour
   (half the signer's 20-per-hour limit), so people can still pair and renew devices during the rollover. A
   connected machine that has to wait is asked again every 5 minutes; a fleet of N machines takes about N/10 hours.
   An identity close to expiry is never held back. People renew their devices on the LAN; Devices shows
   **Issued by the previous device authority**. Remote access shows how many identities remain.
5. When that count is 0, delete `/etc/acc-remote-access/device-ca-previous.pem` and run `rollover finish`, which
   deletes the old certificate. The old authority already left the edge's trust pool on the first apply without its
   leaves.

**Records.** The signer appends each step to the root-only `/var/lib/acc-device-ca/rollover.log` (stage; promote,
with `previous_key_deleted`; finish). A step refuses before it changes anything when that record isn't private to
root (another owner, or readable by others): fix its mode to 0600 and run the step again. accd's audit log
records what the signer reports, at most a minute later:
- `device_ca.authority_changed`, with `from`, `to` and `planned`. `planned` is false when the new authority didn't
  come from a rollover, for example after a restore; investigate that.
- `device_ca.previous_key_deleted`.
- `device_ca.previous_retired`.
- `machine_identity.rollover_request` for each machine asked.
- `machine_identity.issue`, now with `reason` (`expiry` or `rollover`).

Only fingerprints are recorded, never key material. Device, remote-access settings and machine listings refresh
and audit the authority before opening their read transaction. Status formatting uses that pre-fetched authority;
it does not refresh the signer while holding a read snapshot. This keeps cache expiry from blocking the listing's
own audit writer and delaying unrelated Settings saves. Audit writes retain normal writer admission.

A crash during promote leaves the signer without a current authority, so it refuses to issue; run promote again
to resume. A crash after the swap but before the old key was deleted is finished the same way: promote deletes it. Each step refuses out of order: one staged authority at a time, finish before the next stage.

### CA key rotation plan (after a compromise)

1. Use private recovery; turn public access OFF and verify routing removal. Fence
   public ingress separately if helper state cannot be verified. Record the old
   public CA fingerprint and inventory, without copying private material to ACC.
2. Provision a fresh dedicated client-only authority in a new root-controlled
   location using the reviewed deployment procedure. Validate its public key,
   constraints and fingerprint; encrypt and independently restore-test its backup.
   A suspected compromise requires fresh keys, not restoring the compromised CA.
3. Keep the old CA and all old leaves out of the new root helper policy: no
   `device-ca-previous.pem`. The planned rollover above is not a compromise
   response. Do not append roots or switch to chain-only authorization to
   simulate continuity.
4. Invalidate affected device records, pairing capabilities, sign-ins, push and
   streams; re-pair people via private access. Machine identities and their
   rotation protocol must be implemented separately before public machine use.
5. Requalify the exact helper/Caddy composition with no identity, wrong CA,
   expired, old/revoked and newly active identities, private recovery, session
   tickets and stream deadlines. Reconcile the current registry, review the new
   canonical revision and explicitly apply only after all release gates pass.
6. Retain the incident/audit record and encrypted backups under owner-controlled
   retention. Retire old key copies according to that policy. Rollback means OFF
   and private recovery; it must never restore an old leaf allow list.

This intentionally requires a public maintenance window. Uninterrupted rotation
of an individual machine leaf is a different, still unimplemented protocol: stage
a new node-held key/CSR and bounded two-leaf overlap, acknowledge policy on every
edge before switching all consumers, prove the new key, then revoke the old leaf.
Removal must win any issuance/rotation race and close the node link. Restart at
each step must retain a working old identity or remain fail closed, never revive
a removed node. No such continuity is claimed for browser manual renewal, which
currently has zero overlap.

## Machine identities

Owner setting `machine_identities` (default false) in Setting `remote_access`.
Rows are `device` with `kind='node'`, `node_id` (migration 0051), empty `person`
and optional `retire_after`. They are excluded from `/api/devices` and person
operations; node removal revokes them in the same transaction as the node.

- Issuance: after `ready`, the main sends `machine-csr-request` when the node has
  no usable leaf, or its presented leaf has under 10 days left. The node keeps a
  fresh P-256 key in `machine-next.key`, sends `machine-csr` (one in flight, one
  per minute per link). The signer's closed `issue_machine` operation checks proof
  of possession and P-256, ignores all requested names/extensions, and issues
  `OU=acc-machine,CN=<node id>`, URN `urn:acc:machine:<node id>`, clientAuth only,
  at most 30 days. accd re-validates the result against the CSR key; removal wins
  a race with issuance (checked again before the row is written).
- Switch: the node stores certificate+key as `machine-next.pem` (one 0600 file).
  `machine-ready <fingerprint>` is sent only when public access is off, or the
  verified applied revision already contains the successor. The node renames it to
  `machine.pem` (atomic), reconnects once and presents it on the link and update
  download. Seeing the successor presented retires older leaves; `retire_after`
  (24 hours after issuance) bounds the overlap otherwise.
- The hello's optional `machine` fingerprint is node-claimed and only retires that
  node's own older leaves. Through the edge, accd uses the edge header instead and
  refuses a link whose leaf belongs to another node (4401).

Git backups are pushed by brokers, which must not hold the machine key. They keep
the private address by default. Updated nodes have a job-scoped loopback
forwarder that keeps the machine key in the node service. Public fallback also
requires owner consent in `Node.public_git_backup` (migration 0068, default false),
verified remote access and machine identities. Signed jobs bind the project,
node and token commitment; private connections stay preferred. Consent changes
use the machine policy revision and audit `node.public_git_backup`. Opting in
also requires the private address (as pairing judges it: a private client
address, not the public hostname, not through the edge) and the acting owner's
browser sign-in, verified within five minutes (`passkeys.recently_verified`);
never the API token. Refusals audit
`node.public_git_backup_refused`. Opting out
revokes pending/public grants and closes public git streams. See
[Remote access](/User-Guide/Remote-Access#git-backups-while-roaming).

## Public edge identity and established connections

The rendered public route sets `X-Acc-Edge-Client` to Caddy's
`{http.request.tls.client.fingerprint}` (SHA-256 of the leaf DER). Caddy applies
`set` before `delete`, so the name is outside the deleted `X-Acc-Device-*` prefix;
`set` overwrites client values (tested on Caddy 2.6.2). Routes:
machine subject (`vars_regexp` on `{http.request.tls.client.subject}`) to
`/api/nodes/connect`, `/api/nodes/bundle`, `/api/git/*` → proxy; the bootstrap,
pairing, nodes, git and preview prefixes → 403; any other machine request → 403;
everything else → proxy.

`EdgeGuard` (ASGI) treats a request carrying the header as public. Browser/machine
assertions only narrow access. Native possession additionally requires a separate secret assertion, Unix-proxy
ingress and the configured public hostname; a forged TCP header supplies no proof. Each request or
WebSocket is checked against the registry (active, unexpired, not retired, its
person or active node exists, machine setting on; machine paths only for
machines, git only for that machine's projects with public backup consent; denied prefixes for browsers).
Admitted connections are registered by fingerprint. Revocation, removal of a
device, person or node sweep at once; the remote-access worker sweeps every 15
seconds for expiry, retirement and the setting. A swept WebSocket gets close
4403 and the handler sees a disconnect; an HTTP stream sees `http.disconnect`,
and later sends fail. Completed invalidation takes priority over a body or frame
that becomes ready in the same event-loop turn, including during authority rollover.
The guard discards that input and delivers the disconnect instead. Bytes already delivered cannot be recalled.

**Turn off.** Public use also requires the owner's enable intent
(`remote_access_runtime.desired_enabled`). Turn off clears it before calling the
helper and then sweeps, so every registered public request and stream closes at
once, whether the helper confirms OFF, fails or is unreachable; the 15-second
sweep is the backstop for intent cleared elsewhere (a failed apply, edge restart
or drift). This happens before Turn off waits for the remote-access lock: a
reconciliation can hold that lock on an unresponsive helper for up to 30 seconds
per request. Until the owner's next enable, an in-process flag (`Edge.halted`)
refuses public requests, and a reconciliation or apply already in flight can't
re-enable public access. Its verified result is treated as failed. Measured on Caddy 2.6.2 (`test_caddy_off_closes_upgraded_streams_but_not_streamed_responses`):
unloading the public route sends upgraded WebSockets a 1001 close and ends them,
but a streamed HTTP response that began before OFF keeps flowing. accd's own
closure covers that case. Re-run the test after any Caddy upgrade.


## Native companion identities

The separate, off-by-default native extension uses device-held CSRs, leaf-only
delivery and certificate-bound person sessions. It does not change browser import
or machine-route permissions. Native identity proof additionally requires a separately provisioned secret
assertion, trusted Unix-proxy ingress and the configured public hostname; a fingerprint header from
a TCP caller cannot establish possession. See [Mobile devices](/Internal/Mobile-Devices).

Native leaves participate in the same two-authority rollover gate. The previous-authority count includes native
identities while the mobile extension is enabled; disabled native identities are excluded from the public gate
and that count. Turn off and revocation also close their established public connections.
