#!/usr/bin/env bash
# Install or update Rynhult from this repository. Idempotent; run as root. New machines start with deploy/setup.sh,
# which installs the dependencies, writes /etc/default/accd and then runs this; afterwards re-run this alone to
# apply an update:  sudo ./deploy/install.sh
# Everything machine-specific comes from /etc/default/accd (see deploy/accd.env.example):
#   ACC_SERVICES_ACCOUNT  the owner: owns the project checkouts and their (rootless) Docker stacks
#   ACC_AGENT_HOME        the home of the unprivileged account the owner's agents run as (created here)
#   ACC_ALLOWED_ORIGINS   the URL people open Rynhult at;  ACC_WILDCARD_ZONE  the Cloudflare zone for sites (optional)
# Secrets are never in git. Re-provision after a rebuild:
#   /srv/acc/data/secrets/{github_token,cloudflare_token}  (acc 0600)
#   /srv/acc/data/secrets/vault.key: the key vault's data key, made here once (deploy/lib/vault-key, which refuses
#     while acc.db holds keys); restore it from backup with acc.db (without it the vault's values can't be decrypted)
#   /etc/caddy/cloudflare.env                              (root 0600, CF_API_TOKEN=…)
#   each agent account: Claude, Codex and GitHub logins from the Providers page
# Data that is NOT recreated: /srv/acc/data/acc.db (restore from backup), workspaces, transcripts.
set -euo pipefail

# Phase B can be deployed independently of the normal installer: reload only,
# never run setup, account helpers or restart services. Dry-run plans aliases only.
case " $* " in
  *" --aliases-only "*|*" --rollback-aliases "*|*" --dry-run "*)
    alias_repo=$(cd "$(/usr/bin/dirname "$0")/.." && pwd)
    /usr/bin/python3 -I "$alias_repo/deploy/lib/public-aliases.py" --role main "$@"
    case " $* " in *" --dry-run "*) ;; *) /usr/bin/systemctl daemon-reload ;; esac
    exit 0
    ;;
esac
# Never let a misspelled read-only/alias option enter the ordinary root installer.
if [ "$#" -gt 0 ]; then
  echo 'unknown option; use --aliases-only, --rollback-aliases or --dry-run' >&2
  exit 2
fi
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

acc_compat_lib="$(dirname "$0")/acc-env.sh"
[ -f "$acc_compat_lib" ] || acc_compat_lib="$(dirname "$0")/lib/acc-env.sh"
# shellcheck source=/dev/null
. "$acc_compat_lib"

REPO=$(cd "$(dirname "$0")/.." && pwd)
[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
# shellcheck source=deploy/lib/pkg.sh
source "$REPO/deploy/lib/pkg.sh"
PKG=$(pkg_manager || true)
# deploy/setup.sh --unqualified-distro sets ACC_UNQUALIFIED_DISTRO for a test install with dnf, zypper or pacman.
case ${PKG:-none}:${ACC_UNQUALIFIED_DISTRO:-} in
  apt:*|dnf:1|zypper:1|pacman:1) ;;
  *) echo "$(pkg_system) uses ${PKG:-no package manager Rynhult knows}: the main Rynhult supports only Debian and Ubuntu (apt) for now; see docs/LINUX-DISTROS.md" >&2; exit 1 ;;
