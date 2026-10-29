# Live preview

Approved scope: owner decision, 2026-10-22; PLAN and competitor research gap 3.

A Preview tab on projects and sessions embeds a running app, with a listening reserved-port picker, Reload and Open in New Tab. It uses ACC's existing controls and responsive layout. It never starts an app. Element selection is implemented with explicit review before sending (below).

## Trust boundary

Use `preview-<project id>-<port>.<preview domain>`: one isolated origin per project and port, avoiding tabs changing each other's target. The domain defaults to the wildcard zone, or ACC's hostname in internal-CA mode; `ACC_PREVIEW_DOMAIN` may override it. Only these exact names reach a dedicated preview ASGI gateway; they never reach ACC routes or static files. The upstream is always literal `127.0.0.1` and a currently reserved project port in 8000–8199. Redirects are returned, never followed. Projects on other machines use the bounded typed stream below over their authenticated machine link.

A signed-in browser POSTs to ACC to obtain a five-minute, purpose-separated HMAC token scoped to the project, execution machine, port, hostname and server-side login. Bootstrap navigation carries it in a URL fragment (never an access-log query); a small gateway-owned page exchanges it for a Secure, HttpOnly, host-only `__Host-acc_preview` cookie and removes the fragment. Each HTTP request and WebSocket connection rechecks signature, expiry, login existence/expiry, current user/project permission and reservation. WebSockets close on expiry or revocation during the connection. Logout, user removal, ownership changes and port release revoke access. Bearer CLI credentials cannot mint browser preview access. No preview credential authenticates ACC APIs.

The iframe permits scripts, forms and same-origin app storage, but no top navigation, popups or access to the parent. ACC sends `frame-ancestors 'none'` and a frame-src restricted to the configured preview DNS suffix. Preview responses constrain frame ancestors to ACC's configured origins and the same preview origin and apply a CSP sandbox, including when opened directly. ACC rejects foreign Origin requests and non-navigation cross-origin fetch metadata, in addition to existing CSRF checks. Upstream cookies, credential headers, forwarding headers and security-policy headers are stripped at the gateway boundary; the app never receives ACC or preview credentials. Service workers are disabled for previews. Responses are not cached. HTTP and WebSocket dev traffic use the same authorization gate.

## Private routing and certificates

Caddy applies ACC's exact private-network allow list before proxying previews to accd's Unix socket. Preview routing is fixed installer-rendered configuration, never an agent-supplied host/upstream or publishing request. It uses the existing root installer and Unix admin-socket validation/reload path; no runtime Caddy privileges or DNS writes are added to accd. Publishing remains separate and approved; preview hostnames are reserved against publishing.

Cloudflare mode routes matching one-label preview names inside the existing wildcard site, reusing its DNS-01 wildcard certificate. A private wildcard DNS entry for the preview domain must resolve to ACC's private address; no per-project public DNS records or individual certificates are created. Internal-CA mode adds a wildcard preview site with `tls internal`; clients need private wildcard DNS and trust in Caddy's CA. An override in Cloudflare mode must equal the existing wildcard zone (otherwise installation refuses it); internal mode may use a private configured suffix. Prefer the same site as ACC so browsers accept iframe cookies; unrelated preview domains may be blocked by browser third-party-cookie policy. Neither mode contacts public DNS with project names.

## Verification and limits

Tests cover role checks, login revocation, token tampering/scoping/expiry, reserved-port and local-host restrictions, gateway origin isolation and header stripping, WebSocket authorization, both Caddy modes, and the browser flow with a test-owned demo app, including phone layout. Deployment/DNS/CA trust is documented and tested through rendering; this task does not change the host configuration. Already-rendered app pixels cannot be recalled on revocation; further network access is denied. Absolute dev-server URLs and strict dev-server Host allow lists may need the preview hostname configured in the app.

## Projects on other machines: transport design

Design recorded before implementation, 2026-10-22. Negotiate `preview-v1` in authenticated node info capabilities. Legacy nodes retain “Preview is available for projects on this machine” and add “update this machine”. No node listener, publishing action or general proxy is added. The browser origin, token, cookie, sandbox and header rules above remain identical for remote projects.

A separate uppercase binary frame family uses the machine link's five-byte type/id header and independent stream ids: `R` replaces a project's reservation snapshot, `O` opens typed HTTP, WebSocket or listening-probe work, `H` returns metadata, `D` carries HTTP chunks, `T`/`B` carry WebSocket messages, `F` ends an HTTP body, `A` acknowledges consumption, and `C` cancels. Stream ids are positive, odd and increasing; id 0 is reserved for snapshots. The node checks strict metadata, literal `127.0.0.1`, ports 8000–8199 and a fresh project reservation snapshot before connecting. Snapshots are derived from current database reservations on this link's machine, expire after three seconds, and are refreshed every second during active work. Expired/removed reservations terminate streams. Reconnect discards all snapshots.

Content frames are at most 64 KiB, metadata 16 KiB, with at most 16 preview streams per link. Each direction allows only one unacknowledged content frame per stream. Consumption acknowledges credit; slow consumers stop upstream reads without blocking the receive loop. Open/send/HTTP-receive deadlines are 30 seconds. Idle WebSockets remain open under link heartbeat, token rechecks and reservation renewal. Preview outgoing traffic has lower priority than terminals/control and its own bounded allocation; existing broker budgets remain in force. Cancel, disconnect and replacement cancel tasks, close upstreams and discard pending work. HTTP streams both directions with redirects disabled and no environment proxies; WebSocket messages are bounded to 64 KiB. Main-side authorization is rechecked for chunks/messages and idle WebSockets. No credentials reach the node app.

