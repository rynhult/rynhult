# Rebuilding the Android enrollment library

Use a reviewed main checkout on a **disposable Ubuntu 24.04 x86_64 VM**. Main includes the enrollment compile
fixes. Do not run installation mode on an ACC host
or a live instance. No Android device, signing key, store account or server is needed.

## Evidence and limits

The coordinator built the corrected client on a disposable cloud VM and reported **all 26 core unit tests
passing**, plus a debug AAR with SHA-256 prefix `33698b53`. Only that prefix was supplied; use the original
VM evidence for the full digest. This establishes that build and those JVM tests, not physical-device behavior
or byte-for-byte reproducibility across machines. The new offline-cache workflow still needs its own VM run.

This is an Android **library**, so it produces an AAR, not an installable APK. There is no app UI, emulator,
instrumentation run, phone Keystore/StrongBox/TEE/TLS qualification, signing qualification, store submission,
push relay, or public release qualification. The runner prints these limits even when it passes.

## Transfer the saved cache

The operator saved the successful VM's downloads, installed SDK and Gradle dependency cache under
`/srv/acc/build-cache/android/2026-10-26` on the trusted build-cache host. Transfer that directory and a reviewed
main checkout to the disposable VM using your trusted operator channel. Preserve file modes;
SDK executables must remain executable. The cache is private build infrastructure, not a public artifact.
No transfer or upload is performed by the runner.

The copied cache, for example `/mnt/android-cache/2026-10-26`, must have this layout:

```text
SHA256SUMS
downloads/
  jdk.tar.gz
  gradle.zip
  cli.zip
sdk/
  platforms/android-35/
  build-tools/34.0.0/
  licenses/
gradle-home/
  caches/...
```

Keep the cache immutable and separate from `/opt/android-check`; overlapping paths are refused. The cache
must include Gradle plugin metadata and transitive dependencies, not just a few JARs. Preserve SDK licence
files accepted by the operator on the original VM. Don't include signing credentials, user `gradle.properties`
with passwords, credentials for private repositories, or live-instance data.

Under `gradle-home/` only `caches/` is accepted: Gradle runs scripts from `init.d/` and applies
`gradle.properties` from its home, so a cache holding those (or `init.gradle`, `wrapper/`) is refused.

`SHA256SUMS` uses GNU sha256sum format, relative to the cache root, and lists every regular file under
`downloads/`, `sdk/`, and `gradle-home/` exactly once. Both `downloads/file` and `./downloads/file` are accepted.
Unlisted/missing files, malformed or duplicate entries, absolute paths, parent traversal, symlinks and special
files are refused before seeding. Paths using newline/backslash escapes are unsupported. Other top-level
files aren't seeded and must not appear in the manifest. Transport the manifest through the same trusted
operator channel: checksums detect corruption; a malicious publisher can replace both files and manifest.

When intentionally refreshing a stopped, trusted cache, regenerate its manifest on the cache copy:

```sh
cd /path/to/cache-copy
find downloads sdk gradle-home -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
```

Do not regenerate it to bypass a verification failure. Retain and investigate the original manifest. Tool
archives are independently checked against the runner's official-source SHA-256 pins before extraction,
even after manifest verification. Seeded file contents are hashed again after copying, before execution.

## Rebuild offline

VM prerequisites are `bash`, `curl`, Python 3, `tar`, `sha256sum`, `flock`, `tee` and Ubuntu's normal core
utilities. The runner installs no system packages or services. From the source checkout's repository root:

```sh
# Read-only plan; no root required and the cache need not have been copied yet.
bash scripts/android_build_check.sh --dry-run --cache /mnt/android-cache/2026-10-26

# On the disposable VM, as root:
bash scripts/android_build_check.sh --cache /mnt/android-cache/2026-10-26
```

The runner verifies the manifest, replaces its working `downloads/`, `sdk/`, and `gradle-home/` with verified
copies, rechecks the pinned archives, and recreates the tool installations and source copy. All build writes
stay under `/opt/android-check`; the input cache and source checkout remain unchanged. A lock prevents two
runs sharing that build directory. Repeated runs recreate the clean build and overwrite logs/reports, so save
evidence first. Cached files are not hardlinked into the writable build directory.

With `--cache`, missing/wrong tool archives or SDK/licence files fail without downloading. A complete
installed SDK skips `sdkmanager`, since even licence/list operations can contact Google's repository.
Gradle always runs **`--offline` first**. Its dependency resolution determines whether the cache is complete;
a directory-presence check cannot prove every dependency is available. An incomplete dependency cache or
failing test exits nonzero, retaining the log. There is no automatic online retry.

