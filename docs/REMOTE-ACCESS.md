# Remote access without a VPN

**Status: design approved by the owner, 2026-10-22, with the final lifecycle and transport
answers accepted 2026-10-24 in decision 0020 (private decision record).
LAN pairing and owner review/apply/off controls are implemented; both helpers
remain uninstalled and public remote access is not enabled.**

The owner accepted confirmed opt-in, exposing 443 through ACC's own Caddy, and
access for machines in decision 0008 (private decision record).
Decision 0020 supersedes its Cloudflare-terminated fallback: only direct access or
a qualified raw TCP tunnel preserving the Caddy mTLS boundary is approved.
Device pairing precedes any ACC code, followed by normal sign-in. Design approval
does not authorize public exposure or waive the implementation and qualification gates.

## Recommendation and current baseline

Ship direct HTTPS with mandatory device mTLS on a separate public hostname first. Preserve the private hostname for LAN recovery. Qualify phone browsers before promising a PWA. Offer a tunnel only with an equivalent device gate; defer a self-hosted relay.

Today [Caddy templates](../deploy/caddy/) admit only `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, `127.0.0.0/8`, `::1`, `fd00::/8`. Everything else gets Caddy's 403 without reaching accd. [install.sh](../deploy/install.sh) renders templates and validates the Unix admin socket before reload. accd remains on `/run/accd.sock`; do not add a network listener. These network ranges include containers/local programs and are not device authentication.

[Certificate downloads](../src/acc/server/api/certificate.py) install **server trust only**: the existing `.mobileconfig` contains a root certificate, no device identity. [Auth](../src/acc/server/auth.py), [auth routes](../src/acc/server/api/auth.py) and [Security](wiki/Internal/Security.md) provide argon2id password + replay-resistant TOTP, 14-day server sessions, CSRF/Origin checks and terminal revocation. Address failures (5) and account failures (10) trigger ten-minute throttles; a correct login can bypass an account lock from an unblocked address. Local passkeys are now implemented (see [decision 0013](decisions/0013-passkeys.md)); they authenticate a person after any future device gate, not the paired device itself. Public and private hostnames require separate enrollment.

## Pairing and device lifecycle

Use a dedicated private device CA, separate from Caddy's server CA and `ACC_NODE_CA_FILE`. Its client-auth-only issuing policy must never issue server certificates. Prefer an offline root and a constrained online intermediate held by a narrow root-owned signing helper: fixed CSR validation, no arbitrary signing or file paths. accd stores device metadata, not the CA key. Back up encrypted CA material separately with a documented restore/rotation drill. Each certificate has a random device ID, unique serial, clientAuth EKU, and no personal hostname. Record owner, label (for example “Alex's phone”), type (browser/node), SHA-256 fingerprint, expiry, status and last seen; labels confer no privileges.

1. On the LAN, a recently reauthenticated owner chooses **Pair device**, name and type. Generate a 256-bit random one-use secret, hashed at rest, valid for five minutes. QR contains the private HTTPS pairing URL and secret in a fragment; remove it before subsequent navigation. No credentials in queries, analytics or logs. The receiving device must also be on a private address for initial pairing. A photographed QR is a temporary enrollment capability: display the pending device and require owner confirmation before issuance.
2. Exchange the secret in a bounded POST, rate limited by address and attempt count. Consumption and issuance are atomic; retries retrieve only the same pending result, never a second identity. Expired/unclaimed requests are purged. Pairing routes are private-only even when the public hostname is enabled; no public pairing exception to mTLS.
3. Prefer device-generated keys and CSR proof of possession for nodes/native enrollment. WebCrypto keys cannot simply be installed as a browser TLS identity; a JavaScript CSR alone does not solve browser pairing. For initial browser support deliver an encrypted PKCS#12 identity, generated transiently on the main. Display its random import password separately to the owner; never embed that password in the QR/profile. Download once, no-store, no disk retention or backups; expire the import within 15 minutes and discard the transient server key and bundle after redemption/timeout. ACC cannot observe OS import: destroy the server copy on download and instruct the person to delete the local file after import. The implemented five-minute in-memory window is within the approved ceiling. This briefly trusts the main with the device key and allows users to copy it. SCEP/native enrollment can later reduce that risk; a nonexportable hardware key is not promised by a browser download.
4. Confirm installation by opening the mTLS hostname on the device, then normal ACC sign-in. Certificate possession admits the device; it does not select a user or grant owner privileges.

| Platform | Proposed installation and qualification |
|---|---|
| iPhone/iPad | New identity `.mobileconfig` with `com.apple.security.pkcs12`; install in Settings and enter the separate password. Keep the existing server-root download distinct. Use Safari first; test identity selection and Home Screen separately. |
| Android | Import `.p12` as a user identity in system credential settings; test Chrome's certificate picker. OEM/work-profile policies differ; Firefox must be qualified separately. |
| macOS | Profile or `.p12` into login Keychain; test Safari and Chrome selection. Firefox may require its own identity import. |
| Windows | Import `.pfx` into current user's Personal certificate store for Edge/Chrome; Firefox may require its own import. Avoid machine-wide private-key access. |
| Linux | Import `.p12` into the actual browser's certificate store; Chromium/NSS and Firefox profiles must be tested separately. CLI uses restricted PEM key/cert files. |

Apple documents [PKCS#12 profiles](https://developer.apple.com/documentation/devicemanagement/certificatepkcs12/) and [SCEP](https://developer.apple.com/documentation/devicemanagement/scep). These support the proposed delivery mechanisms, not a guarantee that every browser context will use the identity. Do not install the device CA as a trusted **server** root on clients. Public server certificates should use ordinary system trust; retain Caddy's root only for private server names that need it.

Owner-approved browser lifecycle: certificates last 90 days, with manual renewal
from a signed-in device and another manual identity import; current preparation
allows shorter lifetimes. Warn at 30/14/7 days. Retain recent user reauthentication
and an active device (node: active signed link and CSR); bind renewal to that
device ID and allow at most 24 hours of old/new overlap. The current LAN-only
preparation uses zero overlap; public certificate-proven renewal is later work.
No silent renewal with a login cookie alone. After expiry, return to LAN pairing.
Owners see expiry and last connection, can rename/revoke, and can revoke all
identities for one person.

Lost device: from LAN or another paired device, revoke its certificate, associated sessions, preview tokens and push subscriptions; close terminals and node links, then remove the local profile when possible. If session attribution is unavailable, revoke all that user's sessions. Certificates cannot recall already displayed content or push payloads already delivered. Keep a documented LAN/local-console recovery route; never restore access by opening a public login bypass.

## Exact edge boundary

Use `acc.example.lan` for existing private access and `remote.example.com:443` for public mTLS, including when accessed from LAN. A TLS policy cannot be made optional by an HTTP `remote_ip` matcher: TLS completes before HTTP routing. Public SNI has its own explicit connection policy with `client_auth`, `mode require_and_verify`, the dedicated device CA trust pool, and a **leaf verifier containing only active leaf certificates**. Caddy's [TLS documentation](https://caddyserver.com/docs/caddyfile/directives/tls) describes this verifier. Store fingerprints in ACC; render the corresponding public leaf PEMs for Caddy. Require both chain/expiry verification and leaf membership; validate this behavior on the deployed Caddy version before shipping. If its modules cannot enforce that conjunction, block release or implement a reviewed TLS verifier. Do not substitute an HTTP fingerprint matcher or assume CRL/OCSP checking is automatic.

Owner-approved root helper renders an atomic versioned policy, adapts/validates and reloads through the existing admin socket mechanism. No new arbitrary Caddy privileges for accd. Revocation removes a leaf; renewal adds then removes leaves. A failed apply is visibly pending, never “revoked”. Freeze issuance and disable public routing if policy consistency cannot be established. CRL/OCSP is a later alternative requiring explicit fail-closed server support and freshness handling, not a phase-one dependency.

Reload does not revoke existing TLS sessions/HTTP keepalives/WebSockets by itself. Forward a Caddy-derived device ID over the protected Unix socket (strip incoming identity headers). Check active device state on each public request and periodically on streams; close affected terminal, preview and node streams on revocation. Disable public TLS session resumption initially and prove reconnect rejection. Target new connections rejected immediately after successful policy apply and existing streams closed within 30 seconds. Test expiry on long-lived connections too.

The private SNI retains its exact source allow list and never trusts client-supplied forwarding headers. Public requests must not be reclassified as LAN through a loopback tunnel. Reject SNI/Host mismatches and HTTP/2 connection coalescing across private/public policies; test a paired HTTP/2 connection attempting a private Host. Unknown SNI/Host never routes to ACC. Port 80 must not proxy or show ACC; prefer DNS-01 issuance, so no public HTTP challenge exception is needed. Leave public HTTP/3 disabled initially to reduce the qualification matrix. Existing published project sites have separate approval and routing; enabling remote ACC does not change them.

### Preview, terminals, push and PWA

[Preview](PREVIEW.md) currently uses private isolated origins. Keep remote preview off in phase one. If approved later, use a dedicated public preview suffix under the same site as remote ACC, with mTLS on **every** preview hostname before its bootstrap page. Adapt the gateway's exact hostname/token validation and CSP/allowed origins deliberately; DNS alone is insufficient. Preserve per-project/port origin isolation, host-only cookies, token/session checks and disabled preview service workers. Never expose dev ports directly or reuse the private wildcard's weaker TLS policy. Verify iframe identity selection and certificate prompting; offer a separate tab if embedding fails safely.

Terminal WebSockets use the same TLS gate on upgrade, then existing cookie/Origin/session checks. A browser cannot attach a custom TLS certificate from JavaScript; the browser/OS must select it. Reconnection and sleeping-phone wake need tests.

[Web Push](../src/acc/server/push.py) is an outbound encrypted request to a vendor service, so the push vendor needs no inbound certificate or ACC callback exception. Subscription changes still require mTLS and login. [The service worker](../web/public/sw.js) displays payloads without fetching ACC and opens ACC on tap; icon fetching, worker updates, subscription renewal and notification navigation can still require a usable identity. New public origin means new cookies, app installation and push subscription; private-origin subscriptions do not migrate automatically.

[Apple supports Web Push for iOS Home Screen web apps](https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers), but this is not evidence of mTLS compatibility. Safari tabs, standalone iOS PWAs, embedded frames and background workers may differ in certificate availability/selection; workers cannot display an interactive certificate picker. **Do not promise iOS PWA/push reliability behind mTLS until physical-device testing passes**, including reboot, locked phone, cold start, background update and notification tap. If unsupported, label remote use browser-only and retain private/VPN PWA use; an optional native client or separately approved device gateway is future scope. Never open unauthenticated paths to make notifications work.

Bound TLS handshakes/connections at the firewall or a reviewed edge limiter; Caddy core is not assumed to provide an HTTP rate-limit directive. Proposed starting HTTP budget after pairing: 120 requests/minute/device, burst 60, with separately bounded downloads and WebSocket upgrades; tune against dashboard/terminal tests. Preserve existing application login/download limits and 4 MB body bound (git pushes have their existing separate 2 GiB limit). Log bounded TLS rejection counts, policy revision, device ID, path without query, status and revocation events; redact cookies, authorization, CSRF, QR secrets and identity bundles. Restrict access and rotate logs (proposed 14 days). TLS failures may have no HTTP access-log record; monitor them separately. Use device + real peer address for public throttling, never an arbitrary forwarded IP.

## Machines using the public address

Keep [MULTIHOST](MULTIHOST.md)'s Ed25519 nonce/primary-ID challenge and revocation; mTLS is an additional transport gate, not replacement node authentication. Bind each node certificate to its enrolled node ID. Public node identities may reach connect, their certificate-renewal route and required update downloads only; they cannot reach browser APIs. Browser identities cannot impersonate a node. Enforce this route/type boundary at Caddy before proxying; normal node authorization still applies.

| Endpoint group | Private hostname | Public hostname |
|---|---|---|
| Pairing/CA issuance bootstrap | Private address + owner confirmation | Denied even to paired clients |
| `/api/nodes/ca`, `/install.sh`, `/bundle` under `/api/nodes` | Existing limits and private address | Only active paired devices; node downloads bound to their node identity |
| `/api/nodes/enroll` | Private address + existing one-time code | Paired node identity + existing enrollment code only |
| `/api/nodes/connect` | Existing signed-link checks | Node mTLS + same signed-link checks |

The [current bootstrap routes](../src/acc/server/api/nodes.py) are application-public behind today's private edge. The proposal makes install/enroll **LAN-or-paired**, never anonymous internet downloads. Initial public enrollment requires an out-of-band paired node certificate first: generate key/CSR on the node, transfer CSR to the owner on LAN, return certificate securely. Simpler first release: install/enroll on LAN, then switch the existing node to the public address. Do not expose the current curl installer anonymously to break the bootstrap cycle.

[Node TLS contexts](../src/acc/node/identity.py), [enrollment](../src/acc/node/enroll.py), [config](../src/acc/node/config.py) and [client](../src/acc/node/client.py) need explicit cert/key paths, restricted service-account ownership and renewal/reconnect handling. Add client identity options to every downloader, not just the WebSocket. Git backup pushes originate in brokers and also need a scoped device identity/transport strategy; qualify them before advertising complete public-machine support. Never distribute the CA signing key or a shared browser identity to brokers.

Server trust and client identity remain independent: `--ca-fingerprint` pins the **server CA**, while the new device certificate authenticates the node to Caddy. Preserve hostname verification and the pinned-root-only trust behavior. Switching from private Caddy CA to a publicly trusted hostname requires an explicit owner-authorized trust change to `--trust system`, not fallback on verification failure. For private public-endpoint server trust, transfer the pinned root alongside the client certificate; `/api/nodes/ca` cannot be fetched anonymously first. Preserve the existing one-use ten-minute enrollment code and signed challenge after TLS.

## Turning public access on and off

Proposed Settings → Remote access follows [DESIGN.md](design/DESIGN.md); no UI is changed by this document.

1. Default **Off** on install, upgrade and recovery with unknown policy state. Require an owner on the LAN, recent password/TOTP reauthentication, and at least one tested paired recovery device. Show mode, public names, device count, preview scope, risk and rollback in the standard confirmation dialog. Record actor/time/configuration digest; a later hostname, mode or trust-boundary change requires new confirmation.
2. Owner configures a dedicated DNS-only A/AAAA record to the public address, reserves the main's LAN address and forwards TCP 443 to Caddy. Check IPv6 firewall separately; do not publish a broken AAAA. No accd, broker, dev-port or admin-socket exposure, no automatic UPnP. Use split DNS or hairpin NAT for paired LAN access to the public name. Private DNS stays private. Dynamic-IP updates require separately approved DNS credentials. Carrier NAT/double NAT may require the tunnel option.
3. Stage and validate the deny-by-default mTLS policy before forwarding traffic. Owner confirms exposure, then run an **outside** self-test (phone on cellular or a controlled external probe): no cert, wrong CA, expired and revoked cert must fail before any HTTP response from ACC; valid device reaches normal sign-in; wrong private SNI/Host cannot bypass it. Check API/downloads, terminal reconnect, revocation of an open stream and no unexpected IPv6 path. A test from the main or LAN is insufficient. Mark “Enabled — external test pending” until verified; roll back on a failed gate, never weaken it for a public health check.
4. Persistent prominent **Public access ON** indicator for all users, with mode and owner-only manage/off control; Settings shows actual applied policy and external-test time, not just desired state. Audit issuance, confirmation, reload failure, enable and disable. Keep controls keyboard/phone accessible using existing components.
5. **Turn off** immediately closes public streams and removes/disables public proxy/tunnel routes through the helper; keep the private recovery hostname. Confirm applied state and repeat outside rejection test. DNS removal/router cleanup is secondary, since DNS caches persist. Preserve device records for deliberate later reuse; lost-device revocation is separate. If reload fails, show public access as still on and provide owner console/firewall shutdown instructions.

## Carrier-NAT tunnel choices

| Mode | Device gate and consequence |
|---|---|
| Direct Caddy (recommended) | End-to-end browser→Caddy TLS with device verification; requires reachable 443. Internet sees Caddy's TLS surface and server certificate, not ACC. |
| Tailscale Funnel | Prefer raw TCP forwarding of the TLS stream into the dedicated mTLS Caddy listener, **without** `--tls-terminated-tcp` or an HTTP reverse proxy. [Funnel CLI](https://tailscale.com/docs/reference/tailscale-cli/funnel) distinguishes TCP and TLS termination. Qualify that the selected version/port supports passthrough, a matching server certificate for its assigned name, WebSockets and client identity. If it cannot, do not enable it. Funnel itself is public, not tailnet device authentication; Tailscale Serve/VPN is a different mode. Provider availability, names and limits apply. |
| Cloudflare-terminated Tunnel | **Rejected by the owner, 2026-10-24.** Ordinary HTTP Tunnel terminates browser TLS at Cloudflare, so Caddy cannot verify the original client handshake. Provider mTLS/Access assertions do not preserve the accepted browser-to-Caddy boundary. Use direct access or a qualified raw TCP tunnel instead; no provider-terminated fallback. |

No tunnel may target the private hostname as loopback traffic and thereby inherit its LAN allowance. No cookie-only Access/IdP fallback: it removes the required device gate. Only raw TCP passthrough preserving TLS to Caddy is eligible for tunnel qualification. A self-hosted raw TLS relay could preserve Caddy mTLS behind NAT, but adds a public server, patching, abuse handling and availability work; build only if these options fail a demonstrated need.

## Threat model

| Threat/mode | Reach and mitigation / residual risk |
|---|---|
| Public access off | Internet gets current Caddy denial, no ACC routes; LAN clients can still reach login without a device certificate. Untrusted LAN/container access remains existing scope. |
| Direct or passthrough on, no identity | Attacker reaches TCP/TLS and can observe names/certificates, consume handshake capacity or attack Caddy; no ACC login, API, static page, bootstrap or preview code. mTLS is not invisibility or DDoS protection. |
| Cloudflare-terminated boundary (rejected) | Provider compromise/misconfiguration could bypass the transport gate. This trust change is not an approved remote-access mode. |
| Credential stuffing | Unpaired internet attacker cannot submit ACC credentials. Paired malicious clients still face password + TOTP, rate limits, sessions/CSRF and account permissions. Do not rely on current in-memory throttles for distributed attack resilience. |
| Stolen paired device | Certificate admits it to login; an unlocked device with a live session can act immediately. Revoke certificate **and** sessions/streams/push. PKCS#12 export or malware may clone its identity; a certificate proves key possession, not physical hardware. |
| Compromised CA key | Active-leaf allow list prevents a newly forged leaf alone from entry, but compromise of signer plus policy writer defeats that. Disable public access, rotate CA/intermediate, invalidate all affected leaves/session tickets, re-pair via trusted LAN, audit issuance and restore clean policy. Never automatically trust certificates merely because they chain to this CA. |

Out of scope: a compromised main/root, malicious trusted owner, endpoint malware, existing broker/agent isolation weaknesses, router takeover, protecting separately published applications, upstream-provider outages and volumetric attacks. Pairing protects the front door, not execution isolation or data already stolen.

## Effort, phases and owner answers

Planning estimate for one engineer: **5–8 engineer-weeks**, plus device testing/owner network changes; not a delivery commitment. Tunnel/provider and native-client work can extend it.

1. **Qualification (3–5 days):** installed Caddy verifier/chain semantics, empty allow list, SNI/Host/coalescing, resumption, reload revocation, iOS Safari/PWA and Android identity imports. Stop if the pre-ACC gate cannot be proved. Decide browser-only scope if PWA fails.
2. **Pairing + direct edge (2–3 weeks):** signer/helper, device registry/audit, one-use QR/import, expiry/renewal/revocation, approved configuration and public status/off flow. Tests at CA/API/helper boundaries and browser sign-in; no-certificate probes must show zero accd requests.
3. **Machines + operational readiness (1–2 weeks):** all TLS clients/downloaders and git backups, preserve pinned trust/signed link, node removal/rotation, external IPv4/IPv6 tests, lost-device and CA restore drills; document actual installation screens.
4. **Compatibility and optional features (1–2 weeks):** physical OS/browser matrix, sleeping terminals, worker/push lifecycle; remote preview and one tunnel only after their separate gates pass. Record supported versions and failures rather than weakening authentication.

Owner answers accepted 2026-10-24 in decision 0020 (private decision record):
separate public hostname with private LAN fallback; 90-day browser certificates
with manual renewal from a signed-in device; one-use server-generated PKCS#12
phone import expiring within 15 minutes and deleted after import (server copy
destroyed on download); browser-only iOS if the Home Screen app cannot present a
certificate; remote previews private initially; **no Cloudflare-terminated
boundary**, using a direct port or qualified raw TCP tunnel instead. These are
no longer pending owner approval. Exposure and technical qualification remain
separate gates; the broad opt-in/443/machine direction is not reopened.

For [several mains later](research/high-availability.md), use one logical device authority, authoritative versioned revocations and a stable server hostname. Every edge must acknowledge policy before enable/renewal; a stale edge must be fenced from public routing. Do not copy CA keys and independently editable allow lists between mains or call that HA. Signing ownership, recovery and stream invalidation must join that design's leadership/fencing work.

## Phase 1 results — machine qualification, 2026-10-24

**The automated pre-ACC gate is qualified for new TLS connections on the installed
Caddy binary with the exact lab policy below. Overall remote-access release is
NO-GO pending physical-device qualification and the later implementation gates.**
This phase adds no product routes, pairing, UI, deployment configuration or public
exposure. It does not authorize enabling remote access.

### Reproduce and identify the binary

Run from this checkout:

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q tests/test_remote_access_qualification.py
```

