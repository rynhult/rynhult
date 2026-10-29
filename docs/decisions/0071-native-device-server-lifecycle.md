# Native device server lifecycle

Date: 2026-10-25. Status: implemented experimental server contract, off by default.

## Context

MOBILE-APPS phase 1 needs app-held identities without browser PKCS#12 export or an
anonymous public enrollment exception. Device TLS identity and person sign-in are
separate authorities. Public remote access and physical mobile qualification are
still NO-GO; this task authorizes server code, not deployment.

## Decision

`ACC_MOBILE=1` exposes the versioned native lifecycle APIs and admits `native`
registry rows into existing edge policy. It defaults false. Enrollment and manual
rotation require a recently verified human owner at the configured private LAN
origin. The owner names the existing person. A five-minute capability and a P-256
proof bind the receiving installation, CSR, platform and origin; the receiving
six-digit code confirms the winning key. A wrong code cancels before signing.
Approval is compare-and-set; repeats after success return the approved result.
An uncertain signer failure cancels rather than automatically issuing again.

The privileged signer accepts a closed `issue_native` contract, ignores requested
names/extensions, issues `OU=acc-native,CN=<server-assigned device ID>`, clientAuth
only, for at most 90 days, and returns only a leaf. It shares the existing durable
20-per-hour quota. accd validates the key, profile and CA before registry insertion
and delivery. Device private keys never reach accd. Installation IDs are random
32-character lowercase hex values, not hardware identifiers.

Keep cookie, CSRF, Origin, password/TOTP and passkey authentication. Native local
sign-in cookies are fingerprint-bound from creation, initially usable only for
state, sign-out, verification and explicit session binding. Binding rotates both
cookie and CSRF, caps the session at 12 hours and certificate expiry, and closes
old terminal attachments. The exact person must own the native leaf. Proof requires Caddy's overwritten fingerprint, Unix ingress, the configured
public hostname and a separately provisioned secret edge assertion. Unix/Host
alone are insufficient if a different private proxy forwards forged headers. The
root helper refuses native activation without its random 256-bit assertion key;
accd requires the matching private main-owned key file. The assertion is redacted
from reviews and consumed before handlers/upstreams. No app receives it. Native
installation API-token use and OIDC entry/callback are refused. Native OIDC is
left for separate qualification instead of extending another credential path.

Issuance alone means pending. Existing owner review/apply must verify the policy;
only the first bound connection marks it verified. Rotation uses a new key on the
same installation/device ID with zero leaf overlap. Revoke is human-owner-only,
requires recent verification, and is unthrottled as a safety action. Preserve the
existing broad effect: terminate **all** that person's sign-ins, terminals, push
subscriptions and pending pairings. Edge requests reject revoked/expired leaves;
open streams close immediately on revoke, or on the existing 15-second sweep for
expiry, rotation and feature disable. Policy reconciliation can remain pending;
never report a successful reload merely because membership changed.

## Consequences and alternatives

No native bearer/refresh protocol, public renewal, overlapping leaves, PKCS#12
fallback, automatic CA trust bootstrap, relay or app code is added. Apps must
already trust the private server's HTTPS identity and compare the trusted owner's
server name; the client-device CA is not the HTTPS server CA. This is safer than
accepting a trust root supplied by an untrusted QR. A separate app bootstrap flow
and native OIDC need review before adoption.

Owner-only enrollment/rotation/revoke is stricter than self-service device
management. A 12-hour session requires foreground sign-in again; locked background
refresh is not supported. Lost-phone revocation also signs out other devices for
that person. These conservative choices are explicit owner review items.

Local tests do not qualify OS key storage, app lock/uninstall, TLS reuse across
real iOS/Android stacks, a public edge, or the 30-second operational stream target.
No services, account settings, store registrations or live policy are changed.
