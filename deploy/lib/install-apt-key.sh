#!/bin/sh
# Verify in private staging first: rejected keys must not replace live keyrings.
set -eu
url=${1:?key URL}
destination=${2:?destination keyring}
fingerprint=${3:?pinned primary fingerprint}
lib=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
gnupg=''
cleanup() {
  [ -z "$gnupg" ] || rm -rf -- "$gnupg"
  rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
# Independent of TMPDIR: GnuPG may consult an agent socket even for public keys.
gnupg=$(mktemp -d /tmp/acc-gpg.XXXXXXXXXX)
curl -fsSL --proto =https "$url" -o "$work/key.asc"
sh "$lib/verify-apt-key.sh" "$work/key.asc" "$fingerprint"
case $destination in
  *.gpg)
    gpg --no-options --homedir "$gnupg" --batch --no-tty --no-autostart --dearmor --yes -o "$work/key.gpg" "$work/key.asc"
    install -m 0644 "$work/key.gpg" "$destination" ;;
  *.asc) install -m 0644 "$work/key.asc" "$destination" ;;
  *) echo "expected .asc or .gpg keyring" >&2; exit 1 ;;
esac