esac
# acc-agent-share grants agent accounts the shared workspaces with ACLs; without setfacl it would silently grant nothing.
command -v setfacl >/dev/null || { echo "setfacl is missing (the acl package): $(pkg_hint "$PKG") $(pkg_name "$PKG" acl)" >&2; exit 1; }
/usr/bin/python3 -I "$REPO/deploy/lib/unit-aliases.py" --check --unit-dir /etc/systemd/system
/usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role main --dry-run
[ -f /etc/default/accd ] || { echo "no /etc/default/accd: run deploy/setup.sh first (or copy deploy/accd.env.example there and edit it)" >&2; exit 1; }
chown root:root /etc/default/accd
chmod 0600 /etc/default/accd
setting() { acc_setting_file "$1" /etc/default/accd; }
OWNER=${OWNER:-$(setting ACC_SERVICES_ACCOUNT)}
[ -n "$OWNER" ] || { echo "set ACC_SERVICES_ACCOUNT (the owner's Linux user) in /etc/default/accd" >&2; exit 1; }
id "$OWNER" >/dev/null 2>&1 || { echo "user $OWNER doesn't exist" >&2; exit 1; }
AGENT_HOME=$(setting ACC_AGENT_HOME)
AGENT_USER=${AGENT_USER:-${AGENT_HOME:+$(basename "$AGENT_HOME")}}
AGENT_USER=${AGENT_USER:-$OWNER-agent}  # the unprivileged account their agents run as (created below)
ORIGIN=$(setting ACC_ALLOWED_ORIGINS | cut -d, -f1)
ACC_HOST=$(printf '%s' "$ORIGIN" | sed -E 's#^[a-z]+://##; s#[:/].*##')
ZONE=$(setting ACC_WILDCARD_ZONE)
DATABASE_URL=$(setting ACC_DATABASE_URL)
ACC_PREVIEW_DOMAIN=$(setting ACC_PREVIEW_DOMAIN)

echo "== users and directories"
id acc >/dev/null 2>&1 || useradd --system --user-group --no-create-home --home-dir /srv/acc/data --shell /usr/sbin/nologin acc
loginctl enable-linger "$OWNER"  # rootless Docker keeps running
install -d -o root -g root -m 0755 /srv/acc /srv/acc/bin
# Refuse links/wrong owners before doing anything beneath the service account's data.
if [ ! -e /srv/acc/data ] && [ ! -L /srv/acc/data ]; then
  install -d -o acc -g acc -m 0700 /srv/acc/data
fi
/usr/bin/python3 -I "$REPO/deploy/lib/data-paths.py" check /srv/acc/data "$(id -u acc)"
runuser -u acc -- install -d -m 0700 /srv/acc/data/secrets
# The key vault's data key (docs/VAULT.md): made once, atomically, as acc (root never works on paths acc controls);
# never replaced, and never made while acc.db holds encrypted keys (restore it with the database instead)
VAULT_OPTIONS=()
DATABASE_EXTRAS=()
if [ -n "${DATABASE_URL:-}" ]; then
  # A stale local acc.db cannot establish whether the configured database holds encrypted keys.
  VAULT_OPTIONS=(--external-database)
  case "$DATABASE_URL" in postgresql*) DATABASE_EXTRAS=(--extra postgres) ;; esac
fi
runuser -u acc -- /usr/bin/python3 -I "$REPO/deploy/lib/vault-key" /srv/acc/data/secrets/vault.key /srv/acc/data/acc.db "${VAULT_OPTIONS[@]}"
install -d -o acc -g acc -m 0755 /srv/acc/catalog /srv/acc/catalog-local  # catalog-local: items added from the dashboard
install -d -o acc -g acc -m 0700 /srv/acc/git  # git backups: one bare repository per project (docs/MULTIHOST.md)
install -d -o root -g acc -m 0751 /srv/acc/run  # one folder per agent account inside (acc-agent-add)
install -d -o root -g acc -m 0750 /srv/acc/run-root  # root's own sessions: no agent can swap these folders
install -d -o "$OWNER" -g "$OWNER" -m 0755 /srv/acc/workspaces
if [ "$REPO" != /srv/acc/app ]; then
  echo "note: repo is at $REPO; units expect /srv/acc/app (clone there or symlink)"
fi
# SELinux (Fedora, openSUSE Tumbleweed): file contexts for Rynhult's executables, data, run folders and sockets.
# Never the enforcing mode itself.
if command -v selinuxenabled >/dev/null && selinuxenabled; then
  sh "$REPO/deploy/lib/selinux-labels.sh" main
