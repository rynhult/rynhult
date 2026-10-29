#!/usr/bin/env bash
# Shared root-tool trust checks. Callers execute the resolved path after validation.
homes() {
  printf '%s\n' /root /home "$HOME"
  [ -z "${SUDO_USER:-}" ] || getent passwd "$SUDO_USER" | cut -d: -f6
}
trusted_tool() {
  local path real home owner mode
  path=$1
  case $path in /*) ;; *) return 1 ;; esac
  real=$(readlink -f -- "$path") || return 1
  [ -f "$real" ] && [ -x "$real" ] || return 1
  while IFS= read -r home; do
    if [ -n "$home" ] && [ "$home" != / ]; then
      case $real/ in "$home"/*) return 1 ;; esac
    fi
  done <<EOF_HOMES
$(homes)
EOF_HOMES
  while :; do
    read -r owner mode <<EOF_STAT || return 1
$(stat -c '%u %a' -- "$real")
EOF_STAT
    [ "$owner" = 0 ] || [ "$owner" = "${TRUSTED_TOOL_UID:-0}" ] || return 1
    # group/others may not write it; a sticky folder (/tmp) only lets them change their own entries
    if [ $((0$mode & 022)) -ne 0 ] && { [ ! -d "$real" ] || [ $((0$mode & 01000)) -eq 0 ]; }; then return 1; fi
    [ "$real" = / ] && return 0
    real=$(dirname -- "$real")
  done
}
find_uv() {
  local candidate resolved seen=' '
  for candidate in "$(command -v uv || true)" /usr/local/bin/uv /usr/bin/uv; do
    case $seen in *" $candidate "*) continue ;; esac
    seen="$seen$candidate "
    [ -e "$candidate" ] || continue
    # Resolve the candidate once: a caller-controlled link may change after validation.
    resolved=$(readlink -f -- "$candidate") || continue
    if trusted_tool "$resolved"; then echo "$resolved"; return 0; fi
    echo "  not using $candidate: root runs it, so it must be root's and outside home folders" >&2
  done
  return 0
}
