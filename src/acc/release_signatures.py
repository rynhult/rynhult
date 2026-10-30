"""Verify detached release signatures using only the installed public anchor."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

KEY_FILE = Path(__file__).resolve().parents[2] / 'keys/rynhult-release-key.asc'
if not KEY_FILE.is_file():  # wheel resource, independent of the candidate archive
    KEY_FILE = Path(__file__).with_name('_release-key.asc')


class SignatureError(ValueError):
    """Fixed, owner-readable verification error; never includes GnuPG output."""


class MissingSignature(SignatureError):
    def __init__(self):
        super().__init__('Release signature is missing. Updates require a signed SHA256SUMS file.')


def verify(sums: bytes, signature: bytes) -> dict:
    if not signature or len(signature) > 128 << 10 or len(sums) > 1 << 20:
        raise SignatureError('Release signature is invalid or too large.')
    try:
        key = KEY_FILE.read_bytes()
        if len(key) > 1 << 20:
            raise SignatureError('Installed release key is invalid. Reinstall from a trusted source.')
        with tempfile.TemporaryDirectory(prefix='acc-signature-') as temporary:
            home = Path(temporary)
            args = ['/usr/bin/gpg', '--no-options', '--homedir', temporary, '--batch', '--no-tty',
                    '--no-auto-key-retrieve', '--no-auto-key-import', '--auto-key-locate', 'clear', '--no-autostart',
                    '--no-auto-check-trustdb']

            def run(*options, data=None):
                return subprocess.run([*args, *options], input=data, capture_output=True, timeout=20,
                                      env={'PATH': '/usr/bin:/bin', 'HOME': temporary, 'GNUPGHOME': temporary,
                                           'LC_ALL': 'C'})

            imported = run('--import', data=key)
            if imported.returncode:
                raise SignatureError('Installed release key is invalid. Reinstall from a trusted source.')
            listing = run('--with-colons', '--fingerprint', '--list-keys')
            primaries = set()
            primary = False
            for line in listing.stdout.decode('ascii', errors='replace').splitlines():
                fields = line.split(':')
                if fields[0] == 'pub':
                    primary = True
                elif fields[0] == 'sub':
                    primary = False
                elif fields[0] == 'fpr' and primary:
                    primaries.add(fields[9])
                    primary = False
            (home / 'sums').write_bytes(sums)
            (home / 'signature').write_bytes(signature)
            result = run('--status-fd', '1', '--verify', str(home / 'signature'), str(home / 'sums'))
            statuses = [line.split()[1:] for line in result.stdout.decode('ascii', errors='replace').splitlines()
                        if line.startswith('[GNUPG:] ')]
            bad = {'BADSIG', 'ERRSIG', 'NO_PUBKEY', 'EXPSIG', 'EXPKEYSIG', 'REVKEYSIG',
                   'KEYEXPIRED', 'SIGEXPIRED', 'KEYREVOKED', 'FAILURE', 'ERROR'}
            valid = [row for row in statuses if row and row[0] == 'VALIDSIG']
            good = [row for row in statuses if row and row[0] == 'GOODSIG']
            if (result.returncode or any(row[0] in bad for row in statuses if row)
                    or len(valid) != 1 or len(good) != 1 or len(valid[0]) < 11
                    or valid[0][8] not in {'8', '9', '10', '11'} or valid[0][10] not in primaries):
                raise SignatureError('Release signature is invalid, expired, revoked or signed by an untrusted key. Update refused.')
            return {'status': 'verified', 'primary': valid[0][10], 'name': 'Rynhult Releases'}
    except FileNotFoundError:
        raise SignatureError('Release verification needs GnuPG and the installed release key. Ask the installation owner to repair them.') from None
    except (OSError, subprocess.TimeoutExpired):
        raise SignatureError('Release signature verification could not finish. Update refused; try again after checking GnuPG.') from None
