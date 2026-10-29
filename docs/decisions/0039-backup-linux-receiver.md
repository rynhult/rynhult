# 0039: enrolled Linux append-only backup receiver

Renumbered on 2026-10-24 from 0010 to 0039.

Date: 2026-10-23. Implements the Linux-first owner decision for backup phase two.

## Context

The main holds backup encryption/signing keys and can be compromised. A target must retain old ciphertext
even when an attacker holds those main-side keys. The owner alone performs target root setup. Enrollment
must not turn every node into a backup receiver or grant ACC a target root shell.

## Decision

Use pinned/checksummed official rest-server 0.14.0 on Linux amd64/arm64. A dedicated non-login account,
private disk tree, root-owned systemd configuration, append-only/private-repository flags and a separate
random transport token enforce the writer boundary. A typed channel over the existing enrolled TLS link
connects only to the receiver authorized by a root-owned installation/target binding. Never open a broker
channel for maintenance or expose arbitrary sockets, filesystem paths or SSH credentials.

The normal node root updater executes main-supplied bundles. Setup must close that existing bypass:
stop root-helper triggers, wait for active workers, and add root-owned conditions blocking update and
package services/triggers while the receiver binding exists. Node requests and the root update helper
also refuse. Keep broker accounts unprivileged and perform target software updates only under independent
owner review, from independently trusted source.

The owner runs commands rendered by Target setup; ACC only checks via the unprivileged machine link.
Verification initializes/binds the repository, checks encrypted data and proves overwrite/delete refusal
using a disposable valid encrypted snapshot. Main-side capture/readback and signed-manifest validation
remain unchanged. A kit exported before repository binding cannot enable schedules.

Remote retention stays outside ACC under independent administrator custody. Offline restore uses an
administrator-exported ciphertext repository and `--repository`, independent of the old main's link.
Append-only evidence does not set the global protected flag or claim a timed immutable window.

## Consequences

No SSH deployment key, root automation or public receiver listener. Old/unconfigured nodes fail closed.
The existing link's bounds and disconnect/revocation behavior apply; bulk transfers share its budgets.
One receiver per node is supported initially. Fixed target paths/port and a 100 GiB per-repository ceiling
make the first setup reviewable; the ceiling is not a filesystem quota. Disk exhaustion, target root,
receiver-account compromise, poisoned new captures and off-main freshness records remain operator concerns.
Linux systemd setup is owner-operated and not live-qualified in this implementation. S3 provider policy,
Object Lock and separate maintenance identity qualification follow Linux review.

## Alternatives considered

SFTP alone permits deletion through the main's credentials and fails the history-preservation requirement.
SSH forwarding with restricted keys can work but adds a second enrollment/host-key/credential lifecycle.
A custom ciphertext format or REST server expands the cryptographic/storage review surface unnecessarily.
S3 Object Lock needs provider-specific conformance and is a subsequent backend, not evidence for Linux.