The focused run completed with **27 passing tests**. The [qualification harness](../scripts/remote_access_qualification/README.md) and
[pytest probes](../tests/test_remote_access_qualification.py) run the installed
`/usr/bin/caddy`; `caddy version` reports exactly **`2.6.2`**. Its SHA-256 is
`9681d054b6e5137d53f6d027188b349e0603fd12ea6334bd0d49da6755432f1b`.
`caddy list-modules` includes `tls.client_auth.leaf` (98 standard modules,
zero non-standard modules). Requalify a different build/version rather than
assuming this result transfers.

Each test starts its own Caddy and counting stub, with kernel-assigned high
loopback ports, a separate Unix admin socket, throwaway server/device/wrong CAs,
and temporary certificate/key/config/storage/home directories under `/tmp`.
The server certificate covers both example names to exercise coalescing.
No real DNS, `/etc/caddy`, running Caddy, system trust store or ACC socket is
used. All processes and temporary files are cleaned up by the harness.

### Exact passing directives

The harness uses native **JSON**, not a Caddyfile. The complete reproducible
configuration is `Lab.config()` in
[harness.py](../scripts/remote_access_qualification/harness.py). Its essential
fields on Caddy 2.6.2 are:

```json
{
  "apps": {
    "tls": {
      "session_tickets": {"disabled": true}
    },
    "http": {
      "servers": {
        "qualification": {
          "automatic_https": {"disable": true},
          "protocols": ["h1", "h2"],
          "strict_sni_host": true,
          "tls_connection_policies": [
            {
              "match": {"sni": ["remote.example.com"]},
              "client_authentication": {
                "mode": "require_and_verify",
                "trusted_ca_certs": ["BASE64_DER_DEVICE_CA"],
                "verifiers": [{
                  "verifier": "leaf",
                  "TrustedLeafCerts": [{"Raw": "BASE64_DER_ACTIVE_LEAF"}]
                }]
              }
            },
            {"match": {"sni": ["acc.example.lan"]}}
          ]
        }
      }
    }
  }
}
```

