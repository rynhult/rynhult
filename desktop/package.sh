#!/bin/sh
# Build the self-contained Linux .deb or macOS .app/.dmg spike (not a release).
# macOS: --unsigned (default), --signed (Developer ID + notarization), --app-only.
# Linux: --appimage builds an AppImage instead, only with the pinned tools desktop/appimage/fetch-tools.sh fetched
# (desktop/appimage/tools.sha256); desktop/package-rpm.sh turns the .deb into an RPM.
# Tauri host + bundled Python + main,
# built React assets, catalog and Guide. Needs desktop/build-runtime.sh first and
# a desktop web UI (desktop/web-dist). Writes only under desktop/.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
stage="$here/package/stage"
mode=unsigned
app_only=''
appimage=''
while [ $# -gt 0 ]; do
    case "$1" in
        --unsigned) mode=unsigned ;;
        --signed) mode=signed ;;
        --app-only) app_only=1 ;;
        --appimage) appimage=1 ;;
        *) echo "Usage: $0 [--unsigned|--signed] [--app-only] [--appimage]" >&2; exit 1 ;;
    esac
    shift
done
case "$(uname -s):$(uname -m)" in
    Linux:x86_64)
        if [ "$mode" != unsigned ] || [ -n "$app_only" ]; then
            echo "These package options require macOS" >&2; exit 1
        fi
        python_os=linux
        python_arch=x86_64
        # The packages carry the project's version (pyproject.toml), not tauri.conf.json's placeholder.
        version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$root/pyproject.toml" | head -n 1)
        printf '%s\n' "$version" | grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?' ||
            { echo "pyproject.toml's version '$version' is not major.minor.patch[-prerelease]" >&2; exit 1; }
        # Package names and paths without spaces; the menu still shows "Rynhult Linux spike" (its desktop template).
        set -- --config tauri.package.conf.json --config tauri.package.linux.conf.json --config "{\"version\": \"$version\"}"
        if [ -n "$appimage" ]; then
            set -- "$@" --config tauri.appimage.conf.json --bundles appimage
        fi ;;
    Darwin:arm64)
        [ -z "$appimage" ] || { echo "--appimage requires Linux" >&2; exit 1; }
        python_os=darwin
        python_arch=arm64
        export MACOSX_DEPLOYMENT_TARGET=13.0
        bundles=app,dmg
        [ -z "$app_only" ] || bundles=app
        set -- --config tauri.package.conf.json --config tauri.macos.conf.json --bundles "$bundles"
        if [ "$mode" = unsigned ]; then
            unset APPLE_SIGNING_IDENTITY APPLE_CERTIFICATE APPLE_CERTIFICATE_PASSWORD \
                  APPLE_API_ISSUER APPLE_API_KEY APPLE_API_KEY_PATH APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID
            set -- "$@" --no-sign
            echo "Local unsigned spike: no Developer ID signature or notarization."
        else
            if [ -z "${APPLE_SIGNING_IDENTITY:-}" ] || [ "$APPLE_SIGNING_IDENTITY" = - ]; then
                echo "--signed requires APPLE_SIGNING_IDENTITY (Developer ID Application)" >&2; exit 1;
            fi
            if [ -n "${APPLE_API_ISSUER:-}" ] && [ -n "${APPLE_API_KEY:-}" ] && [ -f "${APPLE_API_KEY_PATH:-}" ]; then
                unset APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID
            elif [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_PASSWORD:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ]; then
                unset APPLE_API_ISSUER APPLE_API_KEY APPLE_API_KEY_PATH
            else
                echo "--signed requires complete notarization environment variables; see desktop/README.md" >&2
                exit 1
            fi
        fi ;;
    *) echo "Supported package hosts: Linux x86_64 and macOS arm64" >&2; exit 1 ;;
esac
test -x "$here/runtime/python/bin/python3" || { echo "Run desktop/build-runtime.sh first" >&2; exit 1; }
test -f "$here/web-dist/index.html" || { echo "Build VITE_DESKTOP=1 web UI into desktop/web-dist first" >&2; exit 1; }
"$here/runtime/python/bin/python3" -I -c \
    'import platform,sys; actual=[sys.platform,platform.machine()]; expected=sys.argv[1:]; sys.exit(0 if actual==expected else "Rebuild desktop/runtime for this native host")' \
    "$python_os" "$python_arch"
