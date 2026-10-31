#!/usr/bin/env bash
# First-time setup of Rynhult on a Debian or Ubuntu machine: dependencies, this machine's settings
# (/etc/default/accd), then deploy/install.sh. Safe to re-run; it keeps settings that already exist.
#
#   git clone https://github.com/rynhult/rynhult /srv/acc/app
#   sudo /srv/acc/app/deploy/setup.sh
#
# It asks for each setting. To run unattended, set them in the environment and pass --yes:
#   ACC_OWNER        Linux user who owns Rynhult's projects (default: the user running sudo)
#   ACC_HOSTNAME     name people open Rynhult at, e.g. rynhult.example.com (default: rynhult.<zone>, or this host's name)
#   ACC_ZONE         Cloudflare DNS zone for HTTPS and publishing sites (optional; empty: Caddy's own certificates)
#   CF_API_TOKEN     Cloudflare token with Zone:DNS:Edit on ACC_ZONE (needed with ACC_ZONE)
#   ACC_PUBLIC_IP    bootstrap public target; owners add more in Settings → Publishing (optional)
#   ACC_INTERNAL_IP  LAN/VPN address of this machine: Rynhult itself and private-only sites (default: detected)
#   ACC_AGENT_CLIS   agent CLIs besides Claude Code, space-separated: codex copilot antigravity cursor opencode (default: codex)
#   ACC_DOCKER=no    skip rootless Docker for the owner (projects' compose stacks need it)
#   ACC_OLLAMA=yes   also install Ollama, a local model server agents can run on (default: no)
#   ACC_BACKUP=no    don't turn on the daily backup timer
# --dry-run prints the settings it would write and the steps it would take, and changes nothing (no root needed).
# --unqualified-distro allows a test install on Fedora/RHEL (dnf) openSUSE (zypper) or Arch (pacman), not qualified yet:
#   Podman for project stacks, Claude Code from Anthropic's pinned native release (docs/LINUX-DISTROS.md).
# deploy/acc-install asks these questions in dialogs, then runs this with --yes.
#
# Needs: systemd, apt, Python 3.12+ (Ubuntu 24.04+, Debian 13+), amd64 or arm64. Nothing here is specific to
# one machine; GPUs, disks and network are detected at run time by the host snapshot.
set -euo pipefail
# Only dry runs may use the caller's discovery PATH.
case " $* " in *" --dry-run "*) ;; *) export PATH=/usr/sbin:/usr/bin:/sbin:/bin ;; esac

acc_compat_lib="$(dirname "$0")/acc-env.sh"
[ -f "$acc_compat_lib" ] || acc_compat_lib="$(dirname "$0")/lib/acc-env.sh"
# shellcheck source=/dev/null
. "$acc_compat_lib"

APP=/srv/acc/app
REPO=$(cd "$(dirname "$0")/.." && pwd)
YES=0 DRY='' UNQUALIFIED=''
for arg in "$@"; do
  case $arg in
    --yes|-y) YES=1 ;;
    --dry-run) DRY=1 ;;
    --unqualified-distro) UNQUALIFIED=1 ;;
    -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $arg (try --help)" >&2; exit 2 ;;
  esac
done

[ -n "$DRY" ] || export PATH=/usr/sbin:/usr/bin:/sbin:/bin

log()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
ask() {  # ask VAR "question" default: keeps an existing value; prompts unless --yes
  local var=$1 question=$2 default=${3:-} answer
  [ -n "${!var:-}" ] && return
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then printf -v "$var" '%s' "$default"; return; fi
  printf '%s\n> ' "    $question${default:+ [$default]}:" >&2
  read -r answer
  printf -v "$var" '%s' "${answer:-$default}"
}
yes_no() { case ${1,,} in n|no|0|false|off) return 1 ;; *) return 0 ;; esac; }

