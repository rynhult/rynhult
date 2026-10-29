#!/bin/bash
# Build Rynhult-<version>.pkg, the macOS installer (docs/INSTALLER.md, piece 4). Unsigned. On a Mac (CI: macos-latest):
#   deploy/macos/build-pkg.sh [--dry-run] [--out DIR] [--work DIR] [--version V]
#   --dry-run    stage the payload in the work folder and print every other step without running it (works on Linux)
#   --out DIR    where Rynhult-<version>.pkg goes (default: dist/ in the checkout)
#   --work DIR   build in DIR and keep it (default: a temporary folder, removed afterwards)
#   --version V  default: pyproject.toml's version
# The payload: the Rynhult source (git's tracked files: no .git, .venv, node_modules or data) in
# /Library/Application Support/ACC/src, and Rynhult Setup.app (deploy/macos/acc-setup.applescript) in /Applications.
# deploy/macos/check-pkg.sh checks the result. Written for macOS's bash 3.2.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
HERE=$REPO/deploy/macos
IDENTIFIER=local.acc.pkg
DRY='' OUT=$REPO/dist WORK='' VERSION=''
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) DRY=1 ;;
    --out) OUT=${2:?--out needs a folder}; shift ;;
    --work) WORK=${2:?--work needs a folder}; shift ;;
    --version) VERSION=${2:?--version needs a version}; shift ;;
    -h|--help) sed -n '2,10s/^# \{0,1\}//p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)" >&2; exit 1 ;;
  esac
  shift
done
[ -n "$VERSION" ] || VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml")
[[ $VERSION =~ ^[[:alnum:]._+-]+$ ]] || { echo "bad version '$VERSION'" >&2; exit 1; }

run() { echo "+ $(printf '%q ' "$@")"; [ -n "$DRY" ] || "$@"; }
problems=0
fail() { if [ -n "$DRY" ]; then echo "  WOULD FAIL: $*"; problems=$((problems + 1)); else echo "error: $*" >&2; exit 1; fi; }

[ -n "$DRY" ] && echo "(dry run: only the payload is staged, in the work folder)"
echo "== Rynhult $VERSION: $OUT/Rynhult-$VERSION.pkg"
[ "$(uname -s)" = Darwin ] || fail "pkgbuild, productbuild and osacompile are macOS tools"
for tool in pkgbuild productbuild osacompile plutil codesign; do
  command -v "$tool" >/dev/null || fail "$tool missing"
done

KEEP=$WORK
if [ -n "$WORK" ]; then
  [ ! -e "$WORK" ] || [ -z "$(ls -A "$WORK")" ] || { echo "the work folder $WORK is not empty" >&2; exit 1; }
  mkdir -p "$WORK"
else
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/acc-pkg.XXXXXX")
fi
cleanup() { [ -n "$KEEP" ] || rm -rf "$WORK"; }
trap cleanup EXIT
ROOT=$WORK/root
SRC="$ROOT/Library/Application Support/ACC/src"
APP="$ROOT/Applications/ACC Setup.app"

echo "== payload: the source in /Library/Application Support/ACC/src"
mkdir -p "$SRC" "$ROOT/Applications"
# Tracked files only: nothing ignored (.venv, node_modules, data, .env) or untracked gets in. The checks below are
# for a tree that tracks one by mistake.
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || { echo "$REPO is not a git checkout" >&2; exit 1; }
LIST=$WORK/files
git -C "$REPO" ls-files -z --cached | while IFS= read -r -d '' f; do
  [ -f "$REPO/$f" ] || [ -L "$REPO/$f" ] || continue  # deleted in the working tree
  printf '%s\0' "$f"
done >"$LIST"
excluded=$(tr '\0' '\n' <"$LIST" | grep -E '(^|/)(\.git|\.venv|node_modules|__pycache__)(/|$)|^data/|(^|/)\.env$' || true)
[ -z "$excluded" ] || { echo "error: these must not ship: $excluded" >&2; exit 1; }
echo "+ (cd $(printf %q "$REPO") && git ls-files | tar -c -f - --null -T -) | tar -C $(printf %q "$SRC") -xf -"
(cd "$REPO" && tar -c -f - --null -T "$LIST") | tar -C "$SRC" -xf -
chmod -R u+rwX,go+rX,go-w "$ROOT"
echo "  $(tr -cd '\0' <"$LIST" | wc -c | tr -d ' ') files"
if [ ! -x "$SRC/deploy/node/macos/install.sh" ] || [ ! -x "$SRC/deploy/macos/acc-setup.sh" ]; then
  echo "error: the staged tree lacks the installers (or their x bit; are they committed?)" >&2; exit 1
fi

echo "== payload: /Applications/ACC Setup.app"
run osacompile -o "$APP" "$HERE/acc-setup.applescript"
run plutil -replace CFBundleDisplayName -string "Rynhult Setup" "$APP/Contents/Info.plist"
run plutil -replace CFBundleIdentifier -string local.acc.setup "$APP/Contents/Info.plist"
run plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP/Contents/Info.plist"
# Changing Info.plist breaks osacompile's ad-hoc signature, and Apple silicon runs nothing unsigned: sign again, ad hoc.
run codesign --force --sign - "$APP"
run codesign --verify --strict "$APP"

echo "== component package (scripts: preinstall, postinstall)"
mkdir -p "$WORK/scripts" "$WORK/packages" "$WORK/resources"
cp "$HERE/scripts/preinstall" "$HERE/scripts/postinstall" "$WORK/scripts/"
chmod 0755 "$WORK/scripts/preinstall" "$WORK/scripts/postinstall"
# Not relocatable: Installer would otherwise "update" a Rynhult Setup.app it finds elsewhere instead of /Applications.
run pkgbuild --analyze --root "$ROOT" "$WORK/component.plist"
echo "+ (every bundle in component.plist) BundleIsRelocatable = NO"
if [ -z "$DRY" ]; then
  i=0
  while /usr/libexec/PlistBuddy -c "Print :$i" "$WORK/component.plist" >/dev/null 2>&1; do
    /usr/libexec/PlistBuddy -c "Set :$i:BundleIsRelocatable false" "$WORK/component.plist"
    i=$((i + 1))
  done
  [ "$i" -gt 0 ] || { echo "error: pkgbuild --analyze found no bundle (Rynhult Setup.app)" >&2; exit 1; }
fi
run pkgbuild --root "$ROOT" --component-plist "$WORK/component.plist" --scripts "$WORK/scripts" \
  --identifier "$IDENTIFIER" --version "$VERSION" --install-location / --ownership recommended \
  "$WORK/packages/acc-component.pkg"

echo "== product archive (welcome page, then the component)"
sed "s/@VERSION@/$VERSION/g" "$HERE/distribution.xml" >"$WORK/distribution.xml"
cp "$HERE/resources/welcome.html" "$HERE/resources/conclusion.html" "$WORK/resources/"
run mkdir -p "$OUT"
run productbuild --distribution "$WORK/distribution.xml" --resources "$WORK/resources" \
  --package-path "$WORK/packages" "$OUT/Rynhult-$VERSION.pkg"

if [ "$problems" -gt 0 ]; then
  echo "dry run: $problems problem(s) above would stop a real build" >&2
  exit 2
fi
if [ -n "$DRY" ]; then echo "== dry run done"; else echo "== built $OUT/Rynhult-$VERSION.pkg (unsigned)"; fi
