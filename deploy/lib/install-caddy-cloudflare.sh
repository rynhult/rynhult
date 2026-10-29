#!/bin/sh
acc_compat_lib="$(dirname "$0")/acc-env.sh"
[ -f "$acc_compat_lib" ] || acc_compat_lib="$(dirname "$0")/acc-env.sh"
# shellcheck source=/dev/null
. "$acc_compat_lib"
# The build service is dynamic: require an independently approved hash before
# downloading. Never derive the expected checksum from the downloaded binary.
set -eu
arch=${1:?amd64 or arm64}
sum=${ACC_CADDY_SHA256:-}
case $arch in amd64|arm64) ;; *) echo "unsupported Caddy architecture" >&2; exit 1 ;; esac
if [ "${#sum}" != 64 ] || printf '%s' "$sum" | LC_ALL=C grep -q '[^0-9a-f]'; then
  echo "Caddy checksum required: set ACC_CADDY_SHA256 to an independently approved SHA-256 (deploy/caddy/README.md)" >&2
  exit 1
fi
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL --proto =https --retry 2 --max-time 900 --max-filesize 104857600 -o "$work/caddy" \
  "https://caddyserver.com/api/download?os=linux&arch=$arch&p=github.com%2Fcaddy-dns%2Fcloudflare"
echo "$sum  $work/caddy" | sha256sum -c --status - || { echo "Caddy checksum mismatch: not installing" >&2; exit 1; }
install -o root -g root -m 0755 "$work/caddy" /usr/local/bin/caddy
