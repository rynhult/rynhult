"""Pin validation must catch drift without running installers or contacting vendors."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/check_pins.py'


def run(root):
    return subprocess.run([sys.executable, str(SCRIPT), '--root', str(root), '--json'],
                          capture_output=True, text=True)


@pytest.fixture
def checkout(tmp_path):
    for folder in ('deploy', 'desktop', 'scripts', '.github'):
        shutil.copytree(ROOT / folder, tmp_path / folder,
                        ignore=shutil.ignore_patterns('node_modules', 'target', '__pycache__', 'runtime', 'runtime-deps'))
    shutil.copyfile(ROOT / 'uv.lock', tmp_path / 'uv.lock')
    (tmp_path / 'web').mkdir()
    for name in ('package.json', 'package-lock.json'):
        shutil.copyfile(ROOT / 'web' / name, tmp_path / 'web' / name)
    return tmp_path


def test_current_repository_inventory_is_valid():
    result = run(ROOT)
    assert result.returncode == 0, result.stderr
    pins = json.loads(result.stdout)
    uv = next(p for p in pins if p['name'] == 'uv' and p['variant'] == 'x86_64-unknown-linux-gnu')
    assert uv['url'].endswith('/uv-x86_64-unknown-linux-gnu.tar.gz')
    assert len(uv['sha256']) == 64
    assert any(p['name'] == 'restic' and p['variant'] == 'arm64' for p in pins)
    assert any(p['name'] == 'go' and p['url'].startswith('https://go.dev/dl/') for p in pins)


@pytest.mark.parametrize('value', ['bad', 'g' * 64, 'A' * 64, 'a' * 63, 'a' * 65])
def test_rejects_invalid_download_hash(checkout, value):
    path = checkout / 'deploy/lib/install-uv.sh'
    text = path.read_text()
    path.write_text(re.sub(r'^SUM_X86_64=.*$', 'SUM_X86_64=' + value, text, flags=re.M))
    result = run(checkout)
    assert result.returncode == 1
    assert 'SHA-256' in result.stderr


def test_workflow_uv_version_must_match_installer(checkout):
    path = checkout / '.github/workflows/checks.yml'
    path.write_text(re.sub(r'uv==[\w.+-]+', 'uv==99.0.0', path.read_text()))
    result = run(checkout)
    assert result.returncode == 1
    assert 'uv version drift' in result.stderr


def test_node_url_version_must_match_extraction_path(checkout):
    path = checkout / 'deploy/containers/Containerfile'
    path.write_text(re.sub(r'/dist/v[0-9.]+/', '/dist/v99.0.0/', path.read_text()))
    result = run(checkout)
    assert result.returncode == 1
    assert 'node version drift' in result.stderr


def test_container_base_records_its_release_tag():
    pins = json.loads(run(ROOT).stdout)
    base = next(p for p in pins if p['name'] == 'playwright-python-image')
    assert base['version'] == '1.56.0-noble-amd64'
    assert base['url'] == ('oci://mcr.microsoft.com/playwright/python:v1.56.0-noble-amd64'
                           '@sha256:508b37a524219d210a67a7190beb079efda72553e0dcb9ef21d4dae8f8a7686b')


@pytest.mark.parametrize('reference', ['mcr.microsoft.com/playwright/python',
                                       'mcr.microsoft.com/playwright/python:latest',
                                       'mcr.microsoft.com/playwright/python:v1.56.0-noble',
                                       'registry.example:5000/playwright/python'])
def test_container_base_needs_an_amd64_release_tag(checkout, reference):
    # An untagged digest made Dependabot propose the stale multi-arch `latest` (#350).
    path = checkout / 'deploy/containers/Containerfile'
    path.write_text(re.sub(r'(?m)^FROM \S+@sha256:', f'FROM {reference}@sha256:', path.read_text()))
    result = run(checkout)
    assert result.returncode == 1
    assert 'amd64 release tag' in result.stderr


def test_desktop_runtime_must_match_main_lock(checkout):
    path = checkout / 'desktop/requirements-linux.lock'
    path.write_text(re.sub(r'(?m)^alembic==[^\s]+', 'alembic==99.0.0', path.read_text()))
    result = run(checkout)
    assert result.returncode == 1
    assert 'alembic version drift' in result.stderr


def test_new_pinned_downloader_requires_inventory_adapter(checkout):
    path = checkout / 'deploy/lib/install-example.sh'
    path.write_text('VERSION=1.2.3\nSUM=' + 'a' * 64 + '\n'
                    'curl https://example.com/tool/v1.2.3/tool.tar.gz\n')
    result = run(checkout)
    assert result.returncode == 1
    assert 'uninventoried' in result.stderr


def test_removing_hash_or_url_does_not_silently_drop_pin(checkout):
    path = checkout / 'deploy/lib/install-github-mcp.sh'
    path.write_text(path.read_text().replace('arch=x86_64; sum=', 'arch=x86_64; missing='))
    result = run(checkout)
    assert result.returncode == 1
    assert 'github-mcp' in result.stderr


def test_malformed_runtime_lock_hash_is_rejected(checkout):
    path = checkout / 'desktop/requirements-linux.lock'
    path.write_text(path.read_text().replace('--hash=sha256:', '--hash=sha256:invalid', 1))
    result = run(checkout)
    assert result.returncode == 1
    assert 'SHA-256' in result.stderr


def test_qualified_caddy_requires_both_artifact_hashes(checkout):
    path = checkout / 'deploy/dns/release.json'
    data = json.loads(path.read_text())
    data['status'] = 'qualified'
    data['artifacts'] = {'amd64': {'sha256': 'a' * 64}}
    path.write_text(json.dumps(data))
    result = run(checkout)
    assert result.returncode == 1
    assert 'arm64' in result.stderr


def test_new_digest_in_existing_installer_requires_inventory(checkout):
    path = checkout / 'deploy/lib/install-uv.sh'
    path.write_text(path.read_text() + '\nOTHER_SUM=' + 'b' * 64 + '\n')
    result = run(checkout)
    assert result.returncode == 1
    assert 'uninventoried' in result.stderr


def test_installer_url_cannot_point_at_another_version(checkout):
    path = checkout / 'deploy/lib/install-uv.sh'
    path.write_text(path.read_text().replace('/$UV_VERSION/', '/99.0.0/'))
    result = run(checkout)
    assert result.returncode == 1
    assert 'version' in result.stderr


def test_cargo_checksum_must_be_sha256(checkout):
    path = checkout / 'desktop/src-tauri/Cargo.lock'
    path.write_text(path.read_text().replace('checksum = "', 'checksum = "bad', 1))
    result = run(checkout)
    assert result.returncode == 1
    assert 'SHA-256' in result.stderr


def test_npm_integrity_length_is_validated(checkout):
    path = checkout / 'deploy/mcp/package-lock.json'
    path.write_text(path.read_text().replace('sha512-', 'sha512-YQ==', 1))
    result = run(checkout)
    assert result.returncode == 1
    assert 'npm integrity' in result.stderr


def test_new_url_in_existing_installer_requires_inventory(checkout):
    path = checkout / 'deploy/lib/install-uv.sh'
    path.write_text(path.read_text() + '\ncurl https://example.com/v1.0/tool.tar.gz\n')
    result = run(checkout)
    assert result.returncode == 1
    assert 'uninventoried' in result.stderr


def test_missing_runtime_hash_is_rejected(checkout):
    path = checkout / 'desktop/requirements-linux.lock'
    path.write_text('alembic==1.20.0\n')
    result = run(checkout)
    assert result.returncode == 1
    assert 'SHA-256' in result.stderr


def test_dependabot_covers_package_manifests_and_separates_security_updates():
    import yaml

    config = yaml.safe_load((ROOT / '.github/dependabot.yml').read_text())
    coverage = {}
    for entry in config['updates']:
        ecosystem = entry['package-ecosystem']
        coverage[ecosystem] = entry.get('directories', [entry.get('directory')])
        assert entry['schedule']['interval'] == 'weekly'
        assert 'target-branch' not in entry
        assert 'cooldown' not in entry
        assert {g['applies-to'] for g in entry['groups'].values()} == {'version-updates', 'security-updates'}
    for folder in ('web', 'deploy/mcp', 'deploy/containers/session'):
        assert '/' + folder in coverage['npm']
        assert (ROOT / folder / 'package-lock.json').is_file()
    assert coverage['uv'] == ['/']
    assert coverage['cargo'] == ['/desktop/src-tauri']
    assert coverage['pip'] == ['/deploy/telemetry', '/deploy/containers/session']
    assert coverage['docker'] == ['/deploy/containers']
    assert coverage['github-actions'] == ['/']


def test_npm_manifest_cannot_drift_from_lock(checkout):
    path = checkout / 'deploy/mcp/package.json'
    data = json.loads(path.read_text())
    data['dependencies']['@playwright/mcp'] = '99.0.0'
    path.write_text(json.dumps(data))
    result = run(checkout)
    assert result.returncode == 1
    assert 'version drift' in result.stderr


def test_exact_cargo_manifest_cannot_drift_from_lock(checkout):
    path = checkout / 'desktop/src-tauri/Cargo.toml'
    path.write_text(re.sub(r'(tauri = \{ version = ")=[^"]+', r'\g<1>=99.0.0', path.read_text()))
    result = run(checkout)
    assert result.returncode == 1
    assert 'tauri version drift' in result.stderr


def test_new_malformed_pin_is_not_silently_ignored(checkout):
    path = checkout / 'scripts/install-example.sh'
    path.write_text('VERSION=1.2.3\nSHA256=broken\n'
                    'curl https://example.com/v$VERSION/tool.bin\n')
    result = run(checkout)
    assert result.returncode == 1
    assert 'uninventoried' in result.stderr


def test_pending_caddy_build_inputs_are_in_the_inventory():
    result = run(ROOT)
    assert result.returncode == 0, result.stderr
    pins = json.loads(result.stdout)
    caddy = next(p for p in pins if p['name'] == 'caddy')
    assert caddy['version']
    assert caddy['sha256'] is None
    assert 'build' in caddy['verification']


def test_caddy_candidate_id_cannot_disagree_with_version(checkout):
    path = checkout / 'deploy/dns/release.json'
    data = json.loads(path.read_text())
    data['caddy'] = 'v99.0.0'
    path.write_text(json.dumps(data))
    result = run(checkout)
    assert result.returncode == 1
    assert 'caddy version drift' in result.stderr


def test_macos_runtime_versions_must_match_main(checkout):
    path = checkout / 'desktop/requirements-macos-arm64.lock'
    path.write_text(re.sub(r'(?m)^alembic==[^\s]+', 'alembic==99.0.0', path.read_text()))
    result = run(checkout)
    assert result.returncode == 1
    assert 'alembic version drift' in result.stderr


def test_desktop_build_dependency_hashes_are_validated(checkout):
    path = checkout / 'desktop/requirements-build.lock'
    path.write_text(path.read_text().replace('--hash=sha256:', '--hash=sha256:bad', 1))
    result = run(checkout)
    assert result.returncode == 1
    assert 'SHA-256' in result.stderr


def test_cpython_inventory_includes_both_runtime_targets():
    result = run(ROOT)
    assert result.returncode == 0, result.stderr
    targets = {p['variant'] for p in json.loads(result.stdout) if p['name'] == 'cpython'}
    assert {'linux-x86_64-gnu', 'macos-aarch64-none'} <= targets


def test_native_claude_code_pins_are_in_the_inventory():
    """deploy/packages/acc-packages pins Anthropic's native Claude Code release per platform for machines without apt."""
    result = run(ROOT)
    assert result.returncode == 0, result.stderr
    pins = {p['variant']: p for p in json.loads(result.stdout) if p['name'] == 'claude-code'}
    assert set(pins) == {'linux-x64', 'linux-arm64'}
    for platform, pin in pins.items():
        assert pin['url'] == f"https://downloads.claude.ai/claude-code-releases/{pin['version']}/{platform}/claude"
        assert pin['source'] == 'deploy/packages/acc-packages' and len(pin['sha256']) == 64


