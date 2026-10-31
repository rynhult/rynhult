#!/bin/sh
# GitHub's official RPM repository, with an explicitly pinned signing primary.
# Render the reviewed gh-cli.repo endpoint rather than running dnf4/dnf5 config-manager.
# Rotation source: https://github.com/cli/cli/issues/13118 (September 2026).
set -eu
PIN=7F38BBB59D064DBCB3D84D725612B36462313325
keyring=/etc/pki/rpm-gpg/RYNHULT-GITHUB-CLI.asc
repository=/etc/yum.repos.d/gh-cli.repo
work=$(mktemp -d)
publish_tmp=''
gnupg=''
cleanup() {
  [ -z "$publish_tmp" ] || rm -f -- "$publish_tmp"
  [ -z "$gnupg" ] || rm -r -- "$gnupg"
  rm -r -- "$work"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
# Stage exclusively on the destination filesystem. A failed write/flush/rename
# must leave the previously published file intact. Root invokes these helpers.
publish() {
  publish_tmp=$(mktemp "$2.tmp.XXXXXXXXXX")
  install -o 0 -g 0 -m 0644 "$1" "$publish_tmp"
  if command -v sync >/dev/null 2>&1; then sync -f "$publish_tmp"; fi
  mv -fT -- "$publish_tmp" "$2"
  publish_tmp=''
}
# Keep GnuPG socket paths short even when TMPDIR is deeply nested.
# mktemp creates a private mode-0700 directory independently of TMPDIR.
gnupg=$(mktemp -d /tmp/acc-gpg.XXXXXXXXXX)
gpg_private() {
  /usr/bin/gpg --no-options --homedir "$gnupg" --batch --no-tty --no-autostart \
    --no-auto-key-retrieve --no-auto-key-import --auto-key-locate clear "$@"
}
curl -fsSL --proto =https --proto-redir =https --max-time 60 --max-filesize 1048576 \
  https://cli.github.com/packages/githubcli-archive-keyring.asc -o "$work/download.asc"
[ "$(wc -c < "$work/download.asc")" -le 1048576 ] || { echo 'GitHub CLI key download is too large' >&2; exit 1; }
gpg_private --import "$work/download.asc" >/dev/null 2>&1 || { echo 'Invalid GitHub CLI key download; repository unchanged' >&2; exit 1; }
# The upstream bundle includes the retired primary too. Export just the pinned
# successor; extra imported primaries never enter RPM's trusted key file.
gpg_private --armor --export "$PIN" > "$work/pinned.asc" 2>/dev/null
fp=$(gpg_private --with-colons --show-keys "$work/pinned.asc" 2>/dev/null | \
  awk -F: '/^pub:/ {primary=1; next} primary && /^fpr:/ {print $10; primary=0}')
[ "$fp" = "$PIN" ] || { echo 'GitHub CLI signing key fingerprint mismatch; repository unchanged' >&2; exit 1; }
cat > "$work/gh-cli.repo" <<EOF
[gh-cli]
name=packages for the GitHub CLI
baseurl=https://cli.github.com/packages/rpm
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file://$keyring
includepkgs=gh
EOF
# Nothing under /etc is touched before verification succeeds.
install -d -m 0755 "$(dirname "$keyring")" "$(dirname "$repository")"
publish "$work/pinned.asc" "$keyring"
publish "$work/gh-cli.repo" "$repository"
