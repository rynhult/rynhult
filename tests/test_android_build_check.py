"""Exercise the coordinator script without installing or running a toolchain."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/android_build_check.sh'


def test_dry_run_has_no_downloads_or_filesystem_writes(tmp_path):
    # A tool invocation during a dry run must fail, even on a host with these tools installed.
    sentinels = tmp_path / 'bin'
    sentinels.mkdir()
    for tool in ('curl', 'java', 'sdkmanager', 'gradle', 'mkdir', 'cp', 'tar'):
        p = sentinels / tool
        p.write_text('#!/bin/sh\necho unexpected-tool >&2\nexit 97\n')
        p.chmod(0o755)
    result = subprocess.run(['bash', str(SCRIPT), '--dry-run'], cwd=tmp_path,
                            env={**os.environ, 'PATH': f'{sentinels}:/usr/bin:/bin'},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'unexpected-tool' not in result.stderr
    assert ':enrollment-android:assembleDebug' in result.stdout
    assert ':enrollment-core:test' in result.stdout
    assert ':enrollment-android:testDebugUnitTest' in result.stdout
    assert 'APK: none' in result.stdout
    assert 'AAR' in result.stdout and 'SHA-256' in result.stdout
    assert list(tmp_path.iterdir()) == [sentinels]


def test_bad_arguments_fail_before_work(tmp_path):
    result = subprocess.run(['bash', str(SCRIPT), '--upload'], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Usage:' in result.stderr
    assert not list(tmp_path.iterdir())


def test_dry_run_reads_explicit_project_sdk_versions(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    script = scripts / SCRIPT.name
    script.write_text(SCRIPT.read_text())
    android = tmp_path / 'mobile/android'
    (android / 'enrollment-android').mkdir(parents=True)
    (android / 'build.gradle.kts').write_text('id("com.android.library") version "8.7.3"')
    (android / 'enrollment-android/build.gradle.kts').write_text(
        'compileSdk = 34\nbuildToolsVersion = "35.0.0"\n')
    result = subprocess.run(['bash', str(script), '--dry-run'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'platforms;android-34 and build-tools;35.0.0' in result.stdout


def test_unreviewed_plugin_version_fails_before_download(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    script = scripts / SCRIPT.name
    script.write_text(SCRIPT.read_text())
    android = tmp_path / 'mobile/android'
    (android / 'enrollment-android').mkdir(parents=True)
    (android / 'build.gradle.kts').write_text('id("com.android.library") version "9.0.0"')
    (android / 'enrollment-android/build.gradle.kts').write_text('compileSdk = 35\n')
    result = subprocess.run(['bash', str(script), '--dry-run'], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Review tool pins' in result.stderr


def test_cache_plan_is_offline_and_only_explicit_opt_in_allows_online(tmp_path):
    cache = tmp_path / 'saved cache'
    # Dry run must not read or copy the cache: the plan works before transfer.
    offline = subprocess.run(['bash', str(SCRIPT), '--dry-run', '--cache', str(cache)],
                             capture_output=True, text=True)
    assert offline.returncode == 0, offline.stderr
    assert str(cache) in offline.stdout
    assert 'Gradle --offline' in offline.stdout
    assert 'Online fallback: forbidden' in offline.stdout
    online = subprocess.run(['bash', str(SCRIPT), '--cache', str(cache), '--allow-online', '--dry-run'],
                            capture_output=True, text=True)
    assert online.returncode == 0, online.stderr
    assert 'Online fallback: explicitly allowed' in online.stdout
    assert not cache.exists()


@pytest.mark.parametrize('allow_online,offline_status,want_calls,want_status', [
    ('false', 9, 1, 1), ('true', 9, 2, 0), ('true', 0, 1, 0), ('false', 0, 1, 0),
])
def test_gradle_never_retries_online_without_explicit_permission(
        tmp_path, allow_online, offline_status, want_calls, want_status):
    gradle = tmp_path / 'gradle'
    gradle.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
                      'if [ "$1" = --offline ]; then echo offline-attempt; exit "$OFFLINE_STATUS"; fi\n'
                      'echo online-attempt\n')
    gradle.chmod(0o755)
    calls = tmp_path / 'calls'
    log = tmp_path / 'build.log'
    result = subprocess.run([
        'bash', '-c', 'source "$1"; run_gradle "$2" "$3" true "$4" :enrollment-core:test',
        'check', str(SCRIPT), str(gradle), str(log), allow_online],
        env={**os.environ, 'CALLS': str(calls), 'OFFLINE_STATUS': str(offline_status)},
        capture_output=True, text=True)
    assert result.returncode == want_status, result.stderr
    attempts = calls.read_text().splitlines()
    assert len(attempts) == want_calls
    assert attempts[0] == '--offline :enrollment-core:test'
    assert 'offline-attempt' in log.read_text()
    if want_calls == 2:
        assert attempts[1] == ':enrollment-core:test'
        assert 'online-attempt' in log.read_text()