# ------------------------------------------------------------------ checks --
# A dry run reports what a real run would stop on, and goes on.
check() { if [ -n "$DRY" ]; then info "WOULD FAIL: $1"; else die "$1"; fi; }
[ -z "$DRY" ] || log "Dry run: nothing is changed"
[ -n "$DRY" ] || [ "$(id -u)" = 0 ] || die "run as root: sudo $0 (or --dry-run)"
{ command -v systemctl >/dev/null && [ -d /run/systemd/system ]; } || check "Rynhult needs systemd"
# shellcheck source=deploy/lib/pkg.sh
source "$REPO/deploy/lib/pkg.sh"
PKG=$(pkg_manager || true)
if [ -z "$PKG" ]; then
  check "this script supports Debian and Ubuntu (apt); see README.md to install by hand"
elif ! pkg_qualified "$PKG"; then
  case $PKG:$UNQUALIFIED in
    dnf:1|zypper:1|pacman:1) info "note: $(pkg_system) ($PKG) isn't qualified yet; installing for a test (docs/LINUX-DISTROS.md)"
      export ACC_UNQUALIFIED_DISTRO=1 ;;  # deploy/install.sh's own check
    dnf:|zypper:|pacman:) check "$(pkg_system) uses $PKG: the main Rynhult supports only Debian and Ubuntu (apt) for now; a test install needs --unqualified-distro (docs/LINUX-DISTROS.md)" ;;
    *) check "$(pkg_system) uses $PKG: the main Rynhult supports only Debian and Ubuntu (apt) for now; see docs/LINUX-DISTROS.md" ;;
  esac
fi
mapfile -t PKG_INSTALL < <(pkg_install_command "${PKG:-apt}")
selinux=''
if command -v selinuxenabled >/dev/null && selinuxenabled; then selinux=1; fi
PRETTY_NAME=$(uname -s)
# shellcheck source=/dev/null
[ ! -r /etc/os-release ] || . /etc/os-release
ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
case $ARCH in x86_64) ARCH=amd64 ;; aarch64) ARCH=arm64 ;; esac  # the kernel's names where dpkg is absent
case $ARCH in amd64|arm64) ;; *) check "unsupported architecture $ARCH (amd64 or arm64)" ;; esac

# Rynhult's units run it from /srv/acc/app: set up from there (clone it there first if this checkout lives elsewhere).
if [ "$REPO" != "$APP" ] && [ -n "$DRY" ]; then
  info "would copy this checkout to $APP if absent; an existing installed checkout requires the documented Updating procedure"
  APP=$REPO
elif [ "$REPO" != "$APP" ]; then
  if [ ! -e "$APP" ]; then
    log "Copying this checkout to $APP"
    # The checkout is usually the user's own (git clone, then sudo); git won't clone another user's repository as
    # root, so copy it (history included) and make the copy root's: only root may change what Rynhult runs.
    if ! command -v rsync >/dev/null; then
      if [ "$PKG" = apt ]; then apt-get update -qq; fi
      "${PKG_INSTALL[@]}" rsync >/dev/null
    fi
    install -d -m 0755 "$(dirname "$APP")"
    rsync -a --exclude=.venv --exclude=web/node_modules --exclude=src/acc/server/static "$REPO/" "$APP/"
    chown -R root:root "$APP"
    chmod -R go-w "$APP"
  else
    installed_version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$APP/pyproject.toml" 2>/dev/null || true)
    source_version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml")
    if [ -z "$source_version" ] || [ "$installed_version" != "$source_version" ]; then
      die "Rynhult $installed_version is installed at $APP; this setup is $source_version. Follow the Guide's Updating page (Upgraded by hand), or use Settings → Updates. Nothing was changed."
    fi
    # A matching release may continue from its installed copy, but version strings
    # alone do not identify installer code in development/RC checkouts.
    for installer in deploy/setup.sh deploy/install.sh deploy/lib/install-node.sh; do
      if [ -f "$REPO/$installer" ] && ! cmp -s "$REPO/$installer" "$APP/$installer"; then
        die "The installed checkout at $APP has different installer code for version $source_version ($installer). Follow the Guide's Updating page (Upgraded by hand), or use Settings → Updates. Nothing was changed."
      fi
    done
  fi
  exec "$APP/deploy/setup.sh" "$@"
