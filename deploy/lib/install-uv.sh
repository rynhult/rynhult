#!/bin/sh
# Install a pinned uv into /usr/local/bin for root to run: the release tarball from GitHub, checked against the SHA-256
# written here before anything is unpacked. It replaces piping uv's install script into a root shell.
#   deploy/lib/install-uv.sh [--dry-run]
# To move to a newer uv: change UV_VERSION and both sums (from the release's uv-<target>.tar.gz.sha256 files, checked
# against the downloaded tarballs).
set -eu
UV_VERSION=0.13.1
SUM_X86_64=eadd9d27c134ab5ac1ffd4473b97a741407f3ea238288f03121fc4deddf90220
SUM_AARCH64=9041c73c99fdc5892cf128140beb354eca9231d502068cc36e6cb53a8e59ad14
DEST=${UV_DEST:-/usr/local/bin}

case $(uname -m) in
  x86_64|amd64) target=x86_64-unknown-linux-gnu; sum=$SUM_X86_64 ;;
  aarch64|arm64) target=aarch64-unknown-linux-gnu; sum=$SUM_AARCH64 ;;
  *) echo "install-uv: no pinned uv for $(uname -m)" >&2; exit 1 ;;
esac
url="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$target.tar.gz"
if [ "${1:-}" = --dry-run ]; then
  echo "+ download $url, check sha256 $sum, install uv and uvx $UV_VERSION to $DEST (root:root 0755)"
  exit 0
fi
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL --proto =https --max-filesize 104857600 "$url" -o "$work/uv.tar.gz"
echo "$sum  $work/uv.tar.gz" | sha256sum -c --status - || { echo "install-uv: checksum mismatch for $url: not installing" >&2; exit 1; }
tar -xzf "$work/uv.tar.gz" -C "$work" --no-same-owner "uv-$target/uv" "uv-$target/uvx"
install -o root -g root -m 0755 "$work/uv-$target/uv" "$work/uv-$target/uvx" "$DEST/"
echo "install-uv: uv $UV_VERSION in $DEST"
