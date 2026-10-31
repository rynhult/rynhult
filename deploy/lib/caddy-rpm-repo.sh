#!/bin/sh
# Caddy's official COPR, restricted to caddy on Enterprise Linux.
# Source: https://caddyserver.com/docs/install#fedora-redhat-centos
# COPR signs RPMs, not repository metadata; downloaded keys must match the pin.
set -eu
PIN=4A76F92FF6D40440F8FC4F36C52191B1D605147E
keyring=/etc/pki/rpm-gpg/RYNHULT-CADDY.asc
repository=/etc/yum.repos.d/rynhult-caddy.repo
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
  https://download.copr.fedorainfracloud.org/results/@caddy/caddy/pubkey.gpg -o "$work/download.asc"
[ "$(wc -c < "$work/download.asc")" -le 1048576 ] || { echo 'Caddy key download is too large' >&2; exit 1; }
gpg_private --import "$work/download.asc" >/dev/null 2>&1 || { echo 'Invalid Caddy key download; repository unchanged' >&2; exit 1; }
# Export only the pinned primary; additional downloaded keys are never trusted.
gpg_private --armor --export "$PIN" > "$work/pinned.asc" 2>/dev/null
fp=$(gpg_private --with-colons --show-keys "$work/pinned.asc" 2>/dev/null | \
  awk -F: '/^pub:/ {primary=1; next} primary && /^fpr:/ {print $10; primary=0}')
[ "$fp" = "$PIN" ] || { echo 'Caddy signing key fingerprint mismatch; repository unchanged' >&2; exit 1; }
cat > "$work/rynhult-caddy.repo" <<'EOF'
[rynhult-caddy]
name=Caddy official COPR for Enterprise Linux
baseurl=https://download.copr.fedorainfracloud.org/results/@caddy/caddy/epel-$releasever_major-$basearch/
enabled=1
gpgcheck=1
repo_gpgcheck=0
gpgkey=file:///etc/pki/rpm-gpg/RYNHULT-CADDY.asc
includepkgs=caddy
EOF
# Nothing under /etc is touched before verification succeeds.
install -d -m 0755 "$(dirname "$keyring")" "$(dirname "$repository")"
publish "$work/pinned.asc" "$keyring"
publish "$work/rynhult-caddy.repo" "$repository"
