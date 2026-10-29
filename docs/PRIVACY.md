# Optional usage statistics and bug reports

ACC sends neither unless an owner explicitly opts in under **Settings → Usage and bug reports**.
The two choices are independent and start off. Members cannot read or change them. No third-party
analytics or error SDK is included. Nodes do not send telemetry.

The default collector is `https://telemetry.rynhult.dev`, the project owner's own collector, hosted with
Akamai/Linode in Frankfurt (EU). It has been running as a **pre-release staging service since 2026-10-26**.
It serves its privacy notice (version 0.2; the repository holds 0.4, `deploy/telemetry/privacy.html`, which the
next deploy serves), which names the controller, the contact address `privacy@rynhult.dev`, consent as the legal
basis, and the rights to access, deletion, objection and complaint. During staging the collector has no backups
beyond its virtual machine's own disk, and notice 0.4 says so. Anything it receives before the version 1 launch is test data and is all deleted at
launch; the retention periods apply from version 1.

The client hasn't yet been qualified end to end against this collector. Both choices still start off, and an
installation sends nothing unless an owner turns one on. An installation owner can select an HTTPS collector of
their own; that operator controls its data. Root-owned `ACC_TELEMETRY_URL` can override the address,
and an empty value disables outbound collection.
Owner destination edits are audited; changes to the managed override, including its removal, are
audited as installation-configuration changes when the supervisor observes them.

Usage is a daily summary of enumerated feature/provider/OS values, bucketed inventory and feature
counts, a UTC day, version category, and a rotating pseudonymous installation id. It contains no
prompts, terminal output, transcripts, names, paths, repository identifiers, addresses or free text.
Read `schemas/telemetry/usage-1.json` for the exact contract. Release builds, release candidates included,
report their version (such as `1.0.0` or `0.15.0-rc.1`); development checkouts report `dev`, never a build
hash. Unknown fields and event/provider names are rejected.

Bug reports contain structured ACC error metadata, ACC-only frames, and a scrubbed exception message.
This implementation does not collect terminal output, transcripts, helper tails or log breadcrumbs.
Known secrets (including retained vault values and credential files), known inventory identifiers,
and recognizable token, email, address, hostname and path shapes are removed before trimming; the
trimmed result is scrubbed again. URL encodings are normalized, textual base64
values, authorization/cookie headers and recognizable credential assignments are removed, and paths with spaces or unusual home-directory names are
removed conservatively, possibly omitting the rest of the message. Short known credentials are
also removed. Multiline messages are omitted rather than guessing whether a line break split a secret
or replaced whitespace inside one; relative path text is also omitted rather than guessing its boundaries.
Structured error metadata is retained. Reports are limited to 16 KiB. Crashed sessions include metadata
only. **Scrubbing is best effort:** unknown names and secrets in other encodings can still be missed.
Every report must be shown to and approved by an owner before sending.
Unanswered reports expire after seven days; there is no automatic-report option in this implementation.

Preview is local. Once usage is enabled its next summary is frozen in the queue, and the preview shows
those exact UTF-8 JSON bytes. Approved reports use the same immutable bytes reviewed in Settings.
Saving privacy choices refreshes an open preview. The privacy-review upgrade discards unsent reports
created by the old scrubber, including approvals and retries; it never rewrites reviewed or sent bytes.
HTTPS certificates are verified, redirects are refused, and no cookies are sent. Deletion receipts
must be uncompressed, size-bounded string UUIDs. Signed requests also
carry the public key and a timestamp; the collector sees the connection address for in-memory rate
limits and never stores it with the data. Failed sends remain
quiet and local and retry with backoff for up to seven days. Test/CI processes refuse network sending.

Usage and reports have separate Ed25519 keys, rotated after 90 days and also separated by collector
address. Keys are kept locally only for deletion, for at most 13 months (a 397-day upper bound).
The collector retains raw usage for 90 days, reports for 180 days, and anonymous monthly totals for
13 calendar months. ACC keeps the payload and result of each transmission for 90 days after it is
sent. The reference collector has no public statistics endpoint. Later public figures require at
least ten installations per figure.

Turning a choice off stops new sends of that kind and discards its pending queue. The default checked
option also requests deletion of previously sent raw data. **Delete data already sent** works separately
from consent and sends only explicit deletion requests, including for old keys at previous collectors.
Receipts appear in the local log. An unavailable collector is retried for seven days; check the log for
the result. Anonymous monthly totals no longer carry an installation id and cannot be linked back.
Collector backups must expire within 30 days and preserve deletion on restore.
The reference collector refuses permissive or symlinked storage and unsupported SQLite journal modes.
It requires a private service-owned data directory and database, and uses DELETE journaling with
secure deletion. A transient maintenance database error is logged without details and retried in a
minute rather than permanently disabling idle retention. Disk snapshots, backups and filesystem
recovery remain the operator's responsibility; database deletion is not a physical-media erase.

See [the approved design](TELEMETRY.md) and [reference collector operations](../deploy/telemetry/README.md).
The security and privacy review is recorded in the repository's internal review artifacts, which are
not part of the public source export. It does not qualify a live deployment.
