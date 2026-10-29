#!/bin/sh
# Run only on the target dedicated VM after reviewing docs/TELEMETRY-COLLECTOR.md.
# Installs files and dependencies, but never starts/enables services or edits Caddy.
set -eu
[ "$(id -u)" -eq 0 ] || { echo 'Run as root on the target VM' >&2; exit 1; }
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$script_dir/../.." && pwd)
/usr/bin/python3 -I "$repo_dir/deploy/lib/unit-aliases.py" --check --unit-dir /etc/systemd/system
getent passwd caddy >/dev/null
if ! getent passwd acc-telemetry >/dev/null; then
    useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin acc-telemetry
fi
[ "$(id -u acc-telemetry)" -ne 0 ] || { echo 'Collector account must not be root' >&2; exit 1; }
# Refuse upgrades while running: root-owned code must never change underneath a worker.
if systemctl is-active --quiet acc-telemetry.service; then
    echo 'Stop acc-telemetry.service before installing' >&2
    exit 1
fi
install -d -m 0755 /opt/acc-telemetry /opt/acc-telemetry/acc /opt/acc-telemetry/deploy/telemetry
# A standalone regular package cannot fall through to another installed ACC.
install -m 0644 /dev/null /opt/acc-telemetry/acc/__init__.py
install -m 0644 /dev/null /opt/acc-telemetry/deploy/__init__.py
install -m 0644 /dev/null /opt/acc-telemetry/deploy/telemetry/__init__.py
install -m 0644 "$repo_dir/src/acc/env.py" /opt/acc-telemetry/acc/
install -m 0644 "$repo_dir/src/acc/telemetry_collector.py" "$repo_dir/src/acc/telemetry_schema.py" \
    "$repo_dir/src/acc/telemetry_collector_ops.py" /opt/acc-telemetry/acc/
install -m 0644 "$script_dir/app.py" /opt/acc-telemetry/deploy/telemetry/
install -m 0644 "$script_dir/logging.json" /opt/acc-telemetry/logging.json
/usr/bin/python3 -I -m venv /opt/acc-telemetry/venv
# Every package, including transitive ones, is pinned by SHA-256; wheels only, so no
# package build code runs as root, and no dependency resolution beyond the lock.
/opt/acc-telemetry/venv/bin/python -I -m pip install --disable-pip-version-check --no-input \
    --require-hashes --only-binary :all: --no-deps --requirement "$script_dir/requirements.txt"
install -d -o acc-telemetry -g acc-telemetry -m 0700 /var/lib/acc-telemetry
install -m 0644 "$script_dir/acc-telemetry.service" /etc/systemd/system/acc-telemetry.service
install -d -m 0755 /etc/systemd/system/caddy.service.d
install -m 0644 "$script_dir/caddy-limits.conf" /etc/systemd/system/caddy.service.d/acc-telemetry.conf
/usr/bin/python3 -I "$repo_dir/deploy/lib/public-aliases.py" --role main
systemctl daemon-reload
printf '%s\n' 'Installed; no services started. Follow the runbook for Caddy and first start.'