This is an excerpt with certificate placeholders, not a deployment file. The
harness supplies manually loaded server certificates, loopback-only listeners,
a private admin socket and explicit host routes to the stub. Its private host
retains the source ranges specified above. There is no default TLS policy for
unknown SNI and no unknown-host proxy route. HTTP/3 and automatic HTTP redirects
are disabled in this lab; port 80 is never bound.

Caddy 2.6.2 exposes the leaf module's `TrustedLeafCerts` as Go certificate
objects. The `Raw` field takes base64 DER; the verifier compares
that encoded certificate. This unusual, version-specific representation is
qualified here, not a promise of a stable schema. For **zero active devices**,
keep the module and set `"TrustedLeafCerts": []`. Never omit the verifier.
The version's [connection-policy and leaf-verifier source](https://github.com/caddyserver/caddy/blob/v2.6.2/modules/caddytls/connpolicy.go)
explains the fields; the actual binary's tests are the evidence for this result.

### Observed results and failed alternative

| Probe | Observed result |
|---|---|
| Valid listed client; private recovery with no client identity | Each reaches the stub with HTTP 200; confirms a live upstream and usable policies. |
| No client certificate, TLS 1.2 and TLS 1.3 | TLS refusal; zero upstream requests. |
| Revoked/unlisted leaf with a valid device-CA chain, both TLS versions | TLS refusal; zero upstream requests. |
| Expired leaf **included** in membership, both TLS versions | TLS refusal with a certificate-expired alert; zero upstream requests. Membership does not override expiry. |
| Wrong-CA leaf **included** in membership, both TLS versions | TLS refusal with an unknown-CA alert; zero upstream requests. Membership does not override CA verification. |
| Explicit empty leaf verifier after reload | No identity, valid-chain, revoked, expired and wrong-CA probes all refused; zero upstream requests. |
| Public SNI/private Host; private SNI/public Host without identity | Caddy 421; zero upstream requests. |
| Unknown Host; unknown or absent SNI | 421 or TLS refusal as appropriate; zero upstream requests. |
| HTTP/2 cross-authority reuse, public→private and private→public | Same TLS connection sends allowed, cross-host, allowed streams. Only matching streams reach the stub; cross-host stream adds zero requests. ALPN must actually negotiate `h2`. |
| Successful revoke/reload, TLS 1.2 and TLS 1.3 | Local apply finishes within the asserted two-second ceiling; five immediate new connections with the removed leaf are refused, zero added upstream requests. This is a local test bound, not an internet latency guarantee. |
| Candidate policy session reuse | No ticket issued on either TLS version; explicitly offering the prior SSLSession after revocation/reload fails without reaching the stub. |
| Real previously issued ticket, both TLS versions | Deliberately enable tickets in the disposable lab, obtain one and prove a successful resumed connection. Revoke, reload with tickets disabled, offer the old ticket: refused, zero added upstream requests. |
| **Unsafe alternative: deprecated `trusted_leaf_certs: []` with no verifier** | **FAILS the design:** a valid-chain unlisted/revoked leaf reaches the stub (HTTP 200, count 1). Kept as a negative control proving the counter detects a bypass. Never deploy this representation. |
| Teardown | Owned Caddy exits and its scratch directory is removed. |

