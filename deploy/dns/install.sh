#!/usr/bin/env bash
# Explicit operator deployment, after reviewing the checkout. Does not start/reload Caddy.
set -euo pipefail
export PATH=/usr/bin:/bin
[ "$(id -u)" = 0 ] && [ "$#" = 1 ] || { echo 'usage: sudo deploy/dns/install.sh example.com' >&2; exit 2; }
ZONE=$1
[[ "$ZONE" =~ ^([a-z0-9][a-z0-9-]*\.)+[a-z]{2,63}$ ]] || { echo 'invalid zone' >&2; exit 2; }
REPO=$(cd -- "$(dirname -- "$0")/../.." && pwd)
/usr/bin/python3 -I "$REPO/deploy/lib/unit-aliases.py" --check --unit-dir /etc/systemd/system
install -d -o root -g root -m 0755 /usr/local/lib/acc-dns
install -d -o root -g root -m 0700 /etc/acc-dns /etc/caddy/acc-dns /var/cache/acc-dns
install -d -o root -g caddy -m 2750 /etc/caddy/acc-dns-google
install -d -o root -g acc -m 0750 /run/acc-dns
[ -e /run/acc-dns/caddy-config.lock ] || install -o root -g root -m 0600 /dev/null /run/acc-dns/caddy-config.lock
install -o root -g root -m 0600 "$REPO/src/acc/dns_credentials.py" /usr/local/lib/acc-dns/credentials.py
install -o root -g root -m 0600 "$REPO/src/acc/dns_helper.py" /usr/local/lib/acc-dns/helper.py
install -o root -g root -m 0700 "$REPO/deploy/dns/install-release" /usr/local/lib/acc-dns/install-release
# A pending manifest is safe to deploy, but cannot install or execute a candidate binary.
if [ ! -e /etc/acc-dns/release.json ]; then
  install -o root -g root -m 0600 "$REPO/deploy/dns/release.json" /etc/acc-dns/release.json
fi
# Do not silently expand a pre-existing root policy.
if [ ! -e /etc/acc-dns/policy.json ]; then
  printf '{"zones":["%s"]}\n' "$ZONE" > /etc/acc-dns/policy.json
  chmod 0600 /etc/acc-dns/policy.json
fi
install -m 0644 "$REPO/deploy/dns/acc-dns.socket" /etc/systemd/system/acc-dns.socket
install -m 0644 "$REPO/deploy/dns/acc-dns.service" /etc/systemd/system/acc-dns.service
install -m 0644 "$REPO/deploy/dns/acc-dns.tmpfiles" /etc/tmpfiles.d/acc-dns.conf
install -m 0644 "$REPO/deploy/dns/acc-dns-purge.service" /etc/systemd/system/acc-dns-purge.service
install -m 0644 "$REPO/deploy/dns/acc-dns-purge.timer" /etc/systemd/system/acc-dns-purge.timer
/usr/bin/python3 -I "$REPO/deploy/lib/unit-aliases.py" --unit-dir /etc/systemd/system
# Enabling/starting the helper is an operator's separate activation step after reviewing policy.
echo 'Installed inactive helper. Review root policy and qualified release; activate acc-dns.socket explicitly.'
