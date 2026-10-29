#!/bin/sh
# Verify in private staging first: rejected keys must not replace live keyrings.
set -eu
url=${1:?key URL}
destination=${2:?destination keyring}
fingerprint=${3:?pinned primary fingerprint}
lib=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL --proto =https "$url" -o "$work/key.asc"
sh "$lib/verify-apt-key.sh" "$work/key.asc" "$fingerprint"
case $destination in
  *.gpg)
    gpg --dearmor --yes -o "$work/key.gpg" "$work/key.asc"
    install -m 0644 "$work/key.gpg" "$destination" ;;
  *.asc) install -m 0644 "$work/key.asc" "$destination" ;;
  *) echo "expected .asc or .gpg keyring" >&2; exit 1 ;;
esac
