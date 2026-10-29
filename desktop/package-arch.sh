#!/bin/bash
# Prepare a local PKGBUILD from the .deb produced by desktop/package.sh.
# No installs, downloads or makepkg here. Output must not exist: never overwrite another build.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
if [[ $# != 2 || ! -f $1 || -e $2 ]]; then
    echo "Usage: $0 package.deb NEW-output-directory" >&2
    exit 2
fi
deb=$(realpath "$1")
mkdir -p -- "$2"
work=$(realpath "$2")
mkdir "$work/deb" "$work/payload" "$work/control"
(cd "$work/deb" && ar x "$deb")
data=("$work"/deb/data.tar.*)
control=("$work"/deb/control.tar.*)
[[ ${#data[@]} == 1 && -f ${data[0]} && ${#control[@]} == 1 && -f ${control[0]} ]]
tar -xf "${control[0]}" -C "$work/control"
# Debian's 1.0.0~rc.1 is pacman's 1.0.0rc.1: pacman already sorts a trailing letter segment before the release.
version=$(sed -n 's/^Version: //p' "$work/control/control" | tr -d '~')
architecture=$(sed -n 's/^Architecture: //p' "$work/control/control")
name=$(sed -n 's/^Package: //p' "$work/control/control")
[[ $name == rynhult-linux-spike && $architecture == amd64 && $version =~ ^[0-9][A-Za-z0-9.+_]*$ ]] || {
    echo 'Expected rynhult-linux-spike amd64 with an Arch-compatible version' >&2; exit 1;
}
# Input is a locally built, trusted Tauri package, not an arbitrary downloaded archive.
tar -xf "${data[0]}" -C "$work/payload"
[[ -d $work/payload/usr && ! -L $work/payload/usr ]]
install -Dm644 "$here/../LICENSE" "$work/payload/usr/share/licenses/rynhult-linux-spike/LICENSE"
install -Dm644 "$here/../THIRD_PARTY_NOTICES.md" "$work/payload/usr/share/licenses/rynhult-linux-spike/THIRD_PARTY_NOTICES.md"
tar -czf "$work/rynhult-desktop.tar.gz" -C "$work/payload" usr
hash=$(sha256sum "$work/rynhult-desktop.tar.gz" | cut -d ' ' -f 1)
sed -e "s/@VERSION@/$version/g" -e "s/@SHA256@/$hash/g" "$here/arch/PKGBUILD" > "$work/PKGBUILD"
sha256sum "$work/PKGBUILD" "$work/rynhult-desktop.tar.gz"
printf 'Review PKGBUILD, then as an unprivileged user (Bash): cd %q && makepkg --cleanbuild\n' "$work"