The ticket-disable switch is TLS-app-wide on this tested policy, not scoped to
one SNI. Production integration must review its effect on other published sites;
this isolated configuration is not a drop-in change to the running edge.

The passing candidate uses both normal chain/expiry verification and explicit
leaf membership at TLS, with no HTTP certificate-fingerprint substitute. A gate
regression must fail the suite; it must never be changed to allow traffic or
silently skip qualification. The unsafe alternative is an explicit
characterization test, not a passing security candidate.

### Boundaries and owner checklist

Reload/new-handshake revocation is proved; **reload alone does not revoke an
already authenticated keepalive, HTTP/2 connection, WebSocket or session in
ACC**. Per-request active-state enforcement, stream closure within 30 seconds,
expiry of long-lived connections, identity-header integrity, atomic helper
apply/failure recovery and server/client CA lifecycle remain later product work.
The lab does not exercise real accd, OS identity stores, browser certificate
pickers, public routing, IPv6, firewall limits, port-80 deployment policy,
provider tunnels or an outside-network probe. Those are not inferred from a
loopback pass. No browser/PWA compatibility claim is made.

Owner: record physical device model, OS/browser versions, date, result and any
certificate prompts for each item. Use test identities and retain private/LAN
recovery. Do not forward a public port for this checklist without the later
approved implementation and exposure procedure.

