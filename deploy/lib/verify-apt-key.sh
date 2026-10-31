#!/bin/sh
# Check the primary fingerprint before enabling a signed apt source.
set -eu
key=${1:?key file}
expected=${2:?pinned primary fingerprint}
# Keep public-key inspection private and below Unix socket path limits.
gnupg=$(mktemp -d /tmp/acc-gpg.XXXXXXXXXX)
trap 'rm -rf -- "$gnupg"' EXIT
trap 'exit 1' HUP INT TERM
fp=$(gpg --no-options --homedir "$gnupg" --batch --no-tty --no-autostart --show-keys --with-colons "$key" 2>/dev/null | awk -F: '/^pub:/ {primary=1; next} primary && /^fpr:/ {print $10; primary=0}')
[ "$fp" = "$expected" ] || { echo "apt signing key fingerprint mismatch (got ${fp:-none})" >&2; exit 1; }
