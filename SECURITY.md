# Reporting a vulnerability

Please report suspected vulnerabilities privately. Do not put credentials, exploit details,
private network information or session transcripts in public issues or pull requests.

**Private contact: [security@rynhult.dev](mailto:security@rynhult.dev).**
For the intended `rynhult/rynhult` repository, once created and configured for
GitHub private vulnerability reporting, you can also use its
**Security → Report a vulnerability** form.

Include the affected version or commit, impact, prerequisites and a minimal reproduction
using artificial accounts and documentation addresses. Remove tokens and personal data.
The maintainer will coordinate a fix and disclosure with the reporter; no response time
or supported-version guarantee has been established yet.

Before the first public release candidate, the owner must verify delivery to the private mailbox and exercise
the reporting route. This document does not attest that either route has been tested.

## Verify a source release

The public release key is [keys/rynhult-release-key.asc](keys/rynhult-release-key.asc),
published by the site build at
https://rynhult.dev/keys/rynhult-release-key.asc. Its identity is **Rynhult Releases**.

- Primary fingerprint: `0C2C E126 57BD 833F B5B5 63B2 B555 F6E1 2799 0DB5` (certification only; expires 2031-10-28).
- Signing subkey fingerprint: `B072 4912 051F F1F2 74CD EE7F EDE4 F41B 077B D2A7` (expires 2028-10-28).

For a published release, download its source archive, `BUILD`, `SHA256SUMS` and
`SHA256SUMS.asc` into the same folder. With GnuPG installed:

```sh
curl --fail --show-error --location --output rynhult-release-key.asc \
  https://rynhult.dev/keys/rynhult-release-key.asc
gpg --show-keys --with-subkey-fingerprint rynhult-release-key.asc
```

Compare **both full fingerprints** with an independently trusted copy of the
values above before importing. A downloaded key or matching short key ID alone
does not establish who signed it. If either differs, stop.

```sh
gpg --import rynhult-release-key.asc
gpg --verify SHA256SUMS.asc SHA256SUMS && sha256sum -c SHA256SUMS
```

Stop on a missing signature, failed verification or checksum mismatch; do not
install. On macOS, use `shasum -a 256 -c SHA256SUMS` for the checksum step.
**Only `SHA256SUMS` is signed.** Its verified hashes cover the listed files;
archives, `BUILD` and release tags have no separate signature from this process.
A valid signature does not establish that a release is current or safe.
**The in-app updater does not verify these signatures yet**; adding that
verification is follow-up work. CI creates checksums but holds no private release
key: rc.1 signing and signature upload are owner-run steps, not completed by
publishing this document.
