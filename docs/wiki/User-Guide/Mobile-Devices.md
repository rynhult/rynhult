# Mobile Devices

Native iOS/Android apps are **outside v1.0**. Native apps follow v1 one platform
at a time; the order among native Windows, macOS standalone, Linux desktop, iOS
and Android is not chosen yet. The [phone web app](/User-Guide/Connect-Your-Phone) remains part of v1.
See [platform scope](/User-Guide/Install#v10-platform-scope).

Existing native-device code is experimental groundwork, not a released app.
The source includes an experimental,
off-by-default server enrollment API; it does not install an app, signer, edge
helper or push relay, or enable public access. Existing [browser remote access](/User-Guide/Remote-Access)
and [phone browser setup](/User-Guide/Connect-Your-Phone) remain the available paths.

## Before a future native pilot

An owner must qualify private HTTPS trust, the device signer and public edge,
separately provision the new public-edge assertion key, then explicitly enable
the server deployment flag `ACC_MOBILE=1`. Keep it off
until that qualification. There is no new enrollment screen in this slice.
An app must already trust the private server, such as `https://rynhult.example.lan`;
a pairing QR cannot safely establish trust by itself. The client-device CA and
HTTPS server CA are different.

The server requires a recently verified owner to start pairing for an existing
person, type the six-digit code shown on the receiving app, rotate a credential
on the LAN, or revoke it. An app owns its private key; the server returns only a
public certificate. A certificate permits reaching sign-in, not impersonating
its person. Issuance stays pending until edge activation and a verified connection.

## Recovery and limits

Native certificates last at most 90 days; renewal is manual on the private LAN,
using a new key with no old/new overlap. A failed switch needs private recovery.
Native sign-ins retain normal password/TOTP or [passkey](/User-Guide/Glossary#passkey) checks, then bind to the
phone identity for at most 12 hours and never past certificate expiry. Company
sign-in and authenticated locked-phone background refresh are not supported by
this experimental native contract.

A lost-phone revoke ends **all sign-ins and terminal connections for that person**
and removes their push subscriptions and pending pairings, including on other
browsers. Another trusted owner sign-in on private access is the recovery path.
Already displayed or offline data cannot be recalled. The server has no native
push registration or relay yet. Phone lock, secure storage, reinstall recovery,
accessibility and real-device TLS behavior remain future qualification work.

Device-authority rollover does not restore a revoked or replaced credential.
Once the server closes an invalidated public connection, newly arriving input
on that connection is discarded.

Developers can read [Mobile devices](/Internal/Mobile-Devices) for the API contract.
