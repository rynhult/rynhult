# 0058 — Scoped roaming-machine git backup transport

Date: 2026-10-25. Implements decision 0050's roaming-machine backup approval.

## Context

Git backups are scheduled by the main and run as each project's broker account.
The machine's client key belongs to the node service, and must not be passed to
those brokers. Public edge admission already binds machine leaves to the node's
own projects. LAN and Tailscale remain the normal addresses.

## Decision

Owner amendment 2026-10-25: public git backups are **off by default per machine**,
including existing machines. An owner opts in through revision-checked Machine
settings, from the private address with a freshly verified browser sign-in
(security review and owner decision 2026-10-25; opting out needs neither). Consent is persisted against the immutable node ID, audited on change,
and required at scheduling and public admission. Disabling it revokes queued/
public capabilities and closes public git streams; private transfers continue.
Removing and reenrolling a machine does not inherit the previous consent.

When verified remote access, machine identities and that machine's consent are on, updated nodes
advertise `git-backup-v1`. The main sends a bounded backup job over the existing
signed link. A ten-minute, project/node-scoped in-memory grant lets that job
request its push token at `POST /api/git/{repo}/token`. A retry returns the same
job token; completion/cancellation revokes both capabilities. Browser sign-ins,
installation owner tokens and agent tokens do not authorize this exchange. The
grant alone isn't enough either. The node signs the grant, its project and the
path it is sent on (private or public) with its enrolled link key, in
`X-Acc-Backup-Proof`, and the main checks that signature against the node the
grant was issued for. A grant shown to a fake public server is therefore useless
at the private address, and to anyone without the machine's key (security review 7,
SR7-2).

The node tries the existing private backup origin with its enrollment server
trust. Only failure to establish the connection (DNS/routing/refusal/connect
timeout) permits trying the enabled public origin. TLS verification failures,
HTTP errors and redirects fail visibly. The public client uses system server
trust plus this node's current, node-bound machine identity. It never weakens
server verification or follows redirects. Because system trust admits any
publicly trusted certificate for the public name (or a TLS-inspection root on
the machine), the push token is minted with the grant and the job carries its
SHA-256: the node pushes only after the exchange returned exactly that token,
which no other server can produce (added by the 2026-10-25 security review).
The token request and the push may use several connections, so every public
connection of a job trusts only the leaf certificate one verified connection
presented before the token request (security review 7, SR7-1). A later
connection to any other server fails before the token or git data is sent.

The review's follow-up adds four more rules:

- **Origin pin.** The node takes `git-backup` jobs only over a control link
  established privately, meaning a link host other than the public name, current
  or previously pinned. A job over a link through the public name, or with no
  link, is refused: whoever answers for that name also chooses the job's token
  hash. The node pins the origins learned from private-link jobs, so a link
  through an earlier public name still counts as public. Only the hostnames are
  pinned. The server leaf rotates with ACME and
  keeps normal validation. The edge revision changes with every paired device,
  so pinning it would stop roaming backups whenever a phone is paired.
- **Per-machine queue.** The main queues one job per machine instead of failing
  other projects.
- **Bare origins only.** Jobs are used only when the backup origin is a bare
  HTTPS origin; otherwise the broker push stays.
- **Audit.** The first public exchange of each grant is audited as
  `git_backup.public_token`.

For that job only, the node binds a token-authenticated IPv4 loopback forwarder.
It admits only this project's receive-pack advertisement and push, streams bounded
requests/responses to the selected origin, and refuses redirects. The broker sees
a loopback URL and its existing short-lived token; it receives no machine key or
key path. The broker token header is URL-scoped so repository `insteadOf` rewrites
cannot send it elsewhere. The main checks public policy and the existing active
leaf/project binding on requests. No reverse proxy service is installed.

A successful result must agree with the main's authenticated token-exchange path.
Status records `via: private|public`; public success creates `git_backup.public`
audit and an observation keyed by immutable node ID. Host shows the last public
success even while the machine is offline. A later private success preserves that
historical public timestamp. No database migration is needed.

## Consequences and limits

Everything remains off unless both existing owner settings are enabled and the
edge apply is verified/current. Old nodes and local projects keep private backup
behavior. No control, update, preview, enrollment or certificate-renewal URL is
changed by this fallback. Scheduling still requires the existing signed control
link; this is not an autonomous offline-node backup scheduler. An otherwise
working control link can use a different private/tailnet address from the
configured git-backup origin. Keep the private backup origin configured.

There is briefly a loopback-only listener, one job per node, rather than an
externally reachable node listener. Existing macOS same-user trust is unchanged:
not passing the key to a broker does not isolate processes sharing that account.
Token/grant material is transient memory only; the active grant caches its issued
token for retries, while the push-token verifier indexes hashes. No secret enters
URL, argv, saved remotes, audit or observations.

Live edge deployment, real internet routing and physical roaming machines remain
separate qualification. This change neither installs helpers nor enables remote
access. Overall public-release gates in REMOTE-ACCESS.md still apply.

## Alternatives considered

- Give brokers the machine key: rejected because it expands certificate custody.
- General node proxy or automatic control-link fallback: rejected as outside the
  owner's git-backup-only scope.
- Retry public after a TLS/HTTP refusal: rejected because it hides trust/policy
  failures and risks credential disclosure.
- Local scheduling from cached project metadata: a separate design; not required
  to preserve the existing main-scheduled backup model in this transport slice.
