# Planned device-authority rollover with two trusted authorities

Date: 2026-10-25. Status: accepted implementation choice within the approved remote-access design
([REMOTE-ACCESS.md](../REMOTE-ACCESS.md), decisions 0020 (private decision record) and
[0051](0051-machine-identities-and-edge-enforcement.md)). Everything stays off by default; nothing is installed.

## Context

The device CA is a single online root with a ten-year life. The runbook's only way to replace it was a public
maintenance window: turn public access off, provision a new authority, re-pair every browser and re-issue every
machine. That is right after a compromise, but too disruptive for a scheduled key change, so phase 3 listed
"dual-CA rollover" as outstanding.

## Decision

- **Three root-only owner steps** in the recovery tool, never in the signer's stdin contract:
  - `rollover stage` creates the next authority beside the current one, with a distinct subject so chain building
    stays unambiguous.
  - `rollover promote` makes it current and moves the old one to `previous/`. It refuses unless the edge helper
    already has the staged CA as `device-ca.pem` and the old one as `device-ca-previous.pem`, so no new identity can
    be issued before the edge trusts its authority. It resumes after a crash; a half-moved authority leaves no
    `ca.pem`, so the signer refuses to issue until promote is run again.
  - Promote then deletes the old key; the old certificate stays in `previous/` (amended after the security review).
  - `rollover finish` deletes the old certificate, and refuses while the edge still has `device-ca-previous.pem`.
- **The edge trusts at most two authorities, and only those that issued a listed leaf.** The leaf allow list remains
  the gate, so a second CA never admits an unlisted certificate. The old CA leaves the trust pool on the first
  apply after its last identity is renewed. With a single authority the rendered policy is unchanged.
- **The signer answers one more read-only operation, `authority`**, with its current public certificate. accd caches
  it for 60 seconds per process:
  - machines whose newest leaf predates the rollover request a successor at once over their link, through the
    existing overlap and switch protocol;
  - browser devices are flagged for renewal;
  - owners see a count of identities still on the previous authority.

  If the signer can't answer, nothing is flagged and nothing rotates.

## Consequences

- A scheduled authority change needs no public maintenance window. Machines move on their own, rate-limited by the
  signer's 20-per-hour budget and capped at 10 an hour by accd; people renew their devices on the LAN.
- Compromise response is unchanged: turn off, a fresh authority, no previous file, re-pair. A compromised
  authority must never be kept as "previous".
- The daily encrypted backup covers the current authority only. Back up again right after promote. The previous
  key is not backed up and is deleted at promote.
- Live Caddy, the helper installation and physical devices remain unqualified; the release stays NO-GO.

## Amendment after the security review (2026-10-25)

The owner's answers to `docs/security/2026-10-25-review-remote-access-phase3-rest.md`:
- **L1:** promote deletes the previous authority's key at once. There is no abort path, so keeping it only added
  risk. The signer's `authority` answer now also gives the previous authority's fingerprint and whether its key
  exists.
- **L2:** accd audits what the signer reports: `device_ca.authority_changed` (`from`, `to`, `planned`),
  `device_ca.previous_key_deleted` and `device_ca.previous_retired`. The last answer is kept in the database, so
  a restart doesn't repeat entries. The signer also appends each step to its root-only `rollover.log`.
- **L3:** accd asks at most 10 machines an hour to leave a previous authority (`machine_identity.rollover_request`,
  counted from the audit log, so restarts don't reset it). That leaves half of the signer's 20-per-hour budget
  for people. The signer issues a rollover successor only to a machine that was asked; an unsolicited CSR can't
  spend the share. Expiry is never held back. A connected machine that waits is asked again every 5 minutes.

## Alternatives considered

- **An offline root with rotating intermediates:** the better long-term shape, but it changes the signer's
  custody model and the qualified single-CA helper. It's deferred.
- **Trusting both CAs for the whole rollover**, regardless of the leaves: this widens the pool for no benefit,
  since the per-leaf pool is equally simple and shrinks automatically.
- **accd reading the CA file from its data directory:** a second copy that root must keep in sync. Asking the
  signer gives a single source of truth.
