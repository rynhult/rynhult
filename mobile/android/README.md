# Rynhult Android enrollment (mobile phase 1)

The Android side of native enrollment from [docs/MOBILE-APPS.md](../../docs/MOBILE-APPS.md). It talks to the server
contract on `mobile-phase1-server-review` (`/api/mobile/v1`, documented there in
`docs/wiki/Internal/Mobile-Devices.md`). iOS is a separate package on `ios-enrollment-client`.

It is a small library, not an app: no UI, push, WebView or app lock. **The corrected client built on a disposable Ubuntu 24.04 VM
with all 26 core unit tests passing (2026-10-26, coordinator-reported).** The cached rebuild procedure in
`docs/BUILDING-ANDROID.md` still needs its own VM qualification; no device behavior is qualified. No JDK,
Gradle or Android SDK is installed on the host it was written on, and none was installed for it. The commands below
are what to run first on a machine that has them.

## Layout

| Module | What | Needs |
| --- | --- | --- |
| `enrollment-core` | Pairing link, CSR and claim proof, the API client, credential validation, rotation, revocation, session binding, state | JDK 17 |
| `enrollment-android` | `AndroidKeystoreKeys` (P-256 in the Android Keystore, StrongBox when present, never exportable, usable only while unlocked) and `KeystoreKeyManager` (presents the identity to TLS without the key leaving the Keystore) | Android SDK 35 |
| `tools/record_fixtures.py` | Records the JVM tests' fixtures from the server's own code, in process | a server checkout |
| `tools/check_fixtures.py` | Checks the fixtures without a JVM | Python + `cryptography` |

The package `rynhult.enrollment` is a code namespace, not an application ID. Package IDs and signing keys are the
owner's decision before any release (docs/MOBILE-APPS.md, "Store and signing").

## Build and test (on a machine with the tools)

```sh
cd mobile/android
gradle wrapper --gradle-version 8.10.2          # once: creates gradlew and gradle/wrapper (commit them)
./gradlew :enrollment-core:test                 # JVM unit tests; needs only JDK 17
ANDROID_HOME=$HOME/Android/Sdk ./gradlew :enrollment-android:assembleRelease :enrollment-android:lintRelease
python -I tools/check_fixtures.py               # no JVM needed
```

Without `ANDROID_HOME` (or a `local.properties`), `settings.gradle.kts` leaves out `enrollment-android`, so the
JVM tests run on a plain JDK. Versions: Kotlin 2.0.21, kotlinx-serialization 1.7.3, Android Gradle plugin 8.7.3,
`compileSdk` 35, `minSdk` 28 (StrongBox and unlocked-device-only keys). Settle the real minimum in phase 0.

## Fixtures

`enrollment-core/src/test/resources/fixtures/lifecycle.json` contains 24 request/response exchanges. They were
recorded from the server branch at `40fbfe78`, in process, with the server's own test fixtures:
- a throwaway test device CA;
- an ASGI transport;
- the server tests' simulation of the trusted edge.

The certificates are real output of that test CA for two device keys derived from fixed test labels. That lets the
JVM tests rebuild the keys and validate genuine leaves and chains.

Each claim the client sends is checked as the server would check it: the CSR's self-signature, its key, and the
proof over the documented message. The tokens, receipts and cookies came from a test instance that no longer exists.
`.gitleaks.toml` allowlists only this file. No production root, enrollment secret, private key or signing
credential is in the repository.

The server's test fixture runs without TLS, so the recorded bind uses the plain cookie names. The `__Host-` HTTPS
shape is tested separately, against the attributes `Context.set_login_cookies` sets.

To re-record (for example after the server branch merges, as the drift check):

```sh
cp mobile/android/tools/record_fixtures.py <server checkout>/tests/test_zz_record_android.py
cd <server checkout>
ANDROID_FIXTURES=<this repo>/mobile/android/enrollment-core/src/test/resources/fixtures \
    PYTHONPATH=src python -m pytest -q -p no:cacheprovider tests/test_zz_record_android.py
rm tests/test_zz_record_android.py
```

## How it behaves

Where the iOS and Android clients differed, both follow the stricter behaviour, recorded in
docs/MOBILE-APPS.md, "Client lifecycle rules" (on `ios-enrollment-client-review` until it merges).