fi
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$APP/pyproject.toml")
log "Rynhult $VERSION setup on $(hostname) ($PRETTY_NAME, $ARCH)"

# ---------------------------------------------------------------- settings --
# A dry run may read another file (tests); a real run always uses /etc/default/accd.
ACCD=/etc/default/accd
[ -z "$DRY" ] || ACCD=${ACC_SETUP_TEST_ACCD:-$ACCD}
existing() { acc_setting_file "$1" "$ACCD"; }
if [ -f "$ACCD" ]; then
  # Root-only (0600) on an installed machine: without root a dry run can't know its settings.
  [ -r "$ACCD" ] || die "can't read $ACCD (only root can, on a machine that already has Rynhult): use sudo $0 --dry-run"
  info "keeping $ACCD (delete it to answer the questions again)"
  ACC_OWNER=$(existing ACC_SERVICES_ACCOUNT)
  ACC_ZONE=$(existing ACC_WILDCARD_ZONE)
  ACC_HOSTNAME=$(existing ACC_ALLOWED_ORIGINS | cut -d, -f1 | sed -E 's#^[a-z]+://##; s#[:/].*##')
else
  log "Settings"
  ask ACC_OWNER "Linux user who owns Rynhult's projects" "${SUDO_USER:-}"
  ask ACC_ZONE "Cloudflare DNS zone for HTTPS and published sites (blank for none)" ""
  ask ACC_HOSTNAME "Name to open Rynhult at" "${ACC_ZONE:+rynhult.$ACC_ZONE}"
  [ -n "$ACC_HOSTNAME" ] || ACC_HOSTNAME="$(hostname -s).local"
  detected=$( (tailscale ip -4 2>/dev/null || true) | head -1)
  [ -n "$detected" ] || detected=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')
  ask ACC_INTERNAL_IP "This machine's LAN/VPN address (Rynhult and private sites)" "$detected"
  [ -z "$ACC_ZONE" ] || ask ACC_PUBLIC_IP "Public address for published sites (blank: private sites only)" ""
  ask ACC_AGENT_CLIS "Agent CLIs besides Claude Code (codex, copilot, antigravity, cursor, opencode; space-separated)" "codex"
  ask ACC_OLLAMA "Install Ollama to run agents on local models? (yes/no)" "no"
fi
{ [ -n "${ACC_OWNER:-}" ] && [ "$ACC_OWNER" != root ]; } || die "ACC_OWNER must be a regular user (not root)"
id "$ACC_OWNER" >/dev/null 2>&1 || die "user $ACC_OWNER doesn't exist (create it, or set ACC_OWNER)"