- [ ] **iOS/iPadOS Safari:** import a client identity profile (`com.apple.security.pkcs12`) with the separate password; distinguish it from the existing server-trust-only profile. Confirm certificate selection, fresh navigation, normal sign-in after the gate, browser restart, reboot, and rejection after removal, expiry and revocation.
- [ ] **iOS Home Screen PWA:** qualify separately from Safari. Test first installation, cold start, locked/rebooted phone, background worker/app update, push subscription renewal and notification tap. Verify no hidden unauthenticated exception. If it fails, record browser-only scope and keep private/VPN PWA use; do not weaken the gate.
- [ ] **Phone terminal lifecycle:** sleeping phone, wake, WebSocket reconnect and revocation while open; confirm the later implementation closes affected streams within 30 seconds.
- [ ] **Android Chrome:** import encrypted `.p12` as a user identity; record actual OEM/OS settings and certificate-picker behavior. Test restart, reboot, expiry, revocation, removal/reimport, and work-profile restrictions. Qualify Firefox separately if offered.
- [ ] **Other supported browsers:** macOS Safari/Chrome, Windows Edge/Chrome and Linux browser stores, plus Firefox's separate store as needed. Record identity selection and renewal/manual reimport behavior.
- [ ] **External owner acceptance (later phase):** cellular/outside IPv4 and IPv6 no-cert/invalid-cert probes with zero accd requests, private fallback, enable/off and failed-reload recovery, lost-device/session/stream revocation, and CA restore/rotation drill.

