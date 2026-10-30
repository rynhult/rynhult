"""Disposable OpenPGP keys; never import or use the project's release key."""
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture(scope='session')
def signing_keys(tmp_path_factory):
    homes = []

    def make(*, at=None, expiry='0'):
        home = tmp_path_factory.mktemp('release-test-key')
        home.chmod(0o700)
        homes.append(home)
        args = ['/usr/bin/gpg', '--no-options', '--homedir', str(home), '--batch', '--yes',
                '--pinentry-mode', 'loopback', '--passphrase', '']
        if at:
            args += ['--faked-system-time', str(at)]

        def run(*options, data=None):
            return subprocess.run([*args, *options], input=data, capture_output=True, check=True, timeout=30).stdout

        run('--quick-generate-key', 'Example Release Test <release@example.com>', 'ed25519', 'cert', '0')
        fingerprint = next(line.split(':')[9] for line in run('--with-colons', '--list-keys').decode().splitlines()
                           if line.startswith('fpr:'))
        run('--quick-add-key', fingerprint, 'ed25519', 'sign', expiry)

        def public():
            return run('--armor', '--export', fingerprint)

        def sign(body):
            return run('--armor', '--detach-sign', data=body)

        return SimpleNamespace(home=home, run=run, fingerprint=fingerprint, public=public, sign=sign)

    yield make
    for home in homes:
        subprocess.run(['/usr/bin/gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'],
                       capture_output=True, timeout=10, check=True)


@pytest.fixture
def release_signer(signing_keys, tmp_path, monkeypatch):
    from acc import release_signatures
    signer = signing_keys()
    key = tmp_path / 'trusted.asc'
    key.write_bytes(signer.public())
    monkeypatch.setattr(release_signatures, 'KEY_FILE', key)
    return signer
