#!/bin/bash
# Add/remove only command aliases; never touch launchd or enrollment here.
case " $* " in
  *" --aliases-only "*|*" --rollback-aliases "*)
    [ "$(id -u)" != 0 ] || { echo 'run as yourself, not root' >&2; exit 1; }
    mac_repo=$(cd "$(/usr/bin/dirname "$0")/../../.." && pwd)
    mac_prefix="$HOME/Library/Application Support/ACC"
    mac_brew=$(command -v brew) || { echo 'Homebrew is required' >&2; exit 1; }
    mac_bin="$("$mac_brew" --prefix)/bin"
    exec "$mac_prefix/app/.venv/bin/python" -I "$mac_repo/deploy/lib/public-aliases.py" \
      --role macos --mac-prefix "$mac_prefix" --mac-bin "$mac_bin" "$@"
    ;;
esac

acc_compat_lib="$(dirname "$0")/acc-env.sh"
[ -f "$acc_compat_lib" ] || acc_compat_lib="$(dirname "$0")/../../lib/acc-env.sh"
# shellcheck source=/dev/null
. "$acc_compat_lib"
# Install or update a Rynhult node on a Mac: the broker and acc-node as LaunchAgents of the signed-in user (no separate
# agent account, no dashboard). Idempotent; run as yourself (not sudo) from a checkout of Rynhult, and again after an update:
#   deploy/node/macos/install.sh [--roots A:B] [--dry-run] [--render-dir DIR]
#   --roots A:B        folders sessions may work in (default: ~/acc-workspaces)
#   --dry-run          print every step and change nothing
#   --render-dir DIR   keep the rendered LaunchAgent plists in DIR (CI lints them)
# Needs Homebrew (https://brew.sh); installs python, tmux, git, gh and uv with it when missing.
# Everything lives in ~/Library/Application Support/ACC; logs in ~/Library/Logs/ACC. See docs/MULTIHOST.md.
# Written for macOS's bash 3.2.
set -euo pipefail

redact() { awk '{secret=ENVIRON["CF_API_TOKEN"]; if (length(secret)) { rest=$0; out=""; while ((at=index(rest, secret)) > 0) { out=out substr(rest, 1, at-1) "[redacted]"; rest=substr(rest, at+length(secret)); } $0=out rest; } print; }' | sed -E 's|([A-Za-z][A-Za-z0-9+.-]*://)[^/@[:space:]]*@|\1[redacted]@|g; s/(([Bb][Ee][Aa][Rr][Ee][Rr]|[Bb][Aa][Ss][Ii][Cc])[[:space:]]+).*/\1[redacted]/g; s/(([Tt][Oo][Kk][Ee][Nn]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Ss][Ee][Cc][Rr][Ee][Tt]|[Kk][Ee][Yy]|[Cc][Oo][Dd][Ee])[[:punct:]]?[[:space:]]*[:=][[:space:]]*).*/\1[redacted]/g; s/[[:xdigit:]]{6}(-[[:xdigit:]]{6}){3}/[redacted]/g'; }

# Keep native output as well as checks; do not persist single-use enrollment codes.
case " $* " in
  *" --dry-run "*|*" --help "*|*" -h "*) ;;
  *)
    ACC_INSTALL_LOG="$HOME/Library/Logs/ACC/install-node.log"
    LOGS=$(dirname "$ACC_INSTALL_LOG")
    LOG=$ACC_INSTALL_LOG
    prepare_log() {
      local parent=$LOGS
      while [ "$parent" != / ] && [ "$parent" != . ]; do
        [ ! -L "$parent" ] || { echo "unsafe log directory" >&2; return 1; }
        parent=$(dirname "$parent")
      done
      [ ! -L "$LOG" ] || { echo "unsafe log link" >&2; return 1; }
      (umask 077; mkdir -p "$LOGS") || return 1
      chmod 700 "$LOGS" || return 1
      local fresh
      fresh=$(umask 077; mktemp "$LOGS/.setup.XXXXXX") || return 1
      # Keep old diagnostics only when they are regular and privately owned by this user.
      if [ -f "$LOG" ] && [ -O "$LOG" ]; then redact <"$LOG" >"$fresh"; fi
      mv -f "$fresh" "$LOG"
    }
    prepare_log || exit 1
    exec 3>&1 4>&2
    # Bash 3.2 cannot reliably wait for a process-substitution PID. A private
    # FIFO feeds a normal background pipeline whose final process is our child.
    ACC_INSTALL_LOG_DIR=$(umask 077; mktemp -d "$LOGS/.logger.XXXXXX")
    ACC_INSTALL_LOG_PIPE="$ACC_INSTALL_LOG_DIR/output"
    if ! (umask 077; mkfifo "$ACC_INSTALL_LOG_PIPE"); then
      rmdir "$ACC_INSTALL_LOG_DIR"
      exit 1
    fi
    redact <"$ACC_INSTALL_LOG_PIPE" | tee -a "$ACC_INSTALL_LOG" &
    ACC_INSTALL_LOGGER=$!
    exec >"$ACC_INSTALL_LOG_PIPE" 2>&1
    install_exit() {
      local status=$?
      exec 1>&3 2>&4 3>&- 4>&-
      wait "$ACC_INSTALL_LOGGER" || true
      rm -f "$ACC_INSTALL_LOG_PIPE"
      rmdir "$ACC_INSTALL_LOG_DIR"
      if [ "$status" -ne 0 ]; then
        tail -n 12 "$ACC_INSTALL_LOG" | cut -c 1-300 >&2
        printf 'Full log: %s\n' "$ACC_INSTALL_LOG" >&2
      fi
      exit "$status"
    }
    trap install_exit EXIT
    ;;
