#!/bin/sh
# ACC's Linux bootstrap for an additional machine, served by the main ACC at GET /api/nodes/install.sh with its
# version filled in (docs/INSTALLER.md). Host → Machines → Add machine shows the line that downloads it and runs
#   sudo sh install.sh --primary https://ACC --trust system --code - --name N
# with the one-time code on stdin (--code -), so the code is on no command line (process list, sudo's log); it goes
# on, still on stdin, to deploy/acc-install. --code C still works. With a private CA it gets --ca-fingerprint HEX
# instead of --trust system, and checks the CA it downloads from the main ACC against it before trusting anything. It downloads this ACC's source (GET /api/nodes/bundle), checks
# that it is a gzip tar whose paths all stay inside acc-VERSION/, unpacks it to /opt/acc/src-VERSION and runs
# deploy/acc-install --node with the same values. --dry-run checks and unpacks into a temporary folder only, then
# runs acc-install --dry-run from there (no root needed).
set -eu
umask 022

VERSION='@VERSION@'
PREFIX=/opt/acc
PRIMARY='' TRUST='' FINGERPRINT='' CODE='' NAME='' DRY=''
die() { echo "Rynhult: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) DRY=1; shift; continue ;;
    --primary|--trust|--ca-fingerprint|--code|--name) [ $# -ge 2 ] || die "$1 needs a value" ;;
    *) die "unknown option $1" ;;
  esac
  case $1 in
    --primary) PRIMARY=$2 ;;
    --trust) [ "$2" = system ] || die "--trust must be system"; TRUST=system ;;
    --ca-fingerprint) TRUST=ca FINGERPRINT=$2 ;;
    --code) CODE=$2 ;;
    --name) NAME=$2 ;;
  esac
  shift 2
done
# acc-install checks every value's shape; these keep the URLs and the command line plain.
for value in "$PRIMARY" "$FINGERPRINT" "$CODE" "$NAME"; do
  case $value in *[!A-Za-z0-9./:-]*) die "unexpected characters in $value" ;; esac
done
case $PRIMARY in https://?*) PRIMARY=${PRIMARY%/} ;; *) die "--primary must be the main Rynhult's https:// address" ;; esac
{ [ -n "$TRUST" ] && [ -n "$CODE" ] && [ -n "$NAME" ]; } || die "needs --primary, --trust system or --ca-fingerprint, --code and --name"
[ -n "$DRY" ] || [ "$(id -u)" = 0 ] || die "run as root (with sudo)"
for tool in curl tar gzip; do command -v "$tool" >/dev/null || die "this needs $tool (sudo apt install $tool)"; done
# Before anything is downloaded or installed: the node installer's prerequisites (a newer Node.js, say) would
# otherwise change the main ACC machine and restart its services before the installer itself refused.
if [ -e /etc/systemd/system/accd.service ]; then
  msg="this machine runs the main Rynhult (accd.service): run this command on the machine you're adding, not here"
  if [ -n "$DRY" ]; then echo "WOULD STOP: $msg"; else die "$msg"; fi
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The SHA-256 of the certificate's DER, after rewriting the file to hold that one certificate alone (a file with
# a second one appended would otherwise be trusted for both).
fingerprint() {
  if command -v openssl >/dev/null; then
    openssl x509 -in "$1" -out "$2" 2>/dev/null || return 1
    openssl x509 -in "$2" -outform DER | sha256sum | cut -d' ' -f1
  elif command -v python3 >/dev/null; then
    python3 -I -c 'import hashlib, ssl, sys
der = ssl.PEM_cert_to_DER_cert(open(sys.argv[1]).read())
open(sys.argv[2], "w").write(ssl.DER_cert_to_PEM_cert(der))
print(hashlib.sha256(der).hexdigest())' "$1" "$2" 2>/dev/null
  else
    die "checking the CA needs openssl or python3"
  fi
}

if [ "$TRUST" = ca ]; then
  # Not verified yet: the fingerprint from Add machine decides whether it's the main ACC's CA.
  curl -fsS --proto =https -k --max-filesize 65536 "$PRIMARY/api/nodes/ca" -o "$work/served.pem" \
    || die "couldn't download the CA from $PRIMARY"
  got=$(fingerprint "$work/served.pem" "$work/ca.pem") || die "what $PRIMARY sent isn't a certificate"
  [ "$got" = "$(printf %s "$FINGERPRINT" | tr A-F a-f)" ] \
    || die "the CA from $PRIMARY has a different fingerprint than the command: stopping (nothing was installed)"
  set -- --cacert "$work/ca.pem"
else
  set --
fi
echo "Rynhult $VERSION: downloading from $PRIMARY"
curl -fsS --proto =https "$@" --max-filesize 268435456 "$PRIMARY/api/nodes/bundle" -o "$work/bundle.tar.gz" \
  || die "couldn't download Rynhult from $PRIMARY/api/nodes/bundle"

gzip -t "$work/bundle.tar.gz" 2>/dev/null || die "the download isn't a gzip file"
tar -tzf "$work/bundle.tar.gz" > "$work/paths" 2>/dev/null || die "the download isn't a tar archive"
tar -tvzf "$work/bundle.tar.gz" > "$work/listing" 2>/dev/null || die "the download isn't a tar archive"
[ -s "$work/paths" ] || die "the download is empty"
while IFS= read -r path; do
  case $path in
    "acc-$VERSION"|"acc-$VERSION/"*) ;;
    *) die "the download has a path outside acc-$VERSION/: $path" ;;
  esac
  case /$path/ in */../*|*/./*) die "the download has a path that leaves its folder: $path" ;; esac
done < "$work/paths"
# Regular files and folders only: no links (which could point anywhere), devices or the like.
if cut -c1 "$work/listing" | grep -qv '^[-d]$'; then die "the download has links or special files"; fi

if [ -n "$DRY" ]; then
  dest=$work/src-$VERSION target=$work/src-$VERSION
else
  install -d -o root -g root -m 0755 "$PREFIX"
  dest=$PREFIX/src-$VERSION target=$PREFIX/src-$VERSION.new
  rm -rf "$target"
fi
mkdir -m 0755 "$target"
tar -xzf "$work/bundle.tar.gz" -C "$target" --strip-components=1 --no-same-owner --no-same-permissions
[ -f "$target/deploy/acc-install" ] || die "the download has no deploy/acc-install"
if [ -z "$DRY" ]; then
  chown -R root:root "$target"
  chmod -R go-w "$target"
  rm -rf "$dest"
  mv "$target" "$dest"
fi
echo "Rynhult $VERSION unpacked to $dest"

if [ "$TRUST" = ca ]; then trust="--ca-fingerprint $FINGERPRINT"; else trust="--trust system"; fi
command="acc-node enroll --primary $PRIMARY $trust --code $CODE --name $NAME"
if [ -n "$DRY" ]; then
  bash "$dest/deploy/acc-install" --yes --dry-run --node "$command"
else
  bash "$dest/deploy/acc-install" --yes --node "$command"
fi
