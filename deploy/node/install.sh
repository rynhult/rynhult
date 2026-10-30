#!/usr/bin/env bash
# Install or update a Rynhult node on Linux (Debian/Ubuntu, also inside WSL2; other distributions are not qualified yet,
# docs/LINUX-DISTROS.md): the per-account brokers and acc-node, no
# dashboard or Caddy. Idempotent; run as root from a checkout of Rynhult, and again after pulling an update:
#   sudo deploy/node/install.sh [--agent NAME] [--roots A:B] [--install-prereqs] [--update] [--dry-run]
#   --agent NAME        the agent account sessions on this node run as (default: the accounts it already has, else
#                       <you>-agent, created if missing)
#   --roots A:B         folders sessions may work in (default: each account's current ones, else /srv/acc/workspaces)
#   --install-prereqs   install missing prerequisites (python3, tmux, git with apt; a pinned, checksummed uv)
#   --unqualified-distro  allow a test install with dnf, zypper or pacman (not qualified: docs/LINUX-DISTROS.md)
#   --update            an update (acc-update): keep the agent accounts it has and never create one
#   --wsl-isolate       opt in: disable Windows interop and restrict Windows drives to root (WSL only)
#   --no-wsl-isolate    keep WSL settings unchanged, without prompting
#   --wsl-disable-automount  with --wsl-isolate: disable Windows drive automount instead of restricting it
#   --dry-run           print every step and change nothing (no root needed)
# Then enroll the node with the command the dashboard shows (Host → Machines → Add machine); see docs/MULTIHOST.md.
set -euo pipefail

# Phase B can be deployed independently of the normal installer: reload only,
# never run setup, account helpers or restart services. Dry-run plans aliases only.
case " $* " in
  *" --aliases-only "*|*" --rollback-aliases "*)
    alias_repo=$(cd "$(/usr/bin/dirname "$0")/../.." && pwd)
    /usr/bin/python3 -I "$alias_repo/deploy/lib/public-aliases.py" --role node "$@"
    case " $* " in *" --dry-run "*) ;; *) /usr/bin/systemctl daemon-reload ;; esac
    exit 0
    ;;
esac

case " $* " in *" --dry-run "*) ;; *) export PATH=/usr/sbin:/usr/bin:/sbin:/bin ;; esac

acc_compat_lib="$(dirname "$0")/acc-env.sh"
[ -f "$acc_compat_lib" ] || acc_compat_lib="$(dirname "$0")/../lib/acc-env.sh"
# shellcheck source=/dev/null
. "$acc_compat_lib"

# Keep native output as well as checks; do not persist single-use enrollment codes.
case " $* " in
  *" --dry-run "*|*" --help "*|*" -h "*) ;;
  *)
    ACC_INSTALL_LOG_DIR=$(mktemp -d /tmp/acc-node-install.XXXXXX)
    ACC_INSTALL_LOG="$ACC_INSTALL_LOG_DIR/install.log"
    mkdir -p "$(dirname "$ACC_INSTALL_LOG")"
    touch "$ACC_INSTALL_LOG"
    chmod 600 "$ACC_INSTALL_LOG"
    exec 3>&1 4>&2
    exec > >(awk '{secret=ENVIRON["CF_API_TOKEN"]; if (length(secret)) { rest=$0; out=""; while ((at=index(rest, secret)) > 0) { out=out substr(rest, 1, at-1) "[redacted]"; rest=substr(rest, at+length(secret)); } $0=out rest; } print; }' | sed -E 's|([A-Za-z][A-Za-z0-9+.-]*://)[^/@[:space:]]*@|\1[redacted]@|g; s/(([Bb][Ee][Aa][Rr][Ee][Rr]|[Bb][Aa][Ss][Ii][Cc])[[:space:]]+).*/\1[redacted]/g; s/(([Tt][Oo][Kk][Ee][Nn]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Ss][Ee][Cc][Rr][Ee][Tt]|[Kk][Ee][Yy]|[Cc][Oo][Dd][Ee])[[:punct:]]?[[:space:]]*[:=][[:space:]]*).*/\1[redacted]/g; s/[[:xdigit:]]{6}(-[[:xdigit:]]{6}){3}/[redacted]/g' | tee -a "$ACC_INSTALL_LOG") 2>&1
    ACC_INSTALL_LOGGER=$!
    install_exit() {
      local status=$?
      exec 1>&3 2>&4
      wait "$ACC_INSTALL_LOGGER" || true
      if [ "$status" -ne 0 ]; then
        tail -n 12 "$ACC_INSTALL_LOG" | cut -c 1-300 >&2
        printf 'Full log: %s\n' "$ACC_INSTALL_LOG" >&2
      fi
      exit "$status"
    }
    trap install_exit EXIT
    ;;
