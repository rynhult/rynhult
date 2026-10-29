#!/bin/bash
acc_compat_lib="$(dirname "$0")/acc-env.sh"
[ -f "$acc_compat_lib" ] || acc_compat_lib="$(dirname "$0")/../lib/acc-env.sh"
# shellcheck source=/dev/null
. "$acc_compat_lib"
# Rynhult Setup on a Mac: make this Mac an additional machine of an existing Rynhult, from the command the main Rynhult shows under
# Host → Machines → Add machine. "Rynhult Setup" in /Applications runs it step by step; you can too, as yourself (not sudo):
#   deploy/macos/acc-setup.sh run [--dry-run] 'acc-node enroll --primary https://… --trust system --code … --name …'
#   deploy/macos/acc-setup.sh parse 'COMMAND'             check a pasted command and print the values it holds
#   deploy/macos/acc-setup.sh check                       macOS, not root, Homebrew
#   deploy/macos/acc-setup.sh status                      "enrolled as NAME with PRIMARY" when this Mac already is
#   deploy/macos/acc-setup.sh install [--dry-run]         deploy/node/macos/install.sh
#   deploy/macos/acc-setup.sh enroll [--dry-run] 'COMMAND'
#   deploy/macos/acc-setup.sh self-test FILE              the parser against FILE (tests/data/enroll-commands.txt)
#   --log   real runs append everything to ~/Library/Logs/ACC/setup.log; with --log only there (Rynhult Setup.app),
#           and the last line, success or failure, goes to stdout or stderr
# The pasted command is parsed, never run: only its five values are used, each checked against its shape.
# Exit codes: 0 done, 1 failed, 2 not an Add machine command (or a dry run found problems), 3 Homebrew missing,
# 4 not macOS or run as root. Written for macOS's bash 3.2.
set -euo pipefail

SELF=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$SELF/../.." && pwd)
ACC="$HOME/Library/Application Support/ACC"
LOGS="$HOME/Library/Logs/ACC"
LOG="$LOGS/setup.log"

# ------------------------------------------------------------------ the parser (docs/INSTALLER.md, piece 1)
# Character classes, not ranges: ranges depend on the locale. Only the characters below get past the first check,
# so the regular expressions only ever see ASCII.
ALLOWED='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789./:_-'
LABEL='[[:alnum:]]([[:alnum:]-]{0,61}[[:alnum:]])?'
RE_PRIMARY="^https://$LABEL(\\.$LABEL)*(:[[:digit:]]{1,5})?/?\$"
RE_FINGERPRINT='^[[:xdigit:]]{64}$'
RE_CODE='^[[:xdigit:]]{6}-[[:xdigit:]]{6}-[[:xdigit:]]{6}-[[:xdigit:]]{6}$'  # acc.server.nodes.new_code
RE_NAME='^[[:lower:][:digit:]][[:lower:][:digit:]-]{0,39}$'                  # acc.server.nodes.NAME

P_PRIMARY='' P_TRUST='' P_FINGERPRINT='' P_CODE='' P_NAME='' P_ERROR=''

