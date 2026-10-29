#!/usr/bin/env bash
# Remove the main Rynhult from this Linux machine (docs/RC-TESTING.md, "Uninstall"). A dry run unless --yes.
#   deploy/uninstall.sh                          list exactly what would be removed and kept (no root needed)
#   sudo deploy/uninstall.sh --yes               remove Rynhult's services, units, helpers, settings and accounts
#   sudo deploy/uninstall.sh --yes --purge-data  also your data: asks you to type a phrase first
# Kept unless --purge-data: projects (/srv/acc/workspaces), Rynhult's database and secrets (/srv/acc/data), git
# backups (/srv/acc/git), backups (/var/backups/acc) and the agent accounts' home folders.
# Never removed: packages setup installed (Caddy, Node.js, git, tmux, agent CLIs), apt sources and keys, and your own
# account. An account that still runs processes is skipped, never killed: stop its sessions first.
# ACC_UNINSTALL_ROOT (tests only, never as root): act inside that folder instead of /.
set -euo pipefail

R=${ACC_UNINSTALL_ROOT:-}
YES='' PURGE=''
for arg in "$@"; do
  case $arg in
    --yes|-y) YES=1 ;;
    --purge-data) PURGE=1 ;;
    -h|--help) sed -n '2,10s/^# \{0,1\}//p' "$0"; exit 0 ;;
    *) echo "unknown option $arg (see --help)" >&2; exit 2 ;;
  esac
