#!/usr/bin/env bash
# Owner-run only: the release private key stays in the owner's offline keyring.
set -eu
umask 077

die() { printf 'sign-release: %s\n' "$*" >&2; exit 1; }
[ "$#" -eq 1 ] || die "usage: scripts/sign-release.sh RELEASE_FOLDER_OR_SHA256SUMS"
command -v gpg >/dev/null 2>&1 || die "GnuPG (gpg) is required."

PRIMARY=0C2CE12657BD833FB5B563B2B555F6E127990DB5
SIGNING=B0724912051FF1F274CDEE7FEDE4F41B077BD2A7
ROOT=$(CDPATH='' cd "$(dirname "$0")/.." && pwd)
PUBLIC_KEY="$ROOT/keys/rynhult-release-key.asc"
GNUPGHOME=${GNUPGHOME:-"$HOME/rynhult-signing"}
export GNUPGHOME
[ -d "$GNUPGHOME" ] || die "Signing keyring not found; set GNUPGHOME to the owner's offline keyring."

manifest=$1
if [ -d "$manifest" ]; then manifest="$manifest/SHA256SUMS"; fi
[ "$(basename "$manifest")" = SHA256SUMS ] || die "Only a file named SHA256SUMS may be signed."
if [ ! -f "$manifest" ] || [ ! -r "$manifest" ]; then die "SHA256SUMS is not a readable file."; fi
folder=$(CDPATH='' cd "$(dirname "$manifest")" && pwd)
manifest="$folder/SHA256SUMS"
signature="$manifest.asc"
if [ -e "$signature" ] || [ -L "$signature" ]; then die "SHA256SUMS.asc already exists; review it before removing it."; fi

work=$(mktemp -d "$folder/.rynhult-sign-release.XXXXXX")
verify=''
cleanup() {
    rm -rf "$work"
    if [ -n "$verify" ]; then rm -rf "$verify"; fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
# Without /run/user, GPG derives an agent socket from this home even for public imports.
verify=$(mktemp -d /tmp/rynhult-verify.XXXXXX)
gpg_failure() {
    if [ -s "$work/gpg.stderr" ]; then
        head -c 4096 "$work/gpg.stderr" >&2
        printf '\n' >&2
    fi
    die "$*"
}

# An isolated public-only keyring prevents ambient trust/configuration from choosing the verifier.
gpg --no-options --homedir "$verify" --no-autostart --batch \
    --import "$PUBLIC_KEY" >/dev/null 2>"$work/gpg.stderr" \
    || gpg_failure "Cannot import the repository's public release key."
public=$(gpg --no-options --homedir "$verify" --no-autostart --batch \
    --with-colons --with-subkey-fingerprint --list-keys 2>"$work/gpg.stderr") \
    || gpg_failure "Cannot inspect the repository's public release key."
primary_fingerprint() {
    awk -F: '$1 == "pub" || $1 == "sec" { primary = 1; next }
        primary && $1 == "fpr" { print $10; primary = 0 }'
}
signing_fingerprint() {
    awk -F: '$1 == "sub" || $1 == "ssb" { subkey = 1; next }
        subkey && $1 == "fpr" { print $10; subkey = 0 }'
}
[ "$(printf '%s\n' "$public" | primary_fingerprint)" = "$PRIMARY" ] \
    || die "Repository primary key fingerprint does not match the approved release key."
printf '%s\n' "$public" | signing_fingerprint | grep -Fx "$SIGNING" >/dev/null \
    || die "Repository signing subkey fingerprint does not match the approved release key."

secret=$(gpg --no-options --batch --with-colons --with-subkey-fingerprint \
    --list-secret-keys "$PRIMARY" 2>"$work/gpg.stderr") \
    || gpg_failure "Signing key fingerprint does not match: the approved private key is unavailable."
[ "$(printf '%s\n' "$secret" | primary_fingerprint)" = "$PRIMARY" ] \
    || die "Private primary key fingerprint does not match the approved release key."
printf '%s\n' "$secret" | signing_fingerprint | grep -Fx "$SIGNING" >/dev/null \
    || die "Private signing subkey fingerprint does not match the approved release key."

gpg --no-options --batch --local-user "$SIGNING!" --armor \
    --output "$work/SHA256SUMS.asc" --detach-sign "$manifest" 2>"$work/gpg.stderr" \
    || gpg_failure "Cannot sign SHA256SUMS with the approved signing subkey."
verified=$(gpg --no-options --homedir "$verify" --no-autostart --batch \
    --status-fd 1 --verify "$work/SHA256SUMS.asc" "$manifest" 2>"$work/gpg.stderr") \
    || gpg_failure "The signature could not be verified against the repository's public key."
[ "$(printf '%s\n' "$verified" | awk '$1 == "[GNUPG:]" && $2 == "VALIDSIG" { print $3 }')" = "$SIGNING" ] \
    || die "Signature fingerprint does not match the approved signing subkey."
# Same-filesystem publication without overwriting an existing signature.
ln "$work/SHA256SUMS.asc" "$signature"
printf 'Verified signature written to %s\n' "$signature"
printf '%s\n' 'After transferring it to the release folder, upload manually (replace <tag>):'
printf '%s\n' '  gh release upload <tag> SHA256SUMS.asc --repo rynhult/rynhult'
printf '%s\n' 'For rynhult.dev downloads, keep SHA256SUMS.asc beside the exact SHA256SUMS and release assets.'