accd_settings() {
  cat <<EOF
# Rynhult settings for this machine (written by deploy/setup.sh; see deploy/accd.env.example). Restart accd after edits.
ACC_ALLOWED_ORIGINS=https://$ACC_HOSTNAME
ACC_HOST_LABEL=$(hostname -s)
ACC_SERVICES_ACCOUNT=$ACC_OWNER
ACC_AGENT_HOME=/home/$ACC_OWNER-agent
ACC_BROKER_SOCKET_DIR=/run/acc-broker
ACC_WILDCARD_ZONE=${ACC_ZONE:-}
# Origin addresses bootstrap the Publishing inventory once; Settings manages subsequent targets.
ACC_ORIGIN_PUBLIC_IP=${ACC_PUBLIC_IP:-}
ACC_ORIGIN_INTERNAL_IP=${ACC_INTERNAL_IP:-}
ACC_AGENT_CLIS="${ACC_AGENT_CLIS:-codex}"
EOF
}
if [ -n "$DRY" ]; then
  if [ -f "$ACCD" ]; then
    info "would keep /etc/default/accd"
  else
    log "Would write /etc/default/accd"
    accd_settings | sed 's/^/    /'
  fi
  log "Would install (whatever isn't there yet), then run $APP/deploy/install.sh"
  if [ "$PKG" = apt ] || [ -z "$PKG" ]; then
    info "base packages (git, tmux, python3, sqlite3, …), Node.js 22+, uv, Claude Code"
    if yes_no "${ACC_DOCKER:-yes}"; then info "rootless Docker for $ACC_OWNER"; fi
  else
    info "base packages with $PKG (git, tmux, python3, sqlite, acl, …), the distribution's Node.js 22+, uv"
    [ "$PKG" != dnf ] || info "GitHub CLI from its official RPM repository, signing primary pinned (deploy/lib/gh-rpm-repo.sh)"
    if [ "$PKG" = dnf ] && pkg_enterprise_linux; then
      info "Caddy from its official package-scoped COPR repository, signing primary pinned"
    fi
    info "Claude Code from Anthropic's pinned native release (SHA-256 checked)"
    if yes_no "${ACC_DOCKER:-yes}"; then
      info "Podman for $ACC_OWNER (project stacks; $(pkg_name "$PKG" docker-compose))"
      if [ "$(pkg_name "$PKG" docker-compose)" = docker-compose-plugin ]; then
        info "Compose plugin from Docker's package-scoped RPM repository, signing primary pinned"
      fi
    fi
    [ -z "$selinux" ] || info "SELinux file contexts for Rynhult's paths (deploy/lib/selinux-labels.sh; the enforcing mode is never changed)"
  fi
  if yes_no "${ACC_OLLAMA:-no}"; then info "Ollama"; fi
  if [ -n "${ACC_ZONE:-}" ]; then
    token="missing, it would ask"
    if [ -f /etc/caddy/cloudflare.env ]; then token=kept; elif [ -n "${CF_API_TOKEN:-}" ]; then token=given; fi
    info "Caddy with the Cloudflare DNS module for $ACC_ZONE (Cloudflare token: $token)"
  else
    info "Caddy, signing Rynhult's certificate with its own CA"
  fi
  if yes_no "${ACC_BACKUP:-yes}"; then info "the daily backup timer"; fi
  log "Dry run done: nothing was changed"
  exit 0
fi

# Root never discovers tools in an invoking user's PATH or home.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
# shellcheck source=deploy/lib/trusted-tool.sh
source "$REPO/deploy/lib/trusted-tool.sh"
export TRUSTED_TOOL_UID=0

# ------------------------------------------------------------ dependencies --
export DEBIAN_FRONTEND=noninteractive
log "Base packages"
if [ "$PKG" = apt ]; then
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg git tmux python3 python3-venv jq sqlite3 acl rsync \
  smartmontools uidmap dbus-user-session gh >/dev/null
else
  base=(ca-certificates curl tar gnupg git tmux python3 jq sqlite3 acl rsync smartmontools uidmap)
  [ "$PKG" = dnf ] || base+=(gh)
  [ -z "$selinux" ] || base+=(policycoreutils-python-utils)  # semanage, restorecon
  mapfile -t names < <(for name in "${base[@]}"; do pkg_name "$PKG" "$name"; done)
  "${PKG_INSTALL[@]}" "${names[@]}" >/dev/null
  if [ "$PKG" = dnf ]; then
    sh "$REPO/deploy/lib/gh-rpm-repo.sh" || die "couldn't verify GitHub CLI's RPM repository"
    "${PKG_INSTALL[@]}" gh >/dev/null
  fi
fi
/usr/bin/python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 12))' || die "Rynhult needs Python 3.12+ (this system has $(/usr/bin/python3 -I -V))"
info "installed"

if [ "$PKG" = apt ]; then
log "Node.js (24 from NodeSource unless 22+ is already here)"
sh "$REPO/deploy/lib/install-node.sh" >/dev/null
else
  log "Node.js (the distribution's, 22 or newer)"
  "${PKG_INSTALL[@]}" "$(pkg_name "$PKG" nodejs)" "$(pkg_name "$PKG" npm)" >/dev/null
  node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 22 ? 1 : 0)' || die "Rynhult needs Node.js 22 or newer"
fi
info "node $(node --version)"