esac

REPO=$(cd "$(dirname "$0")/../../.." && pwd)
ACC="$HOME/Library/Application Support/ACC"
LOGS="$HOME/Library/Logs/ACC"
AGENTS="$HOME/Library/LaunchAgents"
DRY='' RENDER='' ROOTS="$HOME/acc-workspaces"
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) DRY=1 ;;
    --roots) ROOTS=${2:?--roots needs folders}; shift ;;
    --render-dir) RENDER=${2:?--render-dir needs a folder}; shift ;;
    -h|--help) sed -n '2,9s/^# \{0,1\}//p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)" >&2; exit 1 ;;
  esac
  shift
done

run() { echo "+ $(printf '%q ' "$@")"; [ -n "$DRY" ] || "$@"; }
ok() { echo "  ok: $*"; }
problems=0
fail() { if [ -n "$DRY" ]; then echo "  WOULD FAIL: $*"; problems=$((problems + 1)); else echo "error: $*" >&2; exit 1; fi; }
q() { printf %q "$1"; }

[ -n "$DRY" ] && echo "(dry run: nothing is changed)"
[ "$(id -u)" != 0 ] || { echo "run as yourself, not root: the broker and acc-node run as the signed-in user" >&2; exit 1; }
ACCOUNT=$(id -un)
UID_NUM=$(id -u)

