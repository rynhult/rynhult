# Usage statistics and bug reports

**Status: approved for implementation by the owner, 2026-10-23 (PR #202).**
Implementation and current limits are described in [the privacy note](PRIVACY.md) and
[reference collector instructions](../deploy/telemetry/README.md). The project owner's collector at
`https://telemetry.rynhult.dev` runs as a pre-release staging service (Frankfurt, since 2026-10-26) with the
privacy notice from `deploy/telemetry/privacy.html`; data it receives before v1 is deleted at launch. The client
is not yet qualified end to end against it, and sending stays off unless an owner opts in.
Security and privacy review is recorded in the repository's internal review artifacts.
The original design below records the broader proposal;
the owner's implementation scope and answers supersede its pending questions and conflicting defaults.

Owner decisions: the owner is the data controller and hosts the collector on a self-hosted machine at the
reserved `https://telemetry.rynhult.dev`; installation owners may configure another HTTPS collector.
Usage and reports have **separate** 90-day rotating keys. Reports ask each time when enabled, and crashed
sessions contain metadata only. Approved retention is 90 days raw usage, 13 months monthly totals,
180 days reports and 90 days local sent history. The drafted usage schema is approved. Public statistics
come later and must count at least ten installations per figure. No deployment was authorized at that point; the owner deployed the staging collector on 2026-10-26.


Owner request (2026-10-23): "opt-in automatic feature usage and bug reporting, no PII, just identifying what's being
used to improve, and reporting scrubbed logs when an issue occurs." ACC is self-hosted and will be published under MIT
at v1.0, so the people sending data will mostly be strangers running their own copy. The design is built for them.

By default ACC sends **no** usage statistics or error reports. Its outbound connections are features an owner configures:
the daily release check against GitHub ([UPDATES](UPDATES.md)), DNS providers, Web Push services, owner-triggered
public-address detection, and the agents' own providers. Nodes talk only to their main.

## Recommendation in one paragraph

Two independent owner-only switches, both off by default: **usage statistics** (one small daily summary made only of
enumerated values and buckets, with no free text anywhere in it) and **bug reports** (a structured report when ACC
itself fails, assembled from allowlisted fields, scrubbed in layers and **shown to an owner before sending** unless
they chose automatic sending). Both go only from the main ACC, over HTTPS, to a **small collector service the project
maintainer hosts** at the planned project domain (written `telemetry.example.com` here until it is chosen), whose
source ships in this repository so forks and companies can run their own or leave it off. Each installation signs with
a key pair it generates when consent is given and rotates every 90 days, which gives rate limiting and self-service
deletion without accounts. Organization policy can force both off. No third-party analytics or error SDK.

## 1. Consent

| | Usage statistics | Bug reports |
|---|---|---|
| Default | Off | Off |
| Choices | Off / On | Off / **Ask each time** / Send automatically |
| Who can change it | Owners only (members never see the section) | Owners only |
| When on, it sends | One summary a day | A report per qualifying failure, at most 10 a day |

- **Where it is asked.** Once, after **Finish setup** on the one-time Set up ACC page, as its own card: both choices
  unticked, an expandable "Exactly what is sent" with the example payloads below, and **Continue** that works with
  nothing ticked. Existing installations get a single dismissible Inbox item for owners after the upgrade that adds
  the feature; dismissing it leaves both off. The Linux terminal installer (`deploy/setup.sh`) does not ask: consent
  belongs to a signed-in owner, not to whoever ran root setup.
- **Settings → Usage and bug reports** (owners): the two switches, what each sends in plain words, **Preview what
  would be sent** (below), the schema version consented to, who consented and when, the last send and its result,
  the local log of sent bug reports, and **Delete data already sent**.
- **Consent is to a schema version.** A release whose usage schema adds a field, or whose report schema adds a
  source, turns that switch back off with an Inbox item explaining the difference; it is never widened silently.
  Removing a field or an enum value doesn't need new consent.
- **Recorded and audited.** A `Setting` row `telemetry.consent` holds `{usage, reports, usage_schema,
  report_schema, by (user id), at}`. Audit events: `telemetry.consent` (old and new choices), `telemetry.send`
  (kind, schema, report id and size, outcome; never content), `telemetry.report` (queued, sent, discarded, by whom),
  `telemetry.delete`.
- **Revoking.** Turning a switch off stops sending at once and discards anything queued. The off dialog has **Also
  ask the collector to delete what this installation sent** ticked by default; it sends signed deletion requests
  (section 4) for every key the installation used within the collector's retention, retries for 7 days, and shows
  the result. Deletion can also be requested later from the same section while the old keys are kept.
- **Organization policy** ([ORGANIZATIONS](ORGANIZATIONS.md)): `telemetry.usage` and `telemetry.reports`, each
  `allowed` or `off`. `off` disables the switch with the limiting source shown, as other policy-limited controls do,
  and stops sending on the next policy revision. Policy can only force off; nothing, including policy, can turn
  sending to the project's collector on without an owner's choice. Root-owned `ACC_TELEMETRY_URL` in
  `/etc/default/accd` names the collector (default: the project's; empty: the feature is absent and the section says
  so), so an acc-writable setting can't redirect reports, as with `ACC_UPDATE_REPOSITORY`.

## 2. Usage statistics

### What is sent

One JSON object a day, built from counters ACC keeps in its own database. **Every value is an enum member, a boolean,
a bucket or a day.** There are no free-form strings, so there is nothing for a prompt, a name or a path to hide in.

| Field | Values |
|---|---|
| `schema` | `"usage/1"` |
| `install` | Current public-key id (section 4): 22 base64url characters, rotates every 90 days |
| `day` | UTC date the counters cover; no time of day. Sent at a random minute of the following day |
| `acc` | `major.minor.patch` (or a release candidate's `major.minor.patch-rc.N`, also `-alpha.N`/`-beta.N`) for an installed release build, else `"dev"`; never the build hash |
| `os` | `linux`, `wsl`, `macos`, `windows`, `other` (main and nodes, as a set) |
| `install_kind` | `release`, `git-checkout`, `windows-installer`, `mac-installer` |
| `machines`, `people`, `projects`, `sessions_running`, `vault_keys` | bucket: `0`, `1`, `2-3`, `4-10`, `11-50`, `51+` |
| `providers_enabled` | subset of `claude`, `codex`, `copilot`, `cursor`, `antigravity`, `opencode` |
| `features_enabled` | subset of a fixed list: `groups`, `organizations`, `containers`, `backups`, `backup_server`, `publishing`, `dns_provider`, `auto_updates`, `push`, `issue_sources`, `remote_access`, `schedules`, `orchestrator`, `preview`, `custom_guardrails` |
| `used` | `{event: bucket}` for events that happened that day, from a fixed list (below); bucket `1`, `2-5`, `6-20`, `21-100`, `101+` |
| `by_provider` | `{provider: bucket}` of sessions launched that day |

Events in `used` (first schema; each names a feature, never an object): `session.launch`, `session.resume`,
`session.multirun`, `session.export`, `schedule.run`, `orchestrator.team`, `explain.run`, `changes.view`,
`files.edit`, `preview.open`, `pr.merge`, `pr.feedback`, `publish.request`, `vault.give`, `catalog.import`,
`search.query`, `backup.run`, `update.install`, `node.enroll`, `inbox.answer`, `guardrail.block`.

**Never sent, by construction:** prompts, transcripts, terminal output, model outputs, file names or contents, paths,
repository or project names, branch names, hostnames, machine names, IP addresses (the payload has none; the
collector's handling of connection addresses is in section 4), usernames, emails, display names, keys or their names
and variables, MCP server names, catalog item names, custom guardrail patterns, model or endpoint names, time zone,
locale, exact counts, exact times.

Example:

```json
{
  "schema": "usage/1",
  "install": "t3Q0b9xGq2m4VwZr8kLp1A",
  "day": "2026-11-02",
  "acc": "1.0.3",
  "os": ["linux", "macos"],
  "install_kind": "release",
  "machines": "2-3", "people": "1", "projects": "4-10", "sessions_running": "2-3", "vault_keys": "4-10",
  "providers_enabled": ["claude", "codex"],
  "features_enabled": ["backups", "publishing", "schedules", "push"],
  "used": {"session.launch": "6-20", "session.resume": "2-5", "changes.view": "21-100", "pr.merge": "1"},
  "by_provider": {"claude": "6-20", "codex": "1"}
}
```

### Schema, allowlist and the viewer

- The schema is a JSON Schema file in the repository (proposed `schemas/telemetry/usage-1.json`), with
  `additionalProperties: false` at every level and enums for every string. The same file is used three times: ACC
  validates each payload against it before queueing (an invalid payload is dropped and logged as a bug, never
  "fixed up"), the collector rejects anything that doesn't validate, and a test proves every counter call in the code
  names an event in the schema.
- Counters are increments of `(day, event, provider?)` rows in a new table, not a log of what happened: there is no
  per-event record with a time or an id to leak. Rows older than 7 days are deleted whether or not they were sent.
- **Preview what would be sent** (owners; also before consenting): shows the exact JSON of the next summary as it would
  be signed, plus the last one sent and when. It is computed locally and sends nothing. The same text is available as
  `GET /api/telemetry/preview` for scripts.
- Nodes don't send. The main already knows its nodes' OS family and sessions; nodes keep no telemetry state.

## 3. Bug reports

### What triggers one

Only failures of ACC's own code or its helpers, never "an agent did something wrong":

| Trigger | Source | Included beyond the common fields |
|---|---|---|
| Unhandled server error | accd request handler returning 500 (a FastAPI exception handler) | route template (`/api/sessions/{id}`, never the filled-in path), method, exception |
| Supervisor stage failure | the `stage()` wrapper in the supervisor loop (`log.exception("supervisor stage %s failed")` today) | stage name, consecutive failure count |
| Failed update or rollback | `update.result` from the root helper ([UPDATES](UPDATES.md)) | step number and name, exit code, rollback outcome, helper log tail |
| Failed install or node install | the setup wizard's and node installer's recorded result, when the main learns it | step name, exit code, log tail |
| Crashed session | a provider CLI process exiting by signal or with a code the provider adapter marks abnormal, or a broker error ending a launch/resume | provider, provider CLI version, exit code or signal, state before and after, which ACC launch step was last; **never** terminal, transcript or hook payload content |
| Broker or node crash | systemd restart of `acc-broker@`/`acc-node` detected by the supervisor | unit kind (not name), exit status, restart count |

Common fields: `schema` (`"report/1"`), a random report id, ACC version and build, OS family, `install_kind`,
component enum, exception class (only classes defined in `acc.*`, the standard library or a pinned dependency;
otherwise `other`), a stack fingerprint, and **frames from ACC's own modules only**: module path relative to the
package (`acc/server/supervisor.py`), function name and line number. No local variables, no arguments, no frames from
dependencies beyond their module name.

A report is deduplicated by fingerprint (exception class + top three ACC frames + component): one per fingerprint per
version per day, at most 10 a day per installation, queued reports expire after 7 days. A failure storm becomes one
report with a count bucket.

### How text is scrubbed

Free text gets in only through three fields: the exception message, the last ACC log lines (`acc.*` loggers, at most
50 lines), and helper/installer log tails (at most 40 lines). Everything else is enumerated. Text goes through these
steps in this order (redact the whole text before trimming anything; security review 3 found that trimming first lets
a long secret's prefix escape):

1. **Strip** ANSI and control characters (as `pulls.clean` does).
2. **Known secrets.** An installation-wide `Redactor` built from every vault key and retired value (not one session's
   keys, as today), plus the other secrets accd holds: files in `data/secrets/` (API token, update token, DNS
   provider tokens, VAPID key, backup passphrase file), TOTP secrets and model endpoint keys from the database. Same
   encodings as the vault redactor (percent, base64 at three alignments, 12+ character prefixes). Placeholder
   `«secret»`: report text never carries a key's name.
3. **Known identifiers.** Built from ACC's own database each time: usernames, agent account names, display names,
   project names and folder names, repository owners and names, machine names and hostnames, publishing hostnames
   and zones, address inventory entries, group and team names. Replaced, case-insensitively, by `<user>`,
   `<project>`, `<repo>`, `<machine>`, `<host>`. This is the same idea as the public-snapshot denylist
   ([PUBLISHING-THE-SOURCE](PUBLISHING-THE-SOURCE.md)), filled automatically.
4. **Shapes.** `pulls.SECRETS` (private keys, GitHub/OpenAI/AWS/Slack/GitLab tokens, `Bearer`, `password=` and
   friends, URL credentials) plus: JWTs, long hex/base64 runs (32+ characters) → `«secret»`; email addresses →
   `<email>`; IPv4 and IPv6 addresses → `<ip>`; URLs → `<scheme>://<host>/…`; absolute paths → `<path>`, except
   ACC's own install tree, which becomes `<app>/acc/server/…`; home folders `~user` → `<home>`; any remaining dotted
   name with a public suffix that isn't on a short allowlist (`github.com`, `pypi.org`, `npmjs.org`, the collector)
   → `<host>`; UUIDs → `<id>`.
5. **Trim**: each line to 200 characters, the message to 300, each tail to its limit; total report at most 16 KiB.
6. **Scrub again** (steps 2–4 on the trimmed text) and validate against `schemas/telemetry/report-1.json`.

Example (after scrubbing):

```json
{
  "schema": "report/1",
  "report": "b1f3c0d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
  "install": "t3Q0b9xGq2m4VwZr8kLp1A",
  "acc": "1.0.3", "build": "4c1e9a0b77d2", "os": "linux", "install_kind": "release",
  "trigger": "supervisor_stage", "component": "supervisor", "stage": "providers", "repeats": "2-5",
  "exception": "TimeoutError",
  "fingerprint": "9d41c2e07ab35f18",
  "frames": [
    {"module": "acc/server/supervisor.py", "function": "refresh_providers", "line": 812},
    {"module": "acc/server/broker_client.py", "function": "call", "line": 141}
  ],
  "message": "broker call providers.check timed out after 30 s on <machine>",
  "log": [
    "WARNING acc.server.supervisor: providers: <machine> did not answer",
    "ERROR acc.server.supervisor: supervisor stage providers failed"
  ]
}
```

### Before sending

- **Ask each time** (recommended when reports are on): each report becomes an owner Inbox item, "ACC hit a problem:
  review the report before it is sent", showing the exact text that would be sent. **Send**, **Don't send**, and
  **Remove lines** (deleting only; nothing typed is added except an optional note, which is scrubbed the same way and
  labelled as the owner's). Unanswered items expire with the queue after 7 days.
- **Send automatically**: no Inbox item; the report is still kept locally.
- Every sent report is kept in Settings → Usage and bug reports → **Sent reports** for 90 days, byte-for-byte, with
  the collector's answer, so an owner can check what left and ask for its deletion by report id.

### What scrubbing can miss

Scrubbing is a best effort, and the design says so in the consent text. It cannot find a secret ACC doesn't hold (a
token an agent created, a password typed into a program) in an unrecognised format, a value
encoded otherwise (hex, JSON `\u` escapes, compression), a person's or customer's name that ACC has no record of
(inside an error message from a library, say), or meaning carried by structure (an unusual stage failing reveals a
setup). The controls that hold are structural: most of a report is enumerated, free text comes only from ACC's own
logs and helpers (never from agents, terminals, transcripts or repositories), the defaults are "off" and then "ask
each time", sent bytes are kept locally, and deletion works. A test corpus of canaries (each secret kind, each
identifier kind, each encoding, long lines crossing the trim boundary) must come out clean, and the existing check
that scans logs for vault values runs over the report pipeline too.

The reviewed implementation omits multiline exception messages instead of guessing how to join
wrapped secrets or names. It also removes textual base64 values, cookie headers and form-encoded
known identifiers. Old unsent reports are discarded on the privacy upgrade rather than rewritten
after review. See [the privacy note](PRIVACY.md) for the implemented limits.

## 4. Where data goes and who runs it

### Options

| | Owner-hosted collector (recommended) | GitHub issues | Third-party service (Sentry, PostHog, …) |
|---|---|---|---|
| Fits usage statistics | Yes | No | Yes |
| Fits bug reports | Yes | Only as a manual follow-up | Yes |
| Who sees data | The maintainer(s) | Everyone: issues are public and permanent, and mirrored | The vendor too, under its terms; a processor for GDPR |
| Credentials on the client | None (installation-generated key) | A GitHub token per installation, or a shared one embedded in public source: both unacceptable | A public DSN/key that anyone can send with |
| Free text control | Our schema, enforced server-side | None once posted | SDKs capture by default (request bodies, breadcrumbs, local variables, IPs); each must be switched off and kept off across SDK updates |
| Deletion | Our endpoint, signed | Only by the maintainer; copies persist | Vendor process |
| Cost and work | Build and run a small service (section 6) | None | Free tier then paid; least code |
| Open-source users | Can read the collector's code, self-host it or turn it off | Need a GitHub account | Must trust a vendor they didn't choose |

**GitHub issues** stay the place for people to report bugs themselves. A sent report's local view gets **Open a
GitHub issue with this report**, which opens the repository's new-issue form in the person's own browser with the
already-scrubbed text prefilled; they read it and submit it under their own account, or don't. ACC never posts
issues. **A self-hosted error tracker** (GlitchTip or self-hosted Sentry) behind the collector is a possible later
convenience for the maintainer's triage, receiving only already-validated reports, not something clients talk to.

### The collector

- **Code:** `src/acc/telemetry_collector.py` and `deploy/telemetry/`, MIT like the rest, a standalone
  FastAPI/SQLite service with a hardened systemd unit and Caddy TLS configuration. See the
  [operator runbook](TELEMETRY-COLLECTOR.md). Nothing has been deployed. Endpoints: `POST /v1/usage`,
  `POST /v1/report`, `POST /v1/delete`, `GET /v1/schemas` (the schema
  versions it accepts, so a client can stop sending a retired version). Caddy also serves an operator-provided privacy notice.
- **Transport:** HTTPS only, certificate verified, no redirects followed, HTTP/1.1 or 2, 10-second timeout. Request
  bodies at most 4 KiB (usage) and 16 KiB (report). The client sends `User-Agent: ACC-telemetry/<version>` and no
  cookies. Errors are recorded as "sending failed" without the network exception text, as the update check already
  does.
- **Authenticated, not trusted.** When a switch is first turned on, accd makes an Ed25519 key pair
  (`data/secrets/telemetry/<id>.key`, 0600); `install` is the public key's id. Each request carries the public key,
  a timestamp and a signature over the body. The collector accepts the first request from a new key (trust on first
  use), then requires the same key for that id. This proves "the same installation as before", which is what rate
  limits, deduplication and deletion need. It cannot prove "a genuine ACC": the code is public, so anyone can make
  keys, and no client secret could change that. Every report is treated as untrusted input (below).
- **Rotation:** a new key pair every 90 days, unrelated to the previous one, so long-term linkage at the collector is
  limited to one period. ACC keeps old private keys only for deletion and forgets each once the collector's longest
  retention for it has passed (13 months).
- **Rate limits:** per key, 2 usage summaries a day and 10 reports a day; per connection address, 60 requests an
  hour (IPv6 per /64) and 20 new keys a day, enforced in bounded worker memory. Each authenticated key also has a
  60-request/hour limit independent of IP and deletion; payload validation before storage. Over-limit requests
  get 429 and the client backs off until the next day.
- **Connection addresses** are used for rate limiting in memory only: not stored with data, not in access logs (the
  collector's Caddy runtime logger also discards records), no GeoIP.
- **Storage and retention:** raw usage summaries 90 days after receipt; monthly aggregates finalize after the
  seven-day late-arrival window, drop installation ids, and are kept 13 calendar months; bug reports 180 days;
  deletion requests processed within 30 days, including the collector's backups (which keep 30 days): a 45-day
  erasure ledger of key/report ids is replayed on restore. A global storage cap refuses new data, not deletion.
- **Server-side scrubbing:** the collector never trusts a client to have scrubbed. It redacts secret and
  identifier shapes (section 3, step 4, without the installation-specific known values) in report text before
  storing it; client-scrubbed text passes unchanged.
- **Access:** the maintainer(s) only through MFA-protected operator access; no administrative HTTP interface.
  Local-only operator commands back up, restore, delete and export. Export omits every cell below ten key ids,
  without grand totals or suppression counts. Finalized unlinked totals cannot be subtracted by key. Any
  published statistics (a public "providers in use" chart, say) only show cells counting at least 10 installations.
- **Deletion:** `POST /v1/delete` signed by the key, for all data of that key or for given report ids. Answer: a
  receipt id; status visible in ACC. A person who has lost the keys (reinstalled) can ask through the published
  contact address with a report id from their Sent reports list; without keys, usage data can't be found, which is
  the point of keeping it unlinkable.

### What open-source users should expect

Published in the README, the Guide and the consent card: off unless an owner turns it on; exactly the fields above;
who runs the collector and where; retention; how to delete; that the collector's code is in the repository; that
`ACC_TELEMETRY_URL` can point to a company's own collector or be emptied to remove the feature; that git checkouts
send as `install_kind: git-checkout` and test runs and CI never send (the client refuses while `ACC_TEST` or `CI` is
set and the test suite stubs the transport). A privacy notice at the collector's address names the controller and
contact.

## 5. Threats and mitigations

| Threat | Mitigation | Residual |
|---|---|---|
| A secret or personal data in a bug report | Allowlisted sources only; layered scrubbing with installation-wide known secrets and identifiers; redact before trim; ask-each-time default; local copy of what was sent; deletion; canary corpus tests | An unknown secret or name in an unrecognised form (section 3) |
| Free text in usage statistics | No string fields except enums; schema enforced at both ends; test binds counters to the schema | None found in design; a schema change needs review and new consent |
| Fingerprinting an installation | Buckets for every count; no time of day, time zone, locale or build hash; key rotation every 90 days; no IPs stored; small-cell suppression in anything published | A rare combination (e.g. 51+ machines on `windows` with `organizations`) is still distinctive within one 90-day period at the collector |
| Linking reports to usage | Separate independently rotated usage/report keys; no collector rotation-alias protocol; ACC deletes with every retained key | Timing and rare attributes may still correlate streams |
| Spoofed or junk data | Treated as untrusted statistics, not decisions; per-key and per-address limits; closed-schema validation; new-key cap per address; export suppresses cells below ten | Fabricated keys and multiple addresses can skew counts; signing keys are not verified people |
| Malicious bug report content (prompt injection, script) | Reports are untrusted data: shown as text, never rendered as HTML; if an agent triages them, they are framed as untrusted input as PR feedback is ([PR-FINISHING](PR-FINISHING.md)) and the agent can't act on them unattended | Human judgement during triage |
| Abuse of the collector (flooding, storage exhaustion) | Streaming body caps, closed schemas, bounded per-IP/per-key limits, single worker, hourly retention, VM disk alerts, systemd memory/CPU/task limits, private Unix socket behind Caddy; separate from ACC and release infrastructure | A large distributed flood or fabricated keys needs hosting-provider protection; restarts reset memory limits |
| Collector compromise | It holds only enumerated usage and scrubbed reports; no credentials of any installation; the maintainer's admin sign-in has a second factor; data minimised so a breach reveals little | Scrubbed reports may still contain whatever scrubbing missed |
| Redirecting reports from a compromised acc-writable setting | Collector address is root-owned configuration, HTTPS verified, no redirects | Root on the main machine is trusted, as everywhere in ACC |
| A member turning it on | Owner-only API and UI; organization policy can force off; audit | None |
| Silent widening in an update | Consent bound to schema versions; new fields switch it off and ask again | None |

**Privacy law basics** (not legal advice; confirm before launch). Under GDPR-style law, a pseudonymous installation id
and a report that might contain a missed name can be personal data, so the design treats it that way: consent as the
legal basis, specific (two switches) and informed (payload shown), as easy to withdraw as to give, with erasure on
request, data minimisation and storage limits as above. The maintainer becomes a controller and needs a published
privacy notice with a contact, a record of processing, a data-processing agreement with the collector's hosting
provider and, preferably, EU hosting so no transfer mechanism is needed. For company installs the owner consents on
the company's behalf; ORGANIZATIONS policy gives the company the off switch. Reports from members' sessions are about
ACC's code, but a crashed session's report still names the provider and timing of a person's work, which is another
reason for "ask each time".

## 6. Effort, phases and questions

Estimates are engineer-weeks including tests and docs.

| Phase | Outcome | Effort |
|---|---|---|
| 0. Decisions | Owner answers below; domain, controller, privacy notice text and hosting chosen | owner time |
| 1. Local only | Counters table and schema, consent record and audit, setup card, Settings section, preview viewer and API, organization policy keys, Guide pages. Nothing is sent; can ship before the collector | 2–3 |
| 2. Collector and usage | `collector/` service, signing and rotation, validation, rate limits, retention, deletion, deployment on the chosen host; client sending and backoff | 2–3 |
| 3. Bug reports | Triggers, report schema, scrubber with installation-wide redactor and identifier lists, Inbox review flow, sent-report log, GitHub-issue handoff | 3–4 |
| 4. Qualification | Canary corpus, security review of client and collector, end-to-end against a staging collector, published privacy notice | 1–2 |

Total **8–12 engineer-weeks**. Phase 1 is useful on its own (owners can see what would be sent). Sending needs
phases 2 and 4 and should be available before v1.0's publication so the first public users see it from the start.

**Questions for the owner**

1. Approve the owner-hosted collector at the planned domain (which name?), with GitHub issues only as a manual,
   person-submitted follow-up and no third-party analytics or error service?
2. Who is the data controller for the privacy notice (you personally or an entity), which contact address, and which
   hosting provider/region for the collector?
3. Bug reports default to **ask each time** when turned on: approve? And should usage and reports share one
   installation key (simpler deletion) or use separate keys (no link between them)?
4. Approve the retention figures (raw usage 90 days, aggregates 13 months, reports 180 days, local sent log 90 days)
   and the 90-day key rotation?
5. Approve the first usage schema (fields and events in section 2)? Anything missing that you want to learn, or
   anything there you'd rather not collect?
6. Should crashed agent sessions trigger reports at all, given they describe a person's work, or only ACC's own
   server, supervisor, broker and update failures?
7. Will you publish aggregate statistics, and if so, is a minimum of 10 installations per cell acceptable?
