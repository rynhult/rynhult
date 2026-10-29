# DNS helper and release gate

This directory contains deployment inputs, not instructions executed by this agent session.
The default root policy allows Cloudflare only. Azure DNS and Route 53 are registered candidates, remain unqualified, and require explicit local policy approval.

1. Release CI builds the exact `release.json` Caddy/module/tool pins for amd64 and arm64. It retains resolved dependency
   locks, a dependency inventory, candidate hashes and workflow artifacts. Review security advisories and licensing,
   produce/review SBOM and signed provenance, run architecture/module checks and disposable-zone qualification.
2. A reviewer commits independently verified SHA-256 values in `artifacts.amd64.sha256` and `artifacts.arm64.sha256`,
   plus provenance/qualification references, and changes status to `qualified`. Never trust a checksum fetched alongside
   a binary. No approved hashes are available from this no-remote worktree; the committed manifest fails closed.
3. From a reviewed checkout, a local root operator may run `deploy/dns/install.sh example.com`. It installs an inactive
   fixed-path helper and single-zone root policy. It never starts or reloads Caddy, silently expands existing policy,
   replaces an approved manifest or installs a binary. Review `/etc/acc-dns/policy.json` and `release.json` locally.
4. Put the reviewed architecture binary in root:root 0600 `/var/cache/acc-dns/caddy-linux-amd64` (or `arm64`). Run the
   installed `/usr/local/lib/acc-dns/install-release` as root. It refuses arguments, symlinks, wrong owners, writable
   ancestors, oversized bytes and a missing/mismatched independently committed digest. It atomically switches the
   verified executable and keeps `caddy.previous`. Never restore a known vulnerable binary without owner approval.
5. The tmpfiles entry creates `/run/acc-dns` and the shared `caddy-config.lock`. Publishing's unit may write that
   directory (`ReadWritePaths=-/run/acc-dns`); reinstall Publishing (`deploy/install.sh`) before activating the helper.
   The operator activates the private socket and retention timer deliberately, then imports legacy credentials in
   Settings. The acc UID is trusted by the helper; an actor label is not authentication. accd enforces browser-owner
   password confirmation. The peer UID check protects against agents and unrelated local accounts.
6. Prepare `caddy.service.d-dns.conf` before first apply and reload systemd metadata. Its optional legacy fallback
   keeps the current token available until the managed generation exists; managed credentials take precedence.
   Save a Cloudflare binding whose zone matches root policy and the installed wildcard template. Apply validates the
   complete fixed Caddyfile with candidate credentials, checks the Unix admin address, reloads expanded JSON over the
   private socket, and atomically persists a protected generation. No caller config, paths, API bases or commands.
7. After staging issuance/TXT cleanup and production health checks, the operator does a controlled restart and
   confirms reboot persistence with the prepared managed EnvironmentFile drop-in. Legacy env-file deletion is explicitly
   deferred until 24 hours after verified cutover; Publishing's legacy token stays until the owner confirms its tested vault cutover.
   Backups may hold old copies. Revoke old tokens at Cloudflare after verification, not merely after saving in ACC.
8. On a failed apply the helper reloads and persists the previous generation. A crash leaves a pending journal that
   blocks another apply; a trusted local recovery call uses the fixed `recover` action. Explicit rollback expires at
   24 hours; the hourly purge removes expired root copies, and accd removes expired encrypted retired revisions.

No release CI, live service activation, production DNS mutation, staging/production ACME or reboot has been verified
here. `Apply` means accepted by Caddy, never successful issuance. The binary gate intentionally prevents premature
activation. The phase 2 Publishing adapter and ledger are described below.

The currently approved build pins come from the official repositories:
- https://github.com/caddyserver/caddy/releases/tag/v2.11.7
- https://github.com/caddyserver/xcaddy/releases/tag/v0.4.7
- https://github.com/caddy-dns/cloudflare/commit/a8737d095ad5a48ca031cea6ab704057dbc2d250

## Phase 2 candidates

The manifest now pins Cloudflare, Azure and Route 53. It remains unqualified with no executable hashes. The fixed
schema registry accepts credential storage for all three; Azure/Route 53 production Publishing remains unavailable.
Both the ordinary installer and DNS installer deploy the same root-owned standalone credential parser. Never source
its generated environment text. The helper accepts a provider only when both hold: the installed
`/etc/acc-dns/release.json` has status `qualified` with `provider_qualification.<id>.certificates` set to `true`, and
a local operator has added the fixed ID to root policy's `providers` list (default `['cloudflare']`). A phase 1 manifest
with no per-provider table qualifies Cloudflare only; a missing manifest qualifies nothing. Policy alone never
enables a provider. The policy zone must match
the entered credential zone and the installed wildcard template.

Before a multicloud apply, the operator must render `wildcard-managed.caddy.in` with only the approved zone/ACC host
substitutions into the existing wildcard site, retain `persist_config off` and prepare `caddy.service.d-dns.conf`.
Its CF default supports recovery of a legacy token; managed generic generations carry their own fixed
`ACC_DNS_CONFIG`. The ordinary installer preserves the managed template when a managed generation exists.
Apply refuses a generic candidate if the adapted JSON does not contain the expected fixed provider fields.
No IPC request chooses a template, command, file path, endpoint or module. Rollback restores the entire previous
credential/provider environment; a controlled restart uses the same persisted generation.

Run the owner-only disposable-zone harness described in `docs/wiki/Internal/Development.md`. Record staging
issuance, shared-TXT preservation, cleanup, renewal, negative zone/type permissions and Publishing lifecycle for
both architectures, then arrange independent security review before merging. The policy examples in `policies/`
use artificial zone IDs/names; replace those precisely, never broaden the resource to `*`.

**Azure certificate hold:** the pinned libdns/azure v0.5.0 deletes whole TXT RRsets and refuses appends beside an
existing value. The harness's seeded TXT sentinel must fail that dependency. A reviewed fixed dependency/patch and
passing shared-TXT/parallel challenge evidence are mandatory before Azure activation. This run did not patch that
third-party Go module, execute CI, or run any live qualification.

## Wave 2 candidates

The manifest also pins `googleclouddns`, `digitalocean` and `hetzner/v2` at exact reviewed-source candidates.
All six providers retain false certificate and Publishing qualification flags; there are still no executable hashes.
Never enable a candidate by changing root policy alone. See the Guide for credentials and the internal Development
page for the owner-run harness (the new IDs are `googleclouddns`, `digitalocean`, `hetzner`).

Google's pinned module uses `gcp_application_default` as a **file path**, and lists managed zones at project scope.
The harness writes its account JSON into a private temporary file. The root helper provisions content-addressed JSON files under the fixed
`/etc/caddy/acc-dns-google` directory (root:caddy 2750), with files root:caddy 0640. No IPC field chooses a path.
It retains files referenced by the current/previous/candidate environments and removes orphan or retired files
after failed validation, rollback and normal purge. Environment files remain root-only. Live custody, rotation, purge
and rollback still require qualification. Never install ambient machine credentials or enable ADC as a workaround.
The DigitalOcean JSON field is `auth_token` while its Caddyfile subdirective is `api_token`; the standalone renderer
handles that distinction. Hetzner `/v2` uses `api.hetzner.cloud/v1`, not the retired `dns.hetzner.com/api/v1` API.
