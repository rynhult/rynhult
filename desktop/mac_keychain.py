"""Desktop credentials behind one small interface: the owner's macOS login Keychain, or an in-memory fake for tests.

The Keychain adapter drives /usr/bin/security without ever putting a secret on a command line, where any process
could read it from the process table. Secrets are written through `security -i` on stdin, base64-encoded so no
quoting is involved, and read back with `find-generic-password -w` on stdout. Names are restricted to a fixed
character set, so nothing a caller passes can add Keychain commands.

Pilot limits, to qualify on the Mac (docs/DESKTOP-APPS.md test plan):
- Items made by the `security` tool trust that tool, so other processes of the same user that run `security` may read
  them without a prompt. The signed app should switch to Security.framework (SecItemAdd with the app's
  code-signing identity) before release; this interface stays the same.
- Errors are recognised by exit status and the item-not-found code (44).

Real use goes through `owner_store()`, which passes the macOS standalone gate first (mac_gate.require_ready).
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import subprocess
from typing import Protocol

SERVICE = 'com.rynhult.desktop'
SECURITY = '/usr/bin/security'
NAME = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
MAX_SECRET = 16384
NOT_FOUND = 44  # errSecItemNotFound, as security(1) exits


class CredentialError(RuntimeError):
    pass


class CredentialStore(Protocol):
    def get(self, name: str) -> str | None: ...

    def put(self, name: str, secret: str) -> None: ...

    def delete(self, name: str) -> bool: ...


def check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('Credential names are 1-64 lower-case letters, digits, dot, dash or underscore')
    return name


def check_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret:
        raise ValueError('A credential needs a non-empty text value')
    data = secret.encode()
    if len(data) > MAX_SECRET:
        raise ValueError(f'Credentials are at most {MAX_SECRET} bytes')
    return data


class MemoryStore:
    """The fake: same validation and behaviour, kept in this process only."""

    def __init__(self):
        self.items: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.items.get(check_name(name))

    def put(self, name: str, secret: str) -> None:
        check_secret(secret)
        self.items[check_name(name)] = secret

    def delete(self, name: str) -> bool:
        return self.items.pop(check_name(name), None) is not None


class KeychainStore:
    """Generic passwords in the calling user's default (login) Keychain: service SERVICE, account '<uid>.<name>'."""

    def __init__(self, owner_uid: int, *, run=subprocess.run, security: str = SECURITY):
        if owner_uid < 501:
            raise ValueError('Keychain items belong to an ordinary macOS login')
        self.owner_uid, self.run, self.security = owner_uid, run, security

    def account(self, name: str) -> str:
        return f'{self.owner_uid}.{check_name(name)}'

    def call(self, args: list[str], stdin: str | None = None):
        if os.geteuid() == 0 or os.getuid() != self.owner_uid:
            raise CredentialError("Keychain access runs only as the owner, never as root or another account")
        return self.run([self.security, *args], input=stdin, capture_output=True, text=True,
                        env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'HOME': os.path.expanduser('~')})

    def get(self, name: str) -> str | None:
        result = self.call(['find-generic-password', '-s', SERVICE, '-a', self.account(name), '-w'])
        if result.returncode == NOT_FOUND:
            return None
        if result.returncode != 0:
            raise CredentialError(f'Keychain read failed (security exit {result.returncode})')
        try:
            return base64.b64decode(result.stdout.strip(), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            raise CredentialError('Keychain item is not one Rynhult wrote') from None

    def put(self, name: str, secret: str) -> None:
        encoded = base64.b64encode(check_secret(secret)).decode()  # [A-Za-z0-9+/=]: nothing to quote
        # -U updates an existing item in place. Interactive mode reads the command from stdin, not argv.
        line = f'add-generic-password -U -s {SERVICE} -a {self.account(name)} -l Rynhult-Desktop -w {encoded}\n'
        result = self.call(['-i'], stdin=line)
        if result.returncode != 0 or 'security: ' in (result.stderr or ''):
            raise CredentialError(f'Keychain write failed (security exit {result.returncode})')
        if self.get(name) != secret:  # interactive mode may report errors only on stderr: read it back
            raise CredentialError('Keychain write could not be read back')

    def delete(self, name: str) -> bool:
        result = self.call(['delete-generic-password', '-s', SERVICE, '-a', self.account(name)])
        if result.returncode == NOT_FOUND:
            return False
        if result.returncode != 0:
            raise CredentialError(f'Keychain delete failed (security exit {result.returncode})')
        return True


def owner_store(owner_uid: int, **gate) -> CredentialStore:
    """The owner's Keychain, only once the macOS standalone gate passes."""
    from mac_gate import require_ready
    require_ready(owner_uid, **gate)
    return KeychainStore(owner_uid)