**Stop rule:** if any required gate probe fails on the intended deployment
binary/configuration, the outcome is **NO-GO** until corrected and requalified.
A physical-device failure narrows supported scope only as already approved
(e.g. browser-only for an incompatible PWA); it never permits an anonymous path.

## Phase 2 first slice — LAN pairing preparation

This subsection records the first shipped slice. The next subsection supersedes
its absence of apply/status and rendered-only revocation.

The device registry, migration 0050 (after current head 0049), dedicated uninstalled
client CA helper, one-use LAN pairing/owner confirmation/import, manual LAN
renewal, rename and revocation are implemented as preparation. Pairing defaults
to off. Settings → Remote access cannot enable public access; it saves preparation
choices and renders a fragment only. Nothing is installed or applied to Caddy.
The renderer targets the exact Phase 1 Caddy 2.6.2 `TrustedLeafCerts` representation,
including the empty verifier, with a registry-to-disposable-Caddy revocation test.

The accepted owner answers use these preparation settings: separate public hostname and
private recovery origin, certificates lasting 90 days (shorter configurable),
manual renewal on LAN, one-use encrypted PKCS#12/Apple identity-profile delivery
with five-minute in-memory retention after confirmation (within the approved
15-minute ceiling), and private previews. No Cloudflare-terminated transport is approved.
Members may start pairing and view their identities; an owner recently verified
on LAN must confirm before issuance. Download consumes the bundle; ACC cannot
observe OS import. A failed download needs fresh pairing. Restart loses bundles.

The initial CA is a dedicated online root with clientAuth-only EKU and path length
zero. Offline-root/intermediate provisioning and encrypted backup/restore drills
remain operational work. The helper has a fixed stdin request and socket client;
installation/service confinement is a later reviewed owner step. The signer key
never enters accd's database or its normal backups.

Renewal currently requires recent sign-in verification and private LAN access;
it preserves the device ID and replaces its leaf with zero overlap. Public
certificate-proven renewal depends on the later trusted edge identity boundary.
Revocation invalidates all that person's sign-ins and push subscriptions and closes
registered terminals. Removing a person also revokes devices and pending imports.
Rendered revocation is always visibly pending until a later owner applies policy.
No applied-edge state, live last-seen identity, public enable/off, remote stream
enforcement, public machine support, phone OS import or PWA compatibility is
claimed by this slice. The overall remote-access release remains NO-GO under the
Phase 1 stop rule until those qualification and implementation gates pass.

See [the Guide](wiki/User-Guide/Remote-Access.md),
[internal boundaries](wiki/Internal/Device-Pairing.md) and the
[helper contract](../deploy/device-ca/README.md).