lower() { printf '%s' "$1" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz'; }
refuse() { P_ERROR=$1; return 1; }

# parse_enroll TEXT: sets P_PRIMARY, P_TRUST (system or ca), P_FINGERPRINT, P_CODE, P_NAME; or P_ERROR and returns 1.
# Accepts exactly `acc-node enroll` and the four options, each once, in any order, with plain values.
parse_enroll() {
  local text=$1 option value hostport port seen=' ' i=2 n
  local -a words
  P_PRIMARY='' P_TRUST='' P_FINGERPRINT='' P_CODE='' P_NAME='' P_ERROR=''
  # A paste often brings a newline or spaces around it; anything on another line is not part of the command.
  while :; do case $text in [[:space:]]*) text=${text#?} ;; *) break ;; esac; done
  while :; do case $text in *[[:space:]]) text=${text%?} ;; *) break ;; esac; done
  [ -n "$text" ] || refuse "nothing was pasted" || return
  [ ${#text} -le 600 ] || refuse "that is too long for an Add machine command" || return
  case $text in
    *[!"$ALLOWED"' 	']*) refuse "it holds characters an Add machine command never has (only letters, digits, spaces and ./:_-)" || return ;;
  esac
  IFS=$' \t' read -r -a words <<<"$text"
  n=${#words[@]}
  if [ "$n" -lt 2 ] || { [ "${words[0]}" != acc-node ] && [ "${words[0]}" != rynhult-node ]; } || [ "${words[1]}" != enroll ]; then
    refuse "it must start with rynhult-node enroll" || return
  fi
  while [ "$i" -lt "$n" ]; do
    option=${words[$i]}
    case $option in
      --primary|--trust|--ca-fingerprint|--code|--name) ;;
      *) refuse "unexpected $option (the command has --primary, --trust or --ca-fingerprint, --code and --name)" || return ;;
    esac
    case $seen in *" $option "*) refuse "$option is given twice" || return ;; esac
    seen="$seen$option "
    [ $((i + 1)) -lt "$n" ] || refuse "$option has no value" || return
    value=${words[$((i + 1))]}
    case $option in
      --primary)
        [[ $value =~ $RE_PRIMARY ]] || refuse "--primary must be https://host or https://host:port" || return
        hostport=${value#https://}
        hostport=${hostport%/}
        case $hostport in
          *:*) port=${hostport##*:}
               { [ "$port" -ge 1 ] && [ "$port" -le 65535 ]; } || refuse "--primary has a bad port" || return ;;
        esac
        P_PRIMARY=https://$hostport ;;
      --trust)
        [ "$value" = system ] || refuse "--trust must be system" || return
        P_TRUST=system ;;
      --ca-fingerprint)
        [[ $value =~ $RE_FINGERPRINT ]] || refuse "--ca-fingerprint must be 64 hexadecimal digits" || return
        P_FINGERPRINT=$(lower "$value")
        P_TRUST=ca ;;
      --code)
        [[ $value =~ $RE_CODE ]] || refuse "--code must look like 3fa21c-0b9d4e-77aa01-c3d2e1" || return
        P_CODE=$(lower "$value") ;;
      --name)
        [[ $value =~ $RE_NAME ]] || refuse "--name must be lowercase letters, digits and dashes (up to 40)" || return
        P_NAME=$value ;;
    esac
    i=$((i + 2))
  done
  case $seen in
    *" --trust "*) case $seen in *" --ca-fingerprint "*)
      refuse "use either --trust system or --ca-fingerprint, not both" || return ;; esac ;;
  esac
  [ -n "$P_PRIMARY" ] || refuse "--primary is missing" || return
  [ -n "$P_TRUST" ] || refuse "--trust system or --ca-fingerprint is missing" || return
  [ -n "$P_CODE" ] || refuse "--code is missing" || return
  [ -n "$P_NAME" ] || refuse "--name is missing" || return
}

# self_test FILE: every example in FILE (format: its header) parses as it says. Prints one line per example.
self_test() {
  local file=$1 line kind rest command expected got failures=0 count=0 tab=$'\t'
  [ -r "$file" ] || { echo "cannot read $file" >&2; return 1; }
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in '#'*|'') continue ;; esac
    kind=${line%%"$tab"*}
    rest=''
    case $line in *"$tab"*) rest=${line#*"$tab"} ;; esac  # "reject" alone: the empty command
    command=${rest%%"$tab"*}
    expected=''
    case $rest in *"$tab"*) expected=${rest#*"$tab"} ;; esac
    count=$((count + 1))
    if parse_enroll "$command"; then
      got="$P_PRIMARY $P_TRUST ${P_FINGERPRINT:--} $P_CODE $P_NAME"
      if [ "$kind" = accept ] && [ "$got" = "$expected" ]; then echo "ok   accept: $command"
      else echo "FAIL $kind, but parsed as: $got: $command"; failures=$((failures + 1)); fi
    else
      if [ "$kind" = reject ]; then echo "ok   reject ($P_ERROR): $command"
      else echo "FAIL accept, but refused ($P_ERROR): $command"; failures=$((failures + 1)); fi
    fi
  done <"$file"
  # Two that a file of single lines can't hold: a pasted newline is fine, a second line is not.
  local good='acc-node enroll --primary https://acc.example.lan --trust system --code 3fa21c-0b9d4e-77aa01-c3d2e1 --name m'
  count=$((count + 2))
  if parse_enroll "$good"$'\n'; then echo "ok   accept: a trailing newline"
  else echo "FAIL a trailing newline was refused ($P_ERROR)"; failures=$((failures + 1)); fi
  if parse_enroll "$good"$'\nrm -rf ~'; then echo "FAIL a second line was accepted"; failures=$((failures + 1))
  else echo "ok   reject ($P_ERROR): a second line"; fi
  echo "$count examples, $failures failed (bash $BASH_VERSION)"
  [ "$failures" -eq 0 ]
}

# ------------------------------------------------------------------ the steps
DRY='' LOG_ONLY='' LAST='' BREW_PREFIX='' LOG_ACTIVE=''
TMP_OUT=''
trap '[ -z "$TMP_OUT" ] || rm -f "$TMP_OUT"' EXIT

say() { echo "$*"; }
# done_ok / done_failed: the one line Rynhult Setup.app shows (with --log, the only output it gets).
done_ok() { echo "$*"; [ -z "$LOG_ONLY" ] || echo "$*" >&3; }
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

