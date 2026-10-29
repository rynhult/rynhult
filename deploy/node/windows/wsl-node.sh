#!/bin/sh
acc_compat_lib="$(dirname "$0")/acc-env.sh"
[ -f "$acc_compat_lib" ] || acc_compat_lib="$(dirname "$0")/../../lib/acc-env.sh"
# shellcheck source=/dev/null
. "$acc_compat_lib"
# Run inside WSL as root by the Windows installers (deploy/windows/install-acc.ps1, deploy/node/windows/install.ps1),
# from the Windows copy of Rynhult (mounted under /mnt/<drive>) as the current folder.
#   wsl-node.sh systemd               turn on systemd in /etc/wsl.conf; exit 10 when it changed (WSL must restart)
#   wsl-node.sh install [options]     copy Rynhult to /root/acc-src, then run deploy/node/install.sh there
#   wsl-node.sh owner NAME            create the Linux user NAME (Rynhult's owner) and make it WSL's default user
#   wsl-node.sh main                  copy Rynhult to /srv/acc/app, then run deploy/setup.sh --yes there; the answers come
#                                     in the environment (ACC_OWNER, ACC_HOSTNAME, ACC_ZONE, CF_API_TOKEN, ACC_AGENT_CLIS)
#   wsl-node.sh uninstall [--dry-run] stop and remove Rynhult's services and programs; data, settings, accounts and Caddy stay
set -eu
conf=${ACC_WSL_CONF:-/etc/wsl.conf}  # tests point it elsewhere

# copy_tree DEST [tar options]: DEST becomes a copy of the current folder, without git, virtualenvs, node_modules or
# data. DEST/.venv stays, so uv only applies what changed. A copy on the Linux side: /mnt/c is slow, and its files may
# carry Windows line endings.
copy_tree() {
  dest=$1
  shift
  mkdir -p "$dest"
  find "$dest" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
  tar -C "$(pwd)" --exclude=.git --exclude=.venv --exclude=node_modules --exclude=./data --exclude=__pycache__ "$@" \
    -cf - . | tar -C "$dest" --no-same-owner -xf -
  find "$dest/deploy" "$dest/bin" -type f -exec sed -i 's/\r$//' {} +
}