log "uv (Python package manager)"
UV=$(find_uv)
if [ -z "$UV" ]; then
  sh "$REPO/deploy/lib/install-uv.sh" >/dev/null
  UV=$(find_uv)
fi
[ -n "$UV" ] || die "no trusted system uv"
info "$("$UV" --version)"

log "Claude Code ($(if [ "$PKG" = apt ]; then echo "Anthropic's signed apt repository"; else echo "Anthropic's pinned native release, after Rynhult"; fi))"
if [ "$PKG" = apt ]; then
if ! dpkg -s claude-code >/dev/null 2>&1; then
  sh "$REPO/deploy/lib/claude-apt-repo.sh" || die "couldn't add Claude Code's apt repository"  # pinned key fingerprint
  apt-get update -qq && apt-get install -y -qq claude-code >/dev/null
fi
info "$(claude --version 2>/dev/null || echo claude-code)"
fi  # elsewhere acc-packages installs Anthropic's pinned native release once Rynhult is installed (below)
# Codex is installed at the Rynhult catalog pin by deploy/install.sh (docs/PROVIDERS.md).

if yes_no "${ACC_DOCKER:-yes}" && [ "$PKG" != apt ]; then
  # Podman, as Fedora and openSUSE ship it; project stacks run through `podman compose` with the compose v2
  # provider (acc.broker.ops.compose_command), on the owner's Podman socket.
  log "Podman for $ACC_OWNER (project stacks; rootless, no root daemon)"
  compose_package=$(pkg_name "$PKG" docker-compose)
  if [ "$compose_package" = docker-compose-plugin ]; then
    sh "$REPO/deploy/lib/docker-compose-rpm-repo.sh" || die "couldn't add Docker's Compose RPM repository"
  fi
  "${PKG_INSTALL[@]}" podman "$compose_package" >/dev/null
  grep -q "^$ACC_OWNER:" /etc/subuid || usermod --add-subuids 100000-165535 "$ACC_OWNER"
  grep -q "^$ACC_OWNER:" /etc/subgid || usermod --add-subgids 100000-165535 "$ACC_OWNER"
  loginctl enable-linger "$ACC_OWNER"
  uid=$(id -u "$ACC_OWNER")
  for _ in $(seq 15); do [ -d "/run/user/$uid" ] && break; sleep 1; done
  runuser -u "$ACC_OWNER" -- env XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    systemctl --user enable --now podman.socket >/dev/null \
    || info "WARNING: Podman's socket didn't start for $ACC_OWNER; projects' stacks won't start until it does"
  info "podman for $ACC_OWNER: $( [ -S "/run/user/$uid/podman/podman.sock" ] && echo ready || echo 'not running yet')"
elif yes_no "${ACC_DOCKER:-yes}"; then
  log "Rootless Docker for $ACC_OWNER (project stacks; no root daemon)"
  if ! dpkg -s docker-ce-rootless-extras >/dev/null 2>&1; then
    install -d -m 0755 /etc/apt/keyrings
    # Docker signing key: verify before replacing the live keyring.
    sh "$REPO/deploy/lib/install-apt-key.sh" "https://download.docker.com/linux/$ID/gpg"       /etc/apt/keyrings/docker.asc 9DC858229FC7DD38854AE2D88D81803C0EBFCD88
    echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
      docker-ce-rootless-extras >/dev/null
    systemctl disable --now docker.service docker.socket >/dev/null 2>&1 || true
  fi
  grep -q "^$ACC_OWNER:" /etc/subuid || usermod --add-subuids 100000-165535 "$ACC_OWNER"
  grep -q "^$ACC_OWNER:" /etc/subgid || usermod --add-subgids 100000-165535 "$ACC_OWNER"
  loginctl enable-linger "$ACC_OWNER"
  uid=$(id -u "$ACC_OWNER")
  for _ in $(seq 15); do [ -d "/run/user/$uid" ] && break; sleep 1; done
  if [ ! -S "/run/user/$uid/docker.sock" ]; then
    runuser -u "$ACC_OWNER" -- env XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
      PATH=/usr/bin:/usr/sbin:/bin:/sbin dockerd-rootless-setuptool.sh install >/dev/null \
      || info "WARNING: rootless Docker setup failed for $ACC_OWNER; projects' stacks won't start until it works"
  fi
  info "docker for $ACC_OWNER: $( [ -S "/run/user/$uid/docker.sock" ] && echo ready || echo 'not running yet')"
