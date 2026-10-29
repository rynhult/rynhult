#!/usr/bin/env bash
# Disposable Ubuntu 24.04 x86_64 VM only; never run on an ACC host.
set -euo pipefail
# Offline resolution is authoritative; only an explicit opt-in permits an online retry.
run_gradle() {
    local gradle=$1 log=$2 cache_mode=$3 online_allowed=$4
    shift 4
    if $cache_mode; then
        if "$gradle" --offline "$@" 2>&1 | tee "$log"; then
            echo 'Offline build completed.'
        elif $online_allowed; then
            echo 'Offline build failed; online retry explicitly authorized.' | tee -a "$log"
            "$gradle" "$@" 2>&1 | tee -a "$log"
        else
            echo 'Offline build failed (cache may lack dependencies); no online retry authorized.' >&2
            return 1
        fi
    else
        "$gradle" "$@" 2>&1 | tee "$log"
    fi
}
# Permit function reuse without installing tools or entering the build pipeline.
if [[ ${BASH_SOURCE[0]} != "$0" ]]; then return 0; fi
usage() { echo "Usage: bash scripts/android_build_check.sh [--dry-run] [--cache DIR] [--allow-online]" >&2; }
dry_run=false
cache=
allow_online=false
while (( $# )); do
    case "$1" in
        --dry-run) dry_run=true; shift ;;
        --cache)
            if (( $# < 2 )) || [[ $2 == --* || -z $2 ]]; then usage; exit 2; fi
            cache=$2; shift 2 ;;
        --allow-online) allow_online=true; shift ;;
        *) usage; exit 2 ;;
    esac
done
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
work=/opt/android-check
jdk_version=17.0.21+9
jdk_url=https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.21%2B9/OpenJDK17U-jdk_x64_linux_hotspot_17.0.21_9.tar.gz
jdk_sha=8b9b611d917e3dfd8419d3566b5949226f69b052fed7e109c1368add1dfe8711
# JDK checksum: official release's adjacent .sha256.txt. Gradle: official release-checksums page.
gradle_url=https://downloads.gradle.org/distributions/gradle-8.10.2-bin.zip
gradle_sha=31c55713e40233a8303827ceb42ca48a47267a0ad4bab9177123121e71524c26
# Pinned from https://developer.android.com/studio#command-line-tools-only (2026-10-26).
# Download verified against that page's SHA-256 and repository2-3.xml SHA-1:
# 040d3996a65543d22ec4bf73e4c37aa37a8d4af4. Launcher requires JDK 17.
cli_url=https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip
cli_sha=4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583

