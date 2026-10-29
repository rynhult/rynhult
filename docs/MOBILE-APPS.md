# Rynhult mobile apps: iOS and Android

Date: 2026-10-24. **Status: proposed app design; experimental phase-1 server slice implemented, off by default.** `ACC_MOBILE=1` gates native enrollment, certificate lifecycle and session binding. No app, relay, service installation or public exposure is introduced. The [server contract](wiki/Internal/Mobile-Devices.md) and [decision 0071](decisions/0071-native-device-server-lifecycle.md) distinguish built behavior from pending device qualification.

**Release scope, owner decision 2026-10-27:** v1.0 is the self-hosted Linux server
used through browsers, including the phone web app, with existing Linux/macOS/
Windows-WSL nodes. Native apps are **post-v1**, one at a time to reduce total
cost. The order is not chosen yet; candidates are native Windows, macOS standalone,
Linux desktop, iOS and Android.
Existing mobile source, including the experimental server contract and Android
enrollment library, is not a v1 native app release. See
[decision 0073](decisions/0073-v1-linux-server-scope.md).

## Scope and recommendation

Build companion apps **after v1 and remote-access qualification**, when the owner
chooses their native-platform turns. Qualify iOS and Android separately; no platform
order is chosen, and the earlier shared initial spike does not schedule both together. Reuse the existing
groundwork and React interface/xterm terminal in small platform hosts (Swift and Kotlin),
with native enrollment, credential storage, push, app lock and share integration.
Keep the web UI bundled with each signed release. Do not commit to a wrapper framework
until its platform's TLS/terminal spike passes.

The apps supervise existing server-side agents: Overview, Inbox, Sessions, Projects, reviewing attention, and foreground terminal access. They do not run agents locally. Initial mobile scope excludes offline terminal execution, background terminal persistence, public previews, vault export, and an independent editor implementation. Existing web capabilities remain available where the shared interface supports them; new native screens should earn their maintenance cost.

[REMOTE-ACCESS](REMOTE-ACCESS.md) and accepted decision 0020 (private decision record) remain authoritative: separate public hostname, device mTLS before any ACC code, then person sign-in; private recovery; 90-day browser certificates with manual renewal and one-use PKCS#12 delivery; browser-only iOS if the Home Screen app fails certificate qualification; no Cloudflare TLS termination. Native CSR enrollment is an experimental, disabled extension, separate from the accepted browser import flow. Public remote access is currently **NO-GO**: helpers are uninstalled, and physical devices, trusted request identity, established-stream revocation and operational gates remain outstanding.

## What native adds, and what the PWA already does

The PWA already has phone layouts, terminal keys, an accessible terminal text view, encrypted Web Push, foreground banner suppression and notification navigation. Its [service worker](../web/public/sw.js) intentionally has no offline cache. The [push sender](../src/acc/server/push.py) implements Web Push encryption and VAPID with a vendor endpoint allow list. Native APNs/FCM is a different transport, not a change of VAPID keys.

| Need | Current PWA/platform boundary | Native acceptance target |
| --- | --- | --- |
| iOS device authentication | Safari identity import and Home Screen certificate selection require separate physical qualification; no claim that every current iOS PWA fails. | App-owned identity selected explicitly for the approved host, across cold launch, fetch, events, WebSocket, background/foreground and expiry. Never add an anonymous API exception. |
| Reliable attention notifications | iOS Home Screen Web Push already exists; it is not a native-only feature. Browser-only fallback may lose that installation's push path. | Native registration and diagnostic state independent of WebKit/service-worker lifecycle. Report permission denied, token invalid and relay unavailable. Measure delivery; never promise guaranteed arrival or an alarm service. |
| Background refresh | Browser execution is constrained; no dependable polling interval. | OS-scheduled reconciliation where permitted, refresh on resume, and visible last-success time. No always-on socket or fixed refresh deadline. |
| Share sheet | Web sharing varies by platform; incoming sharing is not a uniform cross-platform contract. | Receive text/HTTPS URLs as an unsent draft; explicitly choose project/session, review and send. Outgoing share uses a canonical link without credentials, with a private-resource warning. |
| Biometric unlock | A passkey can authenticate a person; it does not lock an already signed-in app or supply a TLS identity. | Face ID/Touch ID or Android BiometricPrompt protects local session/cache access; device-passcode fallback. Server authorization and recent-verification requirements still apply. |

