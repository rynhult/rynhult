# Caddy for ACC

Caddy terminates HTTPS in front of accd, which listens only on `/run/accd.sock` (owner `acc`, group `caddy`, 0660). `deploy/install.sh` renders ACC's site into `/etc/caddy/acc.d/` and adds `import acc.d/*.caddy` to `/etc/caddy/Caddyfile`. It installs the global `admin unix//run/caddy/admin.sock|0660` directive while preserving sites and other global options. Both modes install `caddy.service.d-runtime.conf` as `runtime.conf` for `RuntimeDirectory=caddy`. Adapted JSON must explicitly name that socket before validation or reload; a missing or TCP admin address stops installation. The private allow list is `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, `127.0.0.0/8`, `::1` and `fd00::/8`. This intentionally includes local processes and Docker containers; it is a network filter, not account isolation. Everyone else gets a 403 and is never proxied. Both omit URI and request/response headers from access logs, with root/Caddy-only 0600 access files, 10 MiB rolls, ten retained rolls and 14-day maximum retention.

## With a Cloudflare zone (`ACC_WILDCARD_ZONE` set)
- **Template:** `wildcard.caddy.in`, rendered as `/etc/caddy/acc.d/<zone>-wildcard.caddy`.
- **Certificate:** one `*.<zone>` certificate via DNS-01, so hostnames stay out of Certificate Transparency logs.
- **ACC:** its own name inside the wildcard is proxied to accd. Sites published through ACC become routes in `routes-<zone>/` and share the certificate. Any other name is aborted.
- **Binary:** `/usr/local/bin/caddy` with `dns.providers.cloudflare`. `deploy/setup.sh` downloads it from the caddyserver.com build service only when `ACC_CADDY_SHA256` supplies an independently approved SHA-256 for that architecture. A missing hash or mismatch stops installation before the binary is installed. The service builds on demand, so a hash is not inferred from the download or fetched beside it. Supply a hash from your reviewed build/release process, or preinstall a reviewed root-owned binary with the module; the installer reuses it. Alternatively, build it yourself: `xcaddy build --with github.com/caddy-dns/cloudflare`, then install the result at `/usr/local/bin/caddy`.
- **Drop-in:** `caddy.service.d-acc.conf` → `/etc/systemd/system/caddy.service.d/acc.conf`.
  - It runs that binary and reads `/etc/caddy/cloudflare.env` (root 0600, `CF_API_TOKEN=…`, Zone:DNS:Edit on the zone).
  - It drops `--environ`, which would print the token to the journal.
- **DNS:** an A record for ACC's name pointing at the machine's LAN or VPN address (DNS-only), or a local DNS entry.

## Without a domain
- **Template:** `internal.caddy.in`, rendered as `/etc/caddy/acc.d/acc.caddy` for ACC's name only. It uses Caddy from its apt repository.
- **Certificate:** `tls internal`, so Caddy's own certificate authority signs it. To avoid browser warnings, trust Caddy's root on your devices: it's `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`. Push notifications and installing ACC as an app need a trusted certificate.
- **Publishing:** project sites can't be published in this mode; that needs a zone.

## Revert
1. Remove `/etc/caddy/acc.d/`, both drop-ins, the global admin directive, and the `import acc.d/*.caddy` line.
2. `systemctl daemon-reload && systemctl restart caddy`
3. Delete ACC's DNS record.

## Live previews

The updated templates route `preview-<project id>-<port>.<preview domain>` to accd's isolated authenticated preview gateway, behind the same private-network filter as ACC. They never proxy a preview directly to a dev port. The `preview-` namespace is reserved against publishing.

The preview suffix defaults to `ACC_WILDCARD_ZONE` in Cloudflare mode (an override must equal it), otherwise the first ACC origin's hostname. `ACC_PREVIEW_DOMAIN` can set a private suffix in internal-CA mode. Set private wildcard DNS for this suffix to ACC's private address. There are no per-project public records. Cloudflare mode reuses the existing wildcard certificate; internal mode renders a separate wildcard site using `tls internal`, requiring device trust in Caddy's root. Prefer the same site as ACC for browser iframe-cookie support. Run the normal installer to apply these template changes; accd gains no Caddy admin privileges.

## Phase 1 hardening and portal-managed providers

Both install modes include the sandbox drop-in and `acc-hardening.options`: header read 10s, idle 2m,
and managed proxy dial 5s, without blanket write/body/stream deadlines. See the
[Caddy hardening guide](../../docs/wiki/User-Guide/Caddy-Hardening.md) for verification and recovery.

The accepted [DNS design](../../docs/DNS-PROVIDERS.md) adds a separate encrypted infrastructure namespace,
owner-only Keys and confirmed Settings bindings. Cloudflare is the only phase 1 provider; Azure DNS and
Route 53 follow in phase 2. The [root helper deployment](../dns/README.md) remains inactive until a local
operator configures policy and qualifies a CI-built release. The committed candidate manifest deliberately
has no approved binary hashes. Destination helpers never build Caddy or accept arbitrary downloads/modules.
Legacy bootstrap and pre-existing installations remain available until explicit migration/cutover.

DNS setup never enables public dashboard access. The [remote-access design](../../docs/REMOTE-ACCESS.md)
still requires its separate device gate and exposure controls.