## Phase 2 second slice — owner apply/status and revocation propagation

Settings → Remote access now separates saved LAN preparation from a complete
root-rendered Caddy review, explicit reviewed Apply, and always available Turn off.
The owner sees on/off/unknown, applied hostname, last verified revision, devices
allowed and reload result. Apply requires recent verification on private access;
OFF needs neither recent verification, hostname, CA nor pairing being on. Both
remain authenticated owner operations with normal CSRF/Origin checks.

The **uninstalled** helper in `src/acc/remote_access_helper.py` accepts only bounded
review/apply/off/status requests over a fixed root-verified Unix socket. Root
provisions the OFF base, hostname allow list, CA public certificate and qualification
flag (false by default). Apply independently renders the reviewed revision, verifies
the previous live policy, validates, writes atomically, reloads and verifies the
complete loaded JSON. OFF removes the public route/TLS policy rather than applying
an empty leaf gate. No caller path, command, upstream or arbitrary config is accepted.
Unrelated Caddy drift is refused instead of overwriting published sites or DNS.
Failure attempts OFF, never restores a revoked leaf, and remains unknown/pending
until verified. See [the root contract](../deploy/remote-access/README.md).

A durable Setting holds enable intent and the last acknowledgement. Revoke/remove
and person removal trigger registry reconciliation after commit; a 15-second
worker also detects expiry, registry changes, edge restart and loaded-policy drift.
Healthy helper propagation is bounded below one minute. Failure clears enable
intent and retries OFF, freezing issuance while pending. New enablement never
happens automatically after a restart/drift. Public enablement is refused for HA.

The API-to-helper-to-Phase-1-harness test loads the complete rendered JSON into
disposable Caddy, proves a listed identity reaches the counting stub, revokes and
re-applies, then proves rejection with no additional upstream requests and private
recovery still available. It also checks person disappearance, zero membership,
and actual OFF removal. Helper and API tests cover closed contracts, stale review,
validation/reload failure, drift, recovery and matching-code refusal. Pairing now
shows a six-digit winning-claim-bound code on the receiving device only; the owner
types it, and a wrong code cancels the pairing before signing (security review
2026-10-24).

This slice is **not production qualification or installation**. No live Caddy,
DNS, firewall, router, system packages or services were changed. The installed
Caddy binary/composition, DNS/publication cooperation, confinement, restart-safe
loading and immutable helper environment need a reviewed deployment step before
the root qualification flag can be enabled. Trusted per-request device identity,
existing-stream/expiry enforcement, physical browsers/PWA, outside IPv4/IPv6 and
CA recovery drills remain gates. Overall public remote access remains **NO-GO**.

## Phase 3 first slice — operational drill coverage

This is a bounded **test and runbook slice**, not completion of phase 3. All
product defaults and public node/installation denies remain as shipped. No new
routes, schema, helper operations, installation or exposure are added. Main's
migration head was checked at 0050; this slice needs no migration (0051 is the
next free number at this checkout, to be rechecked by the implementing slice).
The route ownership inventory therefore needs no new classification.

