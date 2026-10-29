# Opt-in usage statistics and bug reports

Date: 2026-10-23. **Status: accepted by the owner; implementation subject to security/privacy review.** Design:
[TELEMETRY](../TELEMETRY.md).

## Approved owner answers

The project owner controls the data and hosts the collector on a self-hosted machine at the reserved
`https://telemetry.rynhult.dev`. No deployment yet. Installation owners can choose a different HTTPS
collector. Usage and reports use separate keys, rotated every 90 days. Reports ask each time and crashed
sessions contain metadata only. Retention and the usage schema below are approved. Public statistics are
later, with at least ten installations per figure. These answers supersede conflicting proposed defaults.
See [the implementation privacy note](../PRIVACY.md) for current behavior.

## Privacy-review hardening

The 2026-10-23 security review found that joining wrapped text
cannot distinguish a token split across lines from a secret or name whose original value contains
spaces. The implemented scrubber therefore omits ambiguous multiline exception text, keeping
structured metadata. Short known credentials, textual base64, URL/form encodings, cookie headers,
IPv6 scopes and paths with spaces or punctuation are handled conservatively. Migration `0044`
discards old unsent reports instead of silently rewriting already-reviewed bytes.

The collector refuses non-private, linked or non-service-owned storage and journal modes other than
DELETE. Secure deletion cannot remove historical WAL frames. A private default data subdirectory
avoids changing checkout permissions. Transient SQLite retention failures are logged generically and
retried after a minute. Collector changes are owner-audited and invalidate visible browser previews.

Consequences: some useful error text is deliberately lost; operators must stop and migrate an old WAL
database before using it. Existing sent history stays immutable, and snapshots/backups remain outside
the SQL deletion guarantee. Alternatives rejected: guessing whitespace at wrap boundaries, rewriting
approved payloads, chmodding an arbitrary shared directory, or claiming secure deletion also erases WAL.

## Context

The owner asked for opt-in feature usage and bug reporting with no personal data, to learn what is used and to get
scrubbed logs when something fails. ACC is self-hosted and will be published under MIT at v1.0, so most senders
will be strangers who must be able to see, limit and undo what leaves their machine. ACC sends no telemetry by default.
Its existing redaction (the vault `Redactor`, `pulls.SECRETS`) is per session or per message and knows only the
secrets it is given; the security reviews found that trimming before redacting leaks prefixes.

## Decision

- Two owner-only switches, both off by default: usage statistics (off/on) and bug reports (off/ask each time/send
  automatically). Asked once after first setup and once on upgrade for existing installs; changed in Settings.
  Consent is bound to a schema version; a widened schema switches that part off and asks again. Consent, sends,
  reviews and deletions are audited. Organization policy can force either off, never on.
- Usage statistics are one daily summary whose every value is an enum member, boolean, bucket or day, validated
  against a versioned JSON Schema with no extra properties, on the client and at the collector. A local viewer shows
  the exact next payload before and after consent.
- Bug reports come only from ACC's own failures (server errors, supervisor stages, updates and installs, broker or
  node crashes, and abnormal agent exits described by metadata only). Free text comes only from exception messages
  and ACC's own log and helper tails, and is scrubbed in order: control characters, installation-wide known secrets,
  known identifiers from ACC's database, secret and personal-data shapes, then trimming, then scrubbing again. Size
  is capped at 16 KiB. Owners review each report before it is sent unless they chose automatic sending; sent
  reports are kept locally for 90 days.
- Data goes only from the main ACC, over verified HTTPS without redirects, to a small collector whose MIT source lives
  in this repository and which the maintainer hosts at the planned project domain. The collector address is
  root-owned configuration; empty removes the feature. Each installation signs with an Ed25519 key it generates and
  rotates every 90 days (trust on first use), which supports rate limits and self-service deletion without accounts.
  Connection addresses are not stored. Retention: raw usage 90 days, unlinked monthly aggregates 13 months, reports
  180 days.
- No third-party analytics or error SDK. GitHub issues remain manual: ACC can open a prefilled issue form with an
  already-scrubbed report for a person to submit under their own account.

## Consequences

- The maintainer runs and secures a small public service and becomes a data controller: a privacy notice, contact,
  hosting agreement and deletion handling are required before sending is enabled.
- Bug reports are lower fidelity than a commercial error tracker (no local variables, no dependency frames, no
  agent output), in exchange for a structure that keeps most user content out by construction.
- Scrubbing still cannot guarantee that free text is clean; the ask-each-time default, local sent log and deletion are
  the backstops, and the design says so to users.
- Signatures authenticate continuity, not genuineness: the collector must treat all data as untrusted statistics and
  untrusted text.
- About 8–12 engineer-weeks across four phases; the local-only phase can ship first and sends nothing.

## Alternatives considered

- **A third-party analytics or error service** (Sentry, PostHog and similar): least code, but adds a processor that
  open-source users didn't choose, uses a public client key anyone can send with, and SDK defaults capture request
  data, breadcrumbs, local variables and addresses that would each need to be switched off and kept off.
- **Automatic GitHub issues:** public and permanent, and needs either a token per installation or a shared token in
  public source.
- **One opt-in switch for both:** simpler, but bug reports carry far more risk than enumerated counters; separate
  consent is clearer and is what GDPR-style "specific" consent expects.
- **Exact counts and event logs:** more useful for analysis, but each exact number and timestamp adds to
  fingerprinting; buckets and daily counters answer "what is used" without it.
- **A stable installation id:** easier trend analysis, but links an installation's whole history; 90-day rotation
  limits that while keeping deletion possible.