fi

install -m 0644 "$REPO/src/acc/env.py" /usr/local/sbin/acc_env.py
install -m 0644 "$REPO/deploy/lib/acc-env.sh" /usr/local/sbin/acc-env.sh
install -m 0644 "$REPO/src/acc/env.py" /srv/acc/bin/acc_env.py
install -m 0644 "$REPO/deploy/lib/acc-env.sh" /srv/acc/bin/acc-env.sh
install -m 0644 "$REPO/deploy/lib/acc-env.sh" /usr/local/bin/acc-env.sh

echo "== hook scripts"
install -o root -g root -m 0755 "$REPO/bin/acc-hook" "$REPO/bin/acc-guard" "$REPO/bin/acc-mcp-team" "$REPO/bin/acc-statusline" "$REPO/bin/acc-request" \
  "$REPO/bin/acc-mcp-github" "$REPO/bin/acc-wiki-check" /srv/acc/bin/
# Commands agents run by name (the acc-workflow skill): put them on every user's PATH.
for cmd in acc-request acc-wiki-check; do acc_safe_link "/srv/acc/bin/$cmd" "/usr/local/bin/$cmd"; done

echo "== python environment"
# shellcheck source=deploy/lib/trusted-tool.sh
source "$REPO/deploy/lib/trusted-tool.sh"
export TRUSTED_TOOL_UID=0
UV=$(find_uv)
if [ -z "$UV" ]; then
  sh "$REPO/deploy/lib/install-uv.sh"
  UV=$(find_uv)
fi
[ -n "$UV" ] || { echo "no trusted system uv" >&2; exit 1; }
/usr/bin/python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 12))' || { echo "Rynhult needs Python 3.12 or newer at /usr/bin/python3" >&2; exit 1; }
(cd "$REPO" && UV_CACHE_DIR=/srv/acc/.uv-cache "$UV" sync --frozen --no-dev --inexact --python /usr/bin/python3 "${DATABASE_EXTRAS[@]}")  # --inexact: keep dev tools on a dev machine

# Rynhult public commands; existing acc* commands remain installed for upgrades and hooks.
for cmd in rynhult acc rynhultd accd rynhult-node; do
  acc_safe_link "$REPO/.venv/bin/$cmd" "/usr/local/bin/$cmd"
done
acc_safe_link /srv/acc/bin/acc-guard /srv/acc/bin/rynhult-guard
acc_safe_link /srv/acc/bin/acc-guard /usr/local/bin/rynhult-guard

echo "== web UI"
bash "$REPO/deploy/lib/build-assets.sh" "$REPO" web

echo "== source bundle: what new machines install, and this version's source for its users (GET /api/nodes/bundle)"
# Built in a folder of root's, then installed: root never writes through paths in data/ (acc's) it hasn't made.
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml")
bundle_tmp=$(mktemp -d)
/usr/bin/python3 -I "$REPO/src/acc/bundle.py" --source "$REPO" --out "$bundle_tmp" | sed 's/^/   /'
/usr/bin/python3 -I "$REPO/deploy/lib/data-paths.py" bundle /srv/acc/data "$(id -u acc)" \
  "$bundle_tmp/acc-$VERSION.tar.gz" "acc-$VERSION.tar.gz"
rm -rf "$bundle_tmp"

echo "== Rynhult updater (root helper; owners request action and version only)"
install -d -o root -g acc -m 0750 /srv/acc/updates /srv/acc/updates/results
install -d -o acc -g acc -m 0750 /srv/acc/updates/requests
install -o root -g root -m 0755 "$REPO/deploy/updates/acc-update" /usr/local/sbin/acc-update
install -m 0644 "$REPO/deploy/updates/acc-update.path" "$REPO/deploy/updates/acc-update.service" /etc/systemd/system/
/usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role main
systemctl daemon-reload
systemctl enable --now acc-update.path

