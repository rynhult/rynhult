#!/bin/sh
# Node.js for the agent CLIs installed with npm (Codex, Copilot, Antigravity, Cursor's helpers): 24 from NodeSource's
# signed apt repository, unless 22 or newer is already here. Shared by setup.sh (main ACC) and deploy/node/install.sh
# (--install-prereqs). Debian/Ubuntu, as root.
#   deploy/lib/install-node.sh [--dry-run]
set -eu
LIB=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
if node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 22 ? 1 : 0)' 2>/dev/null; then
  echo "install-node: node $(node --version) is already here"
  exit 0
fi
if [ "${1:-}" = --dry-run ]; then
  echo "+ add NodeSource's signed apt repository (node_24.x) and apt-get install nodejs"
  exit 0
fi
command -v gpg >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gnupg >/dev/null; }
install -d -m 0755 /usr/share/keyrings
sh "$LIB/install-apt-key.sh" https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key   /usr/share/keyrings/nodesource.gpg 6F71F525282841EEDAF851B42F59B5F99B1BE0B4
echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" \
  > /etc/apt/sources.list.d/nodesource.list
printf 'Package: nodejs\nPin: origin deb.nodesource.com\nPin-Priority: 600\n' > /etc/apt/preferences.d/nodejs
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs >/dev/null
echo "install-node: node $(node --version)"