@pytest.mark.parametrize('value', ['bad', 'A' * 64, 'a' * 63])
def test_native_claude_code_hash_is_validated(checkout, value):
    path = checkout / 'deploy/packages/acc-packages'
    text = path.read_text()
    path.write_text(re.sub(r'("linux-arm64": ")[0-9a-f]{64}"', lambda m: m.group(1) + value + '"', text, count=1))
    result = run(checkout)
    assert result.returncode == 1
    assert 'invalid SHA-256' in result.stderr


def test_native_claude_code_platforms_cannot_be_dropped(checkout):
    path = checkout / 'deploy/packages/acc-packages'
    text = path.read_text()
    path.write_text(re.sub(r',\s*"linux-arm64": "[0-9a-f]{64}"', '', text, count=1))
    result = run(checkout)
    assert result.returncode == 1
    assert 'linux-arm64' in result.stderr


def test_new_digest_in_the_package_helper_requires_inventory(checkout):
    path = checkout / 'deploy/packages/acc-packages'
    path.write_text(path.read_text() + '\nOTHER_SUM = "' + 'c' * 64 + '"\n')
    result = run(checkout)
    assert result.returncode == 1
    assert 'uninventoried' in result.stderr


def test_appimage_tools_are_in_the_inventory():
    """desktop/appimage/fetch-tools.sh pins three release downloads; tools.sha256 holds their digests after Tauri's
    normalization (linuxdeploy's AppImage magic blanked), which desktop/package.sh --appimage checks."""
    result = run(ROOT)
    assert result.returncode == 0, result.stderr
    pins = [p for p in json.loads(result.stdout) if p['source'].startswith('desktop/appimage/')]
    downloads = {p['name']: p for p in pins if p['variant'] == 'x86_64'}
    normalized = {p['name']: p for p in pins if p['variant'] == 'x86_64-normalized'}
    assert set(downloads) == set(normalized) == {
        'AppRun-x86_64', 'linuxdeploy-07333c6-x86_64.AppImage', 'linuxdeploy-plugin-appimage.AppImage'}
    plugin = downloads['linuxdeploy-plugin-appimage.AppImage']
    assert plugin['version'] == '1-alpha-20250213-1' and '/releases/download/1-alpha-20250213-1/' in plugin['url']
    assert plugin['sha256'] == normalized['linuxdeploy-plugin-appimage.AppImage']['sha256']  # not modified
    blanked = 'linuxdeploy-07333c6-x86_64.AppImage'
    assert downloads[blanked]['sha256'] != normalized[blanked]['sha256']


