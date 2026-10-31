#!/bin/sh
# Anthropic's signed apt repository for Claude Code, the one place it's added: deploy/setup.sh (the main ACC),
# deploy/node/install.sh --install-prereqs, and acc-packages (installed beside it as acc-claude-apt-repo) when an owner
# installs Claude Code on a machine that doesn't have the repository yet. The key is fetched into private staging and
# its pinned primary fingerprint checked before anything under /etc/apt changes; a mismatch changes nothing.
#   sh claude-apt-repo.sh [keyring] [sources list]    (defaults below; the arguments are for tests)
set -eu
FINGERPRINT=31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE
KEY_URL=https://downloads.claude.ai/keys/claude-code.asc
REPOSITORY=https://downloads.claude.ai/claude-code/apt/stable
keyring=${1:-/etc/apt/keyrings/claude-code.asc}
list=${2:-/etc/apt/sources.list.d/claude-code.list}
command -v gpg >/dev/null || { echo "gpg is needed to check Claude Code's signing key: apt install gnupg" >&2; exit 1; }
work=$(mktemp -d)
gnupg=''
cleanup() {
  [ -z "$gnupg" ] || rm -rf -- "$gnupg"
  rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
# Independent of TMPDIR: GnuPG may consult an agent socket even for public keys.
gnupg=$(mktemp -d /tmp/acc-gpg.XXXXXXXXXX)
curl -fsSL --proto =https "$KEY_URL" -o "$work/key.asc"
# exactly one primary key, and it is the pinned one (as deploy/lib/verify-apt-key.sh)
fp=$(gpg --no-options --homedir "$gnupg" --batch --no-tty --no-autostart --show-keys --with-colons "$work/key.asc" 2>/dev/null | awk -F: '/^pub:/ {primary=1; next} primary && /^fpr:/ {print $10; primary=0}')
[ "$fp" = "$FINGERPRINT" ] || { echo "Claude Code signing key fingerprint mismatch (got ${fp:-none}); nothing changed" >&2; exit 1; }
printf 'deb [signed-by=%s] %s stable main\n' "$keyring" "$REPOSITORY" > "$work/claude-code.list"
install -d -m 0755 "$(dirname "$keyring")" "$(dirname "$list")"
install -m 0644 "$work/key.asc" "$keyring"
install -m 0644 "$work/claude-code.list" "$list"
