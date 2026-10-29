# Rynhult VERSION (release notes draft)

> Draft: no tag, publication or signing is implied by these notes.

## Changes and known limits

Describe user-visible changes, supported platforms, upgrade/recovery steps and
remaining limits for this version before publication.

## Verify the release before installing

Download the source archive, `BUILD`, `SHA256SUMS` and `SHA256SUMS.asc` from the
published release. Only `SHA256SUMS` is signed; its verified hashes cover listed
files, not Git clones or release tags. The in-app updater does not verify
signatures yet.

Download the public key from https://rynhult.dev/keys/rynhult-release-key.asc
and inspect it:

```sh
curl --fail --show-error --location --output rynhult-release-key.asc \
  https://rynhult.dev/keys/rynhult-release-key.asc
gpg --show-keys --with-subkey-fingerprint rynhult-release-key.asc
```

Compare both full fingerprints with an independently trusted copy before
importing:

- Primary: `0C2C E126 57BD 833F B5B5 63B2 B555 F6E1 2799 0DB5`.
- Signing subkey: `B072 4912 051F F1F2 74CD EE7F EDE4 F41B 077B D2A7`.

Stop if either differs. With all release files in the same folder:

```sh
gpg --import rynhult-release-key.asc
gpg --verify SHA256SUMS.asc SHA256SUMS && sha256sum -c SHA256SUMS
```

On macOS use `shasum -a 256 -c SHA256SUMS` for the checksum step. Do not install
if the signature is missing, signature verification fails, or any checksum fails.
See [Security](../../SECURITY.md#verify-a-source-release) for the trust boundary.
Signing/upload is an [owner-run step](../PUBLISHING-THE-SOURCE.md#owners-offline-signing-and-rc1-attachment),
not performed by generating these notes or building checksums in CI.