Ownership follows high-availability rules: streams and reservation caches belong to one authenticated live machine connection, are rebuildable and never replicated. Restart/replacement/revocation cancels them without replay. A future clustered gateway must route to the connection owner and fence stale owners before issuing snapshots. Local execution never substitutes for an unavailable machine. App mutations retain ordinary browser uncertain-result semantics; ACC never retries them automatically.

Verification covers framing, budgets, backpressure, cancellation, node host/reservation refusal and expiry, linked HTTP/WebSockets, unchanged isolation/token checks, old-node fallback and a demo-machine browser flow. No deployment or HA capability is implied.


### Implemented wire contract and lifecycle

`preview-v1` is advertised only in the authenticated hello (not inferred from a version). A reservation frame uses stream id 0 and JSON `{project: integer, ports: integer[]}`; at most 32 cached project snapshots, with expired entries pruned every half second. Every open has exactly `{project, host, port, mode}` for a probe, or adds `{method, path, headers, protocols}` for HTTP/WebSocket. Paths are origin-relative raw ASCII paths plus query; no authority or scheme comes from the browser. The node constructs the URL from fixed scheme/host and the checked port. HTTP response metadata is `{status, headers}`; WebSocket response metadata is `{protocol}`. Failed opens cancel without exposing upstream errors or credentials. HTTP end frames are acknowledged before stream disposal.

Each stream has one receive slot and one send credit per direction. Each content payload is at most 64 KiB (metadata 16 KiB), including WebSocket messages. There are at most 16 active streams on either end; the node also counts opening and closing tasks until completion. Duplicate, reused and non-increasing stream ids are refused. Send queues account for 256 bytes overhead per message, at most 2 MiB preview traffic and 64 MiB total in the shared scheduler. The primary retains its existing 64 MiB broker outbox, with only one broker frame at a time entering the scheduler; the combined primary outgoing allocation is bounded by 66 MiB. Broker/terminal and text control traffic outrank previews; an already transmitting frame cannot be preempted. Queue overflow, invalid frames and expired deadlines fail closed, with no retry. Streaming responses forward small chunks immediately, including server-sent events. HTTP body length is not buffered or capped as a whole; bounded chunks and flow control constrain memory. On disconnect all stream tasks, sockets, grants and queues are discarded. The existing git-backup broker channel and outbound authenticated git push are unchanged.

Operators update both main and node code; no installer, firewall or node service permissions change. The main's existing private preview DNS/certificates also serve remote previews. Revoking a node or replacing its link ends active previews; reconnecting requires a fresh stream and current database grants. Port release is rechecked on the main immediately on the next request/message, and the node stops existing streams when a replacement snapshot removes it or the three-second lease expires. These transient connection leases are not cluster leadership leases. A compromised authenticated main can declare reservations, as it already controls execution; the node independently confines the resulting connections to declared project ports on literal loopback.

## Element picker (design, 2026-10-25)

Embedded previews use a gateway-owned wrapper at `/__acc_preview/frame` on the same isolated project/port origin,
with the running app in a same-origin child iframe. The wrapper can inspect that child's DOM; it cannot inspect
Rynhult. No app HTML is rewritten and no code from the app enters ACC. Origin isolation, cookie/network gates and CSP
sandboxes remain unchanged. Preview frame-ancestors adds only 'self' to permit the wrapper ancestor, never another
project's preview origin. Normal new-tab previews still open the app directly. Local and linked projects share the wrapper;
no node protocol change is needed. Cross-origin child navigations and nested frames are not inspectable.

A person chooses a running session on the project page (the current session on its own page), then **Pick element**.
A fixed protocol checks the exact parent/preview origins and WindowProxy sources in both directions. A fresh random
pick id correlates one selection; it is not an authentication token. Only a bounded JSON string (4,096 characters)
can cross back, and ACC accepts at most one result while that pick is armed. No message can invoke a command, navigate
ACC or send to an agent. Reload, navigation, port/session changes, Escape, cancellation and timeout discard the pick.

The wrapper gathers a structural CSS path (at most eight tag/nth-of-type segments, 512 characters), a visible text
excerpt (1,000 characters, bounded traversal), and a finite viewport-relative box. It never gathers HTML, URLs,
attributes, input values, passwords, textarea/select contents or editable text. The app can forge its description:
all page data remains untrusted. ACC checks shape/limits again, renders text through React, and asks the existing
session message machinery to prepare a redacted, plain-text, explicitly untrusted draft. The person sees the exact
message and destination in the shared confirmation dialog before sending. Invisible characters (Unicode format,
variation selectors, private-use) are removed before redaction. **Send to agent** is held disabled for 800 ms with
focus on Cancel, because the page controls when the dialog appears. Only ACC's own browser/CSRF-protected
endpoint can consume that short-lived, login/session/project/port-bound draft, once; permissions, reservation and
running managed-session state are checked again. The picker audit stores metadata; the confirmed redacted message also enters the ordinary session message audit and history. No retry after uncertain sends.

This slice omits screenshot crops, shadow-root/nested-frame inspection, source-file inference and automatic changes.
Selectors are best-effort hints for the rendered page, not trusted source mappings; boxes may be stale after layout
changes. Picker failures never grant the preview more access. An ordinary app click is suppressed while picking;
Escape cancels, and focusing an element then pressing Enter provides a keyboard alternative.