case ${1:-} in
  systemd)
    touch "$conf"
    if grep -Eq '^[[:space:]]*systemd[[:space:]]*=[[:space:]]*true' "$conf"; then
      echo "systemd: already on in $conf"; exit 0
    fi
    sed -i '/^[[:space:]]*systemd[[:space:]]*=/d' "$conf"
    if grep -q '^\[boot\]' "$conf"; then sed -i '/^\[boot\]/a systemd=true' "$conf"
    else printf '\n[boot]\nsystemd=true\n' >> "$conf"; fi
    echo "systemd: turned on in $conf"
    exit 10 ;;
  install|install-primary)
    mode=$1
    shift
    if [ "$mode" = install-primary ]; then
      primary=$1 trust=$2 fingerprint=$3
      shift 3
      allow_bundled=0
      if [ "${1:-}" = --allow-bundled-source ]; then
        allow_bundled=1
        shift
      fi
      work=$(mktemp -d)
      trap 'rm -rf "$work"' EXIT
      status=0
      sh deploy/node/windows/source.sh "$work/source" "$primary" "$trust" "$fingerprint" || status=$?
      case $status in
        0)
          # Keep the downloaded build identity and installer, rather than the Windows release's tree.
          (cd "$work/source" && tar --exclude=./web -cf - .) > "$work/source.tar"
          mkdir -p /root/acc-src
          find /root/acc-src -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
          tar -C /root/acc-src --no-same-owner -xf "$work/source.tar"
          ;;
        10)
          if [ "$allow_bundled" -ne 1 ]; then
            echo "Rynhult: stopped without installing; explicit --allow-bundled-source is required for offline setup" >&2
            exit 1
          fi
          bundled_version=$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -n 1)
          [ -n "$bundled_version" ] || { echo "Rynhult: cannot identify bundled source; stopping" >&2; exit 1; }
          echo "WARNING: explicit offline install uses bundled Rynhult $bundled_version; it may be older and vulnerable" >&2
          if [ -f .acc-build ]; then printf 'Rynhult: bundled build %s\n' "$(cat .acc-build)" >&2; fi
          copy_tree /root/acc-src --exclude=./web ;;
        *) exit 1 ;;
      esac
      rm -rf "$work"
      trap - EXIT
    else
      copy_tree /root/acc-src --exclude=./web
    fi
    exec bash /root/acc-src/deploy/node/install.sh --install-prereqs "$@" ;;
  owner)
    name=${2:-}
    case $name in
      ''|root|*[!a-z0-9_-]*|[!a-z_]*) echo "bad owner name: '$name' (a Linux user name other than root)" >&2; exit 1 ;;
    esac
    broker_env=/etc/acc/broker/$name.env
    if [ -e "$broker_env" ] || [ -L "$broker_env" ]; then
      if [ -L "$broker_env" ] || [ ! -f "$broker_env" ]; then
        echo "refusing: unsafe broker identity for $name" >&2; exit 1
      fi
      for role_key in ACC_BROKER_ROLE RYNHULT_BROKER_ROLE; do
        role_count=$(grep -c "^$role_key=" "$broker_env") || true
        if [ "$role_count" -gt 1 ]; then
          echo "refusing: duplicate broker identity for $name" >&2; exit 1
        fi
      done
      broker_role=$(acc_setting_file ACC_BROKER_ROLE "$broker_env") || {
        echo "refusing: cannot inspect broker identity for $name" >&2; exit 1;
      }
      if [ "$broker_role" != services ]; then
        echo "refusing: $name already has an agent broker identity; an agent must never become a sudo owner" >&2
        exit 1
      fi
    fi
    locked=0
    if id "$name" >/dev/null 2>&1; then
      echo "owner: $name exists"
      password_status=$(LC_ALL=C passwd -S "$name") || {
        echo "refusing: cannot inspect owner password status" >&2; exit 1;
      }
      case $password_status in
        "$name NP "*) passwd -l "$name"; locked=1 ;;
        "$name L "*) locked=1 ;;
        "$name P "*) ;;
        *) echo "refusing: unknown owner password status" >&2; exit 1 ;;
      esac
    else
      useradd --create-home --shell /bin/bash "$name"
      passwd -l "$name"
      locked=1
      echo "owner: created $name"
    fi
    if [ "$locked" -eq 1 ]; then
      echo "owner: password locked; from Windows run: wsl -d <your-distro> -u root -- passwd $name"
    fi
    usermod -aG sudo "$name"
    # `wsl` opens as the owner, never the passwordless agent account.
    touch "$conf"
    sed -i '/^[[:space:]]*default[[:space:]]*=/d' "$conf"
    if grep -q '^\[user\]' "$conf"; then
      sed -i "/^\[user\]/a default=$name" "$conf"
    else
      printf '\n[user]\ndefault=%s\n' "$name" >> "$conf"
    fi ;;
  main)
    : "${ACC_OWNER:?ACC_OWNER is missing: the installer passes its answers through WSLENV}"
    if [ -e /srv/acc/app/.git ]; then
      echo "/srv/acc/app is a git checkout: update it with git pull, then sudo deploy/install.sh" >&2; exit 1
    fi
    copy_tree /srv/acc/app
    # Rynhult's units run this code as root: root's, and nobody else may write it.
    chown -R -h root:root /srv/acc/app
    chmod -R go-w /srv/acc/app
    exec bash /srv/acc/app/deploy/setup.sh --yes ;;
  uninstall)
    dry=''
    [ "${2:-}" = --dry-run ] && dry=1
    run() { echo "+ $*"; [ -n "$dry" ] || "$@" || echo "  (failed: $*; going on)"; }
    [ -n "$dry" ] && echo "(dry run: nothing is changed)"
    units=$(systemctl list-unit-files --no-legend 'accd.*' 'acc-*' 2>/dev/null | awk '{print $1}' | grep -v '@\.' || true)
    running=$(systemctl list-units --all --no-legend --plain 'accd.*' 'acc-*' 2>/dev/null | awk '{print $1}' || true)
    # shellcheck disable=SC2086  # one word per unit
    [ -z "$units$running" ] || run systemctl disable --now $units $running
    # Drain new-name consumers before uninstalling. Remove only B-owned aliases,
    # while the reviewed helper/source and journal still exist. A conflict aborts.
    alias_helper="$(/usr/bin/dirname "$0")/../../lib/public-aliases.py"
    echo "+ /usr/bin/python3 -I $alias_helper --role auto --rollback-aliases"
    if [ -z "$dry" ]; then
      /usr/bin/python3 -I "$alias_helper" --role auto --rollback-aliases || exit 1
    fi
    for path in /etc/systemd/system/accd.* /etc/systemd/system/acc-* /srv/acc/app /srv/acc/bin /srv/acc/tools \
      /opt/acc /root/acc-src /usr/local/bin/acc-node /usr/local/bin/acc-request /usr/local/bin/acc-wiki-check \
      /usr/local/sbin/acc-*; do
      if [ -e "$path" ] || [ -L "$path" ]; then run rm -rf "$path"; fi
    done
    run systemctl daemon-reload
    echo "kept: /srv/acc/data, /srv/acc/workspaces, /etc/default/accd, /etc/acc-node.env, /var/lib/acc-node, the"
    echo "      Linux accounts, and Caddy with its settings" ;;
  *)
    echo "usage: wsl-node.sh systemd | install [install.sh options] | owner NAME | main | uninstall [--dry-run]" >&2
    exit 1 ;;
esac
