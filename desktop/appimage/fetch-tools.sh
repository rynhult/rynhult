#!/bin/sh
# Fetch the tools Tauri's AppImage bundler runs, at pinned releases, into the checkout's local tools folder
# (bundle.useLocalToolsDir). Each download must match its digest below before anything uses it. AppRun and
# linuxdeploy match GitHub's published digests; linuxdeploy-plugin-appimage 1-alpha-20250213-1 publishes none, so its
# digest is first-seen (2026-10-25): re-verify it independently in release review. Tauri blanks linuxdeploy's AppImage
# magic (3 bytes at offset 8) before every build; doing the same here gives the stable hashes in tools.sha256, which
# desktop/package.sh --appimage checks before and after building. A mismatch removes the download and stops.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
tools="$here/src-tauri/target/.tauri"
# Check before mkdir/chmod/dd: neither cached tools nor their parents may redirect
# writes outside this checkout. A prior partial download needs explicit cleanup.
for path in "$here/src-tauri" "$here/src-tauri/target" "$tools"; do
    [ ! -L "$path" ] || { echo "Refusing linked AppImage cache: $path" >&2; exit 1; }
done
mkdir -p "$tools"
for name in AppRun-x86_64 linuxdeploy-07333c6-x86_64.AppImage linuxdeploy-plugin-appimage.AppImage; do
    if [ -L "$tools/$name" ] || [ -e "$tools/$name.part" ] || [ -L "$tools/$name.part" ]; then
        echo "Refusing linked tool or existing partial download: $name" >&2; exit 1
    fi
    if [ -e "$tools/$name" ]; then
        if [ ! -f "$tools/$name" ] || [ "$(stat -c %h -- "$tools/$name")" != 1 ]; then
            echo "Refusing nonregular or hardlinked AppImage tool: $name" >&2; exit 1
        fi
    fi
done
fetch() {  # name, url, sha256 of the download
    if [ ! -f "$tools/$1" ]; then
        curl -fsSL --proto '=https' -o "$tools/$1.part" "$2"
        if [ "$(sha256sum "$tools/$1.part" | cut -c1-64)" != "$3" ]; then
            rm -f "$tools/$1.part"
            echo "AppImage tool $1: checksum mismatch; nothing kept" >&2
            exit 1
        fi
        mv "$tools/$1.part" "$tools/$1"
    fi
    # An existing cache is input too. Accept the pinned download or already
    # normalized tool, but never mutate an unverified file before the final check.
    actual=$(sha256sum "$tools/$1" | cut -c1-64)
    normalized=$(awk -v name="$1" '$2 == name { print $1 }' "$here/appimage/tools.sha256")
    if [ "$actual" != "$3" ] && [ "$actual" != "$normalized" ]; then
        echo "Cached AppImage tool $1: checksum mismatch; unchanged" >&2
        exit 1
    fi
    chmod 0755 "$tools/$1"
}
fetch AppRun-x86_64 https://github.com/tauri-apps/binary-releases/releases/download/apprun-old/AppRun-x86_64 \
    f30140a43a0a59e46db21bdefdf749b9e9f2c6946e92afabbacf98b8ae73fb4f
fetch linuxdeploy-07333c6-x86_64.AppImage \
    https://github.com/tauri-apps/binary-releases/releases/download/linuxdeploy-07333c6/linuxdeploy-x86_64.AppImage \
    36a2d7e274d12e1050d0e9ecfe11d339ed54720b2bec464c286d53f8b07f5c62
fetch linuxdeploy-plugin-appimage.AppImage \
    https://github.com/linuxdeploy/linuxdeploy-plugin-appimage/releases/download/1-alpha-20250213-1/linuxdeploy-plugin-appimage-x86_64.AppImage \
    992d502a248e14ab185448ddf6f6e7d25558cb84d4623c354c3af350c25fccb3
dd if=/dev/zero bs=1 count=3 seek=8 conv=notrunc of="$tools/linuxdeploy-07333c6-x86_64.AppImage" 2>/dev/null
(cd "$tools" && sha256sum --strict -c "$here/appimage/tools.sha256")