esac
REPO=$(cd "$(dirname "$0")/../.." && pwd)
PREFIX=/opt/acc            # fixed: the units in deploy/node/ point here
PYTHON=/usr/bin/python3
DRY='' PREREQS='' AGENT='' ROOTS='' UPDATE='' WSL_ISOLATE='' WSL_AUTOMOUNT='' UNQUALIFIED=''
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) DRY=1 ;;
    --install-prereqs) PREREQS=1 ;;
    --update) UPDATE=1 ;;
    --wsl-isolate)
      [ "$WSL_ISOLATE" != no ] || { echo "conflicting WSL isolation flags" >&2; exit 1; }
      WSL_ISOLATE=yes ;;
    --no-wsl-isolate)
      [ "$WSL_ISOLATE" != yes ] || { echo "conflicting WSL isolation flags" >&2; exit 1; }
      WSL_ISOLATE=no ;;
    --wsl-disable-automount) WSL_AUTOMOUNT=disabled ;;
    --agent) AGENT=${2:?--agent needs a name}; shift ;;
    --roots) ROOTS=${2:?--roots needs folders}; shift ;;
    --unqualified-distro) UNQUALIFIED=1 ;;
    -h|--help) sed -n '2,16s/^# \{0,1\}//p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)" >&2; exit 1 ;;
  esac
  shift
done
# The agent accounts this node already has: each broker settings file acc-agent-add wrote for an agents account.
# Reinstalling or updating keeps them (and their folders) instead of making up a new default account: an update
# runs as root without SUDO_USER, so the default would be a stranger (acc-agent).
BROKER_ENV=/etc/acc/broker
[ -n "$DRY" ] && BROKER_ENV=${ACC_INSTALL_TEST_BROKER_ENV:-$BROKER_ENV}  # tests only: where a dry run looks
KEEP=()  # "account roots" pairs
reader_args=()
[ -z "$DRY" ] || reader_args=(--dry-run --owner "$(/usr/bin/id -u)")
read_accounts() (
  # The reviewed checkout may belong to the operator; root imports only protected staged copies.
  stage=$(/usr/bin/mktemp -d /tmp/acc-node-reader.XXXXXX) || exit 1
  trap '/usr/bin/rm -f -- "$stage/node-accounts.py" "$stage/acc_env.py"; /usr/bin/rmdir -- "$stage"' EXIT
  /usr/bin/install -m 0755 "$REPO/deploy/lib/node-accounts.py" "$stage/node-accounts.py" || exit 1
  /usr/bin/install -m 0644 "$REPO/src/acc/env.py" "$stage/acc_env.py" || exit 1
  /usr/bin/python3 -I "$stage/node-accounts.py" "$BROKER_ENV" "${reader_args[@]}"
)
saved=$(read_accounts) || exit 1
while IFS= read -r pair; do [ -z "$pair" ] || KEEP+=("$pair"); done <<< "$saved"
if [ -n "$UPDATE" ] && [ -n "$AGENT" ]; then
  found=''
  for pair in "${KEEP[@]}"; do [ "${pair%% *}" != "$AGENT" ] || found=1; done
  [ -n "$found" ] || { echo "error: an update never creates or enables a new agent account" >&2; exit 1; }
fi
if [ -n "$AGENT" ]; then
  KEEP=("$AGENT ${ROOTS:-/srv/acc/workspaces}")