# Keep all build output in this checkout, including when the caller uses a shared Cargo cache.
export CARGO_TARGET_DIR="$here/src-tauri/target"
tools="$CARGO_TARGET_DIR/.tauri"  # bundle.useLocalToolsDir: the AppImage tools Tauri uses, never its shared cache
if [ -n "$appimage" ]; then
    # Tauri downloads a missing tool (one of them from a moving "continuous" release): never let it.
    (cd "$tools" 2>/dev/null && sha256sum --quiet --strict -c "$here/appimage/tools.sha256") ||
        { echo "Fetch the pinned AppImage tools first: desktop/appimage/fetch-tools.sh" >&2; exit 1; }
fi
cleanup() {
    rm -rf "$stage" "$here/src-tauri/target/release/desktop" "$here/src-tauri/target/release/src" \
           "$here/src-tauri/target/release/docs" "$here/src-tauri/target/release/catalog" "$here/src-tauri/target/release/bin"
}
trap cleanup EXIT
rm -rf "$stage"
mkdir -p "$stage/desktop" "$stage/src" "$stage/docs" "$stage/bin"
cp "$here/main.py" "$here/execution.py" "$here/broker.py" "$here/setup.py" "$here/mac_setup.py" "$here/mac_broker.py" \
    "$here/mac_gate.py" "$here/mac_keychain.py" "$here/mac_launchd.py" "$here/mac_tmux.py" "$stage/desktop/"
cp -a "$here/web-dist" "$stage/desktop/web-dist"
cp "$root"/bin/acc-* "$stage/bin/"
cp "$root/deploy/lib/acc-env.sh" "$stage/bin/acc-env.sh"
cp -a "$here/runtime" "$stage/desktop/runtime"
rsync -a --exclude __pycache__ --exclude static "$root/src/acc" "$stage/src/"
rsync -a --exclude __pycache__ "$root/catalog" "$stage/"
rsync -a "$root/docs/wiki" "$stage/docs/"
cp "$root/LICENSE" "$root/THIRD_PARTY_NOTICES.md" "$stage/docs/"
# Every module the package ships, the bundled runtime's standard library included, compiled now (-f replaces the
# runtime's timestamp-checked caches): nothing is written beside them when the app, or root, first imports them.
"$here/runtime/python/bin/python3" -I -m compileall -q -f -j 0 --invalidation-mode unchecked-hash "$stage/src" "$stage/desktop" >/dev/null
# Installed resources satisfy the protected-tree checks.
chmod -R a+rX,go-w "$stage"
if [ "$python_os" = darwin ]; then
    identity=-
    [ "$mode" = unsigned ] || identity=$APPLE_SIGNING_IDENTITY
    "$here/sign-runtime-macos.sh" "$stage/desktop/runtime/python" "$identity"
fi
cd "$here/src-tauri"
cargo tauri build "$@" -- --locked
if [ -n "$appimage" ]; then
    (cd "$tools" && sha256sum --quiet --strict -c "$here/appimage/tools.sha256") ||
        { echo "An AppImage tool changed during the build: discard this AppImage" >&2; exit 1; }
    find target/release/bundle/appimage -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
    ls -l target/release/bundle/appimage/*.AppImage
elif [ "$python_os" = linux ]; then
    # Only keep the package, not duplicate unpacked wikis.
    find target/release/bundle/deb -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
    case "$version" in *-*)
        # Tauri writes 1.0.0-rc.1, which Debian reads as upstream 1.0.0, revision rc.1: newer than 1.0.0. The Debian
        # form 1.0.0~rc.1 sorts before it (the RPM, made from this package, keeps the ~; Arch drops it).
        deb_version=$(printf '%s' "$version" | sed 's/-/~/')
        built=target/release/bundle/deb/rynhult-linux-spike_${version}_amd64.deb
        repack=$(mktemp -d "$CARGO_TARGET_DIR/deb-version.XXXXXX")
        dpkg-deb -R "$built" "$repack/package"
        sed -i "s/^Version: .*/Version: $deb_version/" "$repack/package/DEBIAN/control"
        dpkg-deb --root-owner-group -b "$repack/package" \
            "target/release/bundle/deb/rynhult-linux-spike_${deb_version}_amd64.deb" >/dev/null
        rm -rf "$repack" "$built" ;;
    esac
    ls -l target/release/bundle/deb/*.deb
else
    ls -ld target/release/bundle/macos/*.app
    [ -n "$app_only" ] || ls -l target/release/bundle/dmg/*.dmg
fi
