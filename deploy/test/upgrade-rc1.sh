#!/usr/bin/env bash
# DESTRUCTIVE qualification on a fresh disposable systemd VM only, never a live host.
# Inputs: signed source archives, with SHA256SUMS + SHA256SUMS.asc beside each.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 022
fail() { printf '%s\n' "$*" >&2; exit 1; }
STRICT=''
if [ "${1:-}" = --strict-umask ]; then STRICT=1; shift; fi
[ "$#" = 2 ] || fail 'usage: upgrade-rc1.sh [--strict-umask] <rc1-bundle> <new-bundle>'
# Default: rc.1 -> candidate -> rollback. Strict mode: fresh candidate under 077;
# unpatched rc.1 cannot be the strict-mode starting install.
[ "$(id -u)" = 0 ] || fail 'Run only as root on a fresh disposable systemd VM.'
[ -d /run/systemd/system ] || fail 'A running systemd VM is required.'
if [ -e /srv/acc ] || [ -L /srv/acc ] || [ -e /etc/default/accd ] || [ -L /etc/default/accd ]; then
  fail 'Refusing an existing installation.'
fi
KIT=$(cd "$(dirname "$0")/../.." && pwd)
OLD=$(realpath "$1")
NEW=$(realpath "$2")
SCRATCH=$(mktemp -d /var/tmp/acc-upgrade-qualification.XXXXXXXX)
# Logs may contain configuration; keep their modes explicit as well as the
# directory private, independently of the code-tree umask.
for log in packages install upgrade rollback; do
  install -m 0600 /dev/null "$SCRATCH/$log.log"
done
printf 'Private qualification files: %s\n' "$SCRATCH"
command -v apt-get >/dev/null || fail 'Qualification requires an Ubuntu/Debian VM.'
apt-get update -qq > "$SCRATCH/packages.log" 2>&1
apt-get install -y python3 gnupg >> "$SCRATCH/packages.log" 2>&1
/usr/bin/python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 12))' || fail 'Python 3.12+ is required.'
# Keep all evidence on failure; no cleanup removes an installation or its backups.
qualification_exit() {
  local result=$?
  if [ "$result" != 0 ]; then
    echo 'Qualification failed; keep the private scratch directory for diagnosis.' >&2
  fi
}
trap qualification_exit EXIT
extract_verified() {
  /usr/bin/python3 -I - "$KIT" "$1" "$2" <<'PY'
import importlib.util
import pathlib
import sys
kit, archive, destination = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('acc', kit / 'src/acc/__init__.py',
                                            submodule_search_locations=[str(kit / 'src/acc')])
module = importlib.util.module_from_spec(spec)
sys.modules['acc'] = module
spec.loader.exec_module(module)
from acc import bundle
from acc.offline_upgrade import bounded
from acc.release_signatures import verify
from acc.updates import release_checksum, version
name = archive.name
assert name.startswith('acc-') and name.endswith('.tar.gz'), 'source archive name required'
target = name[4:-7]
version(target)
sums = bounded(archive.parent / 'SHA256SUMS', 1 << 20)
verify(sums, bounded(archive.parent / 'SHA256SUMS.asc', 128 << 10))
print(bundle.unpack(archive, destination, target, release_checksum(sums, target)))
PY
}
# Only public verified source is service-readable. Evidence and key copies stay
# beneath mktemp's mode-0700 SCRATCH; never chmod that directory to traverse it.
SOURCES=$(mktemp -d /var/tmp/acc-upgrade-source.XXXXXXXX)
chmod 0755 "$SOURCES"
install -d -m 0755 "$SOURCES/old" "$SOURCES/new"
OLD_TREE=$(extract_verified "$OLD" "$SOURCES/old")
NEW_TREE=$(extract_verified "$NEW" "$SOURCES/new")
old_version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$OLD_TREE/pyproject.toml")
new_version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$NEW_TREE/pyproject.toml")
[ "$old_version" = 0.15.0-rc.1 ] || fail 'First input must be rc.1.'
[ "$new_version" != "$old_version" ] || fail 'The new bundle must change the version.'
INSTALL_TREE=$OLD_TREE INSTALL_VERSION=$old_version INSTALL_UMASK=022
if [ -n "$STRICT" ]; then
  INSTALL_TREE=$NEW_TREE INSTALL_VERSION=$new_version INSTALL_UMASK=077