echo "== prerequisites"
if [ "$(uname -s)" = Darwin ]; then ok "macOS $(sw_vers -productVersion 2>/dev/null || true)"
else fail "this is the macOS installer (Linux: deploy/node/install.sh)"; fi
[[ $ACCOUNT =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "your short user name ($ACCOUNT) must be lowercase letters, digits, - or _"
socket="$ACC/sock/$ACCOUNT.sock"
[ ${#socket} -lt 104 ] || fail "socket path too long for macOS (${#socket} characters): $socket"
BREW=$(command -v brew || true)
for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -n "$BREW" ] || { [ -x "$candidate" ] && BREW=$candidate; }; done
if [ -n "$BREW" ]; then
  BREW_PREFIX=$("$BREW" --prefix)
  ok "Homebrew at $BREW_PREFIX"
else
  fail "Homebrew missing: install it from https://brew.sh, then run this again"
  BREW=brew BREW_PREFIX=/opt/homebrew
fi
PYTHON=$BREW_PREFIX/bin/python3
if [ -x "$PYTHON" ] && "$PYTHON" -I -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
  ok "python $("$PYTHON" -I -c 'import platform; print(platform.python_version())')"
else
  run "$BREW" install python
fi
if command -v tmux >/dev/null; then ok "tmux"; else run "$BREW" install tmux; fi
if command -v git >/dev/null && git --version >/dev/null 2>&1; then ok "git"; else run "$BREW" install git; fi
if command -v gh >/dev/null; then ok "gh"; else run "$BREW" install gh; fi
UV=$(command -v uv || true)
[ -n "$UV" ] || { [ -x "$BREW_PREFIX/bin/uv" ] && UV=$BREW_PREFIX/bin/uv; } || true
if [ -n "$UV" ]; then ok "uv at $UV"; else run "$BREW" install uv; UV=$BREW_PREFIX/bin/uv; fi

# Existing installations preflight alias conflicts before replacing app files.
if [ -d "$ACC" ]; then
  run "$PYTHON" -I "$REPO/deploy/lib/public-aliases.py" --role macos --mac-prefix "$ACC" --mac-bin "$BREW_PREFIX/bin" --dry-run
fi

echo "== folders"
run install -d -m 0700 "$ACC" "$ACC/sock" "$ACC/run" "$ACC/run/$ACCOUNT" "$ACC/node"
run install -d -m 0755 "$ACC/bin" "$ACC/catalog-local" "$LOGS"
for root in $(printf '%s' "$ROOTS" | tr ':' ' '); do
  if [ -d "$root" ]; then ok "workspace folder $root"; else run install -d -m 0750 "$root"; fi
done

echo "== Rynhult in $ACC/app"
if [ "$REPO" = "$ACC/app" ]; then
  ok "running from $ACC/app itself"
else
  # A fresh copy each time (removed files go too); the virtualenv stays so uv only applies what changed.
  run install -d -m 0755 "$ACC/app"
  run find "$ACC/app" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
  echo "+ tar -C $(q "$REPO") --exclude=.git --exclude=.venv --exclude=web … -cf - . | tar -C $(q "$ACC/app") -xf -"
  [ -n "$DRY" ] || tar -C "$REPO" --exclude=.git --exclude=.venv --exclude=web --exclude=__pycache__ \
    --exclude=.pytest_cache --exclude=.ruff_cache --exclude=node_modules -cf - . | tar -C "$ACC/app" -xf -
fi
# Match the main Rynhult's full source build even though the node's own copy omits web/.
if [ "$REPO" != "$ACC/app" ]; then
  echo "+ stamp full source build in $(q "$ACC/app/.acc-build")"
  [ -n "$DRY" ] || "$PYTHON" -I "$REPO/src/acc/bundle.py" --source "$REPO" --identity > "$ACC/app/.acc-build"
fi
echo "+ cd $(q "$ACC/app") && UV_PYTHON_DOWNLOADS=never $UV sync --frozen --no-dev --python $PYTHON"
[ -n "$DRY" ] || (cd "$ACC/app" && UV_PYTHON_DOWNLOADS=never "$UV" sync --frozen --no-dev --python "$PYTHON")
run install -m 0644 "$REPO/src/acc/env.py" "$ACC/bin/acc_env.py"
run install -m 0644 "$REPO/deploy/lib/acc-env.sh" "$ACC/bin/acc-env.sh"
# Linked shell commands resolve their sibling bridge through Homebrew's bin.
run install -m 0644 "$REPO/deploy/lib/acc-env.sh" "$BREW_PREFIX/bin/acc-env.sh"

run install -m 0755 "$REPO/bin/acc-hook" "$REPO/bin/acc-guard" "$REPO/bin/acc-mcp-team" "$REPO/bin/acc-statusline" "$REPO/bin/acc-wiki-check" "$REPO/bin/acc-request" \
  "$REPO/deploy/node/macos/acc-node" "$ACC/bin/"
run install -m 0644 "$REPO/src/acc/tooling.py" "$ACC/bin/acc-tool-policy.py"
# Their shebangs name /usr/bin/python3, which on a Mac is Apple's Python 3.9 (or a stub that offers to install the
# developer tools): too old for Rynhult, so the guard failed on every tool call and the CLIs refused the call.
for script in acc-guard acc-request acc-mcp-team; do
  run sed -i '' -e "1s|^#!/usr/bin/python3|#!$PYTHON|" -e "1s|^#!/usr/bin/env python3|#!$PYTHON|" "$ACC/bin/$script"
done
run acc_safe_link "$ACC/bin/acc-request" "$BREW_PREFIX/bin/acc-request"
run acc_safe_link "$ACC/bin/acc-node" "$BREW_PREFIX/bin/acc-node"
run acc_safe_link "$ACC/bin/acc-node" "$ACC/bin/rynhult-node"
run acc_safe_link "$ACC/bin/acc-node" "$BREW_PREFIX/bin/rynhult-node"
run acc_safe_link "$ACC/bin/acc-guard" "$ACC/bin/rynhult-guard"
run acc_safe_link "$ACC/bin/acc-guard" "$BREW_PREFIX/bin/rynhult-guard"
run acc_safe_link "$ACC/app/.venv/bin/rynhult" "$BREW_PREFIX/bin/rynhult"
run acc_safe_link "$ACC/app/.venv/bin/rynhultd" "$BREW_PREFIX/bin/rynhultd"
run acc_safe_link "$ACC/bin/acc-wiki-check" "$BREW_PREFIX/bin/acc-wiki-check"  # agents run it by name

# Journal new public names before any job activation; legacy hooks remain configured.
run "$PYTHON" -I "$REPO/deploy/lib/public-aliases.py" --role macos --mac-prefix "$ACC" --mac-bin "$BREW_PREFIX/bin"

echo "== LaunchAgents: local.acc.broker and local.acc.node"
STAGE=${RENDER:-$(mktemp -d "${TMPDIR:-/tmp}/acc-node.XXXXXX")}
mkdir -p "$STAGE"
path="$HOME/.local/bin:$BREW_PREFIX/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# render <job> <module> <json overrides>: the template with @NAME@ filled in, then the job's own keys.
render() {
  local renderer=$PYTHON
  [ -x "$renderer" ] || renderer=$(command -v python3)
  "$renderer" -I - "$REPO/deploy/node/macos/acc.plist.in" "$STAGE/local.acc.$1.plist" "$3" \
    "JOB=$1" "MODULE=$2" "PYTHON=$ACC/app/.venv/bin/python" "PATH=$path" "HOME=$HOME" "LOGS=$LOGS" <<'PY'
import json, plistlib, sys
template, out, overrides, *pairs = sys.argv[1:]
values = dict(pair.split("=", 1) for pair in pairs)
def fill(item):
    if isinstance(item, str):
        for key, value in values.items():
            item = item.replace(f"@{key}@", value)
        return item
    if isinstance(item, list):
        return [fill(i) for i in item]
    if isinstance(item, dict):
        return {k: fill(v) for k, v in item.items()}
    return item
with open(template, "rb") as f:
    plist = fill(plistlib.load(f))
extra = json.loads(overrides)
plist["ProgramArguments"] += extra.pop("args", [])
plist["EnvironmentVariables"].update(extra.pop("env", {}))
plist.update(extra)
with open(out, "wb") as f:
    plistlib.dump(plist, f)
PY
}
broker_env=$(printf '{"env": {"ACC_BROKER_SOCKET": "%s", "ACC_RUN_DIR": "%s", "ACC_ALLOWED_UIDS": "%s",
  "ACC_ALLOWED_ROOTS": "%s", "ACC_HOOK_PATH": "%s", "ACC_CATALOG_DIR": "%s", "ACC_CATALOG_LOCAL_DIR": "%s",
  "TERM": "tmux-256color"}}' "$socket" "$ACC/run/$ACCOUNT" "$UID_NUM" "$ROOTS" "$ACC/bin/acc-hook" \
  "$ACC/app/catalog" "$ACC/catalog-local")
# acc-node starts when enrolling has written its config, and stays up while it exists.
node_env=$(printf '{"args": ["run"], "env": {"ACC_NODE_CONFIG": "%s", "ACC_NODE_STATE_DIR": "%s",
  "ACC_NODE_SOCKET_DIR": "%s"}, "RunAtLoad": false, "KeepAlive": {"PathState": {"%s": true}}}' \
  "$ACC/acc-node.env" "$ACC/node" "$ACC/sock" "$ACC/acc-node.env")
render broker acc.broker.server "$broker_env"
render node acc.node "$node_env"
for job in broker node; do
  plist=local.acc.$job.plist
  if command -v plutil >/dev/null; then plutil -lint -s "$STAGE/$plist" || fail "rendered $plist does not lint"; fi
done
run install -d -m 0755 "$AGENTS"
for job in broker node; do
  plist=local.acc.$job.plist
  run install -m 0644 "$STAGE/$plist" "$AGENTS/$plist"
  [ -z "$DRY" ] || sed 's/^/    /' "$STAGE/$plist"
  # Reload so a new version takes effect. Sessions survive: their tmux server is outside the broker's job.
  echo "+ launchctl bootout gui/$UID_NUM/local.acc.$job (if loaded)"
  [ -n "$DRY" ] || launchctl bootout "gui/$UID_NUM/local.acc.$job" 2>/dev/null || true
  echo "+ launchctl bootstrap gui/$UID_NUM $(q "$AGENTS/$plist")"
  if [ -z "$DRY" ]; then  # bootout finishes asynchronously: retry briefly
    tries=0
    until launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$plist"; do
      tries=$((tries + 1)); [ $tries -lt 5 ] || fail "launchctl could not load $plist"; sleep 1
    done
  fi
done
[ -n "$RENDER" ] || rm -rf "$STAGE"

version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml")
echo "== done: Rynhult $version node; sessions run as $ACCOUNT"
if [ -f "$ACC/acc-node.env" ] && [ -n "$(acc_setting_file ACC_NODE_ID "$ACC/acc-node.env")" ]; then
  echo "   already enrolled; logs: $LOGS/node.log"
else
  echo "   Next, on the dashboard: Host → Machines → Add machine, name this Mac, then run the command it shows here:"
  echo "     acc-node enroll --primary https://<your Rynhult> --trust system --code <code> --name <name>"
  echo "   acc-node starts by itself once that succeeds (logs: $LOGS/node.log)."
fi
if [ "$problems" -gt 0 ]; then
  echo "dry run: $problems problem(s) above would stop a real install" >&2
  exit 2
fi