Reproduce with:

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q tests/test_remote_access_readiness.py
```

The tests join actual signer issuance and owner APIs to complete helper-rendered
policy in the disposable Caddy harness. They cover Revoke and Remove separately,
refusal of new TLS 1.2/1.3 connections within one minute with no additional
upstream requests, surviving identity access, affected-person sign-out and private
recovery. The CA restore drill loses a temporary signer, restores a test snapshot
predating revocation into new state, proves both an old active identity and a new
issuance still work, and proves the revoked leaf remains refused. Restoring only
the signer never restores enable intent. Current revocations remain authoritative;
an explicit review/apply is required after OFF.

The snapshot is a test-only, already decrypted filesystem fixture. Encrypted
authority backup/restore automation is **not shipped**. Production restore must
verify an independently recorded CA fingerprint, protect/recover the password,
retain the issuance ledger and preserve current revocations. If current
membership cannot be reconstructed after database loss, remain OFF and re-issue
affected identities from private access rather than trusting an old snapshot.
See the [recovery and CA key rotation runbook](wiki/Internal/Device-Pairing.md#operational-drills-and-authority-recovery)
and the [owner network/phone checklist](wiki/User-Guide/Remote-Access.md#ca-recovery-and-real-network-acceptance).

## Phase 3 second slice — machine identities and established connections

Recorded in [decision 0048](decisions/0051-machine-identities-and-edge-enforcement.md).
Migration 0051 adds `device.node_id` and `retire_after`. Everything remains off
by default (`machine_identities` false, public access off, helpers uninstalled).

- **Machine identities.** The root signer's closed `issue_machine` signs a
  node-held P-256 CSR as `OU=acc-machine,CN=<node id>`, clientAuth only, at most
  30 days. Issuance and rotation ride the existing signed link; the node keeps its
  key, presents the identity on the link and update download (`NodeTransaction`),
  and keeps its pinned server CA and Ed25519 challenge unchanged.
- **Removal and rotation.** Removing a node revokes its leaves in the same
  transaction, sweeps its open public connections and reconciles policy. The
  successor is requested with 10 days left; the node switches only after the
  verified applied policy admits it (or public access is off), reconnects once,
  and the old leaf retires when the successor is presented or 24 hours after issue.
- **Route split.** At Caddy by verified subject: machines reach only connect,
  bundle and `/api/git/*`; browsers never do; install/enroll/CA/pairing/preview
  stay denied to every public identity. Install and enroll therefore remain
  private-address only (the simpler first release this design allows).
- **Established connections.** The edge sets `X-Acc-Edge-Client`; accd checks
  every public request and WebSocket against the registry, binds a machine leaf to
  its node and its projects, and closes registered streams with 4403 at once on
  revoke/remove (device, person, node) and within 15 seconds on expiry,
  retirement or turning machine identities off.
- **Authority backup.** Root-only `acc-device-ca-recovery.py` seals key,
  certificate and ledger (scrypt + AES-256-GCM) with a root passphrase outside the
  backup set; restore requires the independently recorded fingerprint and a new
  directory. Root `acc-backup` adds `device-ca.enc` or reports that it skipped it.

Tests: `tests/test_device_ca_machine.py` (profile, closed contract, restore drill,
recovery tool, acc-backup), `tests/test_machine_identities.py` (link issuance,
rotation, binding, removal, sweep, per-request split, ASGI stream closure, node
TLS), `tests/test_remote_access_machines.py` (route split, header overwrite and
removal against disposable Caddy 2.6.2).

Still outstanding: public
install/enroll for paired devices, live helper/Caddy installation and qualification,
physical machines, outside IPv4/IPv6 and carrier-NAT tests, real phone imports,
dual-CA rollover and HA. Overall release remains **NO-GO**.

## Phase 3 remainder — Turn off ends open public connections

Measured against disposable Caddy 2.6.2: unloading the public route on OFF closes
upgraded WebSockets (close 1001) but keeps streaming any HTTP response that began
before OFF. accd therefore treats a cleared enable intent as a reason to refuse
public requests and to close every registered public stream (4403 /
`http.disconnect`) immediately, before and regardless of the helper's answer; the
15-second sweep covers intent cleared by a failed apply, restart or drift.
The security review (`docs/security/2026-10-25-review-remote-access-phase3-rest.md`,
M1) moved this ahead of the remote-access lock. A reconciliation stuck on the
helper can no longer delay Turn off, and an apply in flight can't re-enable access. Tests:
`tests/test_remote_access_off_streams.py` and the Caddy behaviour test in
`tests/test_remote_access_machines.py`.

## Phase 3 remainder — machine identities on Machines, planned authority rollover

Host → Machines shows each machine's public identity read-only: issued or pending, valid until, last use through
the public address, rotating, from the previous authority, and an expires-soon or renewal-overdue warning.

A scheduled device-authority change no longer needs a public maintenance window
([decision 0062](decisions/0062-planned-device-authority-rollover.md)). Root stages a new authority, installs it
and the old one in the edge's policy directory, and promotes. The edge trusts at most those two, and only those
that issued a listed leaf. Machines on the old authority rotate at once, browsers are flagged for renewal, and
owners see the remaining count before `finish` retires the old certificate. After the security review, promote
deletes the old key, accd audits each authority change, and rollover reissues are capped at 10 an hour.
Tests: `tests/test_device_ca_rollover.py`, including both authorities live in disposable Caddy 2.6.2.
Compromise response is unchanged.

Still outstanding: public install/enroll for paired devices and the git-backup forwarder decision (owner), live
helper/Caddy installation and qualification, physical machines and phones, outside IPv4/IPv6 and carrier NAT,
and HA. Overall release remains **NO-GO**.

## Roaming git-backup transport (decision 0058)

Updated nodes now support a job-scoped local forwarder for git backup, gated by
machine identities, verified remote-access enablement and explicit per-machine
owner consent (`Node.public_git_backup`). Owner decision 2026-10-25: that consent
is false by default, including existing machines after migration 0068. Owners
change it in revision-checked Machine settings; `node.public_git_backup` audits
changes. The main sends a
short-lived project/node token-exchange grant over its signed link. The node tries
the usual private backup origin first and falls back to the public origin only
on connection-establishment failures. It uses system server trust and its own
client leaf publicly; TLS/HTTP refusals and redirects never trigger fallback.
The node pushes only after the exchange returns the token whose SHA-256 the signed
job carries, so a server that only holds a certificate accepted by system trust
(another CA's, or a TLS-inspection root's) gets no git data.

- **Origin pin.** The node takes `git-backup` jobs only over a control link
  established privately; one arriving through the public name is refused. It
  pins the origins learned privately, so a former public name still counts as
  public.
- **No other pins.** The server leaf is not pinned (it rotates with ACME), and
  neither is the edge revision.
- **Consent withdrawal.** Recheck after the per-node queue wait and at public
  edge/token/push admission. Opt-out revokes pending/public grants and closes
  this node’s public git streams, without closing private pushes or control links.
  Already committed refs are retained.
- **Audit.** The first public token exchange per grant is audited as
  `git_backup.public_token`.

Only this project's token exchange and receive-pack protocol use that selected
path. The broker gets a token-scoped loopback URL, never the machine key; the
main retains active-leaf/project authorization and confirms the token path before
recording successful public use. Host shows its timestamp per selected machine.
Control, updates, enrollment and preview addressing are unchanged. Scheduling
still needs an active signed node link; a disconnected node does not schedule
backups from cached projects. Details: MULTIHOST.md, “Roaming machines”.

Tests join the actual broker, streamed Git protocol, token endpoint and registry
with temporary servers, and prove the client certificate/leaf removal against
disposable Caddy. No live edge, helper, DNS, firewall, bucket or service is touched.
Physical roaming and public-network qualification remain outstanding; public
release remains NO-GO.
