#!/usr/bin/env bash
# Verify that no secret value appears in the system or the owner's user journal (run as root). Prints counts only.
#   sudo [OWNER=user] scripts/check-logs.sh [since]   (owner default: ACC_SERVICES_ACCOUNT in /etc/default/accd)
set -euo pipefail
since=${1:-"7 days ago"}
owner=${OWNER:-$(sed -n 's/^ACC_SERVICES_ACCOUNT=//p' /etc/default/accd 2>/dev/null | tail -1)}
[ -n "$owner" ] || { echo "set OWNER (no ACC_SERVICES_ACCOUNT in /etc/default/accd)" >&2; exit 1; }
declare -A secrets
for f in /srv/acc/data/secrets/*; do secrets["acc:$(basename "$f")"]=$(cat "$f"); done
[ -f /etc/caddy/cloudflare.env ] && secrets["caddy:CF_API_TOKEN"]=$(cut -d= -f2- /etc/caddy/cloudflare.env)
secrets["$owner:gh-token"]=$(sudo -u "$owner" -H gh auth token 2>/dev/null || true)
system=$(journalctl --since "$since" --no-pager -o cat 2>/dev/null)
user=$(journalctl --user -M "$owner@" --since "$since" --no-pager -o cat 2>/dev/null || true)
leaks=0
for name in "${!secrets[@]}"; do
  value=${secrets[$name]}
  [ ${#value} -ge 16 ] || continue
  hits=$(printf '%s\n%s\n' "$system" "$user" | grep -cF -- "$value" || true)
  if [ "$hits" -gt 0 ]; then echo "LEAK: $name appears $hits time(s)"; leaks=1; else echo "ok: $name"; fi
done
exit $leaks
