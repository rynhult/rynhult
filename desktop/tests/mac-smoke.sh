#!/bin/sh
# Owner-run on a logged-in Apple-silicon Mac with Xcode, uv, Rust/Tauri and web dependencies.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
root=$(dirname "$here")
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
    echo "mac-smoke.sh requires a logged-in macOS arm64 desktop; no Mac results can be produced on Linux." >&2
    exit 1
fi
export PATH="$HOME/.cargo/bin:$PATH"
test -d "$root/web/node_modules" || { echo "Prepare web dependencies first (cd web && npm ci)." >&2; exit 1; }
(cd "$root/web" && VITE_DESKTOP=1 npm run build -- --configLoader runner --outDir ../desktop/web-dist)
"$here/build-runtime.sh"
"$here/package.sh" --unsigned --app-only
app="$here/src-tauri/target/release/bundle/macos/Rynhult Mac spike.app"
"$here/runtime/python/bin/python3" -I "$here/tests/mac_smoke.py" \
    --binary "$app/Contents/MacOS/rynhult-desktop-spike" --artifacts "$root/.acc/artifacts" \
    --idle-seconds "${DESKTOP_IDLE_SECONDS:-300}"
