#!/usr/bin/env bash
# AppArmor allowances for sandboxes that need unprivileged user namespaces (deploy/apparmor), and the Codex sandbox
# post-check. Sourced by deploy/install.sh; the paths below are variables only so the tests can point them elsewhere.
ACC_USERNS_SYSCTL=${ACC_USERNS_SYSCTL:-/proc/sys/kernel/apparmor_restrict_unprivileged_userns}
ACC_SYSTEM_BWRAP=${ACC_SYSTEM_BWRAP:-/usr/bin/bwrap}
ACC_AGENT_PATH=${ACC_AGENT_PATH:-/usr/local/bin:/usr/bin:/bin}
ACC_CODEX_BWRAP_GLOB=${ACC_CODEX_BWRAP_GLOB:-/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-*/vendor/*/codex-resources/bwrap}

# Ubuntu 24.04's default: unconfined programs may not create user namespaces without a profile that allows it.
userns_restricted() {
  [ "$(cat "$ACC_USERNS_SYSCTL" 2>/dev/null)" = 1 ]
}

# The same profile, ignoring comments and whitespace: an installed copy that says the same is kept byte-identical.
profile_matches() {
  [ -f "$2" ] && [ "$(sed 's/#.*//' "$1" | tr -d '[:space:]')" = "$(sed 's/#.*//' "$2" | tr -d '[:space:]')" ]
}

install_profile() {
  local source=$1 target=$2
  profile_matches "$source" "$target" || install -m 0644 "$source" "$target"
  apparmor_parser -r "$target" || echo "WARNING: AppArmor profile $(basename "$target") not loaded (its sandbox may not work)" >&2
}

# The bubblewrap Codex runs: 0.163+ prefers the distribution's when it is installed, otherwise its bundled copy.
codex_bwrap() {
  local bundled
  if [ -x "$ACC_SYSTEM_BWRAP" ]; then
    echo "$ACC_SYSTEM_BWRAP (the distribution's bubblewrap; profile acc-system-bwrap)"
    return
  fi
  for bundled in $ACC_CODEX_BWRAP_GLOB; do
    [ -e "$bundled" ] && { echo "$bundled (Codex's bundled bubblewrap; profile acc-codex-bwrap)"; return; }
  done
  echo "no bubblewrap found (neither $ACC_SYSTEM_BWRAP nor Codex's bundled copy)"
}

# As the agent account: does Codex's sandbox run a command? A warning, never an install failure.
check_codex_sandbox() {
  local account=$1 home output
  command -v codex >/dev/null 2>&1 || return 0
  home=$(getent passwd "$account" | cut -d: -f6) || return 0
  [ -n "$home" ] || return 0
  # Agent-account code never gets root's terminal: a new session without a controlling tty, stdin from /dev/null.
  # The timeout runs inside that session, so it still ends the check's whole process group.
  if output=$(cd / && setsid --wait timeout 30 runuser -u "$account" -- env -i HOME="$home" PATH="$ACC_AGENT_PATH" \
      codex sandbox -- /bin/true </dev/null 2>&1); then
    echo "Codex sandbox: works for $account"
    return 0
  fi
  echo "WARNING: Codex's sandbox doesn't work for $account: 'codex sandbox -- /bin/true' failed" \
       "($(printf '%s\n' "$output" | tail -n 1 | cut -c1-200)). Codex used $(codex_bwrap)." \
       "Sandboxed Codex sessions will stop for approval on every command until that bubblewrap may create user" \
       "namespaces: check that its AppArmor profile is loaded (sudo aa-status | grep bwrap)." >&2
  return 0
}
