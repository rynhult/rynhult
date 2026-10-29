#!/bin/sh
# Assemble the native Linux x86_64 or macOS arm64 runtime inside desktop/runtime/.
# uv downloads a pinned python-build-standalone build and verifies its SHA-256
# against uv's built-in manifest; dependencies install only with locked hashes.
# User-local and project-local: nothing outside this checkout changes.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
version=3.12.15
runtime="$here/runtime"
case "$(uname -s):$(uname -m)" in
    Linux:x86_64)
        target="cpython-$version-linux-x86_64-gnu"
        lock=requirements-linux.lock
        set -- ;;
    Darwin:arm64)
        target="cpython-$version-macos-aarch64-none"
        lock=requirements-macos-arm64.lock
        export MACOSX_DEPLOYMENT_TARGET=13.0
        # http-ece is the sole allowed sdist (pure Python). Refuse native source builds.
        set -- --only-binary :all: --no-binary http-ece --require-build-hashes \
               --build-constraints "$here/requirements-build.lock" ;;
    *)
        echo "Supported runtime hosts: Linux x86_64 and macOS arm64. Intel Mac wheels are not qualified." >&2
        exit 1 ;;
esac
rm -rf "$runtime"
mkdir -p "$runtime"
UV_PYTHON_INSTALL_DIR="$runtime/.install" uv python install --no-bin --no-config "$target"
mv "$runtime/.install/$target" "$runtime/python"
rm -rf "$runtime/.install"
python="$runtime/python/bin/python3"
# Bundle only what the main imports; drop pip/ensurepip, tests and IDLE/Tk to save space.
# http-ece ships only a hashed pure-Python sdist; everything else is a wheel.
uv pip install --no-config --python "$python" --break-system-packages --require-hashes \
    "$@" -r "$here/$lock"
lib="$runtime/python/lib/python3.12"
rm -rf "$lib/ensurepip" "$lib/idlelib" "$lib/tkinter" "$lib/turtledemo" "$lib/test" \
       "$lib/site-packages/pip" "$lib/site-packages"/pip-* "$runtime/python/include" \
       "$runtime/python/share" "$runtime/python/lib"/libtcl* "$runtime/python/lib"/libtk* \
       "$runtime/python/lib/tcl"* "$runtime/python/lib/tk"* "$runtime/python/lib/itcl"* \
       "$runtime/python/lib/thread"* "$runtime/python/lib/pkgconfig"
find "$lib/lib-dynload" -name '_tkinter*' -type f -delete
if [ "$lock" = requirements-linux.lock ]; then
    # Linux's interpreter is static; macOS's interpreter needs libpython3.12.dylib.
    # The libpython3.12.so link goes too: left dangling, Tauri refuses to bundle the resources.
    find "$runtime/python/lib" -maxdepth 1 -name 'libpython3*' \( -type f -o -type l \) -delete
fi
dangling=$(find "$runtime" -xtype l)
[ -z "$dangling" ] || { echo "Dangling links in the runtime (Tauri can't bundle them): $dangling" >&2; exit 1; }
# Tauri resources copy symlink targets, so keep one real interpreter and no helper scripts.
bin="$runtime/python/bin"
mv "$bin/python3.12" "$runtime/python3"
rm -rf "$bin"
mkdir "$bin"
mv "$runtime/python3" "$bin/python3"
# Strip debug symbols from shared objects (python-build-standalone ships them).
if [ "$lock" = requirements-macos-arm64.lock ]; then
    find "$runtime/python" -type f \( -name '*.so*' -o -name '*.dylib' \) -exec strip -x {} +
    strip -x "$python"
    # Stripping changes Mach-O signatures. Repair them ad hoc for local unsigned runs.
    "$here/sign-runtime-macos.sh" "$runtime/python" -
else
    find "$runtime/python" -name "*.so*" -type f -exec strip --strip-unneeded {} +
fi
# Precompile so a read-only install never writes bytecode. Hash-based .pyc files stay
# valid when packaging copies the sources with new timestamps (no recompiling per launch).
"$python" -I -m compileall -q -j 0 --invalidation-mode unchecked-hash "$lib" >/dev/null
"$python" -I -c 'import sys, fastapi, uvicorn, sqlalchemy, cryptography; print(sys.version.split()[0])'
du -sh "$runtime/python"
