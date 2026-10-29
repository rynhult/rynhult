#!/bin/sh
# Sign nested Python code before Tauri seals the outer app. "-" is local ad hoc only.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
runtime=${1:?runtime/python directory required}
identity=${2:?signing identity or - required}
[ "$(uname -s)" = Darwin ] || { echo "Runtime signing requires macOS" >&2; exit 1; }
test -x "$runtime/bin/python3" || { echo "Missing bundled Python" >&2; exit 1; }
set -- --force --sign "$identity"
if [ "$identity" != - ]; then
    set -- "$@" --timestamp
fi
find "$runtime" -type f \( -name '*.so*' -o -name '*.dylib' \) -exec codesign "$@" {} +
find "$runtime" -type f \( -name '*.so*' -o -name '*.dylib' \) -exec codesign --verify --strict {} +
if [ "$identity" != - ]; then
    set -- "$@" --options runtime --entitlements "$here/src-tauri/entitlements.macos.plist"
fi
codesign "$@" "$runtime/bin/python3"
codesign --verify --strict "$runtime/bin/python3"
