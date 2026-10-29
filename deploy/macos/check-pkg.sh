#!/bin/bash
# Check a built Rynhult-<version>.pkg without installing it (CI on macOS, after deploy/macos/build-pkg.sh):
#   deploy/macos/check-pkg.sh dist/Rynhult-<version>.pkg
# Lists the payload, then checks: the source and Rynhult Setup.app are there and nothing else is; no .git, .venv,
# node_modules or data; files owned by root and the installers executable; the pre/postinstall scripts and that the
# postinstall never runs install.sh (that runs as the user, from Rynhult Setup); the welcome page says what a Mac can be.
set -euo pipefail

PKG=${1:?usage: check-pkg.sh Rynhult-<version>.pkg}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/acc-pkg-check.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
SRC='./Library/Application Support/ACC/src'
APP='./Applications/ACC Setup.app'
problems=0
bad() { echo "FAIL: $*"; problems=$((problems + 1)); }

echo "== pkgutil --expand"
pkgutil --expand "$PKG" "$WORK/x"
ls -l "$WORK/x"
COMPONENT=$WORK/x/acc-component.pkg
[ -d "$COMPONENT" ] || { echo "FAIL: no acc-component.pkg inside" >&2; exit 1; }

echo "== payload"
(cd "$WORK" && gzip -dc "$COMPONENT/Payload" | cpio -itv 2>/dev/null) >"$WORK/long.txt"
(cd "$WORK" && gzip -dc "$COMPONENT/Payload" | cpio -it 2>/dev/null) >"$WORK/files.txt"
echo "$(wc -l <"$WORK/files.txt" | tr -d ' ') entries; pkgutil --payload-files says:"
pkgutil --payload-files "$PKG" >"$WORK/pkgutil.txt" 2>&1 || true
head -n 5 "$WORK/pkgutil.txt"
echo "  … $(wc -l <"$WORK/pkgutil.txt" | tr -d ' ') lines"
for need in "$SRC/pyproject.toml" "$SRC/uv.lock" "$SRC/src/acc/node/__main__.py" \
            "$SRC/deploy/node/macos/install.sh" "$SRC/deploy/node/macos/acc-node" "$SRC/deploy/node/macos/acc.plist.in" \
            "$SRC/deploy/macos/acc-setup.sh" "$APP/Contents/Info.plist" "$APP/Contents/MacOS/applet" \
            "$APP/Contents/Resources/Scripts/main.scpt"; do
  grep -qxF "$need" "$WORK/files.txt" || bad "missing $need"
done
outside=$(grep -vxF -e . -e ./Library -e './Library/Application Support' -e './Library/Application Support/ACC' \
  -e ./Applications "$WORK/files.txt" | grep -vF -e "$SRC" -e "$APP" || true)
if [ -z "$outside" ]; then echo "ok: only the source and Rynhult Setup.app"; else bad "outside Rynhult's folders: $outside"; fi
forbidden=$(grep -E '/(\.git|\.venv|node_modules|__pycache__)(/|$)|/\.env$|/\.DS_Store$' "$WORK/files.txt" || true)
forbidden=$forbidden$(grep -E "^$SRC/data(/|\$)" "$WORK/files.txt" || true)
if [ -z "$forbidden" ]; then echo "ok: no .git, .venv, node_modules, data"; else bad "must not ship: $forbidden"; fi
for exe in "$SRC/deploy/node/macos/install.sh" "$SRC/deploy/macos/acc-setup.sh" "$SRC/deploy/node/macos/acc-node"; do
  line=$(grep -F " $exe" "$WORK/long.txt" | head -n 1)
  case $line in -rwxr-xr-x*) ;; *) bad "$exe is not -rwxr-xr-x: $line" ;; esac
done
not_root=$(grep -F "$SRC" "$WORK/long.txt" | awk '$3 != "root" && $3 != "0"' | head -n 3)
if [ -z "$not_root" ]; then echo "ok: the source is root's"; else bad "not owned by root: $not_root"; fi
writable=$(grep -F "$SRC" "$WORK/long.txt" | awk '$1 !~ /^l/ && (substr($1, 6, 1) == "w" || substr($1, 9, 1) == "w")' | head -n 3)
if [ -z "$writable" ]; then echo "ok: nothing group or world writable"; else bad "writable by others: $writable"; fi

echo "== scripts"
for script in preinstall postinstall; do
  if [ -x "$COMPONENT/Scripts/$script" ]; then echo "ok: $script"; else bad "no executable Scripts/$script"; fi
done
if grep -n 'install\.sh' "$COMPONENT/Scripts/postinstall"; then bad "postinstall mentions install.sh"; fi
grep -q 'launchctl asuser' "$COMPONENT/Scripts/postinstall" || bad "postinstall does not use launchctl asuser"

echo "== PackageInfo and Distribution"
cat "$COMPONENT/PackageInfo"
grep -q 'identifier="local.acc.pkg"' "$COMPONENT/PackageInfo" || bad "PackageInfo: identifier"
grep -q 'install-location="/"' "$COMPONENT/PackageInfo" || bad "PackageInfo: install-location"
if grep -q 'relocatable="true"' "$COMPONENT/PackageInfo" || grep -q '<relocate>' "$COMPONENT/PackageInfo"; then bad "Rynhult Setup.app is relocatable"; fi  # an empty <relocate/> relocates nothing
grep -q 'welcome file="welcome.html"' "$WORK/x/Distribution" || bad "Distribution: no welcome page"
welcome=$(find "$WORK/x/Resources" -name welcome.html | head -n 1)
if [ -n "$welcome" ]; then
  grep -q 'additional machine only' "$welcome" || bad "welcome: does not say a Mac is an additional machine only"
  grep -q 'Sessions run as you' "$welcome" || bad "welcome: does not say sessions run as the signed-in user"
else
  bad "no Resources/welcome.html"
fi

if [ "$problems" -gt 0 ]; then echo "$problems problem(s)"; exit 1; fi
echo "== $PKG looks right"