def test_unmodified_appimage_tool_digests_must_agree(checkout):
    path = checkout / 'desktop/appimage/tools.sha256'
    path.write_text(re.sub(r'^[0-9a-f]{64}(  AppRun-x86_64)$', 'd' * 64 + r'\1', path.read_text(), flags=re.M))
    result = run(checkout)
    assert result.returncode == 1
    assert 'AppRun-x86_64' in result.stderr and 'drift' in result.stderr


def test_every_fetched_appimage_tool_needs_a_normalized_digest(checkout):
    path = checkout / 'desktop/appimage/tools.sha256'
    path.write_text(''.join(line for line in path.read_text().splitlines(True) if 'AppRun' not in line))
    result = run(checkout)
    assert result.returncode == 1
    assert 'AppRun-x86_64' in result.stderr


def test_appimage_tool_hash_is_validated(checkout):
    path = checkout / 'desktop/appimage/fetch-tools.sh'
    path.write_text(path.read_text().replace('36a2d7e274d12e1050d0e9ecfe11d339ed54720b2bec464c286d53f8b07f5c62',
                                             '36A2D7E274D12E1050D0E9ECFE11D339ED54720B2BEC464C286D53F8B07F5C62'))
    result = run(checkout)
    assert result.returncode == 1
    assert 'invalid SHA-256' in result.stderr


