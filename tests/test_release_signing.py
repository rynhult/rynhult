"""Offline manifest signing, public trust-anchor consistency and fail-closed key selection."""
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRIMARY = '0C2CE12657BD833FB5B563B2B555F6E127990DB5'
SIGNING = 'B0724912051FF1F274CDEE7FEDE4F41B077BD2A7'
KEY = ROOT / 'keys/rynhult-release-key.asc'
SCRIPT = ROOT / 'scripts/sign-release.sh'


def gpg(home, *args, check=True):
    return subprocess.run(['gpg', '--no-options', '--homedir', str(home), '--batch', *args],
                          text=True, capture_output=True, check=check, timeout=30)


def fingerprints(listing):
    return [line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:')]


def test_public_key_and_verification_docs_agree(tmp_path):
    home = tmp_path / 'verify'
    home.mkdir(mode=0o700)
    listed = gpg(home, '--no-autostart', '--with-colons', '--import-options', 'show-only',
                 '--import', str(KEY))
    assert fingerprints(listed.stdout) == [PRIMARY, SIGNING]
    for name in ['SECURITY.md', 'README.md', 'docs/PUBLISHING-THE-SOURCE.md',
                 'docs/release-notes/TEMPLATE.md', 'docs/release-notes/0.15.0-rc.1.md',
                 'docs/wiki/User-Guide/Install.md', 'site/pages/security.html']:
        text = (ROOT / name).read_text()
        published = [value.replace(' ', '') for value in re.findall(r'\b[0-9A-F]{4}(?: [0-9A-F]{4}){9}\b', text)]
        assert PRIMARY in published, f'{name}: missing or drifted primary fingerprint'
        assert SIGNING in published, f'{name}: missing or drifted signing fingerprint'


@pytest.fixture
def test_key():
    # Keep agent sockets short, private and separate from any real keyring.
    with tempfile.TemporaryDirectory(prefix='acc-sign-key-', dir='/tmp') as scratch:
        home = Path(scratch) / 'gnupg'
        home.mkdir(mode=0o700)
        agent = subprocess.run(['gpg-connect-agent', '--homedir', str(home), 'GETINFO pid', '/bye'],
                               capture_output=True, text=True, check=True, timeout=10)
        agent_pid = int(re.search(r'^D (\d+)$', agent.stdout, re.MULTILINE)[1])
        try:
            gpg(home, '--no-autostart', '--pinentry-mode', 'loopback', '--passphrase', '',
                '--quick-generate-key', 'Release Test <release@example.com>', 'ed25519', 'cert', '1d')
            primary = fingerprints(gpg(home, '--no-autostart', '--with-colons', '--list-keys').stdout)[0]
            gpg(home, '--no-autostart', '--pinentry-mode', 'loopback', '--passphrase', '',
                '--quick-add-key', primary, 'ed25519', 'sign', '1d')
            primary, signing = fingerprints(gpg(home, '--no-autostart', '--with-colons',
                                                '--with-subkey-fingerprint', '--list-keys').stdout)
            yield home, primary, signing
        finally:
            os.kill(agent_pid, signal.SIGTERM)


def signing_checkout(tmp_path, test_key, *, trusted):
    home, primary, signing = test_key
    tree = tmp_path / 'checkout'
    (tree / 'scripts').mkdir(parents=True)
    (tree / 'keys').mkdir()
    script = SCRIPT.read_text()
    if trusted:
        # Substitute the test trust anchor only in a disposable copy, never in the real checkout.
        script = script.replace(PRIMARY, primary).replace(SIGNING, signing)
        key = gpg(home, '--no-autostart', '--armor', '--export', primary).stdout
    else:
        key = KEY.read_text()
    (tree / 'scripts/sign-release.sh').write_text(script)
    (tree / 'keys/rynhult-release-key.asc').write_text(key)
    return tree / 'scripts/sign-release.sh'


def test_signing_refuses_an_unrelated_private_key_without_creating_a_signature(tmp_path, test_key):
    script = signing_checkout(tmp_path, test_key, trusted=False)
    manifest = tmp_path / 'SHA256SUMS'
    manifest.write_text('0' * 64 + '  artifact.tar.gz\n')
    result = subprocess.run(['bash', str(script), str(manifest)], capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'GNUPGHOME': str(test_key[0])})
    assert result.returncode != 0
    assert 'fingerprint' in result.stderr.lower()
    assert not manifest.with_suffix('.asc').exists()


