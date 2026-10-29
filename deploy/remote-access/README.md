# Remote access apply helper (uninstalled)

This slice installs nothing. There is no production enablement approval. The
machine's Caddy, DNS, routing and services are untouched. Overall remote-access
release remains NO-GO under docs/REMOTE-ACCESS.md. Do not set `qualified: true`
until the remaining edge, per-request/stream and physical-device gates pass.

`src/acc/remote_access_helper.py` follows the DNS helper's root-owned directory-FD,
no-follow atomic writes and shared Caddy lock pattern. It accepts one bounded JSON
line on a connected, socket-activated stdin/stdout channel. No CLI arguments,
environment-selected paths, shell, restart, generic commands, Caddy fragments,
caller-selected upstreams or arbitrary files are accepted. Both peers check the
kernel-reported UID. The root helper admits only the provisioned accd UID.

## Closed wire contract

- `{"action":"review","hostname":"remote.example.com","leaves":["PEM..."]}`:
  renders the complete native JSON and its SHA-256 canonical revision. No writes
  or reloads. Only provisioned public hostnames and at most 200 directly signed,
  clientAuth-only non-CA leaves are accepted. Public leaves are not secrets.
- `{"action":"apply","hostname":"remote.example.com","leaves":["PEM..."],"revision":"64 hex"}`:
  independently renders and checks the reviewed revision and qualification flag;
  verifies the previous live configuration, validates the candidate, atomically
  writes `current.json`, reloads, then verifies Caddy's complete `/config/` result.
  No successful acknowledgement precedes verification. An empty registry retains
  an explicit leaf verifier with an empty list.
- `{"action":"off"}`: requires neither a hostname nor CA nor qualification flag;
  validates/writes/reloads/verifies the reviewed OFF base. The public route and
  public TLS policy disappear. It preserves the private recovery route and sites
  in the root base. The application never requires recent reauthentication for OFF.
- `{"action":"status"}`: reads durable last acknowledgement; a previously
  verified generation is checked against the actual loaded configuration.
  Drift becomes unknown, not On. No status read enables routing.

The rendered public route sends machine identities (`OU=acc-machine` subjects)
only to `/api/nodes/connect`, `/api/nodes/bundle` and `/api/git/*`, refuses
those to browser identities, refuses bootstrap/pairing/preview to everyone, and
sets `X-Acc-Edge-Client` to the verified leaf's SHA-256 for accd's per-request
and open-stream checks (decision 0048).

Extra keys and other actions are refused. Responses contain a complete review or
`enabled`, `hostname`, `last_applied_revision`, `devices_allowed`, and
`last_reload_result`. Errors on the privileged wire are a fixed sanitized refusal.
Requests/responses are bounded to 1 MiB; the application deadline is 30 seconds.
The production helper holds the shared `/run/acc-dns/caddy-config.lock` (3-second
acquisition bound), then uses a 20-second total Caddy-operation budget (at most
5 seconds per operation). A future accepted-connection service must also enforce
an outer process deadline, memory/process limits, no core dumps and accd-only
socket permissions, including for a caller that never finishes its input line.

## Root-owned provisioned inputs — not installed by this change

The immutable helper and its Python/crypto dependencies must live outside every
agent-writable checkout or editable environment. No installer or service unit is
provided: deployment confinement and restart composition need an owner review.
The only production inputs are regular root-owned mode-0600 files, reached through
root-owned non-writable ancestors in `/etc/acc-remote-access`:

- `policy.json`: `{"qualified":false,"hostnames":["remote.example.com"],"server":"srv0","accd_uid":1234}`
  (the UID is an example; a reviewed installer must resolve the installed account).
- `base.json`: the complete reviewed public-OFF native Caddy JSON, including all
  private policies, published sites, server certificates, DNS issuer settings,
  and Unix admin address `unix//run/caddy/admin.sock`. The designated HTTPS server
  must exist, with explicit SNI policies and routes. No public hostname is present.
- `device-ca.pem`: the dedicated device CA's public certificate.
- `device-ca-previous.pem` (only during a planned rollover): the previous authority's public certificate.
  The rendered gate trusts only authorities that issued a listed leaf, at most two; delete the file before
  `rollover finish`.

State lives in root-owned mode-0700 `/var/lib/acc-remote-access`; its fixed files
are mode 0600, atomically renamed and fsynced. No CA private key is read. The
candidate and current configuration are fixed-path private files. The applied
revision identifies their exact contents; pending state is persisted before the
reload, so a crash is never reported as a verified apply.

The runner drops to the caddy UID/GID (with no supplementary groups), passes
the rendered JSON over stdin, executes only `/usr/bin/caddy validate` and `reload`, and GETs only
`/config/` over `/run/caddy/admin.sock`. It has no restart or arbitrary command.
This native JSON renderer is qualified in disposable tests against Caddy 2.6.2,
not the DNS helper's potentially different `/usr/local/bin/caddy`. Deployment must
pin and requalify the actual binary and certificate/DNS modules, compose the
reviewed base under the shared DNS/publication lock, and arrange restart-safe
loading. This is **not** a drop-in override of the current Caddyfile deployment.
A Caddy restart to the private base is detected and clears enable intent; it never
automatically re-exposes ACC.

A stale base must never overwrite concurrent site/DNS changes. The helper refuses
when loaded JSON differs from both its last generation and (for OFF) the reviewed
base. The owner action remains available but reports pending on such a refusal;
an operator must reconcile the root-owned base under the shared lock. Installation
must integrate this composition with DNS/publishing before qualification. Do not
bypass the check or restore an old allow list. On reload/verification failure the
helper attempts the OFF base rather than restoring a revoked leaf; failure to
verify OFF remains unknown and visible. accd persists OFF intent and retries it.

The fixed public browser route rejects pairing, device issuance, nodes and
previews, strips incoming device/forwarding assertions and proxies only to
`unix//run/accd.sock`. h3 is disabled, strict SNI/Host is enabled and TLS session
tickets are disabled globally while on. Existing connection identity checks and
stream expiry/revocation are still a later release gate; no trusted device header
is invented. Tests inject only the helper object's runner, directory FD and
upstream dependency for a disposable counting stub, never over production IPC.

## Experimental native device prerequisite

The `ACC_MOBILE` server slice is off by default. Before admitting native leaves,
the owner must separately provision a fresh random 256-bit assertion key (64
lowercase hex characters) in this root policy as `native_edge_key`, and the same
value in `<ACC_DATA_DIR>/secrets/mobile-edge-key` as a main-owned 0600 regular file
(64 bytes, optional newline). Neither helper nor server provisions it. Use the
owner's secret-management process; never distribute it to phones or diagnostics.

Native leaves without a valid key cause `native_edge_not_provisioned`, before any
reload. The public proxy overwrites `X-Acc-Native-Edge`; review output redacts its
value. accd requires this assertion plus Unix ingress/public hostname and consumes
it before handlers or project/node forwarding. The root-only Caddy configuration
contains the real key. Existing browser/machine-only policies need no new key.
Keep mobile off during assertion-key recovery, use OFF/private recovery, rotate
both copies, and review/apply/qualify again. This extra prerequisite does not
qualify device TLS, server trust, or any public installation.