def test_android_build_check_pins_are_in_the_inventory():
    """scripts/android_build_check.sh downloads Temurin, Gradle and the Android command-line tools on a disposable VM;
    each is pinned by URL and SHA-256 (checked against the publishers' checksums on 2026-10-26)."""
    result = run(ROOT)
    assert result.returncode == 0, result.stderr
    pins = {p['name']: p for p in json.loads(result.stdout) if p['source'] == 'scripts/android_build_check.sh'}
    assert set(pins) == {'temurin-jdk', 'gradle', 'android-commandlinetools'}
    assert pins['temurin-jdk']['version'] == '17.0.21' and '/jdk-17.0.21%2B9/' in pins['temurin-jdk']['url']
    assert pins['gradle']['sha256'] == '31c55713e40233a8303827ceb42ca48a47267a0ad4bab9177123121e71524c26'
    assert pins['android-commandlinetools']['version'] == '15859902'


@pytest.mark.parametrize('pattern, replacement, message', [
    (r'^jdk_version=.*$', 'jdk_version=17.0.22+7', 'temurin-jdk version drift'),
    (r'^Gradle: 8\.10\.2,', 'Gradle: 8.11.0,', 'gradle version drift'),
    (r'^Android command-line tools: \d+,', 'Android command-line tools: 11076708,', 'android-commandlinetools version drift'),
    (r'^cli_sha=.*$', 'cli_sha=' + 'A' * 64, 'invalid SHA-256'),
    (r'^gradle_sha=.*$', 'gradle_sha=' + 'a' * 64 + '\n# also ' + 'b' * 64, 'uninventoried SHA-256 literal'),
])
def test_android_build_check_drift_is_refused(checkout, pattern, replacement, message):
    path = checkout / 'scripts/android_build_check.sh'
    text = path.read_text()
    assert re.search(pattern, text, flags=re.M)
    path.write_text(re.sub(pattern, replacement, text, count=1, flags=re.M))
    result = run(checkout)
    assert result.returncode == 1
    assert message in result.stderr


def test_rc_version_is_valid_for_the_pin_inventory():
    import runpy

    inventory = runpy.run_path(str(SCRIPT))
    assert inventory['version']('0.15.0-rc.1', 'release') == '0.15.0-rc.1'
    assert inventory['version']('v0.15.0-rc.1', 'release') == '0.15.0-rc.1'
