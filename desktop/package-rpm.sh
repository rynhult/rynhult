#!/bin/sh
# Repackage the .deb that desktop/package.sh built as an .rpm, with the same files at the same paths
# (desktop/rpm/rynhult-desktop.spec). A spike, not a release; not qualified on any RPM distribution yet
# (docs/LINUX-DISTROS.md). Needs rpmbuild (rpm-build) and ar (binutils). Writes only under desktop/package/rpm.
#   desktop/package-rpm.sh [path/to/package.deb]
set -eu
here=$(cd "$(dirname "$0")" && pwd)
deb=${1:-}
if [ -z "$deb" ]; then
    for candidate in "$here"/src-tauri/target/release/bundle/deb/*.deb; do deb=$candidate; done
fi
[ -f "$deb" ] || { echo "Build the .deb first (desktop/package.sh), or name one" >&2; exit 1; }
command -v rpmbuild >/dev/null || { echo "rpmbuild is missing: install rpm-build" >&2; exit 1; }
command -v ar >/dev/null || { echo "ar is missing: install binutils" >&2; exit 1; }
deb=$(cd "$(dirname "$deb")" && pwd)/$(basename "$deb")
work="$here/package/rpm"
rm -rf "$work"
mkdir -p "$work/deb" "$work/SOURCES/root"
(cd "$work/deb" && ar x "$deb")
data=$(ls "$work"/deb/data.tar.*)
tar -xf "$data" -C "$work/SOURCES/root"
# Own the package's own files and its resource folder, never the system folders around them (/usr/bin, ...).
(cd "$work/SOURCES/root" && {
    find usr/lib -mindepth 1 -maxdepth 1 -type d
    find . -path ./usr/lib -prune -o -type f -print -o -type l -print
} | sed 's#^\./##; s#^#/#; s#.*#"&"#') > "$work/SOURCES/files.list"
mkdir "$work/control"
tar -xf "$(ls "$work"/deb/control.tar.*)" -C "$work/control"
field() { sed -n "s/^$1: //p" "$work/control/control" | head -n 1; }
name=$(field Package) version=$(field Version)
# An RPM Version has no '-' or ':' (Debian revisions and epochs): refuse rather than guess.
case "$name" in ''|*[!a-z0-9.+-]*) echo "unexpected package name in $deb" >&2; exit 1 ;; esac
case "$version" in ''|*[!A-Za-z0-9.+~_]*) echo "unexpected package version in $deb (an RPM version has no - or :)" >&2; exit 1 ;; esac
rpmbuild --quiet --define "_topdir $work" --define "rynhult_name $name" --define "rynhult_version $version" \
    -bb "$here/rpm/rynhult-desktop.spec"
ls -l "$work"/RPMS/*/*.rpm