fi

if yes_no "${ACC_OLLAMA:-no}"; then  # installed (and kept updated) by acc-packages with the agent CLIs
  case " ${ACC_AGENT_CLIS:-codex} " in *" ollama "*) ;; *) ACC_AGENT_CLIS="${ACC_AGENT_CLIS:-codex} ollama" ;; esac
fi

# -------------------------------------------------------------------- HTTPS --
# Caddy from its own signed apt repository (distributions' packages can be too old for Rynhult's site config).
log "Caddy"
if [ "$PKG" != apt ]; then
  if [ "$PKG" = dnf ] && pkg_enterprise_linux; then
    sh "$REPO/deploy/lib/caddy-rpm-repo.sh" || die "couldn't verify Caddy's official COPR signing key"
  fi
  "${PKG_INSTALL[@]}" caddy >/dev/null  # the distribution's signed package (Fedora, openSUSE)
else
CADDY_KEYRING=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
CADDY_FINGERPRINT=65760C51EDEA2017CEA2CA15155B6D79CA56EA34
if [ ! -f /etc/apt/sources.list.d/caddy-stable.list ]; then
  # Verified in private staging (exactly one primary key, the pinned one) before the live keyring changes, like the
  # Docker and NodeSource keys (install and update review, IU-3).
  sh "$REPO/deploy/lib/install-apt-key.sh" https://dl.cloudsmith.io/public/caddy/stable/gpg.key "$CADDY_KEYRING" \
    "$CADDY_FINGERPRINT" || die "Caddy signing key refused (not exactly the pinned key)"
  echo "deb [signed-by=$CADDY_KEYRING] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
fi
apt-get install -y -qq caddy >/dev/null
fi
info "$(caddy version | cut -d' ' -f1)"
if [ -n "${ACC_ZONE:-}" ]; then
  # A zone needs the Cloudflare DNS module (one wildcard certificate via DNS-01): Caddy's build service compiles it.
  CF_CADDY=$(readlink -f -- /usr/local/bin/caddy || true)
  if ! { trusted_tool "$CF_CADDY" && "$CF_CADDY" list-modules 2>/dev/null | grep -q dns.providers.cloudflare; }; then
    info "downloading Caddy with the Cloudflare DNS module (built on request: can take a few minutes)"
    sh "$REPO/deploy/lib/install-caddy-cloudflare.sh" "$ARCH"
    CF_CADDY=$(readlink -f -- /usr/local/bin/caddy)
    trusted_tool "$CF_CADDY" || die "untrusted Caddy binary"
  fi
  info "$("$CF_CADDY" version | cut -d' ' -f1) with Cloudflare DNS, sha256 $(sha256sum "$CF_CADDY" | cut -c1-16)…"
  if [ ! -f /etc/caddy/cloudflare.env ]; then
    if [ -z "${CF_API_TOKEN:-}" ] && [ "$YES" = 0 ] && [ -t 0 ]; then
      printf '%s\n> ' "    Cloudflare API token (Zone:DNS:Edit on $ACC_ZONE; not shown):" >&2
      read -r -s CF_API_TOKEN; printf '\n' >&2
    fi
    [ -n "${CF_API_TOKEN:-}" ] || die "a Cloudflare token is needed for $ACC_ZONE (set CF_API_TOKEN)"
    # The header goes on stdin (printf is a builtin), never on curl's command line, which every local account can read
    # in /proc (install and update review, IU-4).
    found=$(printf 'Authorization: Bearer %s\n' "$CF_API_TOKEN" |
      curl -fsS --proto =https -H @- "https://api.cloudflare.com/client/v4/zones?name=$ACC_ZONE" \
      | jq -r '.result | length' 2>/dev/null || echo 0)
    [ "$found" = 1 ] || die "that token can't see the zone $ACC_ZONE"
    install -m 0600 /dev/null /etc/caddy/cloudflare.env
    printf 'CF_API_TOKEN=%s\n' "$CF_API_TOKEN" > /etc/caddy/cloudflare.env
    info "token checked and saved to /etc/caddy/cloudflare.env (root 0600)"
  fi
