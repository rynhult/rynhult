# Machine identities, edge identity header and device-CA backup

Date: 2026-10-24. Status: accepted implementation choice within the approved remote-access
design ([REMOTE-ACCESS.md](../REMOTE-ACCESS.md), decisions 0020 (private decision record)
and [0022](0022-remote-access-owner-apply-contract.md)). Everything stays off by default.

## Context

Phase 3 needs enrolled machines to use the public address without weakening pinned server trust or
the signed Ed25519 link, revocation that also ends open connections, and an encrypted production
backup of the device authority. The owner authorized the backup/restore tooling for this work.

## Decision

- **Machine profile from the same device CA.** The root signer gains one closed operation,
  `issue_machine`: a node-held P-256 CSR (proof of possession checked), subject
  `OU=acc-machine,CN=<node id>`, clientAuth only, at most 30 days. Requested names and extensions
  are ignored. Machine rows live in the device registry (`kind='node'`, `node_id`, migration 0051),
  never as a person's device.
- **Rotation over the signed link**, owner setting `machine_identities` (default off). Successor
  requested with 10 days left; the node switches only after the main reports that the verified
  applied policy (or public access off) admits it; the old leaf retires when the successor is
  presented, at most 24 hours after issuance. Node removal revokes in the same transaction.
- **Route split at the edge by subject** (`vars_regexp` on Caddy's client subject placeholder):
  machines reach only `/api/nodes/connect`, `/api/nodes/bundle` and `/api/git/*`; browsers never
  reach those; install, enroll, CA download and pairing stay denied for every public identity.
- **Edge identity header.** The public route sets `X-Acc-Edge-Client` to the verified leaf's
  SHA-256. accd treats its presence as a public request, which can only narrow access, checks the
  registry per request, binds a machine leaf to its node's link and projects, and closes registered
  streams on revocation (at once) or at the 15-second sweep (expiry, retirement, setting off).
- **Backup** with a separate root-only recovery tool: scrypt + AES-256-GCM sealing of key,
  certificate and ledger with a root passphrase file outside the backup set; restore only into a
  new directory after an independently recorded fingerprint matches. The root `acc-backup` adds
  `device-ca.enc` or reports it was skipped; no plain copy.

## Consequences

- No new HTTP routes; issuance rides the existing link. Private access is unchanged.
- A machine reconnects once per rotation. Git backups from brokers keep the private address: the
  broker accounts must not hold the machine key, and a scoped node-local forwarder is future work.
- The header is trusted from accd's Unix socket. Any local process able to reach that socket was
  already trusted with private access; forging the header can only reduce what it gets.
- Live Caddy, helper installation, physical machines and internet paths remain unqualified; the
  release stays NO-GO.

## Alternatives considered

- A separate machine CA: a second trust pool and helper policy per CA; rejected for now because the
  single-CA helper is the qualified shape and subjects already separate the profiles.
- Server-generated machine keys delivered over the link: puts private keys through accd; rejected.
- HTTP-header fingerprint matching at Caddy instead of the leaf verifier: rejected by the design;
  the header is used only after TLS verification for attribution and stream closure.
- Fallback from the new to the old certificate on TLS failure: hides policy problems; the node
  switches only on an explicit acknowledgement.
- Reusing `ACC_BACKUP_PASSPHRASE_FILE` / openssl CBC: no integrity and shared custody; rejected.
