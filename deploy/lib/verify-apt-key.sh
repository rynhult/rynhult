#!/bin/sh
# Check the primary fingerprint before enabling a signed apt source.
set -eu
key=${1:?key file}
expected=${2:?pinned primary fingerprint}
fp=$(gpg --show-keys --with-colons "$key" 2>/dev/null | awk -F: '/^pub:/ {primary=1; next} primary && /^fpr:/ {print $10; primary=0}')
[ "$fp" = "$expected" ] || { echo "apt signing key fingerprint mismatch (got ${fp:-none})" >&2; exit 1; }