WebKit documents [Home Screen Web Push from iOS/iPadOS 16.4](https://webkit.org/blog/13878/). Apple's [background notification guidance](https://developer.apple.com/documentation/usernotifications/pushing-background-updates-to-your-app) says delivery may be throttled or omitted; [FCM priority guidance](https://firebase.google.com/docs/cloud-messaging/customize-messages/setting-message-priority) likewise distinguishes delayed background delivery from attempts at immediate delivery. These are integration improvements, not immunity to Focus, notification settings, battery restrictions, force-quit or network loss.

## Architecture choices

Estimates below compare an initial useful release on both platforms, including integration and device qualification, after the server prerequisites. They are planning ranges for one engineer familiar with this codebase, not benchmarked commitments. Store waiting time and purchasing/access to a Mac and physical phones are additional.

| Approach | Benefits | Costs and risks for one maintainer | Indicative effort |
| --- | --- | --- | --- |
| SwiftUI + Kotlin/Compose, fully native screens | Best platform semantics, lifecycle and accessibility control; direct networking. | Two UI implementations, navigation systems and regression matrices; terminal still needs an embedded renderer or substantial new work. Web feature parity drifts. | 18–28 engineer-weeks |
| Small Swift/Kotlin hosts + bundled React UI (recommended spike) | Reuse phone layouts, terminal and UI fixes; explicit ownership of TLS and OS features; modest native surface. | Two hosts remain. mTLS for embedded fetch/events/WebSocket, bridge security, cookies and passkeys can defeat the apparent simplicity. Bundled UI versions lag server releases. | 11–17 engineer-weeks, phased below |
| Capacitor-style shared shell | Existing React reuse plus lifecycle/plugin scaffolding. | Stock plugins are not evidence of working client-certificate transport. Custom Swift/Kotlin plugins, plugin upgrades and bridge audits remain; a framework does not solve WKWebView TLS. | Similar range until a measured spike proves savings |
| React Native / Flutter / shared Kotlin core | More shared UI or domain code, mature platform integration options. | Rebuild much of the React DOM UI, retain native TLS/security work, introduce another toolchain; xterm needs a WebView anyway. Kotlin shared logic alone does not share both UIs. | 15–24 engineer-weeks |

A plain remote-URL WebView or Android Trusted Web Activity is too weak a starting assumption: browser credential behavior, update coupling and native capability gaps remain. Native utility also needs to justify [App Store guideline 4.2](https://developer.apple.com/app-store/review/guidelines/); approval is uncertain even with the proposed features.

### Recommended boundary and stop rule

```text
 Signed app: bundled React + xterm (untrusted rendering of server content)
       │ bounded, typed requests; no keys or generic native HTTP exposed to JS
       ▼
 Swift / Kotlin host: identity, TLS, session, local lock, push, share drafts
       │ HTTPS + client certificate; normal person auth and capability checks
       ▼
 Caddy device gate → accd API → existing brokers/agents

 accd attention outbox → optional push relay → APNs / FCM → generic alert
 app foreground/resume → authenticated fetch from Caddy; relay never proxies API
```

First try WebView TLS challenge handling for the one configured origin: Apple's [WKNavigationDelegate challenge API](https://developer.apple.com/documentation/webkit/wknavigationdelegate/webview(_:didreceive:completionhandler:)) and Android's [WebViewClient client-certificate callback](https://developer.android.com/reference/android/webkit/WebViewClient) provide integration points. This is an inference about a possible design, **not proof** that all transports use them. URLSession, WKWebView, worker fetch and WebSocket may have different connection and cookie stores. Test each explicitly, including redirects and server-initiated disconnects.

If direct WebView networking cannot satisfy the gate, evaluate native URLSession/Android networking for a narrow API/events/terminal adapter, leaving React as a renderer. This costs more: adapt TanStack Query and xterm transports, handle streaming/backpressure, preserve errors and cancellation, and implement a coherent session boundary. Do not expose a general URL-fetch bridge, inject certificate bytes into JavaScript, intercept HTTPS with an unqualified localhost proxy, or move API traffic outside mTLS. Stop after the timeboxed spike if neither design is credible; retain browser support and re-estimate native screens rather than silently weakening the gate.

Package UI assets with the app; disallow remote script injection, eval and arbitrary native plugins. Allow only reviewed static content and bounded data. Disable the PWA service worker in the host. Separate navigation to external sites into the system browser with no native bridge. Do not embed project previews on the privileged origin. Version the bridge, API capabilities and event schema; support the current and previous stable mobile API contracts, and retain compatible server behavior for at least six months after deprecation notice. Older clients get an actionable upgrade screen; never an undocumented downgrade of authentication. This compatibility policy itself needs owner acceptance.

Native requests must retain normal sign-in, scoped permissions, CSRF/Origin protections where cookie auth applies, one-use terminal tickets and the single-writer lock. Do not manufacture a trusted Origin header as a substitute for authentication. The spike must choose either a qualified HTTPS WebView origin with existing cookie rules, or a separately reviewed native session protocol with device-bound scoped credentials; no native bearer/refresh-token protocol is introduced. The server slice preserves cookie/CSRF authentication, binds native login cookies to their certificate from creation, and requires explicit verified-edge session binding before ordinary API use. Native OIDC is refused pending separate qualification. Passkeys require separate qualification for bundled/local origins and arbitrary self-hosted domains; retain password + TOTP sign-in rather than promising seamless browser-cookie or passkey transfer.

## Device identity and enrollment

Treat a device identity, a person's sign-in, app signing and push registration as **four separate authorities**. A paired app can reach login; it cannot impersonate the owner. Native identity rows need an explicit native type, app installation identifier, owner, public key/leaf fingerprint, serial, expiry and active/revoked state. The installation ID is random and resettable, not a hardware tracking ID. The server slice adds `kind=native`, installation/platform/public-key metadata, a leaf-only `issue_native` signer profile and conditional policy integration. It does not reuse machine identities or change public node-route permissions.

### Proposed LAN pairing

1. A recently verified owner on private access starts pairing. The QR contains the private HTTPS origin and a 256-bit one-use secret in the fragment, expiring after five minutes. Receiving app must also be on private access. Offer accessible manual entry with the same authority and expiry; camera permission is optional. Strip fragments before any subsequent navigation and redact all enrollment logs.
2. Before sending the secret, verify server TLS and confirm the expected hostname. Existing private CA installations need app-scoped trust: recommend adding the expected server-CA fingerprint to the QR and comparing it with the trusted owner's screen. This adds a trust-bootstrap format requiring review; it is not shipped. Never use global trust installation or accept-all TLS. A malicious QR can name an attacker-controlled server even with a valid public certificate, so show the server and require trusted-screen comparison.
3. Generate an app-owned P-256 private key locally; send a bounded CSR and proof of possession bound to this claim. The signer validates algorithm, size, signature, clientAuth-only policy, server-assigned subject/device ID and pending claim. It must not honor arbitrary CSR extensions, SANs or file paths. The machine signer already accepts CSRs; the experimental native profile now independently validates signed P-256 requests, discards requested names/extensions and returns only the server-assigned clientAuth leaf. API proof binds the key to this specific claim.
4. Preserve the current receiving-device six-digit claim code: the owner types it on the trusted screen. Wrong code cancels before signing. Bind approval atomically to the winning claim and public-key hash. A photographed QR cannot silently enroll another key. Retries yield the same approved result, not multiple devices.
5. Issue the leaf/chain only. Activate through the existing reviewed edge-policy path, then prove a new connection with the identity. Show pending until verified; do not claim success from CSR issuance alone. Normal person sign-in follows. No anonymous public pairing/bootstrap endpoint is added.

Browser PKCS#12 import remains as accepted, including separate password, one download and deletion. Do not assume the app can read an identity installed into Safari or another app's store. If a temporary native PKCS#12 import is proposed to reduce development time, require an explicit owner choice and record its export/copy risk; it is not the recommended durable native enrollment.

### Storage, lock and background tension

On iOS keep the identity in the app's Keychain access group, device-only and available while unlocked. Prefer Secure Enclave generation **only if** CSR signing and the full TLS stack accept that key; otherwise use a protected Keychain key and state its assurance accurately. On Android use an app-specific [Android Keystore](https://developer.android.com/privacy-and-security/keystore) alias, hardware-backed where available and qualified; store the public chain alongside it. Confirm the selected TLS/WebView stack can actually use the key without exporting it. Do not equate nonexportability with protection from a compromised process using the key.

Biometric app lock defaults on after setup: cover the app-switcher snapshot immediately on background, suspend terminal input, cancel privileged requests and close terminal connections; unlock on return, then recheck session/device state. Keep the local cache encryption key/session secret behind platform authentication. Offer device passcode fallback and an accessible full sign-in/re-pair recovery path if keys are invalidated or biometrics change. Local biometric success is not server-side recent reauthentication. Test already-open TLS connections as well as key access; merely locking a key does not invalidate an established connection.

Locked-device background refresh conflicts with this default. Apple's [Keychain accessibility rules](https://developer.apple.com/documentation/security/restricting-keychain-item-accessibility) distinguish unlocked access from access after first unlock. **Recommend no authenticated refresh while app-locked in v1**: generic pushes still arrive; refresh runs when unlocked and whenever the OS allows during an unlocked period. Do not select a weaker accessibility class or an ungated duplicate key to advertise background refresh. A later opt-in after-first-unlock transport identity plus a narrowly scoped refresh session needs separate threat review, API work and battery testing. Use BGAppRefreshTask on iOS and WorkManager on Android only for opportunistic reconciliation; expiration cancels work cleanly, with no foreground service or background audio workaround. Background execution after reboot is never assumed.

### Renewal, revocation and recovery

Recommend native certificates also last 90 days, with warnings at 14, 7 and 1 days. Start with manual renewal on LAN after recent sign-in and owner/device checks, matching the current operational boundary. Public renewal is future work requiring proven edge device identity; possession of a cookie is insufficient. Generate a new key/CSR, validate the returned chain, confirm edge activation, atomically switch credentials and clear connection pools. Initially use zero leaf overlap, as current browser renewal does; show an expected reconnect and retain private recovery if switching fails. An acknowledged, bounded overlap scheme is a later change, not a promise of uninterrupted rotation.

Revocation updates authoritative membership and propagates to the edge; also invalidate sign-ins/push registrations and close existing requests, events and terminals within the remote-access design's 30-second stream target. Reload/new-handshake rejection alone is inadequate. Current implementation signs out all sessions and push subscriptions for that person on device revocation; preserve and explain that broader effect until finer scoping is deliberately implemented. Push-token refresh cannot resurrect a revoked device. Expiry receives the same active-state enforcement, including long-lived connections.

On sign-out delete local sessions, snapshots and drafts and deregister push; offer **Forget this server** to delete its identity too. If offline, wipe local access immediately and explain that remote deregistration/revocation remains pending; an owner can revoke from another trusted device. Uninstall/reinstall, backups and phone replacement require fresh pairing: detect a fresh installation and do not silently reuse surviving iOS Keychain items. Never sync identities or session secrets through cloud backups. Lost phone: revoke via private access/another paired device, terminate affected sessions, rotate any independently exposed secrets. CA compromise follows the existing OFF, CA rotation and LAN re-pair drill; app signing compromise is a separate release-key incident.

## Implemented phase-1 server boundary

See the [versioned API contract](wiki/Internal/Mobile-Devices.md) for request
bodies and the exact claim signature encoding. Every mobile endpoint is 404 by
default. With `ACC_MOBILE=1`, a recently verified human owner starts/approves
five-minute private-LAN claims for an existing person; rotation and revoke are
also owner-only. Native PKCS#12 export, public bootstrap/renewal and a separate
native bearer/refresh protocol are absent. Signer uncertainty cancels rather than
blindly issuing another certificate. Native/browser/machine signing shares the
existing durable 20/hour quota; enrollment, claims and credential reads also have
10-attempt/10-minute per-prefix/person limits. Safety revoke is unthrottled.

Leaf issuance reports pending, verified policy advances to awaiting connection,
and binding a new trusted-edge connection establishes verified status. A native
sign-in cookie is certificate-bound immediately, then explicit binding rotates
its cookie/CSRF and caps it at twelve hours or leaf expiry. The exact person must
own the native leaf. A separate owner-provisioned secret edge assertion is required in addition to
Unix/public-host ingress and the overwritten fingerprint; a private proxy
forwarding forged client headers is not proof. The assertion never reaches app
handlers or upstreams. Missing provisioning blocks native activation/binding.
Header-only proof over TCP, installation API-token use and
native OIDC start/callback are refused. Existing private/browser authentication
is preserved. Revocation retains the broad all-person sign-out/push-removal policy.

Safer choices pending owner review: stricter owner-only lifecycle operations,
zero-overlap manual LAN renewal, twelve-hour foreground sessions, no native OIDC,
a separate root/main edge-assertion provisioning prerequisite,
and **no automatic trust bootstrap from a QR**. The app must already trust the
private server's HTTPS identity; the device CA is not a server trust anchor.
No app/relay implementation, OS lock/secure-store/uninstall proof, background
refresh promise or real-phone/HTTP2/public-edge qualification is claimed. Normal
remote-access NO-GO prerequisites still apply.

## Push delivery and privacy

### Delivery options

| Option | Operational trade-off | Recommendation |
| --- | --- | --- |
| Owner-operated Rynhult relay to APNs and FCM | One service holds the official app's provider credentials; self-hosted mains make outbound authenticated requests. Creates an availability, abuse-control and metadata trust dependency. | Optional initial production route, only if the owner accepts running it. No relay work authorized here. |
| Each main sends directly to APNs/FCM | Avoids a Rynhult intermediary, but needs provider credentials authorized for that app. Distributing the official app's APNs key/FCM service account to installations is unsafe. | Support for owners building/signing their own app identifiers and provider projects; not a zero-configuration official-app solution. |
| Self-hosted Android delivery, e.g. UnifiedPush with a compatible distributor | Can avoid Google but adds user setup, endpoint SSRF defense, distributor dependency and OEM battery qualification. It does not replace APNs for a normal iOS release. | Later optional FOSS flavor. Initially allow no-push/foreground-only operation. |
| Persistent app socket/polling | Simple while foreground; background suspension and battery use make this unsuitable for dependable alerts. | Foreground events only; never market as reliable background push. |

Self-hosting a relay means hosting the server that submits pushes; it does **not** eliminate Apple/Google from their native delivery paths. FCM's [architecture](https://firebase.google.com/docs/cloud-messaging/fcm-architecture) routes Apple delivery through APNs; use APNs directly for iOS to avoid an unnecessary additional processor.

### Proposed protocol

After person sign-in over device mTLS and an explicit notification opt-in, bind the current installation's provider token and environment (APNs sandbox/production or Android flavor) to that device/person/server registration. Provider tokens rotate: upsert idempotently, remove invalid tokens, and reconcile on foreground launch. Keep native registrations separate from existing Web Push endpoints while reusing attention preferences and authorization. Do not let an arbitrary supplied URL cause server-side POSTs; UnifiedPush would require its own constrained endpoint policy.

A main obtains a revocable relay sender credential through an owner-reviewed enrollment, with explicit quotas. The app associates its provider token with an opaque relay installation handle; accd stores that handle and a server-local device binding. Relay requests may address only registered handles for that sender. No relay credential grants access to ACC. Authenticate sender requests, rate limit per sender/device, deduplicate by random event ID, honor TTL and revocation, rotate credentials, and cap retry queues. A relay is a new service with an abuse/cost budget, not a free property of the app.

Default lock-screen text is **“Rynhult needs your attention”**, with no project/session name, prompt, terminal text, hostname, URL, username or approval content. Carry only a random delivery ID and opaque local server selector; map it to authoritative attention state after unlock and mTLS sign-in. Never embed cookies, pairing secrets or actionable approval tokens. Both relay and provider can observe tokens, timing, source/destination network metadata and message size; generic content minimizes exposure but does not provide anonymity. Native APNs/FCM payloads do not inherit Web Push's end-to-end content encryption. Rich previews, if ever offered, need separate explicit consent and an encryption design; omit them initially.

Use an accd outbox with bounded exponential retry, jitter, five-minute alert TTL and coalescing per device/attention category. High priority is reserved for actual visible time-sensitive attention; ordinary sync uses normal priority. A provider acknowledgement means accepted, not displayed. On a notification tap, unlock, fetch current Inbox, verify permissions and resolve the item; stale/revoked/deleted targets lead to Inbox or sign-in. A notification can never approve an action. Deduplicate notifications across the active native window and PWA where feasible; offer a preferred notification device rather than promising perfect cross-client suppression.

Recommend relay delivery logs retain status, random IDs and timestamps for seven days, omit content and redact tokens; delete inactive registration mappings after 30 days and immediately on explicit deregistration. Keep provider secrets in a dedicated secret store, separate from device CA and app signing keys. Describe Apple/Google and the optional relay in privacy disclosures; no analytics SDK by default. Already queued or displayed pushes cannot reliably be recalled after revocation. Offline users still see retained server Inbox events on next sign-in even when alert TTL expired.

## Offline and foreground lifecycle

The first native-app release (post-v1) works online for actions. The signed app shell opens offline and explains connectivity, identity expiry and last successful sync separately. No automatic offline launch, Stop, Interrupt, approval, acknowledgment or terminal-input queue: stale actions can affect a different state and ambiguous results must never be replayed blindly. Preserve single-writer acquisition only on a fresh foreground terminal connection; a sleeping phone releases control. On reconnect fetch a snapshot before resuming events, reconcile gaps by server cursor/revision (new API work if unavailable), and label unknown results for manual review.

Default persistent cache is off. Offer an explicit **Keep recent status on this phone** option: encrypted summaries of Overview/Inbox only, bounded to 100 items and 24 hours, partitioned by server/person, with last-fetched timestamps and an unmistakable **Offline · saved at …** label. No transcripts, files, vault data or terminal output in that cache. Cache encryption keys stay device-only and behind local unlock. Clear on sign-out, detected revocation or identity expiry; offline revocation cannot remotely erase already saved information, so enforce the local retention deadline too. Never render stale data as Live or an unavailable fetch as an empty Inbox.

Incoming shares are bounded (64 KiB text or one HTTPS URL initially); no arbitrary attachment ingestion or automatic URL fetch. Treat shared text as untrusted input. The iOS share extension passes a bounded draft through an App Group to the main app; it gets no TLS identity or server session. Android receives a bounded ACTION_SEND intent and requires the same review. Protect any extension handoff file with OS data protection, consume once and purge after five minutes. Keep drafts in memory by default. If offline or signed out, explain that leaving may discard the draft; optional encrypted draft persistence needs a separate retention choice. Before sending show the server, destination and exact content, refresh destination permissions, then require Send. No terminal paste or agent launch occurs just by sharing. User-generated content remains escaped; no privileged bridge access from rendered Markdown.

## Accessibility and visual design

Follow [DESIGN](design/DESIGN.md) and the reviewed brand kit in the reviewed Rynhult brand kit (README, palette and icon masters). Preserve Charcoal `#10191F`, Slate `#263B46`, Frost `#B9CFE1`, Snow `#F0F3F3` and restrained Periwinkle, using the design system's contrast-adjusted interaction tokens rather than copying the palette blindly. Keep status green/amber/red separate from brand accent, with words and labelled icons. Use the angular R mark for launch icons, preserve its clear space and proportions, and generate proper iOS and Android adaptive/monochrome assets from masters; existing PWA PNGs are candidates, not store-ready proof. The kit does not establish artwork/trademark licensing: confirm redistribution before store/F-Droid submission.

Retain Overview, Inbox, Sessions and Projects bottom navigation, More/drawer destinations, compact session header and first keys Enter, Esc and Ctrl-C. Show health/errors in normal flow above bottom navigation; respect safe areas, keyboard insets and Android Back. Do not auto-focus a terminal when opening it on touch. Keep web Inter/JetBrains Mono; native OS dialogs use system fonts, with text scaling supported throughout. App lock, pairing, permission education and share destination review are native accessible flows.

Acceptance requires VoiceOver and TalkBack reading order and labels across native/WebView boundaries, Dynamic Type/Android font scaling to 200%, reflow without lost actions, light/dark/high-contrast and reduced-motion settings, 44 pt iOS/48 dp Android targets, switch/external-keyboard access and visible focus. QR pairing has manual entry and nonvisual code comparison. Notification and connection state announcements must be concise, not a stream of terminal output. Retain xterm screen-reader mode and the paginated text/snapshot alternative, including the accepted 20 snapshots/minute/person and busy-audit warning from decision 0020. Test selection/copy and escaping from the terminal with assistive technology. Automated axe/browser checks cannot establish native accessibility or substitute for real screen-reader testing.

## Distribution, signing and ownership

- **iOS:** use TestFlight for the device pilot, then seek App Store distribution if the owner wants a maintained public companion. [TestFlight builds expire after up to 90 days](https://developer.apple.com/help/app-store-connect/test-a-beta-version/testflight-overview/), so it is not permanent private distribution. Budget Mac/Xcode access, developer membership, device testing, screenshots, privacy disclosures and review/rework time; exact fees, SDK minimums and regional distribution policies must be checked at implementation. Give reviewers a fictional-data demo server plus a scoped pairing path or an agreed full demo mode; never waive production mTLS or expose a real owner's main for review. App Review acceptance remains uncertain.
- **Android:** Play internal/closed testing followed by production if wanted. A signed APK can support a small pilot without Play, with manual update instructions and a published signing fingerprint. F-Droid is a separate release target: [its inclusion policy](https://f-droid.org/docs/Inclusion_Policy/) requires FLOSS dependencies and a flavor without proprietary Firebase/GMS code. Recommend an initial no-push FOSS flavor, then qualify UnifiedPush. F-Droid admission, build reproducibility and artwork licensing are explicit gates, not guaranteed by the repository's software license.
- **Ownership:** the Rynhult owner/legal publisher owns developer accounts, bundle/package IDs, store listings, privacy contact and recovery access. No agent or contractor is the sole account holder. Use MFA, least-privilege CI submission access and separately backed-up recovery material. Keep Apple distribution credentials, APNs auth keys, Android signing/upload keys and FCM service accounts distinct from the device CA; none belong in git, web assets, app binaries or agent prompts.
- **Android updates:** [Play App Signing separates upload and distribution keys](https://developer.android.com/studio/publish/app-signing). Decide package IDs and key custody before the first public release. Prefer separate IDs for Play and FOSS channels initially; channels may use different signatures, so users cannot assume in-place cross-channel updates. Document export-free migration as re-pairing and a fresh sign-in. If later sharing a signature with a reproducible F-Droid build, agree on its signing procedure explicitly. Test upgrade preservation and downgrade refusal; backup the self-distributed signing key offline and rehearse restore.

A solo owner must budget a monthly maintenance/release window (initial estimate 1–3 engineer-days/month, plus incidents), yearly OS/device compatibility work and ongoing relay hosting/support if accepted. No paid build or relay accounts are created by this task.

## Threat model and verification gates

Protect device/session keys, pairing authority, attention/terminal content, action integrity and signing/update authority. Trust boundaries are: QR/owner approval; OS store/native host; untrusted rendered content/bridge; internet/Caddy; accd/broker; push relay/providers; app distribution/CI. Server authorization is authoritative on every action.

| Threat | Required mitigation / test | Residual limitation |
| --- | --- | --- |
| Stolen locked or unlocked phone | Device-only key/session storage, local unlock, hidden task-switcher content, short local lock boundary, owner revoke, session/stream teardown tests. | An unlocked signed-in phone can act until locked/revoked; offline cached data cannot be recalled. |
| Rooted device, malware or key extraction/use | No export API, qualified hardware key use where supported, sanitized diagnostics, no secrets in backups; support re-pairing. | No promise against endpoint compromise, including signing with a nonexportable key from a compromised process. |
| QR theft, replay or malicious origin | Five-minute single use, private-only pairing, trusted-screen host/CA comparison, claim-code/public-key binding, atomic confirmation and race tests. | A deceived owner can approve the wrong endpoint; physical QR secrecy alone is insufficient. |
| MITM, redirect or cross-host certificate disclosure | Normal server trust plus scoped private-CA bootstrap; exact scheme/host/port allow list; refuse cross-origin credential forwarding; test DNS/CA change and HTTP/2 reuse. | Public CA or main compromise needs its own recovery; no accept-all TLS escape hatch. |
| XSS, malicious Markdown, preview or share | Bundled reviewed scripts, CSP, escaped data, no generic bridge fetch/files/key APIs, external browser isolation, typed bounded bridge messages and main-frame checks. | XSS within the authorized UI can still attempt actions as the current user; native confirmation for sensitive local operations is additional defense. |
| Revoked/expired identity over existing socket | Trusted edge identity on each request; close streams within 30 seconds, invalidate sessions and registrations; test open WebSocket/HTTP2/event connections. | Registry/session checks and stream closure have isolated server tests, including a real terminal bridge; the real edge/device operational gate remains outstanding. New-handshake lab results alone cannot approve a mobile release. |
| Push spoofing, replay, relay compromise or outage | Generic inert payload, authenticated registration, scoped relay sender, quotas/TTL/deduplication, fetch authoritative state on unlock; test invalid token and relay downtime. | Traffic metadata and already displayed notifications remain; alerts can be delayed/lost. |
| Supply-chain or signing-key compromise | Pinned/audited dependencies, protected release workflow, restricted credentials, provenance and tested update signing; separate provider/CA credentials. | A malicious correctly signed app update can steal app-accessible data. Owner account/release-key recovery is essential. |
| Stale cache and ambiguous mutation | Timestamps, cache isolation, no offline action queue, no blind retries; reconnect fetch and explicit uncertain-result handling. | Owner must inspect uncertain outcomes; local unlock does not make old state current. |

A compromised main/root or malicious trusted owner remains outside this app's protection. mTLS does not prevent denial of service or secure separately published previews. Do not request broad storage, contacts, location or background-service permissions; camera and notifications are optional and requested at the point of use.

## Phases and effort

One engineer-week means five focused days. Recommended-host total is **11–17 engineer-weeks**, plus **25–40% contingency**, owner decisions, store waits and the outstanding remote-access work (estimated separately in REMOTE-ACCESS). No parallel team is assumed. The ranges may grow materially if the network adapter or native auth protocol proves necessary. The fully native alternative needs a new estimate after the spike.

These are cumulative estimates for both later mobile platforms, not a v1
commitment or a joint pilot. Apply the phases to iOS first and then adapt and
qualify Android in its own turn, reusing already built server/client components.

| Phase | Effort | Concrete exit / tests |
| --- | --- | --- |
| 0. Prerequisite review + architecture spike | 1–2 weeks | Record server remote-access gates; on physical iPhone and Android, prove app-owned key/CSR/TLS across navigation, fetch, events and WebSocket; verify no-cert denial before accd, redirects, cold start, phone lock, cookie/TOTP sign-in and rotation. Produce go/no-go and measured adapter cost. A local lab spike may precede public qualification; no public release does. |
| 1. Enrollment and credential lifecycle | 2–3 weeks | **Server slice implemented off by default:** owner LAN enrollment/rotation/revoke, native CSR profile, policy/session binding, audit and rate limits. Related API/security tests cover claims, signer interruption and terminal closure; physical/platform work remains. Helper CSR validation/unit tests, API claim/confirmation races, registry and policy integration, secure store/lock/uninstall tests; revoke during open terminal, expired leaf, failed activation, lost-phone and CA restore drill. Normal private/browser access survives. |
| 2. Foreground companion pilot | 3–4 weeks | Bundle shared UI; auth/bridge contracts and compatibility errors, Inbox/status, terminal writer/reconnect, native accessible pairing/lock. Browser regressions for shared UI plus real-device VoiceOver/TalkBack, keyboard, text size, background/foreground and no blind mutation replay. TestFlight and signed Android pilot only after gates pass. |
| 3. Push and bounded refresh | 2–3 weeks | Owner first accepts provider/relay model. Outbox/relay authorization and privacy tests, token rotation, denied permissions, duplicate/out-of-order pushes, TTL expiry, revoked user/device, relay outage; device matrix under Focus, Doze, force-quit and reboot. Log observed latency/missed alerts without inventing an SLA. Locked fetch remains disabled by default. |
| 4. Share, optional offline cache and release | 3–5 weeks | Share extension/intent validation, wrong-server review, offline draft discard, cache retention/isolation/deletion tests, store/privacy assets and signing restore/update drill. TestFlight/Play review; separately build/audit no-Firebase FOSS flavor. Publish supported OS/device matrix and known failures. |

### Release-candidate items (owner, 2026-10-25)

These wait for a **native-app release candidate after v1**, not the Linux-server
rc.1, together with package IDs, signing keys, minimum OS versions and device qualification:
- **Key attestation (decided: yes).** Today the server only takes the phone's word that its key is hardware-backed;
  the Android client reports where a key lives but sends no proof (review A-6 in
  `docs/security/2026-10-25-review-android-enrollment-client.md`). At the release candidate:
  - Android sends its Keystore attestation chain with the claim (`setAttestationChallenge` bound to the claim
    proof's message, so a chain can't be replayed for another pairing);
  - iOS sends App Attest evidence where the key allows it.

  **Server-side verification goes with it, in the same change.** The server must:
  - verify the chain to the platform's attestation roots, which it pins and keeps up to date, together with the
    revocation list Google publishes;
  - check the challenge, the app's package or team and signing-certificate digest, the security level (StrongBox,
    TEE or software) and that the attested key is the CSR's key;
  - record the verified level on the device;
  - show that level on the owner's confirmation screen and in the device list.

  An owner policy can then require hardware-backed keys. Without verification, a client-reported level is never
  shown as proven.

Run the existing Python tests with `PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q`; for shared UI changes also typecheck/build, run relevant Playwright and axe coverage. Add Swift/Kotlin unit tests for native contracts and on-device integration tests for OS behavior; mocks alone cannot qualify identity selection, background execution or delivery. Release evidence must record device model, OS/WebView version, network (LAN/cellular, IPv4/IPv6), app/server versions, steps and failures. No native build, device run or push-delivery experiment has been performed for this design document.

## Phase 0 results — iOS Simulator lab (2026-10-24)

A standalone lab on the owner's MacBook (demo-mac) tested the key unknown above: whether WKWebView's client-certificate challenge covers every transport the UI uses. It was a Simulator-only run, without Rynhult itself, the React UI or production certificates.

**Result: GO for direct WKWebView mTLS in the Simulator.** Top-level navigation, `fetch()`, `EventSource` and `WebSocket` all presented the app-owned client identity and succeeded. This held after same-origin 302 redirects, after server-side disconnects with reconnect, after terminate and cold relaunch, and after backgrounding and return. WebSocket and SSE authenticated even when they were the first request of a launch, before any other transport warmed credentials. In the final run, the server logged 164 requests and upgrades, all with an authorized client certificate, over 105 TLS connections. No native URLSession adapter was needed.

**Setup:** macOS 26.6.2 (arm64), Xcode 27.1, iOS 27.0 Simulator (iPhone 18 Pro; the 27.1 runtime reported the device as incompatible). A local Node HTTPS server bound to loopback required a client certificate from a seven-day TEST ONLY CA; without one, curl was rejected at TLS before any HTTP request. The app imported a bundled test PKCS#12 into its own keychain group and answered the challenge from `webView(_:didReceive:completionHandler:)`. Server trust was pinned to the test CA for the one configured host and port, with hostname validation. No CA went into any trust store, no accept-all handler or ATS exemption was used, and no key material reached JavaScript. The app was ad-hoc signed for the Simulator; no Apple ID, team or Developer Program membership was used.

**Not established yet:** a physical iPhone, older iOS versions, the real React terminal and event code, production Caddy, HTTP/2 and HTTP/3, real certificate renewal and revocation, Wi-Fi/cellular handoff, cross-origin redirects (credentials must never follow them) and a negative certificate/hostname attack matrix. Streams are not promised to stay open while iOS suspends the app; the test opened fresh connections on return.

**Fallback if a device test fails:** an origin- and endpoint-allowlisted URLSession adapter only for the failing transport (SSE via `URLSessionDataDelegate`, the terminal via `URLSessionWebSocketTask`), with bounded subscribe/send/close messages and no generic fetch bridge. Rough estimate: 3–5 engineer-days for a prototype plus 3–5 for lifecycle, integration and security validation.

**Next:** the owner runs the same lab on an iPhone with a free personal team in Xcode (Developer Mode on the phone, LAN-only test server, fresh test certificates). The lab's `RESULTS.md` on demo-mac has the exact steps; its test keys and build output stay on the Mac and are not copied into this repository.

## Owner questions and recommended answers

These are pending choices; recommendations are not accepted decisions and do not reopen decision 0020.

| Question | Recommended answer | Why / uncertainty |
| --- | --- | --- |
| What earns the first app release? | Post-v1 iOS pilot with identity, local lock, Inbox and foreground terminal, after the three native desktop turns; Android follows in its own turn using the same host design. | Owner decision 0073 serializes platforms to reduce total cost; reuse does not require simultaneous qualification. |
| Which architecture should be funded? | Timebox the shared-UI/platform-host spike to two weeks; proceed only with a demonstrated TLS/session boundary. | Avoid committing to framework savings before proving transport and auth. |
| Native identity issuance and duration? | Device-generated CSR, 90 days, manual LAN renewal initially; explicitly approve the native enrollment extension when implemented. | Avoids server possession of native keys; current signer and native registry support are missing. Browser PKCS#12 policy stays intact. |
| Should locked phones refresh authenticated data? | No in the first native-app release; generic push, fetch after unlock. | Strong local key/session protection conflicts with unattended background access. Accept stale badges and no fixed refresh interval. |
| Who operates push, and is it optional? | Owner-operated opt-in relay using direct APNs/iOS and FCM/Play Android; all app use works without push. If owner cannot operate a relay, ship foreground-only pilot. | Official provider credentials must not be distributed to every main. Hosting, abuse handling and metadata trust need an owner. |
| What may appear on lock screens? | Generic attention text only; seven-day minimal relay status logs, no analytics SDK. | Minimizes provider/relay disclosure; metadata remains visible. Rich encrypted previews are separate work. |
| How much offline data? | No persistent cache by default; opt-in 24-hour/100-item encrypted status summaries, no offline actions. | Makes the lost-device and stale-state cost explicit. |
| Which stores/channels and who owns signing? | Owner/publisher accounts; TestFlight → App Store, signed Android pilot → Play, separate FOSS package/channel with no-push first. | TestFlight expires, F-Droid acceptance/signing and App Review need qualification; permanent IDs and custody are hard to reverse. |
| What devices and tool budget? | Fund a Mac/Xcode environment and physical iPhone plus Android; choose minimum OS versions from the owner's actual devices during phase 0. | This Linux documentation session cannot establish signing access, current SDK requirements or device compatibility. Do not promise an untested OS range. |
| What ongoing support contract? | Reserve 1–3 days/month initially and a six-month deprecated-API window; reassess after pilot. | Store releases, relay support and server/client version skew continue after the first build. |

## Evidence and remaining uncertainty

Local inputs: PLAN's native-app sequencing; REMOTE-ACCESS including current shipped slices and unimplemented gates; decision 0020; DESIGN's phone navigation/accessibility; the current worker and push sender; brand-kit README/palette/icon masters. Official platform sources linked above were consulted on 2026-10-24. Platform documentation proves available APIs and stated restrictions, not compatibility with Rynhult's exact edge and device matrix. Recheck policies, SDK versions and provider behavior during implementation. The app proposal remains unapproved. The experimental server slice adds decision 0071 and API/Guide documentation; it installs no operational service and is not a mobile release qualification.
