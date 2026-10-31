#!/bin/sh
# Docker's RHEL RPM source, limited to the Compose plugin used by Podman on EL.
# Pin source: https://docs.docker.com/engine/install/rhel/
# Require both RPM and repository metadata signatures.
set -eu
PIN=060A61C51B558A7F742B77AAC52FEB6B621E9F35
keyring=/etc/pki/rpm-gpg/RYNHULT-DOCKER.asc
repository=/etc/yum.repos.d/rynhult-docker-compose.repo
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
  https://download.docker.com/linux/rhel/gpg -o "$work/download.asc"
[ "$(wc -c < "$work/download.asc")" -le 1048576 ] || { echo 'Docker Compose key download is too large' >&2; exit 1; }
gpg_private --import "$work/download.asc" >/dev/null 2>&1 || { echo 'Invalid Docker Compose key download; repository unchanged' >&2; exit 1; }
# Export only the pinned primary; additional downloaded keys are never trusted.
gpg_private --armor --export "$PIN" > "$work/pinned.asc" 2>/dev/null
fp=$(gpg_private --with-colons --show-keys "$work/pinned.asc" 2>/dev/null | \
  awk -F: '/^pub:/ {primary=1; next} primary && /^fpr:/ {print $10; primary=0}')
[ "$fp" = "$PIN" ] || { echo 'Docker Compose signing key fingerprint mismatch; repository unchanged' >&2; exit 1; }
cat > "$work/rynhult-docker-compose.repo" <<'EOF'
[rynhult-docker-compose]
name=Docker Compose plugin for Podman
baseurl=https://download.docker.com/linux/rhel/$releasever_major/$basearch/stable
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RYNHULT-DOCKER.asc
includepkgs=docker-compose-plugin
EOF
# Nothing under /etc is touched before verification succeeds.
install -d -m 0755 "$(dirname "$keyring")" "$(dirname "$repository")"
publish "$work/pinned.asc" "$keyring"
publish "$work/rynhult-docker-compose.repo" "$repository"