fi

# ------------------------------------------------------------- write config --
if [ ! -f /etc/default/accd ]; then
  log "Writing /etc/default/accd"
  accd_settings > /etc/default/accd
  chmod 0600 /etc/default/accd
  info "done"
fi

# ------------------------------------------------------------------ install --
log "Installing Rynhult"
"$APP/deploy/install.sh"
if [ "$PKG" != apt ] && [ ! -e /usr/local/bin/claude ]; then
  log "Claude Code (Anthropic's pinned native release)"
  /usr/local/sbin/acc-packages --install claude | sed 's/^/    /'
fi
if [ -n "${CF_API_TOKEN:-}" ] && [ ! -f /srv/acc/data/secrets/cloudflare_token ]; then
  /usr/bin/python3 -I "$REPO/deploy/lib/data-paths.py" check /srv/acc/data "$(id -u acc)"
  printf '%s' "$CF_API_TOKEN" | runuser -u acc -- sh -c 'umask 077; set -C; cat > /srv/acc/data/secrets/cloudflare_token'  # publishing uses it for DNS records
fi
if yes_no "${ACC_OLLAMA:-no}" && [ ! -f /etc/systemd/system/ollama.service.d/acc.conf ] && command -v ollama >/dev/null; then
  install -d -m 0755 /etc/systemd/system/ollama.service.d
  printf '[Service]\n# Agent CLIs send long instructions: give models room (Rynhult: Model endpoints, context window)\nEnvironment=OLLAMA_CONTEXT_LENGTH=32768\nEnvironment=OLLAMA_FLASH_ATTENTION=1\n' \
    > /etc/systemd/system/ollama.service.d/acc.conf
  systemctl daemon-reload && systemctl restart ollama
  info "Ollama is running. Pull a model (e.g. ollama pull qwen3-coder:30b), then add it in Rynhult: Providers → Model endpoints → Ollama."
fi
if yes_no "${ACC_BACKUP:-yes}" && ! systemctl is-enabled -q acc-backup.timer 2>/dev/null; then
  systemctl enable --now acc-backup.timer && info "daily backups on (to /var/backups/acc; see docs/OPERATIONS.md)"
fi

AGENT=$(basename "$(existing ACC_AGENT_HOME)")
ADDRESS=$(existing ACC_ORIGIN_INTERNAL_IP)
ADDRESS=${ADDRESS:-"this machine's LAN/VPN address"}
CERT_NOTE=""
if [ -z "${ACC_ZONE:-}" ]; then
  CERT_NOTE="     Without a zone, Caddy signs Rynhult's certificate itself: trust its root on your devices
     (sudo cat /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt) or accept the browser warning."
fi
cat <<EOF

==> Rynhult $VERSION is installed.

Next:
  1. DNS: point $ACC_HOSTNAME at $ADDRESS (a local DNS entry, or a DNS-only
     record in ${ACC_ZONE:-your DNS}). Rynhult answers private addresses only (LAN, VPN, Tailscale).
${CERT_NOTE}
  2. Open https://$ACC_HOSTNAME and set up the first user with this one-time token:
       sudo cat /srv/acc/data/secrets/setup_token
  3. Providers page: sign the agent account ($AGENT) in to Claude, Codex and GitHub.
  4. Optional: a GitHub token for repository views and creation in /srv/acc/data/secrets/github_token (acc 0600).
  5. Firewall: allow 80/443 to Caddy; keep 8000-8199 (project ports) to your LAN. Nothing here changes the firewall.

Updates later: sudo git -C $APP pull && sudo $APP/deploy/install.sh
EOF
