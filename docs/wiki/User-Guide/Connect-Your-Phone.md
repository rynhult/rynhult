# Connect your phone

Rynhult works in a phone's browser, installs on the home screen like an app, and sends a notification when a [session](/User-Guide/Glossary#session) needs you. Then you answer it in the session's terminal. It takes four steps, once per phone.

**This phone web app is part of v1.0**, connected to your self-hosted Linux
server. It does not need the future native iOS or Android apps, which come
after v1, one platform at a time, in an order not chosen yet.
See [platform scope](/User-Guide/Install#v10-platform-scope).

When an owner has enabled LAN device pairing, the Inbox offers signed-in people **Connect your phone** under **Information**. Open it to follow this guide. Dismiss the notice to hide it across your browsers; it also disappears once you have a paired device or turn on notifications.

**Before you start:** Rynhult is installed and you've signed in from a computer (see [Getting started](/User-Guide/Getting-Started)).

## 1. Reach Rynhult from the phone
Rynhult only answers private addresses: your home or office network, a VPN, or Tailscale. It is never on the open internet.
- **On the same Wi-Fi:** open Rynhult's name, for example `https://rynhult.example.lan`, in the phone's browser.
- **Away from home:** connect the phone to your VPN or Tailscale first.

The phone has to find Rynhult's name. Phones can't take a hosts-file entry, so use one of these:
- a DNS entry for the name on your router or local DNS server (pointing at Rynhult's LAN address, for example `10.0.0.20`);
- Tailscale's MagicDNS name for the machine, for example `acc-host.example-tailnet.ts.net` (install Rynhult with that name);
- a Cloudflare zone (chosen at install), which gives Rynhult a public DNS name and a public certificate. The address it points to is still private.

See [Install → No DNS server?](/User-Guide/Install#no-dns-server) for computers.

## 2. Trust Rynhult's certificate
Skip this if Rynhult uses a Cloudflare zone: its certificate is public. Otherwise Caddy signs Rynhult's certificate itself, and the phone has to trust Caddy's root once:
1. On the phone, open Rynhult (accept the warning this one time), sign in, and open **Settings → Certificate**.
2. **iPhone or iPad:** download **rynhult-root.mobileconfig**, install it in **Settings → General → VPN & Device Management**, then turn it on under **Settings → General → About → Certificate Trust Settings**.
3. **Android:** download **rynhult-root.crt** and install it under **Settings → Security → Encryption & credentials → Install a certificate → CA certificate** (the menu names vary by maker).

**What you should see:** Rynhult opens with no warning, and the browser shows the padlock.

## 3. Install the app
- **iPhone or iPad:** in Safari, tap **Share**, then **Add to Home Screen**, then **Add**. Open Rynhult from the Home Screen from now on.
- **Android:** in Chrome, open the menu and tap **Install app**.

## 4. Turn on notifications
In the app, open **Settings**, select **Turn on notifications**, allow them, then select **Send a test**. A notification should arrive within a few seconds. On iPhone and iPad (iOS 16.4 or later) this only works from the Home Screen app. More in [Notifications and the app](/User-Guide/Notifications-and-App).

**Then:** on the Overview, dismiss the **Connect your phone** banner with **×** (the installed app never shows it).

## Answering from the phone
A notification opens the item in the **[Inbox](/User-Guide/Glossary#inbox)**. **Answer in terminal** opens the session with the key bar: ⏎, Esc, ^C and the arrows are first, and **More terminal keys** has the rest. Tapping a key sends it without opening the on-screen keyboard (if the keyboard is already open, it stays open); tap the terminal itself to type, or **Ctrl**, which opens it for the key to combine. Ctrl stays visibly pressed until the next letter sends Ctrl+letter once; tap Ctrl again to cancel. It also cancels on blur, loss of browser visibility or after 10 seconds without a letter. The fixed keys remain available. **Expand** fills the browser window with the terminal; **Collapse** returns (or press F6, then C); Esc goes to the terminal program, and reload does not remember expansion.

## Troubleshooting
| What happens | Why | Fix |
|---|---|---|
| The page doesn't load away from home | The phone isn't on your VPN or Tailscale | Connect it, then reload |
| *Server not found* | The phone can't resolve Rynhult's name | Add a DNS entry on your router, or use the MagicDNS name (step 1) |
| A certificate warning every time | Caddy's root isn't trusted on the phone | Step 2; on iPhone, also the Certificate Trust Settings switch |
| **Turn on notifications** does nothing on iPhone | Safari, not the Home Screen app | Install the app (step 3) and turn them on from there |

## Direct remote access qualification

Direct access without a VPN is still unavailable. A disposable local test lab
has qualified Caddy's device-certificate gate. Disabled LAN pairing preparation
is now available under Settings, with owner confirmation and a separately
installed device signer. The certificate download in step 2 above installs server
trust only and does not pair a device.

Owner qualification still requires real iOS Safari and Home Screen app tests,
including certificate selection, reboot, locked-phone/background behavior and
notification taps. Android needs actual identity import and certificate-picker
tests for each supported browser and device policy. Public release remains
blocked pending these checks and the later implementation gates. Continue to
use the private address or VPN/Tailscale. See
[Security](/Internal/Security#remote-access-device-gate-qualification) for the
local test scope and remaining work.

The disabled-by-default [device pairing preparation](/User-Guide/Remote-Access)
now creates one-use client identity imports on the LAN after owner confirmation.
It does not enable [remote access](/User-Guide/Glossary#remote-access) or change the server trust download described
above. Physical phone/browser qualification remains pending.

## Pair a phone with a QR code

When an owner has enabled LAN pairing, verify your sign-in and open **Settings →
Access devices**. Enter a device name and select **Pair a phone**. Scan the QR on
that phone using the private address you already use. The link lasts five minutes
and works once. A newer code, or signing out anywhere (including another browser
or the phone), cancels that person's QR pairing in progress. Once an owner has
confirmed it, the download stays available until its own expiry.

Before requesting pairing, compare the installation address, person and desktop
match code on both screens. Confirm the checkbox on the phone only when all match.
Then an owner reads the phone's separate matching code and confirms issuance on
the trusted screen. Follow [device pairing](/User-Guide/Remote-Access#pair-a-browser)
for the encrypted identity import and individual revocation. Pairing does not
sign you in or turn on public access. Last connection is shown when observed;
an unobserved connection stays unknown.