done
if [ -n "$R" ]; then
  case $R in /*) ;; *) echo "ACC_UNINSTALL_ROOT must be an absolute folder" >&2; exit 2 ;; esac
  if [ "$R" = / ] || [ ! -d "$R" ]; then echo "ACC_UNINSTALL_ROOT must be an existing folder other than /" >&2; exit 2; fi
  # A test root still runs systemctl and userdel from PATH: never as root, so it can't reach this machine's real ones.
  [ "$(id -u)" != 0 ] || { echo "ACC_UNINSTALL_ROOT is for tests and is refused as root" >&2; exit 2; }
elif [ -n "$YES" ] && [ "$(id -u)" != 0 ]; then
  echo "run as root: sudo $0 --yes (without --yes it only lists)" >&2; exit 1
fi
PHRASE='delete my Rynhult data'
if [ -n "$PURGE" ] && [ -n "$YES" ]; then
  printf 'This deletes your projects, Rynhult'"'"'s database and secrets, git backups, backups and the agents'"'"' home folders.\n'
  printf 'Type "%s" to continue: ' "$PHRASE"
  read -r typed || typed=''
  [ "$typed" = "$PHRASE" ] || { echo; echo "Not confirmed: nothing was changed." >&2; exit 1; }
fi

act() { if [ -n "$YES" ]; then "$@"; else :; fi; }
say() { if [ -n "$YES" ]; then printf '%s\n' "$1"; else printf 'would %s\n' "$1"; fi; }
# A link is an entry to remove, not a reason to inspect its target (also in a fake root).
gone() { [ -L "$R$1" ] || [ -e "$R$1" ]; }
remove() {  # remove PATH: a file, a link or a folder, only if it is there
  if gone "$1"; then say "remove $1"; act rm -rf -- "$R$1"; fi
}

[ -n "$YES" ] || echo "Dry run: nothing is changed. Add --yes to remove."
units_dir=$R/etc/systemd/system

# Accounts first, while /etc/acc/broker still lists the agent accounts acc-agent-add created. Only those it wrote
# as agent brokers: setup also writes one for the owner's own account (ACC_BROKER_ROLE=services), which this script
# never removes (security review 10, SR10-1). The services account and the person running sudo are kept regardless.
services=$(sed -n 's/^ACC_SERVICES_ACCOUNT=//p' "$R/etc/default/accd" 2>/dev/null | tail -n 1 || true)
agents=()
for env in "$R"/etc/acc/broker/*.env; do
  [ -f "$env" ] || continue
  name=$(basename "$env" .env)
  [[ $name =~ ^[a-z_][a-z0-9_-]*$ ]] || continue
  grep -qx 'ACC_BROKER_ROLE=agents' "$env" || continue
  [ "$name" != "$services" ] && [ "$name" != "${SUDO_USER:-}" ] && agents+=("$name")
done

echo "== services"
units=()
# Rynhult's units are accd.* and acc-*: a bare acc* would also take the system's accounts-daemon (SR10-2).
for path in "$units_dir"/accd.* "$units_dir"/acc-* "$units_dir"/rynhult*; do
  { [ -L "$path" ] || [ -e "$path" ]; } || continue
  name=$(basename "$path")
  case $name in *.d) continue ;; esac  # drop-in folders go with their files below
  units+=("$name")
done
for name in "${units[@]}"; do
  case $name in
    *@.service|*@.socket) say "stop ${name/@./@*.}"; act systemctl stop "${name/@./@*.}" || true ;;
    *) say "stop and disable $name"; act systemctl disable --now "$name" || true ;;
  esac
done

echo "== unit files and drop-ins"
for name in "${units[@]}"; do remove "/etc/systemd/system/$name"; done
for path in "$units_dir"/accd.*.d "$units_dir"/acc-*.d "$units_dir"/*.wants/accd.* "$units_dir"/*.wants/acc-* \
            "$units_dir"/*.wants/rynhult*; do
  if [ -L "$path" ] || [ -e "$path" ]; then remove "${path#"$R"}"; fi
done
caddy_changed=''
for dropin in acc.conf runtime.conf hardening.conf distro.conf; do
  if gone "/etc/systemd/system/caddy.service.d/$dropin"; then caddy_changed=1; fi
  remove "/etc/systemd/system/caddy.service.d/$dropin"
done
if [ -d "$units_dir/caddy.service.d" ] && [ -z "$(ls -A "$units_dir/caddy.service.d")" ]; then
  remove /etc/systemd/system/caddy.service.d
fi
say "reload systemd"; act systemctl daemon-reload || true

echo "== Caddy"
remove /etc/caddy/acc.d
remove /etc/caddy/acc-dns
remove /etc/caddy/cloudflare.env
remove /etc/caddy/acc-hardening.options
if [ -f "$R/etc/caddy/Caddyfile" ] && grep -qx 'import acc\.d/\*\.caddy' "$R/etc/caddy/Caddyfile"; then
  say "remove the line 'import acc.d/*.caddy' from /etc/caddy/Caddyfile"
  act sed -i '/^import acc\.d\/\*\.caddy$/d' "$R/etc/caddy/Caddyfile"
  caddy_changed=1
fi
if [ -n "$caddy_changed" ]; then say "restart Caddy (if it runs) without Rynhult's sites"; act systemctl try-restart caddy.service || true; fi

echo "== AppArmor profiles"
for profile in acc-chrome acc-codex-bwrap acc-system-bwrap acc-podman acc-podman-runtime; do
  if gone "/etc/apparmor.d/$profile"; then
    say "unload and remove /etc/apparmor.d/$profile"
    act apparmor_parser -R "$R/etc/apparmor.d/$profile" 2>/dev/null || true
    act rm -f -- "$R/etc/apparmor.d/$profile"
  fi
done

echo "== helpers, commands and settings"
for name in acc-update acc-packages acc-tool-policy.py acc-claude-apt-repo acc-agent-add acc-agent-share acc-publish \
            acc-hoststate acc-backup acc-accounts acc-root-session acc-containers acc_env.py acc-env.sh; do
  remove "/usr/local/sbin/$name"
done
remove /usr/local/bin/acc-env.sh
remove /usr/local/lib/acc-dns
# Commands are links into Rynhult's tree or helpers: remove only links that point there.
for path in "$R"/usr/local/bin/acc* "$R"/usr/local/bin/rynhult* "$R"/usr/local/sbin/rynhult*; do
  [ -L "$path" ] || continue
  case $(readlink "$path") in /srv/acc/*|/usr/local/sbin/acc*|/usr/local/bin/acc*) remove "${path#"$R"}" ;; esac
done
for path in /etc/default/accd /etc/default/acc-backup /etc/default/acc-root-session /etc/acc /etc/rynhult-phase-b.json \
            /var/log/acc-install.log; do
  remove "$path"
done
echo "   no sudoers files: Rynhult installs none"

echo "== Rynhult's folders"
keep=(data git workspaces)
for path in "$R"/srv/acc/* "$R"/srv/acc/.[!.]*; do
  { [ -L "$path" ] || [ -e "$path" ]; } || continue
  name=$(basename "$path")
  [[ " ${keep[*]} " == *" $name "* ]] && [ -z "$PURGE" ] && continue
  remove "/srv/acc/$name"
done
if [ -n "$PURGE" ]; then
  remove /srv/acc
  remove /var/backups/acc
fi

echo "== accounts"
for user in acc acc-build "${agents[@]}"; do
  id "$user" >/dev/null 2>&1 || continue
  if [ -n "$(ps -o pid= -u "$user" 2>/dev/null)" ]; then
    echo "   skipped $user: it still runs processes. Stop its sessions (or archive them in Rynhult), then run this again."
    continue
  fi
  if [ -n "$PURGE" ]; then say "delete account $user and its home folder"; act userdel -r "$user" || true
  else say "delete account $user (its home folder stays)"; act userdel "$user" || true; fi
done

echo "== left in place"
if [ -z "$PURGE" ]; then
  echo "   your data: /srv/acc/workspaces, /srv/acc/data, /srv/acc/git, /var/backups/acc, the agents' home folders"
  echo "   (add --purge-data to delete them)"
fi
echo "   packages and their apt sources (Caddy, Node.js, Claude Code, git, tmux, agent CLIs; Docker if you chose it)"
echo "   Caddy's own settings in /etc/caddy/Caddyfile besides Rynhult's import line"
echo "   your account, its linger setting and its rootless Docker or Podman"
if [ -n "$YES" ]; then echo "Done."; else echo "Dry run finished: nothing was changed."; fi