elif [ ${#KEEP[@]} -gt 0 ]; then
  if [ -n "$ROOTS" ]; then for i in "${!KEEP[@]}"; do KEEP[i]="${KEEP[i]%% *} $ROOTS"; done; fi
elif [ -n "$UPDATE" ]; then
  echo "error: this node has no agent account configured in $BROKER_ENV, and an update never creates one" >&2
  echo "       run sudo $0 --agent NAME once to set it up" >&2
  exit 1
else
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then AGENT=$SUDO_USER-agent; else AGENT=acc-agent; fi
  KEEP=("$AGENT ${ROOTS:-/srv/acc/workspaces}")
fi
for pair in "${KEEP[@]}"; do
  [[ ${pair%% *} =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { echo "bad agent account name" >&2; exit 1; }
  case ${pair%% *} in root|nobody|acc|acc-node|acc-build|acc-svc) echo "unsafe agent account" >&2; exit 1 ;; esac
  [[ ${pair#* } =~ ^[A-Za-z0-9_./:\ -]+$ ]] || { echo "unsafe workspace roots" >&2; exit 1; }
  if [ -z "$DRY" ] && id "${pair%% *}" >/dev/null 2>&1; then
    account_uid=$(id -u "${pair%% *}")
    if [ "$account_uid" -lt 1000 ] || [ "$account_uid" -eq 65534 ]; then echo "unsafe system agent account" >&2; exit 1; fi
  fi
  IFS=: read -r -a folders <<< "${pair#* }"
  [[ ${pair#* } != *: ]] || { echo "unsafe workspace roots" >&2; exit 1; }
  for folder in "${folders[@]}"; do
    [[ $folder =~ ^/[A-Za-z0-9_./\ -]+$ && $folder != *'//'* && $folder != */../* && $folder != */./* && $folder != */.. && $folder != */. ]] || {
      echo "unsafe workspace roots" >&2; exit 1;
    }
  done
  [ ${#folders[@]} -gt 0 ] || { echo "no folders configured" >&2; exit 1; }
done

# Every change goes through run (a command) or run_sh (a pipeline): --dry-run prints it instead.
run() { echo "+ $*"; [ -n "$DRY" ] || "$@"; }
run_sh() { echo "+ $1"; [ -n "$DRY" ] || bash -c "$1"; }
ok() { echo "  ok: $*"; }
problems=0
fail() { if [ -n "$DRY" ]; then echo "  WOULD FAIL: $*"; problems=$((problems + 1)); else echo "error: $*" >&2; exit 1; fi; }

[ -n "$DRY" ] && echo "(dry run: nothing is changed)"
[ -n "$DRY" ] || [ "$(id -u)" = 0 ] || { echo "run as root: sudo $0 (or --dry-run)" >&2; exit 1; }

WSL_CONF=/etc/wsl.conf
[ -n "$DRY" ] && WSL_CONF=${ACC_INSTALL_TEST_WSL_CONF:-$WSL_CONF}
wsl_args=(--path "$WSL_CONF")
if [ -n "$UPDATE" ]; then
  WSL_ISOLATE=no
  echo "--update never changes WSL isolation; existing settings kept."
else
  wsl=$(/usr/bin/python3 -I "$REPO/src/acc/wsl.py" --detect)
  if [ "$wsl" = 0 ] && { [ "$WSL_ISOLATE" = yes ] || [ -n "$WSL_AUTOMOUNT" ]; }; then
    echo "WSL isolation is only available inside WSL" >&2
    exit 1
  fi
  if [ "$wsl" != 0 ] && [ -z "$WSL_ISOLATE" ] && [ -t 0 ]; then
    echo "Dedicated WSL node: isolation disables Windows programs and restricts Windows drives to root."
    echo "It takes effect after wsl --shutdown from Windows, which stops all running WSL distributions."
    read -r -p "Opt into WSL isolation? [y/N] " answer || answer=''
    case $answer in
      y|Y|yes|YES)
        WSL_ISOLATE=yes
        read -r -p "Also disable Windows drive automount completely? [y/N] " answer || answer=''
        case $answer in y|Y|yes|YES) WSL_AUTOMOUNT=disabled ;; esac ;;
    esac
  fi
  if [ -n "$WSL_AUTOMOUNT" ]; then
    [ "$WSL_ISOLATE" = yes ] || { echo "--wsl-disable-automount requires --wsl-isolate" >&2; exit 1; }
    wsl_args+=(--disable-automount)
  fi
  if [ "$WSL_ISOLATE" = yes ]; then
    # Validate before the installer makes any system changes. Applying it rechecks the file.
    /usr/bin/python3 -I "$REPO/src/acc/wsl.py" "${wsl_args[@]}" --dry-run || exit 1
  else
    echo "WSL isolation is unchanged (opt in with --wsl-isolate on a dedicated WSL node)."
  fi
fi

# The node drop-ins change every acc-broker@ unit (socket group, ExecStart): never on the primary itself. Checked
# before the prerequisites, whose packages would already change the main Rynhult machine and restart its services.
if [ -e /etc/systemd/system/accd.service ]; then
  fail "this machine runs the Rynhult dashboard (accd.service); a node can't also be the primary (docs/MULTIHOST.md)"
fi

run /usr/bin/python3 -I "$REPO/deploy/lib/unit-aliases.py" --check --unit-dir /etc/systemd/system
run /usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role node --dry-run

echo "== prerequisites"
# uv runs as root here, so only a uv no other user can change counts: the file and every folder above it owned by
# root and not writable by group or others (sticky folders like /tmp aside), and nowhere in a home folder. A uv in
# ~/.local/bin is that user's, not root's: install one for the system (or use --install-prereqs).
OWNER=0
[ -n "$DRY" ] && OWNER=${ACC_INSTALL_TEST_OWNER:-0}  # tests only: whose files a dry run treats as root's
# shellcheck source=deploy/lib/trusted-tool.sh
source "$REPO/deploy/lib/trusted-tool.sh"
TRUSTED_TOOL_UID=$OWNER
# shellcheck source=deploy/lib/pkg.sh
source "$REPO/deploy/lib/pkg.sh"
PKG=$(pkg_manager || true)
if [ -z "$PKG" ]; then
  fail "$(pkg_system) has no package manager Rynhult knows (apt, dnf, zypper or pacman): see docs/LINUX-DISTROS.md"
  PKG=apt  # a dry run goes on with apt's names
elif ! pkg_qualified "$PKG"; then
  if [ -n "$UNQUALIFIED" ]; then
    echo "  note: $(pkg_system) ($PKG) isn't qualified yet; installing for a test (docs/LINUX-DISTROS.md)"
  else
    fail "$(pkg_system) uses $PKG, which Rynhult hasn't qualified yet (docs/LINUX-DISTROS.md); a test install needs --unqualified-distro"
  fi
fi
mapfile -t PKG_INSTALL < <(pkg_install_command "$PKG")
packages() { local name; for name in "$@"; do pkg_name "$PKG" "$name"; done; }
missing_apt=()
if [ -x "$PYTHON" ] && "$PYTHON" -I -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
  ok "python $("$PYTHON" -I -c 'import platform; print(platform.python_version())')"
else
  missing_apt+=(python3)
fi
for tool in tmux git; do
  if command -v "$tool" >/dev/null; then ok "$tool"; else missing_apt+=("$tool"); fi
done
# SELinux tooling is a prerequisite in its own right, even on a machine whose
# Python, tmux, git and uv are already installed. Never change enforcing mode.
SELINUX=''
if command -v selinuxenabled >/dev/null; then
  selinux_status=0
  selinuxenabled || selinux_status=$?
  case "$selinux_status" in
    0)
      SELINUX=1
      command -v semanage >/dev/null || missing_apt+=(policycoreutils-python-utils)
      command -v restorecon >/dev/null || missing_apt+=(policycoreutils)
      ;;
    1) ;; # selinuxenabled documents 1 as disabled, not an error.
    *) fail "cannot determine whether SELinux is enabled (selinuxenabled exit $selinux_status)" ;;
  esac
fi
UV=$(find_uv)
[ -n "$UV" ] && ok "uv at $UV"
if [ ${#missing_apt[@]} -gt 0 ] || [ -z "$UV" ]; then
  if [ -n "$PREREQS" ]; then
    command -v curl >/dev/null || missing_apt+=(curl ca-certificates)
    if [ ${#missing_apt[@]} -gt 0 ]; then
      refresh=$(pkg_refresh_command "$PKG")
      # shellcheck disable=SC2086  # a fixed command, split on purpose
      [ -z "$refresh" ] || run $refresh
      mapfile -t names < <(packages "${missing_apt[@]}")
      run "${PKG_INSTALL[@]}" "${names[@]}"
    fi
    [ -n "$UV" ] || run sh "$REPO/deploy/lib/install-uv.sh"  # a pinned release, checked by SHA-256
    UV=$(find_uv)
    [ -n "$UV" ] || [ -n "$DRY" ] || fail "uv still missing after installing it"
  else
    if [ ${#missing_apt[@]} -gt 0 ]; then
      mapfile -t names < <(packages "${missing_apt[@]}")
      fail "missing ${missing_apt[*]}: $(pkg_hint "$PKG") ${names[*]} (or rerun with --install-prereqs)"
    fi
    [ -n "$UV" ] || fail "uv missing (or not root's): sudo sh $REPO/deploy/lib/install-uv.sh (or rerun with --install-prereqs)"
  fi
  [ -n "$DRY" ] || "$PYTHON" -I -c 'import sys; sys.exit(sys.version_info < (3, 12))' || fail "Rynhult needs Python 3.12 or newer at $PYTHON"
fi
# Node.js 22+: what the agent CLIs installed with npm need (Host → Providers → Install on this machine). Not needed to
# relay sessions, so only installed with --install-prereqs; otherwise a note.
if node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 22 ? 1 : 0)' 2>/dev/null; then
  ok "node $(node --version)"
elif [ -n "$PREREQS" ] && [ "$PKG" = apt ]; then
  run sh "$REPO/deploy/lib/install-node.sh"
elif [ -n "$PREREQS" ]; then
  # The distribution's own Node.js (22 or newer on current Fedora, Tumbleweed and Arch); NodeSource's repository is apt's.
  mapfile -t names < <(packages nodejs npm)
  run "${PKG_INSTALL[@]}" "${names[@]}"
else
  echo "  note: Node.js 22 or newer isn't here, so agent CLIs can't be installed from the dashboard yet: sudo sh $REPO/deploy/lib/install-node.sh (or rerun with --install-prereqs)"
fi
# Claude Code comes from Anthropic's signed apt repository (pinned key fingerprint). acc-packages adds it when an owner
# installs Claude Code from the dashboard; --install-prereqs adds it now.
if [ "$PKG" != apt ]; then
  echo "  note: on $PKG systems Claude Code comes from Anthropic's pinned native release, when an owner installs it (Host → Providers)"
elif [ -e /etc/apt/sources.list.d/claude-code.list ]; then
  ok "Claude Code apt repository"
elif [ -n "$PREREQS" ]; then
  command -v gpg >/dev/null || run "${PKG_INSTALL[@]}" gnupg
  run sh "$REPO/deploy/lib/claude-apt-repo.sh"
fi
UV=${UV:-/usr/local/bin/uv}
if [ -d /run/systemd/system ]; then
  ok "systemd"
elif grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
  fail "systemd is not running in this WSL distribution: add [boot] systemd=true to /etc/wsl.conf, then wsl --shutdown (install.ps1 does this)"
else
  fail "systemd is not running (the brokers and acc-node are systemd units)"
fi

case $REPO/ in
  "$PREFIX/app/") ;;
  "$PREFIX"/src-*/) ;;  # unpacked by the bootstrap (GET /api/nodes/install.sh): kept, never replaced
  "$PREFIX"/*) fail "run this from a checkout outside $PREFIX (it is replaced by the copy)" ;;
esac

echo "== users and folders"
if id acc-node >/dev/null 2>&1; then
  ok "user acc-node"
else
  run useradd --system --user-group --no-create-home --home-dir /var/lib/acc-node --shell /usr/sbin/nologin acc-node
fi
run install -d -o root -g root -m 0755 "$PREFIX" "$PREFIX/bin" /srv/acc
run install -d -o root -g root -m 0755 "$PREFIX/catalog-local"
run install -d -o acc-node -g acc-node -m 0700 /var/lib/acc-node

echo "== Rynhult in $PREFIX/app"
if [ "$REPO" = "$PREFIX/app" ]; then
  ok "running from $PREFIX/app itself"
else
  # A fresh copy each time (removed files go too); the virtualenv stays so uv only applies what changed.
  run_sh "install -d -m 0755 $PREFIX/app && find $PREFIX/app -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +"
  run_sh "tar -C $(printf %q "$REPO") --exclude=.git --exclude=.venv --exclude=web --exclude=__pycache__ \
--exclude=.pytest_cache --exclude=.ruff_cache --exclude=node_modules -cf - . | tar -C $PREFIX/app --no-same-owner -xf -"
fi
# Stamp the full source before the installed node omits web/. Release bundles already carry this identity.
if [ "$REPO" != "$PREFIX/app" ]; then
  echo "+ stamp full source build in $PREFIX/app/.acc-build"
  [ -n "$DRY" ] || "$PYTHON" -I "$REPO/src/acc/bundle.py" --source "$REPO" --identity > "$PREFIX/app/.acc-build"
fi
# Root imports this code (enrolling) and the services run it: root's, and nobody else may write any of it, whatever
# modes the checkout had. That includes a virtualenv kept from before.
run chown -R -h root:root "$PREFIX/app"
# The copy takes the source folder's own mode (tar's "." entry): one unpacked under a private folder left /opt/acc/app
# 0700, and acc-node could no longer run its Python.
run chmod 0755 "$PREFIX/app"
run chmod -R go-w "$PREFIX/app"
run env UV_CACHE_DIR="$PREFIX/.uv-cache" UV_PYTHON_DOWNLOADS=never "$UV" sync --frozen --no-dev --python "$PYTHON" \
  --directory "$PREFIX/app"
run install -m 0644 "$REPO/src/acc/env.py" /usr/local/sbin/acc_env.py
run install -m 0644 "$REPO/deploy/lib/acc-env.sh" /usr/local/sbin/acc-env.sh
run install -m 0644 "$REPO/src/acc/env.py" "$PREFIX/bin/acc_env.py"
run install -m 0644 "$REPO/deploy/lib/acc-env.sh" "$PREFIX/bin/acc-env.sh"
run install -m 0644 "$REPO/deploy/lib/acc-env.sh" /usr/local/bin/acc-env.sh

run install -o root -g root -m 0755 "$REPO/bin/acc-hook" "$REPO/bin/acc-guard" "$REPO/bin/acc-mcp-team" "$REPO/bin/acc-statusline" \
  "$REPO/bin/acc-wiki-check" "$REPO/bin/acc-request" "$PREFIX/bin/"
run install -o root -g root -m 0644 "$REPO/src/acc/tooling.py" "$PREFIX/bin/acc-tool-policy.py"
run acc_safe_link "$PREFIX/bin/acc-request" /usr/local/bin/acc-request
run acc_safe_link "$PREFIX/bin/acc-wiki-check" /usr/local/bin/acc-wiki-check  # agents run it by name (acc-workflow skill)
run install -o root -g root -m 0755 "$REPO/deploy/node/acc-node" /usr/local/bin/acc-node
run acc_safe_link /usr/local/bin/acc-node /usr/local/bin/rynhult-node
run acc_safe_link "$PREFIX/bin/acc-guard" "$PREFIX/bin/rynhult-guard"
run acc_safe_link "$PREFIX/bin/acc-guard" /usr/local/bin/rynhult-guard
run acc_safe_link "$PREFIX/app/.venv/bin/rynhult" /usr/local/bin/rynhult
run acc_safe_link "$PREFIX/app/.venv/bin/rynhultd" /usr/local/bin/rynhultd

echo "== brokers: one per agent account, reachable only by acc-node"
run install -d -m 0755 /etc/acc/broker /etc/systemd/system/acc-broker@.socket.d /etc/systemd/system/acc-broker@.service.d
run install -m 0644 "$REPO/deploy/broker/acc-broker@.socket" "$REPO/deploy/broker/acc-broker@.service" /etc/systemd/system/
run install -m 0644 "$REPO/deploy/node/acc-broker@.socket.d-node.conf" /etc/systemd/system/acc-broker@.socket.d/node.conf
run install -m 0644 "$REPO/deploy/node/acc-broker@.service.d-node.conf" /etc/systemd/system/acc-broker@.service.d/node.conf
run install -o root -g root -m 0755 "$REPO/deploy/broker/acc-agent-add" "$REPO/deploy/broker/acc-agent-share" /usr/local/sbin/
run /usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role node
# Create writable service directories before restorecon, including the runtime
# parent otherwise first created by acc-agent-add. No service is activated yet.
run install -d -o root -g acc-node -m 0751 /srv/acc/run
run install -d -o root -g acc-node -m 0750 /srv/acc/updates /srv/acc/updates/results
run install -d -o acc-node -g acc-node -m 0750 /srv/acc/updates/requests
run install -d -o root -g acc-node -m 0750 /srv/acc/packages /srv/acc/packages/results
run install -d -o acc-node -g acc-node -m 0750 /srv/acc/packages/requests
# Restore labels after both executables and writable paths exist, before brokers
# or privileged request consumers start. Missing paths would otherwise be skipped.
if [ -n "$SELINUX" ]; then
  run sh "$REPO/deploy/lib/selinux-labels.sh" node
fi
run systemctl daemon-reload
# Creates the account if needed (no sudo, no docker), writes /etc/acc/broker/<account>.env and starts its broker.
# Accounts the node already has are refreshed with their own folders.
for pair in "${KEEP[@]}"; do
  run env ACC_AGENT_CLIENT=acc-node ACC_AGENT_PREFIX="$PREFIX" /usr/local/sbin/acc-agent-add "${pair%% *}" --roots "${pair#* }"
done

echo "== Rynhult updater (the primary supplies its version, build and checksum)"
run install -o root -g root -m 0755 "$REPO/deploy/updates/acc-update" /usr/local/sbin/acc-update
run install -m 0644 "$REPO/deploy/updates/acc-update.path" "$REPO/deploy/updates/acc-update.service" /etc/systemd/system/
run install -d -m 0755 /etc/systemd/system/acc-update.service.d
run install -m 0644 "$REPO/deploy/node/acc-update.service.d-node.conf" /etc/systemd/system/acc-update.service.d/node.conf
run /usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role node
run systemctl daemon-reload
run systemctl enable --now acc-update.path

echo "== agent software: acc-packages (the primary asks through acc-node; the same catalog and rules as on the primary)"
# acc-node only drops requests (a catalog name and an action) in requests/ and reads state.json; the root helper does
# the work, started by its path unit, plus a daily check.
run install -o root -g root -m 0755 "$REPO/deploy/packages/acc-packages" /usr/local/sbin/acc-packages
run install -o root -g root -m 0644 "$REPO/src/acc/tooling.py" /usr/local/sbin/acc-tool-policy.py
run install -o root -g root -m 0755 "$REPO/deploy/lib/claude-apt-repo.sh" /usr/local/sbin/acc-claude-apt-repo
run install -m 0644 "$REPO/deploy/packages/acc-packages.path" "$REPO/deploy/packages/acc-packages.service" \
  "$REPO/deploy/packages/acc-packages-daily.service" "$REPO/deploy/packages/acc-packages-daily.timer" /etc/systemd/system/
run install -d -m 0755 /etc/systemd/system/acc-packages.service.d /etc/systemd/system/acc-packages-daily.service.d
run install -m 0644 "$REPO/deploy/node/acc-packages.service.d-node.conf" /etc/systemd/system/acc-packages.service.d/node.conf
run install -m 0644 "$REPO/deploy/node/acc-packages.service.d-node.conf" /etc/systemd/system/acc-packages-daily.service.d/node.conf
run /usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role node
run systemctl daemon-reload
run systemctl enable --now acc-packages.path acc-packages-daily.timer

echo "== acc-node service"
run install -m 0644 "$REPO/deploy/node/acc-node.service" /etc/systemd/system/acc-node.service
run /usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role node
run systemctl daemon-reload
run systemctl enable acc-node.service
enrolled=''
[ -f /etc/acc-node.env ] && [ -n "$(acc_setting_file ACC_NODE_ID /etc/acc-node.env)" ] && enrolled=1
if [ -n "$enrolled" ]; then
  run systemctl restart acc-node.service  # picks up the new version
else
  ok "acc-node.service starts once the node is enrolled"
fi

if [ "$WSL_ISOLATE" = yes ] && [ -z "$DRY" ]; then
  run /usr/bin/python3 -I "$REPO/src/acc/wsl.py" "${wsl_args[@]}"
fi

version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml")
accounts=$(for pair in "${KEEP[@]}"; do printf '%s ' "${pair%% *}"; done)
echo "== done: Rynhult $version node, agent account(s) ${accounts% }"
if [ -n "$enrolled" ]; then
  echo "   already enrolled as $(acc_setting_file ACC_NODE_NAME /etc/acc-node.env) with $(acc_setting_file ACC_NODE_PRIMARY /etc/acc-node.env)"
  echo "   check: systemctl status acc-node"
elif [ -n "${ACC_INSTALL_ENROLLS:-}" ]; then
  echo "   deploy/acc-install enrolls it next."
else
  echo "   Next, on the dashboard: Host → Machines → Add machine, name this machine, then run the command it shows here:"
  echo "     sudo acc-node enroll --primary https://<your Rynhult> --trust system --code <code> --name <name>"
  echo "   It starts acc-node.service when it succeeds."
fi
if [ "$problems" -gt 0 ]; then
  echo "dry run: $problems problem(s) above would stop a real install" >&2
  exit 2
fi

run /usr/bin/python3 -I "$REPO/deploy/lib/public-aliases.py" --role node
run systemctl daemon-reload