- **Configuration:** the server's private origin, the person, and the device-issuer CA's SHA-256, all approved up
  front by comparing them with the owner's trusted screen. The CA is never learned from the first credential.
- **Pairing link:** must be `https://<the origin the person chose>/pair-mobile#<id>:<token>`. Any other origin, a
  query, user info or another path is refused. The token is redacted from `toString`.
- **Claim:** a fresh Keystore key, a CSR, and the proof signed with that key. The receipt and the six-digit code
  stay in memory; the person shows the code to the owner. A claim ends the session first. A refused claim deletes
  its key. Call `recover()` at app start and on lock: an unfinished claim's receipt was in memory, so its staged key
  is dropped.
- **Credential:** answers mean:
  - 409: still waiting for the owner;
  - 410: the pairing expired, and its key is deleted;
  - 429: throttled.

  On success the leaf is validated the way the server validates its signer's output:
  - signed by the approved CA;
  - our key;
  - `CN=<device id>, OU=acc-native`;
  - clientAuth only;
  - valid now;
  - its expiry matches.

  Then the chain is stored next to the key, and the state switches to the new alias in one write, unbound.
- **Rotation:** the same flow while enrolled, keeping the device ID and installation ID. The working identity stays
  stored, but nothing uses it while claiming: `KeystoreKeyManager` presents nothing and `bindSession` refuses.
  - At the switch the old key is deleted at once (zero overlap), and the person signs in and binds again.
  - A refused or abandoned rotation returns to the old identity.
  - A new leaf that fails validation wipes everything, since the owner already approved its replacement.
  - `onIdentityChanged` must drop pooled connections and the host's session.
- **Session binding:** after a normal person sign-in through the public edge with this identity.
  - `/api/auth/state` must show the approved person, signed in, with no step-up pending; otherwise nothing is bound.
  - A 403 then only ends the session; a 404 or 410 forgets the identity.
  - The rotated cookies are accepted only in the server's shape: `Path=/`, no `Domain`, a positive `Max-Age`, an
    HttpOnly session, and `Secure` with `__Host-` names over HTTPS.
  - Sessions are held in memory only.
- **Status:** the identity is forgotten when:
  - the server's device listing doesn't show exactly this device (same person, native, active, not `inactive`, the
    same leaf fingerprint and expiry);
  - the listing answers 403, 404 or 410;
  - the leaf has expired locally.

  Forgetting deletes the keys, and a new installation ID starts, since the server refuses to enroll the same
  installation twice. An abandoned first pairing (expired, refused, an invalid leaf, or dropped by `recover()` on lock
  or restart) also starts a new installation ID: the owner may already have confirmed it, and the server's device
  record then holds that installation. A rotation keeps its installation ID. A 401 only ends the binding. Renewal
  warnings come at 14, 7 and 1 days.
- **Server answers** are decoded through `Api.decode`. A malformed answer becomes a fixed error message with no
  cause attached, so no body text (a receipt, say) reaches a log or crash report.
- **Storage:**
  - The state file holds no secrets and goes in `noBackupFilesDir`. It is synced before its atomic rename, and an
    unreadable file counts as not enrolled.
  - `recover()` deletes any `rynhult-identity-*` Keystore key the record doesn't name; `forget()` deletes them all.
  - The TLS key manager offers the identity only to a server that names no acceptable issuers or names the
    device CA.
  - The app must also turn off backup or device transfer for its data.
  - A software-only Keystore is refused unless the app explicitly allows it, and `storage(alias)` reports where a
    key really lives.

## Not verified here

- **Offline-cache rebuilding.** The coordinator has built the corrected library and run all 26 core JVM tests
  successfully on a disposable VM; the new cache transfer/offline path has only isolated Linux script tests.
  The CSR encoder was also checked by porting it line for line to Python, where the server's parser
  (`cryptography`) accepted the result.
- **Everything on a device:**
  - StrongBox and TEE behaviour, and `KeyInfo` levels;
  - Conscrypt's use of the Keystore key in a TLS handshake, and `KeystoreKeyManager` key types;
  - WebView client-certificate callbacks;
  - backup exclusion, uninstall and reinstall;
  - biometric lock.

  Phase 0 and phase 1 qualification lists these.
- **Server TLS trust for a private CA.** The transport takes an `SSLSocketFactory`. Building one pinned to the
  server's CA is the app's job, and the CA fingerprint in the QR is not part of the shipped contract yet.