# Read the project's declarations; never silently install a guessed SDK after a project change.
read -r platform build_tools < <(python3 - "$repo/mobile/android" <<'PY'
import re
import sys
from pathlib import Path
root = Path(sys.argv[1])
android = (root / 'enrollment-android/build.gradle.kts').read_text()
plugins = (root / 'build.gradle.kts').read_text()
platform = re.search(r'^\s*compileSdk\s*=\s*(\d+)\s*$', android, re.M)
explicit = re.search(r'^\s*buildToolsVersion\s*=\s*"([\d.]+)"', android, re.M)
# AGP 8.7's documented default is 34.0.0, despite compileSdk 35.
# https://developer.android.com/build/releases/agp-8-7-0-release-notes
assert platform, 'Missing compileSdk declaration'
assert 'id("com.android.library") version "8.7.3"' in plugins, 'Review tool pins for new AGP'
print(platform[1], explicit[1] if explicit else '34.0.0')
PY
)
plan() {
    cat <<PLAN
Disposable VM Android qualification; all writes under $work
JDK: Temurin $jdk_version, SHA-256 $jdk_sha
  $jdk_url
Gradle: 8.10.2, SHA-256 $gradle_sha
  $gradle_url
Android command-line tools: 15859902, SHA-256 $cli_sha
  $cli_url
SDK selection: platforms;android-$platform and build-tools;$build_tools. Cache mode reuses packages/licences; cold mode installs and accepts licences.
Copy mobile/android into $work/project; clean build; no signing credentials or uploads.
Run :enrollment-core:test :enrollment-android:assembleDebug :enrollment-android:testDebugUnitTest
AAR: $work/project/enrollment-android/build/outputs/aar/enrollment-android-debug.aar (print SHA-256 after build)
APK: none (project is a library, not an application).
Reports/logs: $work/project/*/build/reports and $work/build.log
PLAN
}
plan
if [[ -n $cache ]]; then
    printf 'Cache: %s (verify SHA256SUMS before seeding downloads/, sdk/, gradle-home/)\n' "$cache"
    echo 'Gradle --offline first; use installed SDK packages/licences without sdkmanager network calls.'
    if $allow_online; then echo 'Online fallback: explicitly allowed'; else echo 'Online fallback: forbidden'; fi
fi
if $dry_run; then exit 0; fi
[[ $EUID == 0 ]] || { echo 'Run as root on the disposable VM only.' >&2; exit 1; }
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || { echo 'Requires Linux x86_64.' >&2; exit 1; }
for tool in curl python3 tar sha256sum flock tee; do
    command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool (Ubuntu 24.04 base tools required)." >&2; exit 1; }
done
[[ ! -L $work ]] || { echo "Refusing symlink: $work" >&2; exit 1; }
mkdir -p "$work"
exec 9>"$work/check.lock"
flock -n 9 || { echo 'Another build check is running.' >&2; exit 1; }
# Refuse unexpected preexisting symlinks before any deletion or extraction.
for entry in downloads tmp home sdk jdk gradle project gradle-home android-home; do
    [[ ! -L $work/$entry ]] || { echo "Refusing symlink: $work/$entry" >&2; exit 1; }
done
mkdir -p "$work"/{downloads,tmp,home,sdk,gradle-home,android-home}
export TMPDIR=$work/tmp GRADLE_USER_HOME=$work/gradle-home ANDROID_USER_HOME=$work/android-home
export ANDROID_HOME=$work/sdk ANDROID_SDK_ROOT=$work/sdk
export JAVA_HOME=$work/jdk
export PATH="$JAVA_HOME/bin:/usr/bin:/bin"
unset JDK_JAVA_OPTIONS _JAVA_OPTIONS GRADLE_OPTS
export JAVA_TOOL_OPTIONS="-Duser.home=$work/home -Djava.io.tmpdir=$work/tmp"
phase=download
summary() {
    result=$?
    if (( result )); then
        echo "FAIL: Android build check stopped during $phase (exit $result). No qualification claimed." >&2
        echo "Retained evidence under $work" >&2
    fi
}
trap summary EXIT
if [[ -n $cache ]]; then
    phase="cache-verification"
    python3 "$repo/scripts/android_build_cache.py" "$cache" "$work"
fi
phase=download
fetch() {
    local url=$1 sha=$2 target=$3
    if [[ -f $target ]] && printf '%s  %s\n' "$sha" "$target" | sha256sum --check --status; then return; fi
    if [[ -n $cache ]] && ! $allow_online; then
        echo "Missing or wrong pinned archive: $target; offline cache mode refuses downloads. Use --allow-online explicitly." >&2
        return 1
    fi
    curl --fail --location --proto '=https' --proto-redir '=https' --retry 3 --connect-timeout 30 \
        --max-time 1200 --output "$target.part" "$url"
    printf '%s  %s\n' "$sha" "$target.part" | sha256sum --check
    mv -- "$target.part" "$target"
}
fetch "$jdk_url" "$jdk_sha" "$work/downloads/jdk.tar.gz"
fetch "$gradle_url" "$gradle_sha" "$work/downloads/gradle.zip"
fetch "$cli_url" "$cli_sha" "$work/downloads/cli.zip"
phase=extract
# Re-extract verified archives every run: no success markers can hide an interrupted installation.
rm -rf -- "$work/jdk" "$work/gradle" "$work/sdk/cmdline-tools"
mkdir -p "$work/jdk" "$work/gradle" "$work/sdk/cmdline-tools/22.0"
tar -xzf "$work/downloads/jdk.tar.gz" -C "$work/jdk" --strip-components=1
python3 - "$work" "$repo" <<'PY'
import shutil
import sys
import zipfile
from pathlib import Path
work, repo = map(Path, sys.argv[1:])
with zipfile.ZipFile(work / 'downloads/gradle.zip') as z:
    z.extractall(work / 'gradle')
with zipfile.ZipFile(work / 'downloads/cli.zip') as z:
    z.extractall(work / 'tmp/cli')
shutil.copytree(work / 'tmp/cli/cmdline-tools', work / 'sdk/cmdline-tools/22.0', dirs_exist_ok=True)
shutil.rmtree(work / 'tmp/cli')
project = work / 'project'
if project.exists():
    shutil.rmtree(project)
shutil.copytree(repo / 'mobile/android', project,
                ignore=shutil.ignore_patterns('build', '.gradle', 'local.properties', '.kotlin'))
PY
chmod +x "$work/gradle/gradle-8.10.2/bin/gradle" "$work/sdk/cmdline-tools/22.0/bin/"*
sdkmanager=$work/sdk/cmdline-tools/22.0/bin/sdkmanager
phase="SDK-cache-check"
sdk_ready=false
if [[ -s $ANDROID_HOME/platforms/android-$platform/android.jar &&
      -s $ANDROID_HOME/platforms/android-$platform/package.xml &&
      -s $ANDROID_HOME/build-tools/$build_tools/package.xml &&
      -x $ANDROID_HOME/build-tools/$build_tools/aapt2 &&
      -s $ANDROID_HOME/licenses/android-sdk-license ]]; then sdk_ready=true; fi
if [[ -n $cache ]] && $sdk_ready; then
    echo 'Using verified installed SDK packages and accepted licences; sdkmanager skipped.'
else
    if [[ -n $cache ]] && ! $allow_online; then
        echo 'SDK/licences incomplete in cache; refusing network. Restore a complete cache or explicitly use --allow-online.' >&2
        exit 1
    fi
    phase=licences
    # Finite stdin avoids yes's SIGPIPE hiding sdkmanager's real status under pipefail.
    python3 -c 'print("y\n" * 200)' > "$work/tmp/licence-answers"
    "$sdkmanager" --sdk_root="$ANDROID_HOME" --licenses < "$work/tmp/licence-answers" > "$work/licences.log" 2>&1
    phase=sdk-install
    "$sdkmanager" --sdk_root="$ANDROID_HOME" "platforms;android-$platform" "build-tools;$build_tools" \
        < "$work/tmp/licence-answers" > "$work/sdk-install.log" 2>&1
fi
phase=build-and-unit-tests
cd "$work/project"
"$JAVA_HOME/bin/java" -version
gradle_args=(--no-daemon --no-scan --console=plain --max-workers=2
    -Dorg.gradle.java.installations.auto-download=false -Pandroid.builder.sdkDownload=false
    -Pkotlin.compiler.execution.strategy=in-process
    clean :enrollment-core:test :enrollment-android:assembleDebug :enrollment-android:testDebugUnitTest)
cache_mode=false
if [[ -n $cache ]]; then cache_mode=true; fi
run_gradle "$work/gradle/gradle-8.10.2/bin/gradle" "$work/build.log" "$cache_mode" "$allow_online" "${gradle_args[@]}"
phase=artifacts
aar=$work/project/enrollment-android/build/outputs/aar/enrollment-android-debug.aar
[[ -s $aar ]] || { echo "Missing debug AAR: $aar" >&2; exit 1; }
echo 'PASS: debug library build and unit-test tasks completed (Android module may report NO-SOURCE).'
printf 'AAR: %s\nSHA-256: ' "$aar"
sha256sum "$aar"
echo 'APK: none (library project; no app or signing key created).'
echo "Reports: $work/project/enrollment-core/build/reports/tests/test/index.html"
echo "Build log: $work/build.log; SDK/licence logs: $work/{sdk-install,licences}.log"
echo 'No device, emulator, instrumentation, store-signing or release qualification performed.'
