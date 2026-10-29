#!/bin/sh
# Pinned GitHub MCP release; sums from the immutable v2.0.2 release's checksums.
# https://github.com/github/github-mcp-server/releases/tag/v2.0.2
set -eu
VERSION=2.0.2
case $(uname -m) in
  x86_64|amd64) arch=x86_64; sum=815e27d931c796af0c97f24fd211e14f8a4607d4528377719a5ad2352086b0b0 ;;
  aarch64|arm64) arch=arm64; sum=73f5e76c6a3b5e0d731e1134a7c38575df6ec96c646192095a3be2d4d0d6c1fc ;;
  *) echo "install-github-mcp: unsupported architecture" >&2; exit 1 ;;
esac
url="https://github.com/github/github-mcp-server/releases/download/v$VERSION/github-mcp-server_Linux_$arch.tar.gz"
if [ "${1:-}" = --dry-run ]; then
  echo "+ download $url, check sha256 $sum, install /srv/acc/tools/github-mcp-server (root:root 0755)"
  exit 0
fi
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL --proto =https --max-filesize 104857600 "$url" -o "$work/release.tar.gz"
echo "$sum  $work/release.tar.gz" | sha256sum -c --status - || { echo "install-github-mcp: checksum mismatch" >&2; exit 1; }
tar -xzf "$work/release.tar.gz" -C "$work" --no-same-owner github-mcp-server
[ -f "$work/github-mcp-server" ] && [ ! -L "$work/github-mcp-server" ] || exit 1
install -d -o root -g root -m 0755 /srv/acc/tools
install -o root -g root -m 0755 "$work/github-mcp-server" /srv/acc/tools/github-mcp-server
[ -x /srv/acc/tools/github-mcp-server ] || exit 1