redact() { awk '{secret=ENVIRON["CF_API_TOKEN"]; if (length(secret)) { rest=$0; out=""; while ((at=index(rest, secret)) > 0) { out=out substr(rest, 1, at-1) "[redacted]"; rest=substr(rest, at+length(secret)); } $0=out rest; } print; }' | sed -E 's|([A-Za-z][A-Za-z0-9+.-]*://)[^/@[:space:]]*@|\1[redacted]@|g; s/(([Bb][Ee][Aa][Rr][Ee][Rr]|[Bb][Aa][Ss][Ii][Cc])[[:space:]]+).*/\1[redacted]/g; s/(([Tt][Oo][Kk][Ee][Nn]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Ss][Ee][Cc][Rr][Ee][Tt]|[Kk][Ee][Yy]|[Cc][Oo][Dd][Ee])[[:punct:]]?[[:space:]]*[:=][[:space:]]*).*/\1[redacted]/g; s/[[:xdigit:]]{6}(-[[:xdigit:]]{6}){3}/[redacted]/g'; }
done_failed() {
  local message="$*" context=''
  message=$(printf '%s\n' "$message" | redact)
  message=${message:0:600}
  # A failed preflight must point at a real log too; successful checks and dry runs write nothing.
  if [ -z "$DRY" ] && [ -z "$LOG_ACTIVE" ]; then
    if ! prepare_log; then printf 'Cannot safely open the setup log.\n' >&2; return; fi
    printf 'error: %s\n' "$message" | redact >>"$LOG"
  fi
  [ ! -f "$LOG" ] || context=$(tail -n 12 "$LOG" | redact | cut -c 1-300)
  printf 'error: %s\n%s\nFull log: %s\n' "$message" "$context" "$LOG" | redact >&2
  if [ -n "$LOG_ONLY" ]; then
    printf '%s\n%s\nFull log: %s\n' "$message" "$context" "$LOG" | redact >&4
  fi
}

# run_logged CMD…: run it with its output in the log, keeping its last line in LAST; returns its status.
run_logged() {
  local status=0
  TMP_OUT=$(mktemp "${TMPDIR:-/tmp}/acc-setup.XXXXXX")
  set +e
  # Not fds 3 and 4: Rynhult Setup.app waits until every holder of its pipe has closed it.
  "$@" 2>&1 3>&- 4>&- | redact | tee "$TMP_OUT"
  status=${PIPESTATUS[0]}
  set -e
  LAST=$(sed '/^[[:space:]]*$/d; s/^[[:space:]]*//' "$TMP_OUT" | tail -n 1 | cut -c 1-300)
  rm -f "$TMP_OUT"
  TMP_OUT=''
  return "$status"
}

find_brew() {
  local brew candidate
  brew=$(command -v brew || true)
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -n "$brew" ] || { [ -x "$candidate" ] && brew=$candidate; } || true
  done
  [ -n "$brew" ] || return 1
  BREW_PREFIX=$("$brew" --prefix)
  # Rynhult Setup.app starts with launchd's short PATH: put Homebrew's tools where install.sh looks for them.
  PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:$PATH"
}

do_check() {
  if [ "$(id -u)" = 0 ]; then
    done_failed "Run Rynhult Setup as yourself, not as root: Rynhult runs as the signed-in user on a Mac."; exit 4
  fi
  if [ "$(uname -s)" != Darwin ]; then
    done_failed "This is Rynhult Setup for macOS (on Linux: deploy/node/install.sh)."; exit 4
  fi
  if ! find_brew; then
    done_failed "Rynhult needs Homebrew, which installs the tools Rynhult uses (Python, tmux, git, uv). Open https://brew.sh,\
 copy the one command shown there into Terminal and follow it (it asks for your password), then open Rynhult Setup again."
    exit 3
  fi
  say "ok: macOS, user $(id -un), Homebrew at $BREW_PREFIX"
}

do_status() {
  local config="$ACC/acc-node.env" name primary
  [ -f "$config" ] && [ -n "$(acc_setting_file ACC_NODE_ID "$config")" ] || return 0
  name=$(acc_setting_file ACC_NODE_NAME "$config")
  primary=$(acc_setting_file ACC_NODE_PRIMARY "$config")
  echo "enrolled as ${name:-?} with ${primary:-?}"
}

do_install() {
  local status=0
  say "== installing the Rynhult node (deploy/node/macos/install.sh)"
  if [ -n "$DRY" ]; then run_logged "$REPO/deploy/node/macos/install.sh" --dry-run || status=$?
  else run_logged "$REPO/deploy/node/macos/install.sh" || status=$?; fi
  if [ "$status" -eq 2 ] && [ -n "$DRY" ]; then
    say "dry run: install.sh found problems a real install would stop on"; return 2
  elif [ "$status" -ne 0 ]; then
    done_failed "Installing failed: ${LAST:-install.sh exited with $status}"; exit 1
  fi
}