The SDK selection is compileSdk 35 and build-tools 34.0.0 (the project's AGP 8.7.3 default); any explicit
project `buildToolsVersion` is honored. A changed AGP requires review of tool pins. JVM/SDK auto-downloads
are disabled in Gradle. Tasks are:

```text
clean :enrollment-core:test :enrollment-android:assembleDebug :enrollment-android:testDebugUnitTest
```

Only if an online repair/retry is intended:

```sh
bash scripts/android_build_check.sh --cache /mnt/android-cache/2026-10-26 --allow-online
```

This permits missing archive downloads and SDK installation, then an online Gradle retry if the offline
attempt fails. It never bypasses manifest or pinned checksum failures. A corrupt supplied manifest/cache
still fails; repair the cache from a trusted source. The input cache is not updated: any repaired dependencies
live only in `/opt/android-check`. Without `--cache`, the original cold online build mode remains available.
No build scan, upload, publishing, signing or store operation is requested.

## Google download refusal and prefetch workaround

Google returned **HTTP 404 for the cloud VM's IP** when downloading the command-line tools ZIP, although
the same pinned archive was downloadable from another trusted host. The coordinator prefetched it through
the trusted LAN build host, verified the pinned checksum, then transferred it to the VM. `sdkmanager` itself
worked there. A 404 from that VM did not establish that the archive was removed.

Current archive (tools 22.0):

- [Official Google ZIP](https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip)
- SHA-256: `4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583`
- [Official listing](https://developer.android.com/studio#command-line-tools-only)

The saved cache's `downloads/cli.zip` avoids that request entirely. For a cold build, an operator can prefetch
on a trusted host and transfer the verified archive to `/opt/android-check/downloads/cli.zip` before running
the script. Do not disable checksums, TLS verification or use an unverified mirror to work around the refusal.

## Results to retain

On success the script prints the AAR path and full SHA-256:

```text
/opt/android-check/project/enrollment-android/build/outputs/aar/enrollment-android-debug.aar
```

Keep `/opt/android-check/build.log` and the core test XML/HTML under
`project/enrollment-core/build/test-results/test/` and `project/enrollment-core/build/reports/tests/test/`.
Android unit-test tasks may report `NO-SOURCE` because the Android module currently has no unit tests;
the reported 26 are core JVM tests. Also record the source commit, cache manifest digest and VM architecture.
If an online bootstrap ran, preserve `sdk-install.log` and `licences.log` too. A new AAR digest may differ;
the preserved prefix alone isn't an integrity oracle for a future build.

## Dependency verification metadata (security review 9, SR9-9)

`mobile/android/gradle/verification-metadata.xml` makes Gradle check every dependency and plugin it resolves against
a committed SHA-256. Generate it once on the disposable VM, after a successful offline build from the saved cache:

1. Run the offline build as above:

   ```sh
   bash scripts/android_build_check.sh --cache /mnt/android-cache/2026-10-26
   ```

   **What you should see:** `PASS: debug library build and unit-test tasks completed`.
2. In the same root shell, write the metadata with the same tasks, offline, from the build's own Gradle home:

   ```sh
   cd /opt/android-check/project
   env TMPDIR=/opt/android-check/tmp GRADLE_USER_HOME=/opt/android-check/gradle-home \
     ANDROID_USER_HOME=/opt/android-check/android-home ANDROID_HOME=/opt/android-check/sdk \
     ANDROID_SDK_ROOT=/opt/android-check/sdk JAVA_HOME=/opt/android-check/jdk \
     PATH=/opt/android-check/jdk/bin:/usr/bin:/bin \
     JAVA_TOOL_OPTIONS="-Duser.home=/opt/android-check/home -Djava.io.tmpdir=/opt/android-check/tmp" \
     /opt/android-check/gradle/gradle-8.10.2/bin/gradle --offline --no-daemon --no-scan --console=plain \
       --max-workers=2 --write-verification-metadata sha256 \
       -Dorg.gradle.java.installations.auto-download=false -Pandroid.builder.sdkDownload=false \
       -Pkotlin.compiler.execution.strategy=in-process \
       clean :enrollment-core:test :enrollment-android:assembleDebug :enrollment-android:testDebugUnitTest
   sha256sum gradle/verification-metadata.xml
   ```

   **What you should see:** the build succeeds again, offline, and `gradle/verification-metadata.xml` exists. It has
   `<verify-metadata>true</verify-metadata>`, and one `<component>` per resolved module with a `<sha256>` for each
   artifact, `.pom` and `.module` file (`origin="Generated by Gradle"`), and no `trusted-artifacts`.
3. Bring that one file back through the trusted operator channel, with its SHA-256. Commit it as
   `mobile/android/gradle/verification-metadata.xml`.
4. Prove it's enforced: run step 1 again. It must pass. Then change one `sha256` value in a copy of the project and
   run the step-2 command without `--write-verification-metadata sha256`. Gradle must fail with
   `Dependency verification failed`.

The checksums come only from the saved cache's files, with no network fetch, because the build is `--offline`. They
pin what the cache holds: if the cache itself was poisoned before it was saved, the metadata pins that too. Review the
module list for anything unexpected before committing. `tests/test_sr9_followups.py` checks the committed file's shape.
