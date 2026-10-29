#!/usr/bin/env bash
# npm and all dependency code run as a dedicated, non-login build account.
set -euo pipefail
repo=${1:?repository}
mode=${2:?web or mcp}
account=acc-build
id "$account" >/dev/null 2>&1 || useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$account"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if [ "$mode" = web ]; then
  mkdir -p "$work/web" "$work/src/acc/server"
  tar -C "$repo/web" --exclude=./node_modules -cf - . | tar -C "$work/web" -xf -
elif [ "$mode" = mcp ]; then
  mkdir "$work/mcp"
  cp "$repo/deploy/mcp/package.json" "$repo/deploy/mcp/package-lock.json" "$work/mcp/"
else
  echo "expected web or mcp" >&2; exit 1
fi
chown -R "$account:$account" "$work"
# No root environment/secrets are inherited by dependency scripts.
# shellcheck disable=SC2016 # expanded by the unprivileged shell
runuser -u "$account" -- env -i HOME="$work" PATH=/usr/bin:/bin /bin/sh -eu -c '
  cd "$1/$2"
  npm ci --ignore-scripts --no-audit --no-fund --silent
  if [ "$2" = web ]; then npm run build --silent; fi
' sh "$work" "$mode"
if [ "$mode" = web ]; then
  /usr/bin/python3 -I "$repo/deploy/lib/copy-web.py" "$work/src/acc/server/static" "$repo/src/acc/server/static" --keep-previous-assets
else
  /usr/bin/python3 -I "$repo/deploy/lib/copy-web.py" "$work/mcp" /srv/acc/tools/mcp --internal-links
fi