echo "== agent software: acc-packages (installs and updates from the dashboard, plus a daily check)"
install -d -o root -g acc -m 0750 /srv/acc/packages /srv/acc/packages/results
install -d -o acc -g acc -m 0750 /srv/acc/packages/requests
install -o root -g root -m 0755 "$REPO/deploy/packages/acc-packages" /usr/local/sbin/acc-packages
install -o root -g root -m 0644 "$REPO/src/acc/tooling.py" /usr/local/sbin/acc-tool-policy.py
install -o root -g root -m 0755 "$REPO/deploy/lib/claude-apt-repo.sh" /usr/local/sbin/acc-claude-apt-repo
install -o root -g root -m 0644 "$REPO/src/acc/tooling.py" /srv/acc/bin/acc-tool-policy.py
install -m 0644 "$REPO/deploy/packages/acc-packages.path" "$REPO/deploy/packages/acc-packages.service" \
  "$REPO/deploy/packages/acc-packages-daily.service" "$REPO/deploy/packages/acc-packages-daily.timer" /etc/systemd/system/
/usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role main
systemctl daemon-reload
systemctl enable --now acc-packages.path acc-packages-daily.timer
# The CLIs this machine offers besides Claude Code (ACC_AGENT_CLIS in /etc/default/accd), at their latest versions;
# afterwards the daily timer keeps them current (switch auto-update per package on the Providers page).
clis=$(setting ACC_AGENT_CLIS | tr -d '"' | tr ',' ' ')
# shellcheck disable=SC2086  # a list of names, split on purpose
[ -z "$clis" ] || /usr/local/sbin/acc-packages --install $clis | sed 's/^/   /'
dpkg -s claude-code >/dev/null 2>&1 && apt-mark hold claude-code >/dev/null  # updated by acc-packages, not unattended-upgrades

echo "== brokers: one per agent account, plus the owner's for Docker stacks only"
install -d -m 0755 /etc/acc/broker
install -m 0644 "$REPO/deploy/broker/acc-broker@.socket" "$REPO/deploy/broker/acc-broker@.service" /etc/systemd/system/
install -o root -g root -m 0755 "$REPO/deploy/broker/acc-agent-add" "$REPO/deploy/broker/acc-agent-share" /usr/local/sbin/
/usr/local/sbin/acc-agent-add "$AGENT_USER"
/usr/local/sbin/acc-agent-add "$OWNER" --services
/usr/local/sbin/acc-agent-share "$AGENT_USER" "$OWNER" /srv/acc/workspaces

install -m 0644 "$REPO/deploy/accd.service" "$REPO/deploy/accd.socket" /etc/systemd/system/
/usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role main
systemctl daemon-reload
systemctl enable --now accd.socket
systemctl enable accd.service
systemctl restart accd.service

echo "== MCP servers: installed once from the committed lockfile (root-owned), never fetched at session start"
install -d -o root -g root -m 0755 /srv/acc/tools
bash "$REPO/deploy/lib/build-assets.sh" "$REPO" mcp
sh "$REPO/deploy/lib/install-github-mcp.sh"