fi
OWNER=upgrade-owner
! id "$OWNER" >/dev/null 2>&1 || fail 'Qualification owner already exists; use a fresh VM.'
useradd --create-home --shell /bin/bash "$OWNER"
(umask "$INSTALL_UMASK"
ACC_OWNER=$OWNER ACC_HOSTNAME=upgrade.example.lan ACC_ZONE='' ACC_INTERNAL_IP=127.0.0.1 \
  ACC_DOCKER=no ACC_OLLAMA=no ACC_BACKUP=no ACC_AGENT_CLIS=claude \
  bash "$INSTALL_TREE/deploy/setup.sh" --yes) > "$SCRATCH/install.log" 2>&1
check_health() {
  /usr/bin/python3 -I - "$1" <<'PY'
import http.client
import json
import pathlib
import socket
import sys
import time
for attempt in range(120):
    connection = http.client.HTTPConnection('localhost', timeout=3)
    try:
        connection.sock = socket.socket(socket.AF_UNIX)
        connection.sock.settimeout(3)
        connection.sock.connect('/run/accd.sock')
        connection.request('GET', '/api/health')
        response = connection.getresponse()
        body = json.loads(response.read(4096))
        if (response.status == 200 and body['version'] == sys.argv[1]
                and body['build'] == pathlib.Path('/srv/acc/app/.acc-build').read_text().strip()):
            break
    except (OSError, ValueError):
        pass
    finally:
        connection.close()
    time.sleep(1)
else:
    raise SystemExit('Version/build health check failed')
PY
  for unit in accd.service accd.socket acc-update.path caddy.service; do
    systemctl is-active --quiet "$unit" || fail "Not active: $unit"
  done
}
check_health "$INSTALL_VERSION"
runuser -u acc -- test -r /srv/acc/app/deploy/lib/vault-key || fail 'Application code is unreadable by acc'
[ "$(stat -c %a /srv/acc/data/secrets/vault.key)" = 600 ] || fail 'Vault key must remain 0600'
[ "$(stat -c %a /etc/default/accd)" = 600 ] || fail 'Service configuration must remain 0600'
[ "$(stat -c %a "$SCRATCH")" = 700 ] || fail 'Evidence directory must remain private'
if [ -n "$STRICT" ]; then
  printf 'PASS: fresh candidate under umask 077, healthy services, readable code, private secrets. Logs: %s\n' "$SCRATCH"
  exit 0
fi
/usr/bin/python3 -I - <<'PY'
import json
import pathlib
import sqlite3
with sqlite3.connect('/srv/acc/data/acc.db') as db:
    db.execute("INSERT INTO setting (key,value,updated_at) VALUES ('rc-upgrade-marker', ?, '2026-10-30')",
               (json.dumps({'kept': True}),))
pathlib.Path('/srv/acc/data/secrets/vault.key').read_bytes()  # must exist before upgrade
PY
cp /srv/acc/data/secrets/vault.key "$SCRATCH/vault-before"
/usr/bin/python3 -I "$NEW_TREE/deploy/updates/acc-update" --offline \
  --bundle "$NEW" --version "$new_version" --yes > "$SCRATCH/upgrade.log" 2>&1
check_health "$new_version"
check_state() {
  /usr/bin/python3 -I - <<'PY'
import json
import sqlite3
with sqlite3.connect('/srv/acc/data/acc.db') as db:
    assert db.execute("select value from setting where key='rc-upgrade-marker'").fetchall() == [(json.dumps({'kept': True}),)]
PY
  cmp -s "$SCRATCH/vault-before" /srv/acc/data/secrets/vault.key || fail 'Vault key changed'
}
check_state
rollback_id=$(/usr/bin/python3 -I -c 'import json; print(json.load(open("/srv/acc/updates/state.json"))["id"])')
/usr/bin/python3 -I "$NEW_TREE/deploy/updates/acc-update" --offline \
  --rollback "$rollback_id" --yes > "$SCRATCH/rollback.log" 2>&1
check_health "$old_version"
check_state
printf 'PASS: rc.1 installation, signed upgrade, preserved state, healthy services, rollback. Logs remain private.\n'