do_enroll() {
  local node="$ACC/bin/acc-node"
  if ! parse_enroll "$1"; then done_failed "That isn't the command from Add machine: $P_ERROR."; exit 2; fi
  set -- enroll --primary "$P_PRIMARY"
  if [ "$P_TRUST" = ca ]; then set -- "$@" --ca-fingerprint "$P_FINGERPRINT"; else set -- "$@" --trust system; fi
  set -- "$@" --code "$P_CODE" --name "$P_NAME"
  say "== enrolling with $P_PRIMARY as $P_NAME"
  # The one-time code stays out of the log.
  say "+ $(printf '%q ' "$node" "${@:1:$(($# - 4))}")--code <the code> --name $P_NAME"
  if [ -n "$DRY" ]; then
    say "+ launchctl kickstart -k gui/$(id -u)/local.acc.node"
    return 0
  fi
  [ -x "$node" ] || { done_failed "acc-node is not installed at $node: run the install step first."; exit 1; }
  if ! run_logged "$node" "$@"; then done_failed "Enrolling failed: ${LAST:-acc-node enroll failed}"; exit 1; fi
  # The LaunchAgent starts acc-node once the config exists; restart it so a re-enrollment takes effect now.
  say "+ launchctl kickstart -k gui/$(id -u)/local.acc.node"
  launchctl kickstart -k "gui/$(id -u)/local.acc.node" 3>&- 4>&- \
    || say "  (not loaded yet: launchd starts it by itself)"
  "$node" status 3>&- 4>&- || true
}

usage() { sed -n '2,15s/^# \{0,1\}//p' "$0"; }

main() {
  local command=${1:-} text='' have_text='' status=0 enrolled
  [ $# -eq 0 ] || shift
  while [ $# -gt 0 ]; do
    case $1 in
      --dry-run) DRY=1 ;;
      --log) LOG_ONLY=1 ;;
      -h|--help) usage; exit 0 ;;
      --) shift; [ $# -le 1 ] || { echo "one command only (quote it)" >&2; exit 1; }
          [ $# -eq 0 ] || { text=$1; have_text=1; }; break ;;
      *) [ -z "$have_text" ] || { echo "one command only (quote it)" >&2; exit 1; }
         text=$1; have_text=1 ;;
    esac
    shift
  done
  case $command in
    parse)
      if parse_enroll "$text"; then
        printf 'primary=%s\ntrust=%s\nfingerprint=%s\ncode=%s\nname=%s\n' \
          "$P_PRIMARY" "$P_TRUST" "$P_FINGERPRINT" "$P_CODE" "$P_NAME"
        exit 0
      fi
      echo "$P_ERROR" >&2; exit 2 ;;
    self-test) self_test "${text:?self-test needs the examples file}"; exit ;;
    status) do_status; exit 0 ;;
    check|install|enroll|run) ;;
    -h|--help|'') usage; [ -n "$command" ]; exit ;;
    *) echo "unknown command $command (see --help)" >&2; exit 1 ;;
  esac
  case $command in enroll|run) [ -n "$have_text" ] || { echo "$command needs the command from Add machine" >&2; exit 1; } ;; esac

  if [ -n "$DRY" ]; then
    LOG_ONLY=''
    echo "(dry run: nothing is changed)"
  elif [ "$command" != check ]; then  # successful checks write nothing; failures create a log
    prepare_log || exit 1
    LOG_ACTIVE=1
    if [ -n "$LOG_ONLY" ]; then exec 3>&1 4>&2 >>"$LOG" 2>&1
    else exec > >(tee -a "$LOG") 2> >(tee -a "$LOG" >&2); fi
    echo "=== Rynhult Setup $command, $(date '+%Y-%m-%d %H:%M:%S'), from $REPO"
  else
    LOG_ONLY=''
  fi
  # Check a pasted command before anything is installed.
  if [ -n "$have_text" ] && ! parse_enroll "$text"; then
    done_failed "That isn't the command from Add machine: $P_ERROR."; exit 2
  fi
  do_check
  case $command in
    check) exit 0 ;;
    install) do_install || status=$? ;;
    enroll) do_enroll "$text" ;;
    run) do_install || status=$?; do_enroll "$text" ;;
  esac
  [ "$status" -eq 0 ] || exit "$status"
  if [ -n "$DRY" ]; then done_ok "dry run done: nothing was changed"; return; fi
  case $command in
    install)
      enrolled=$(do_status)
      done_ok "Rynhult $(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml") is installed on this Mac${enrolled:+, $enrolled}.\
 Log: $LOG" ;;
    *) done_ok "This Mac is now the machine $P_NAME of $P_PRIMARY: it shows as connected under Host → Machines\
 in a minute. Log: $LOG" ;;
  esac
}

# Sourcing it (the tests) gives the functions without running anything.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