echo "== root helpers: publishing (acc-publish), host snapshot (acc-hoststate), backups (acc-backup)"
install -d -o root -g acc -m 0750 /srv/acc/publish /srv/acc/publish/results /srv/acc/hoststate
install -d -o acc -g acc -m 0750 /srv/acc/publish/requests
install -d -o root -g acc -m 0750 /srv/acc/accounts /srv/acc/accounts/results
install -d -o acc -g acc -m 0750 /srv/acc/accounts/requests  # new agent accounts asked for from the dashboard
install -d -o root -g root -m 0755 /usr/local/lib/acc-dns
install -o root -g root -m 0600 "$REPO/src/acc/dns_credentials.py" /usr/local/lib/acc-dns/credentials.py
install -o root -g root -m 0755 "$REPO/deploy/publish/acc-publish" /usr/local/sbin/acc-publish
install -o root -g root -m 0755 "$REPO/deploy/hoststate/acc-hoststate" /usr/local/sbin/acc-hoststate
install -o root -g root -m 0755 "$REPO/deploy/backup/acc-backup" /usr/local/sbin/acc-backup
install -o root -g root -m 0755 "$REPO/deploy/accounts/acc-accounts" /usr/local/sbin/acc-accounts
# Root's admin session (optional): configure /etc/default/acc-root-session, then enable acc-root-session.service.
install -o root -g root -m 0755 "$REPO/deploy/root-session/acc-root-session" /usr/local/sbin/acc-root-session
install -m 0644 "$REPO/deploy/root-session/acc-root-session.service" /etc/systemd/system/
install -m 0644 "$REPO/deploy/publish/acc-publish.path" "$REPO/deploy/publish/acc-publish.service" \
  "$REPO/deploy/hoststate/acc-hoststate.service" "$REPO/deploy/hoststate/acc-hoststate.timer" \
  "$REPO/deploy/backup/acc-backup.service" "$REPO/deploy/backup/acc-backup.timer" \
  "$REPO/deploy/accounts/acc-accounts.path" "$REPO/deploy/accounts/acc-accounts.service" /etc/systemd/system/
install -o root -g root -m 0755 "$REPO/bin/acc-request" "$REPO/bin/acc-mcp-github" /srv/acc/bin/
# The backup unit sandboxes itself with ReadWritePaths=/var/backups/acc, which systemd refuses when the folder is
# missing, so a fresh install never made a backup until this existed (found on a fresh VM, Oct 29).
install -d -o root -g root -m 0700 /var/backups/acc
# Backup mirrors (ACC_BACKUP_MIRRORS in /etc/default/acc-backup, user:/source:/dest;…): each destination must be
# writable inside the sandboxed unit.
mirrors=""
[ -f /etc/default/acc-backup ] && mirrors=$(acc_setting_file ACC_BACKUP_MIRRORS /etc/default/acc-backup | tr -d '"')
if [ -n "$mirrors" ]; then
  install -d -m 0755 /etc/systemd/system/acc-backup.service.d
  { echo "# Rendered by deploy/install.sh from ACC_BACKUP_MIRRORS in /etc/default/acc-backup."
    echo "[Service]"
    printf '%s\n' "$mirrors" | tr ';' '\n' | while IFS=: read -r _ _ dest; do [ -n "$dest" ] && echo "ReadWritePaths=-$(dirname "$dest")"; done
  } > /etc/systemd/system/acc-backup.service.d/mirrors.conf
else
  rm -f /etc/systemd/system/acc-backup.service.d/mirrors.conf
fi
/usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role main
systemctl daemon-reload
systemctl enable --now acc-publish.path acc-accounts.path acc-hoststate.timer
# The backup timer is the operator's choice (setup.sh asks); this never turns it on or off.

install -d -o root -g root -m 0755 /srv/acc/tools/ms-playwright

source "$REPO/deploy/lib/apparmor-userns.sh"
if [ -d /sys/kernel/security/apparmor ] && command -v apparmor_parser >/dev/null; then  # sandboxes that need user namespaces
  profiles="acc-chrome acc-codex-bwrap"  # the browser MCP's Chrome; Codex's bundled bubblewrap
  # The distribution's bubblewrap, which Codex 0.163+ prefers once the package is there (it arrives with WebKitGTK and
  # Flatpak): installed even before /usr/bin/bwrap exists. It lets any local account create user namespaces through
  # it, as on systems without the restriction (docs/wiki/Internal/Operations.md, Codex sandbox).
  if userns_restricted; then profiles="$profiles acc-system-bwrap"; fi
  for profile in $profiles; do
    install_profile "$REPO/deploy/apparmor/$profile" "/etc/apparmor.d/$profile"
  done
fi
check_codex_sandbox "$AGENT_USER"

