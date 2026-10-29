# Mobile Devices

This is the experimental server contract for native iOS/Android enrollment and
credentials. It supplies no app or push relay and does not qualify public access.
It builds on [Device pairing](/Internal/Device-Pairing) and leaves browser and
machine identities separate. The design and threat gates are in `docs/MOBILE-APPS.md`.

The October 27 v1 scope keeps the Linux server and browser/phone web app in v1;
native apps are post-v1, delivered one platform at a time in an order not chosen
yet (native Windows, macOS standalone, Linux desktop, iOS, Android). This existing server contract and
mobile source remain experimental groundwork, not a v1 native app commitment.
See [the scope record](/Internal/Decisions-and-Roadmap#v10-linux-server-and-browser-scope).

## Configuration and trust boundaries

`ACC_MOBILE` defaults off. Only `1`, `true` and `yes` enable it. Off hides every
`/api/mobile/v1` endpoint with 404 and excludes native leaves from edge policy;
per-request enforcement and the existing sweep reject/close their connections.
Private/browser access survives. There is no setting screen or automatic helper
installation. Deployment and physical qualification remain owner work.

```text
Owner private HTTPS sign-in + recent verification
  → five-minute pairing → app-held key/CSR + claim proof
  → owner types receiving-device code → root signer (leaf only)
  → native registry leaf → existing reviewed edge apply
  → new mTLS connection + normal person sign-in → bound session
```

The root signer and policy helper are the existing fixed-socket services, still
uninstalled by this feature. A native identity is `kind=native`; its random
installation ID, platform (`ios`/`android`) and public-key hash accompany existing
person, fingerprint, serial and expiry fields. Migration 0067 also adds native
claim metadata to `device_pairing` and fingerprint/binding time to `auth_session`.
Existing pairings default to browser. Unique person/kind/installation tuples
prevent duplicate enrollment. Fresh installs must generate a new installation ID.
No private app key, session bearer token or pairing capability is stored in clear.
A separate edge assertion key is retained only in private provisioned files; it is
never an app/session credential.

## API v1

All native responses are no-store. Normal cookie mutations require the existing
CSRF header and allowed Origin. An API token is not a person. Private endpoints
also require a private peer, the exact configured private hostname, pairing on and
an allowed/absent Origin; forwarding headers over TCP cannot spoof private access.

| Method and suffix under `/api/mobile/v1` | Contract |
| --- | --- |
| POST `/pairings` | Recent human owner on private LAN; `{person,label}` starts a five-minute link. Response has ID, private origin, expiry and `/pair-mobile#<id>:<token>` capability for native parsing, not a browser enrollment screen. |
| POST `/devices/{id}/rotate` | Same authority/body; active unexpired native device of that person. Claim must retain installation/platform and use a fresh key. |
| POST `/claim` | Private capability plus `{token,installation_id,platform,csr,proof}`; one winning receiver gets a receipt and six-digit matching code. |
| POST `/pairings/{id}/confirm` | Recent private owner supplies `{matching_code}`. Wrong code cancels; successful retry returns the same device, not a second issuance. |
| POST `/credential` | Private `{receipt}` returns the approved current leaf, CA chain, device ID, expiry and activation state until the original five-minute expiry. It is retryable because it contains no private key. |
| GET `/devices` | Human owner sees native metadata; a member sees only their own. Each device has a short one-way `installation` fingerprint. The `installation_id` (a rotation-claim factor) appears only on the private connection, never through the public edge. |
| POST `/session/bind` | Recent person sign-in through that person's native mTLS identity and verified edge policy; rotates cookie/CSRF, records proof of connection and closes prior terminal attachments. |
| POST `/devices/{id}/revoke` | Recent human owner; revokes native membership, all person sessions/push subscriptions and pending pairings, then closes streams and attempts edge reconciliation. |

Activation is `pending` until the verified policy revision includes the registry,
`awaiting_connection` until the first successful binding, `verified` afterward,
and `inactive` for a revoked/expired leaf. Registry `status=active` alone is not
proof that a phone has connected. Renewal clears prior connection proof.

The receiving app signs the UTF-8 encoding of these fields joined by exactly one
newline, with **no trailing newline**, using ECDSA/SHA-256 and its CSR P-256 key:

```text
acc-mobile-claim-v1
<pairing ID>
<raw pairing token>
<configured private HTTPS origin>
<32 lowercase hex installation ID>
<ios or android>
<lowercase SHA-256 hex of the exact CSR UTF-8 string>
```

`proof` is the DER ECDSA signature encoded as base64url without padding. CSR bytes
are limited to 2048; requested subjects, SANs and extensions convey no authority.
The root signer independently validates P-256 and the CSR signature. It issues a
clientAuth-only native subject for 1–90 days (owner's existing certificate policy).
Server output validation checks CA signature, exact profile/key and expiry. No
PKCS#12, password or export endpoint exists for native identities.

## Separate edge assertion prerequisite

A Unix peer and Host header alone do not prove that the public mTLS gate ran:
another private proxy path might pass forged client headers. Before native
activation, the owner must separately provision the same **random 256-bit key**
as 64 lowercase hex characters in root's `native_edge_key` policy field and in
`<ACC_DATA_DIR>/secrets/mobile-edge-key`. The main's file must be a regular,
no-follow, main-owned 0600 file with exactly 64 bytes (optional final newline).
No enrollment endpoint creates, reads back or distributes this key. Do not put
it in an app, QR, environment dump, repository or diagnostic output.

The root helper refuses a policy containing native leaves without this key. It
overwrites `X-Acc-Native-Edge` only on its gated public proxy and redacts that
header from review output; the real root-owned loaded configuration retains it.
accd compares it in constant time, also requires the Unix/public-host boundary,
then **removes the header before any handler or upstream can receive it**. Missing,
wrong, duplicate, unsafe-file or unreadable assertions refuse native requests.
Changing/removing the main's key ends existing native streams at the sweep.
Browser/machine requests do not acquire native authority through this header.

Key compromise requires OFF/private recovery, a fresh assertion key on both
sides, review/apply and qualification again. Keep the app feature off during that
maintenance; neither file nor Caddy is provisioned by this implementation. The
key is extra protection for the existing overwritten fingerprint, not a
substitute for mTLS, person sign-in or trustworthy server TLS.

## Session and failure behavior

Native password/TOTP and passkey sign-ins produce fingerprint-bound cookies from
creation. Before explicit binding only sign-in state, logout and verification are
usable. A cookie alone, another leaf, a different person's identity or an
installation API token cannot authorize native actions. Binding and every native request need the trusted public Unix-proxy path,
configured public hostname, separately provisioned secret edge assertion and
current verified policy at binding.
Native OIDC start/callback are refused until separately qualified. Sessions last
at most 12 hours or leaf expiry. Local biometrics do not satisfy server reauth.

No database lock is held while the signer runs. Claim/confirmation use guarded
state changes; person, approver role/session, feature flag, policy and rotated
leaf are rechecked before insertion. Uncertain signer failures cancel and audit;
start a new pairing rather than replaying an unknown issuance. No background
retry issues certificates. Parallel rotations cannot restore a revoked leaf.

Limits use three separate buckets, each 10 per 10 minutes per client prefix:
- **Device:** every claim counts, and a credential request counts when its receipt is unknown. A phone polling its own receipt while the owner confirms never uses up the bucket.
- **Bind:** session binding counts every attempt, also per person.
- **Owner:** start, rotate and confirm count only **failed** attempts, also per person. A refused verification or
  wrong pairing counts; an owner's successful steps never do.

An unauthenticated receiver therefore can't use up an owner's attempts. The in-memory buckets reset on server
restart. The root signer's shared persistent 20-issuance/hour ledger remains the independent bound. Revoke is
unthrottled, so rate-limit exhaustion can't prevent a safety action.

Audit actions are `mobile.pairing_start`, `mobile.claim`, `mobile.issue`, `mobile.rotate`,
`mobile.pairing_mismatch`, `mobile.issue_failed`, `mobile.pairing_cancel`, `mobile.session_bind`, `mobile.revoke`
and `mobile.refused`. `mobile.refused` covers every refused lifecycle attempt, including an owner-only or
person-only refusal, a missing recent verification, a 429, an invalid proof and a refused bind. Its detail holds
the operation, status, fixed reason and client address. These rows are size-capped: at most 10 per client and 100
in all per ten minutes, so a flood can't fill the audit log. No tokens, codes, proof signatures, CSRs or private
keys enter any detail. Metadata reads have no extra audit.

Use private recovery for a failed zero-overlap switch. For CA compromise/restore,
use the existing OFF, independently verified CA restore/rotation and LAN re-pair
drill; do not replace CA trust from a QR. A chain changed before delivery is refused.
The encrypted CA backup contains no app-held private key. Restore cannot recreate
a lost phone identity; a fresh installation pairs again.

The tests cover isolated API, signer, SQLite migration/races, actual terminal
bridge revocation, and local Caddy leaf admission. PostgreSQL races use the
throwaway harness when available. They do not qualify phones, app lock/secure
store/uninstall, HTTP/2 reuse on devices, public routing or relay delivery.

The Android enrollment library's TLS key manager reads the installed leaf/CA chain and opaque private-key
handle from Android Keystore for the current enrolled alias. It returns nothing for a stale alias or while a
claim is unfinished, and retains the device-CA issuer filter. The first disposable VM compilation exposed
missing accessors and ambiguous test transport arguments. The corrected client subsequently built on the
disposable VM with all 26 core unit tests passing; actual device TLS/Keystore qualification remains open.

## Disposable Android build check

On a reviewed main checkout, run `bash scripts/android_build_check.sh --dry-run` to inspect the
plan without downloading or writing anything. On a disposable Ubuntu 24.04 x86_64 VM, as root, run
`bash scripts/android_build_check.sh`. Never run its installation mode on an ACC host. The VM needs
`curl`, Python 3, `tar`, `sha256sum`, `flock` and `tee`; the script changes no system packages.

It verifies pinned JDK, Gradle and Android command-line archive checksums, accepts SDK licences, and builds
an isolated copy under `/opt/android-check`. It installs the declared compile SDK and explicit build-tools
version, or AGP 8.7.3's documented default build-tools 34.0.0. It runs the core JVM tests, Android debug
library build and Android unit-test task. The debug output is an **AAR**, whose path and SHA-256 are printed;
there is no application module or APK. No signing/store credentials are needed. Cold downloads require internet;
there is no publish/upload task or build scan. Logs and test reports stay under the same directory.

For a saved cache, use `bash scripts/android_build_check.sh --cache /mnt/android-cache/2026-10-26`. The cache
contains `SHA256SUMS`, `downloads/`, `sdk/` (including licences), and `gradle-home/`. Every file is verified before
seeding; unsafe paths, links and unlisted files are refused. This mode skips SDK network calls when the packages
are present and tries Gradle `--offline`. It fails on missing dependencies unless `--allow-online` was explicitly
supplied. Checksums require a trusted manifest and cache publisher. Full transfer, checksum and result instructions
are in the repository document `docs/BUILDING-ANDROID.md`.

Google refused the cloud VM IP for the tools ZIP with HTTP 404; fetching on another trusted host, checking the
pinned checksum and transferring the archive worked. A complete cache avoids that request entirely.

Rerunning reuses verified downloads and dependencies but recreates the source copy and tool installations
before a clean build. Preserve reports before rerunning. A successful build does not qualify Keystore,
StrongBox, TLS, phone lock or physical-device behaviour; those still need the Android device gate.
