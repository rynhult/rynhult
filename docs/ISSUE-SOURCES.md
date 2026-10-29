# Issue sources

Approved scope: Linear and GitLab (2026-10-22), plus owner-allowed self-hosted GitLab and Gitea/Forgejo (2026-10-23; PLAN).

## Model and credentials

Keep GitHub's existing snapshot and launch shortcuts. Add an optional repository remote to projects (the current
registry only stores GitHub's `owner/name`), plus project-scoped GitLab and Linear connection settings. GitLab
derives the repository path from that remote, supports nested groups, and uses an explicit HTTPS base URL for
self-hosted installations. Linear stores a team id and optional project/label ids.

Connections contain **vault key ids only**, never token values. A selected key must already be given to the project
and usable by the signed-in person. Recheck access, grants and key rotation on every request, before consulting the
cache. Keys used by these server integrations are excluded from session environment delivery and key context.
Owners can access all projects; members can access only their own. Private vault keys remain private even to an ACC
owner viewing a member's project.

## API and UI

Small server-side GitLab REST and Linear GraphQL clients return a shared issue shape: source, kind, id, identifier,
title, description, state, labels, assignee, updated time and safe HTTPS link. Lists are paginated and filterable;
GitLab includes separate issue and merge-request lists. Linear's configured team/project/label restrictions apply
to both lists and individual issue reads. External failures use fixed, actionable messages (expired token, missing
scope, inaccessible repository/team, rate limit), never upstream bodies or exception URLs that might echo secrets.

The project page gets an Issue sources configuration card and source lists, using DESIGN's existing cards, fields,
QueryState and buttons. No token entry field: add a key in Settings, give it to the project, then select it here.
GitLab's base URL must match the configured remote's origin/path prefix. Reject credentials, queries, fragments,
non-HTTPS URLs and unsafe path components; never follow redirects or pagination URLs carrying credentials.

New session supports selecting a source and issue/MR, as well as the project-page shortcut. It carries only an
issue reference. Preview and launch fetch the authoritative issue server-side and append its title, description
and link inside explicitly untrusted reference framing, not instructions. Changing projects clears the reference.
The person's assignment remains editable. ACC does not create, assign, close or merge issues/MRs.

## Requests and optional write-back

Use a short (30-second), bounded in-memory cache partitioned by credential identity/version and connection/filter.
Serialize requests per credential, impose a bounded request budget and honor provider rate-limit backoff.
Pagination is bounded; return explicit continuation rather than silently truncating lists. Do not persist tokens,
upstream errors or raw HTTP objects in logs, audit rows or settings.

The optional Linear PR-link comment is implemented and off by default. It retains the originating issue on
the session, uses the launcher's still-authorized vault key, requires the project's explicit opt-in at send time,
detects a PR on that session's own worktree branch, and persists delivery status to avoid repeated comments. Failures
are visible, with no automatic issue status changes. GitHub uses the existing authorized project snapshot; GitLab
polls the source branch once a minute. A pre-send marker prevents automatic duplicate comments after uncertain
delivery (including a restart). Read-only issue sources do not depend on write-back. Server-only vault grants
remain server-only after disconnect; explicitly removing/giving a grant resets its purpose.

## Verification

Stub HTTP at the transport boundary: pagination, filters, provider errors, rate-limit/cache isolation, safe URLs
and redirects. API tests cover vault-only configuration, revoked/private/ungranted keys, permissions, and both
preview and launch with untrusted issue content. Playwright covers configuration, lists, errors, filtering and the
New session handoff. Update Projects/New session Guide pages, internal architecture, Unreleased and PLAN; finish
with a handoff containing results and any remaining limitations.

## Owner-allowed self-hosted servers (2026-10-23)

Settings → **Issue sources** manages an instance-wide allowlist, exclusively for owners. Each entry stores an
HTTPS base URL (including optional port/path prefix), the exact resolved address set and an optional uploaded PEM
CA certificate in `setting` rows named `issue_server.<id>`. No schema migration is needed. Resolve first, inspect
and explicitly confirm the addresses, then save; saving resolves again and refuses a changed set. Re-confirming
an existing entry keeps its URL fixed. Removal immediately disables subsequent source reads/previews/launches
using that entry, including cached data. Add, re-confirm and remove decisions are audited without PEM contents.
Members see allowed entry ids/URLs and whether a custom CA is configured, never the pinned addresses or PEM.

Project GitLab/Gitea connections select `server_id`; the server supplies its current base URL and policy on every
vault-authorized read. Members entering a base URL directly must resolve exclusively to public addresses when
saving. Every outbound connection resolves again, compares the entire answer set to the owner-confirmed pins,
and connects to a checked numeric IP while retaining the original Host and TLS certificate name. DNS drift,
including another private address, requires owner re-confirmation. Cache hits make no network connection; changed
pins/CA partition the cache, and removed entries fail before lookup. Public HTTPS hosts remain usable without an
entry and retain the public-only transport policy from SR3-01.

Only ordinary RFC1918 LAN and IPv6 ULA addresses extend the public policy. Loopback, link-local/metadata,
unspecified, multicast, mapped and transition addresses remain refused even to an owner. HTTPS certificate/name
verification stays enabled. Custom CA trust uses a fresh SSL context confined to the selected entry's host,
port and installation path; it never changes machine trust or other sources. Environment proxies and redirects
remain disabled. Upload the CA again when re-confirming to retain it; leaving that field empty uses system trust.

GitLab self-managed uses the existing GitLab REST adapter, including nested namespaces and merge requests.
**Gitea / Forgejo** is a second connection kind (`gitea`), using compatible `/api/v1/repos/{owner}/{repo}/issues`
endpoints, with `type=issues` or `type=pulls`; individual reads check PR metadata against the selected kind.
Both issues and pull requests support list/detail/preview/session launch, with open/closed/all states and
label/assignee filters. Closed pull requests may report merged state; a merged-only filter is not offered.
Use a project-granted vault token with repository/issue read access. The token is sent only in the Authorization
header (`token …`), never query parameters. See the [Gitea issue API](https://docs.gitea.com/api/next/operations/issue-list-issues/).

Gitea pagination requests 30 items per page, with a bounded numeric cursor. Matching HTTPS Link headers can supply a checked next-page number;
unrelated/credential-bearing links are ignored. A full page also offers a local continuation without following provider URLs (the final full page can lead to an empty next page). Shared transport rules
provide the 2 MiB uncompressed response cap, 20-second total network deadline, bounded cache/request budget,
provider backoff, sanitized errors, token redaction and explicitly untrusted launch text. This adds no issue/PR
write operations or Gitea PR-link polling/write-back.