echo "== trusted container launcher (acc-containers; used only by projects an owner switches to containers)"
# grants/: accd (the control plane) authorizes each container session there; state/: root's records; audit/: read by
# accd into Rynhult's audit log. The policy (Rynhult's pinned images) is the owner's: never overwritten here, and without it
# every container launch is refused. The AppArmor profile (deploy/apparmor/acc-podman) is installed by the owner
# during qualification (docs/CONTAINERS.md), not here.
install -d -o root -g root -m 0755 /srv/acc/containers /etc/acc/containers /etc/acc/containers/hooks.d
install -d -o acc -g acc -m 0750 /srv/acc/containers/grants
install -d -o root -g root -m 0700 /srv/acc/containers/state
install -d -o root -g acc -m 0750 /srv/acc/containers/audit
install -o root -g root -m 0755 "$REPO/deploy/containers/acc-containers" /usr/local/sbin/acc-containers
install -o root -g root -m 0644 "$REPO/src/acc/container_credentials.py" /srv/acc/containers/credentials.py
install -o root -g root -m 0644 "$REPO/deploy/containers/acc-container-exec" /srv/acc/containers/acc-container-exec
install -o root -g root -m 0644 "$REPO/deploy/containers/policy.example.json" /etc/acc/containers/policy.example.json
install -m 0644 "$REPO/deploy/containers/acc-launcher.slice" "$REPO/deploy/containers/acc-containers.socket" "$REPO/deploy/containers/acc-containers@.service" \
  "$REPO/deploy/containers/acc-containers-reconcile.service" "$REPO/deploy/containers/acc-containers-reconcile.timer" \
  /etc/systemd/system/
/usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role main
systemctl daemon-reload
systemctl enable --now acc-containers.socket acc-containers-reconcile.timer

