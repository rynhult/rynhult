#!/bin/sh
# Fetch the main Rynhult's current source for Windows node setup. Exit 10 only for an unreachable main.
# Usage: source.sh DEST PRIMARY system|ca FINGERPRINT
set -eu
dest=$1 PRIMARY=$2 TRUST=$3 FINGERPRINT=${4:-}
die() { echo "Rynhult: $*" >&2; exit 1; }
case $PRIMARY in https://?*) ;; *) die "primary must use https" ;; esac
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
fetch() {
  status=0
  curl -fsS --proto =https --connect-timeout 10 --max-time 120 "$@" || status=$?
  case $status in
    0) ;;
    5|6|7|28) echo "Rynhult: main Rynhult cannot be reached; current source unavailable (offline consent required)" >&2; exit 10 ;;
    *) die "download failed (curl $status); stopping" ;;
  esac
}
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
  fetch -k --max-filesize 65536 "$PRIMARY/api/nodes/ca" -o "$work/served.pem"
  got=$(fingerprint "$work/served.pem" "$work/ca.pem") || die "invalid CA certificate"
  [ "$got" = "$(printf %s "$FINGERPRINT" | tr A-F a-f)" ] || die "CA fingerprint mismatch"
  set -- --cacert "$work/ca.pem"
elif [ "$TRUST" = system ]; then
  set --
else
  die "unknown trust mode"
fi
fetch "$@" --max-filesize 268435456 "$PRIMARY/api/nodes/bundle" -o "$work/bundle.tar.gz"
# The main can be newer than the Windows release; derive the version from its authenticated archive.
VERSION=$(tar -tzf "$work/bundle.tar.gz" 2>/dev/null | head -n 1 | cut -d/ -f1)
case $VERSION in acc-?*) VERSION=${VERSION#acc-} ;; *) die "invalid bundle root" ;; esac
case $VERSION in *[!0-9A-Za-z.+-]*) die "invalid bundle version" ;; esac
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

mkdir -p "$work/tree"
tar -xzf "$work/bundle.tar.gz" -C "$work/tree" --strip-components=1 --no-same-owner --no-same-permissions
[ -f "$work/tree/deploy/node/install.sh" ] || die "download has no node installer"
[ ! -e "$dest" ] || die "destination already exists"
mv "$work/tree" "$dest"
echo "Rynhult $VERSION: downloaded current source from main Rynhult"