@pytest.mark.parametrize('shell,folder,home_mode', [
    ('bash', True, 'explicit'), ('bash', False, 'explicit'), ('bash', False, 'default'),
    ('bash', False, 'unset-deep'), ('zsh', False, 'explicit')])
def test_signs_only_the_manifest_and_verifies_against_the_repository_key(tmp_path, test_key, shell, folder, home_mode):
    if not shutil.which(shell):
        pytest.skip(f'{shell} is not installed')
    script = signing_checkout(tmp_path, test_key, trusted=True)
    release = tmp_path / 'release files'
    if home_mode == 'unset-deep':
        release = release.joinpath(*['nested-' + 'x' * 64] * 4)
    release.mkdir(parents=True)
    manifest = release / 'SHA256SUMS'
    manifest.write_text('0' * 64 + '  artifact.tar.gz\n')
    original = manifest.read_bytes()
    env = {**os.environ, 'GNUPGHOME': str(test_key[0])}
    if home_mode == 'default':
        env.pop('GNUPGHOME')
        env['HOME'] = str(test_key[0].parent / 'owner')
        Path(env['HOME']).mkdir()
        (Path(env['HOME']) / 'rynhult-signing').symlink_to(test_key[0], target_is_directory=True)
    elif home_mode == 'unset-deep':
        env.pop('HOME', None)
    result = subprocess.run([shell, str(script), str(release if folder else manifest)],
                            capture_output=True, text=True, timeout=30,
                            env=env)
    assert result.returncode == 0, result.stderr
    signature = release / 'SHA256SUMS.asc'
    assert signature.is_file()
    assert manifest.read_bytes() == original
    assert sorted(p.name for p in release.iterdir()) == ['SHA256SUMS', 'SHA256SUMS.asc']
    verifier = test_key[0].parent / 'verifier'
    verifier.mkdir(mode=0o700)
    gpg(verifier, '--no-autostart', '--import', str(script.parents[1] / 'keys/rynhult-release-key.asc'))
    verified = gpg(verifier, '--no-autostart', '--status-fd', '1', '--verify', str(signature), str(manifest))
    assert f'[GNUPG:] VALIDSIG {test_key[2]} ' in verified.stdout
    assert 'gh release upload' in result.stdout
    assert 'PRIVATE KEY' not in result.stdout + result.stderr
    manifest.write_text('tampered\n')
    assert gpg(verifier, '--no-autostart', '--verify', str(signature), str(manifest), check=False).returncode != 0


def test_signing_refuses_a_replaced_repository_key_and_preserves_existing_signature(tmp_path, test_key):
    script = signing_checkout(tmp_path, test_key, trusted=False)
    (script.parents[1] / 'keys/rynhult-release-key.asc').write_text(
        gpg(test_key[0], '--no-autostart', '--armor', '--export', test_key[1]).stdout)
    manifest = tmp_path / 'SHA256SUMS'
    manifest.write_text('0' * 64 + '  artifact.tar.gz\n')
    signature = tmp_path / 'SHA256SUMS.asc'
    signature.write_text('existing signature\n')
    env = {**os.environ, 'GNUPGHOME': str(test_key[0])}
    result = subprocess.run(['bash', str(script), str(manifest)], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert signature.read_text() == 'existing signature\n'
    signature.unlink()
    result = subprocess.run(['bash', str(script), str(manifest)], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and 'fingerprint' in result.stderr.lower()
    assert not signature.exists()


@pytest.mark.parametrize('bad_key', ['missing', 'noisy'])
def test_signing_reports_bounded_public_key_import_errors(tmp_path, test_key, bad_key):
    script = signing_checkout(tmp_path, test_key, trusted=False)
    key = script.parents[1] / 'keys/rynhult-release-key.asc'
    if bad_key == 'missing':
        key.unlink()
        diagnostic = 'No such file or directory'
    else:
        key.write_text('-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n' + '!' * 1000
                       + '\n-----END PGP PUBLIC KEY BLOCK-----\n')
        diagnostic = 'invalid radix64 character'
    release = tmp_path.joinpath(*['deep-' + 'x' * 64] * 4)
    release.mkdir(parents=True)
    manifest = release / 'SHA256SUMS'
    manifest.write_text('0' * 64 + '  artifact.tar.gz\n')
    env = {**os.environ, 'GNUPGHOME': str(test_key[0]), 'LC_ALL': 'C'}
    env.pop('HOME', None)
    result = subprocess.run(['bash', str(script), str(manifest)], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert len(result.stderr.encode()) <= 4352
    assert 'PRIVATE KEY' not in result.stdout + result.stderr
    assert not (release / 'SHA256SUMS.asc').exists()