echo "== caddy: HTTPS in front of accd (deploy/caddy/README.md)"
PREVIEW_DOMAIN=${ACC_PREVIEW_DOMAIN:-}
PREVIEW_DOMAIN=${PREVIEW_DOMAIN:-${ZONE:-$ACC_HOST}}
# A DNS suffix is data, never Caddy/sed syntax. Wildcard mode must reuse its existing certificate.
[[ "$PREVIEW_DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || { echo "invalid ACC_PREVIEW_DOMAIN" >&2; exit 1; }
if [ -n "$ZONE" ] && [ "$PREVIEW_DOMAIN" != "$ZONE" ]; then
  echo "ACC_PREVIEW_DOMAIN must equal ACC_WILDCARD_ZONE in wildcard mode" >&2; exit 1
fi
render() { sed "s/@ZONE@/$ZONE/g; s/@ACC_HOST@/$ACC_HOST/g; s/@PREVIEW_DOMAIN@/$PREVIEW_DOMAIN/g" "$REPO/deploy/caddy/$1"; }
CADDY="" CADDY_UNIT_CHANGED="" CADDY_SIGNS=""
CADDY_ENV_FILE=/etc/caddy/cloudflare.env
CADDY_DROPIN="$REPO/deploy/caddy/caddy.service.d-acc.conf"
if [ -e /etc/caddy/acc-dns/current.env ] || [ -L /etc/caddy/acc-dns/current.env ]; then
  CADDY_ENV_FILE=/etc/caddy/acc-dns/current.env
  CADDY_DROPIN="$REPO/deploy/dns/caddy.service.d-dns.conf"
fi
if [ -x /usr/local/bin/caddy ] && [ -f "$CADDY_ENV_FILE" ]; then  # the Cloudflare build (setup.sh, with a zone)
  CADDY=$(readlink -f /usr/local/bin/caddy)
  install -d -m 0755 /etc/systemd/system/caddy.service.d
  cmp -s "$CADDY_DROPIN" /etc/systemd/system/caddy.service.d/acc.conf || CADDY_UNIT_CHANGED=1
  install -m 0644 "$CADDY_DROPIN" /etc/systemd/system/caddy.service.d/acc.conf
elif command -v caddy >/dev/null; then
  CADDY=$(readlink -f -- "$(command -v caddy)")
fi
[ -z "$CADDY" ] || trusted_tool "$CADDY" || { echo "untrusted Caddy binary" >&2; exit 1; }
if [ -z "$ACC_HOST" ] || [ -z "$CADDY" ] || [ ! -f /etc/caddy/Caddyfile ]; then
  echo "skipped: needs ACC_ALLOWED_ORIGINS in /etc/default/accd and Caddy installed (deploy/setup.sh does both)"
  CADDY=""
elif [ -n "$ZONE" ] && [ -f "$CADDY_ENV_FILE" ]; then
  # A Cloudflare zone: one wildcard certificate via DNS-01; Rynhult and published sites live inside it.
  install -d -m 0755 /etc/caddy/acc.d "/etc/caddy/acc.d/routes-$ZONE"
  if [ "$CADDY_ENV_FILE" = /etc/caddy/acc-dns/current.env ]; then
    sed "s/@ZONE@/$ZONE/g; s/@ACC_HOST@/$ACC_HOST/g" "$REPO/deploy/dns/wildcard-managed.caddy.in" > "/etc/caddy/acc.d/$ZONE-wildcard.caddy"
  else
    render wildcard.caddy.in > "/etc/caddy/acc.d/$ZONE-wildcard.caddy"
  fi
else
  # No domain: Rynhult alone, on Caddy's own certificate authority (publishing sites needs a zone).
  install -d -m 0755 /etc/caddy/acc.d
  render internal.caddy.in > /etc/caddy/acc.d/acc.caddy
  CADDY_SIGNS=1
fi
if [ -n "$CADDY" ]; then
  install -d -m 0755 /etc/systemd/system/caddy.service.d
  cmp -s "$REPO/deploy/caddy/caddy.service.d-runtime.conf" /etc/systemd/system/caddy.service.d/runtime.conf || CADDY_UNIT_CHANGED=1
  install -m 0644 "$REPO/deploy/caddy/caddy.service.d-runtime.conf" /etc/systemd/system/caddy.service.d/runtime.conf
  cmp -s "$REPO/deploy/caddy/caddy.service.d-hardening.conf" /etc/systemd/system/caddy.service.d/hardening.conf || CADDY_UNIT_CHANGED=1
  install -m 0644 "$REPO/deploy/caddy/caddy.service.d-hardening.conf" /etc/systemd/system/caddy.service.d/hardening.conf
  install -m 0644 "$REPO/deploy/caddy/acc-hardening.options" /etc/caddy/acc-hardening.options
  if [ "${PKG:-apt}" != apt ]; then
    # Fedora's unit validates with its own /usr/bin/caddy first (which lacks the DNS module of a Cloudflare build),
    # and openSUSE's starts with --resume (Caddy's autosave before the Caddyfile): run exactly the chosen Caddy on
    # the Caddyfile, as Debian's unit does (docs/LINUX-DISTROS.md, "systemd units").
    distro_unit=$(mktemp)
    printf '[Service]\nExecStartPre=\nExecStart=\nExecStart=%s run --environ --config /etc/caddy/Caddyfile\n' "$CADDY" > "$distro_unit"
    cmp -s "$distro_unit" /etc/systemd/system/caddy.service.d/distro.conf || CADDY_UNIT_CHANGED=1
    install -m 0644 "$distro_unit" /etc/systemd/system/caddy.service.d/distro.conf
    rm -f "$distro_unit"
  fi
  # Before persist_config off, Caddy autosaved the expanded Cloudflare credential in its home. Remove that copy as
  # caddy, so a link planted in Caddy's writable home cannot redirect a root deletion.
  if id caddy >/dev/null 2>&1; then runuser -u caddy -- rm -f /var/lib/caddy/.config/caddy/autosave.json || true; fi
  /usr/bin/python3 -I "$REPO/deploy/lib/caddy-admin.py" configure /etc/caddy/Caddyfile
  grep -q 'import acc.d/\*.caddy' /etc/caddy/Caddyfile || printf '\nimport acc.d/*.caddy\n' >> /etc/caddy/Caddyfile
  systemctl daemon-reload
  # Adapt first, and fail closed: a missing admin directive defaults to TCP.
  (cd /etc/caddy && /usr/bin/python3 -I "$REPO/deploy/lib/caddy-check.py" adapt "$CADDY" Caddyfile "$CADDY_ENV_FILE") |
    /usr/bin/python3 -I "$REPO/deploy/lib/caddy-admin.py" check
  if (cd /etc/caddy && /usr/bin/python3 -I "$REPO/deploy/lib/caddy-check.py" validate "$CADDY" Caddyfile "$CADDY_ENV_FILE" >/dev/null 2>&1); then
    if [ -n "$CADDY_UNIT_CHANGED" ]; then systemctl restart caddy  # a new binary or environment
    else systemctl reload-or-restart caddy; fi                   # reload when running: sites stay up
  else
    echo "WARNING: Caddy rejected /etc/caddy/Caddyfile; it was not restarted. Check: $CADDY validate --config /etc/caddy/Caddyfile" >&2
  fi
fi

echo "== private certificate: Caddy's own CA, for new machines (--ca-fingerprint) and devices (Settings → Certificate)"
CADDY_ROOT=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
NODE_CA=/srv/acc/data/node-ca.pem
configured=$(setting ACC_NODE_CA_FILE)
if [ -z "$CADDY_SIGNS" ]; then
  echo "not needed: Caddy doesn't sign Rynhult's certificate here (a Cloudflare zone, or no Caddy)"
elif [ -n "$configured" ] && [ "$configured" != "$NODE_CA" ]; then
  echo "kept: ACC_NODE_CA_FILE is $configured (/etc/default/accd)"
else
  for _ in $(seq 15); do [ -f "$CADDY_ROOT" ] && break; sleep 1; done  # Caddy makes its CA when it first starts
  if [ -f "$CADDY_ROOT" ]; then
    # Copied every time: a renewed root (every 10 years) reaches Rynhult on the next install or update. Read as caddy
    # (root never opens a path in Caddy's writable home, where a link could name a root-only file) and only one
    # certificate, re-encoded, is copied: /api/nodes/ca serves it to anyone (install and update review, IU-2).
    if ! root_pem=$(runuser -u caddy -- cat -- "$CADDY_ROOT" | /usr/bin/python3 -I "$REPO/deploy/lib/single-cert.py"); then
      echo "WARNING: $CADDY_ROOT isn't a single certificate; not copied. Check Caddy's local CA" >&2
    else
      # cat reads the inherited pipe; reopening /dev/stdin as acc fails (root owns the pipe), which stopped fresh installs.
      # shellcheck disable=SC2016  # $1 is expanded by the inner sh, on purpose
      printf '%s\n' "$root_pem" | runuser -u acc -- sh -c 'umask 022; cat > "$1.new" && chmod 0644 "$1.new" && mv -f "$1.new" "$1"' sh "$NODE_CA"
      if [ -z "$configured" ]; then
        { echo "# Caddy signs Rynhult's certificate: its root, copied by deploy/install.sh (nodes and devices trust it)"
          echo "ACC_NODE_CA_FILE=$NODE_CA"; } >> /etc/default/accd
        systemctl restart accd.service
      fi
      echo "Caddy's root copied to $NODE_CA"
    fi
  else
    echo "WARNING: Caddy's root ($CADDY_ROOT) isn't there yet; run this again once Caddy is running" >&2
  fi
fi

echo "== done: Rynhult $(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml") at ${ORIGIN:-https://<your Rynhult host>}"
[ -f /srv/acc/data/secrets/setup_token ] && echo "   first sign-in: sudo cat /srv/acc/data/secrets/setup_token"
true

/usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role main
systemctl daemon-reload
