#!/usr/bin/python3 -I
"""Uninstalled device signer. Fixed stdin contract; never accepts paths.

Provisioning calls initialize() from a reviewed owner installation step. The runtime
entry point only issues client identities from its fixed root-owned directory: browser
identities with a transient key, machine identities for a node-held key's CSR, and
native identities for an app-held key's CSR.
Encrypted backup and restore are root-only owner operations (acc-device-ca-recovery.py),
never reachable through the stdin contract.
"""
import base64
import fcntl
import functools
import json
import os
import re
import secrets
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

STATE = Path('/var/lib/acc-device-ca')
# Issuances per trailing hour. Pairing needs an owner's confirmation each time, so a
# caller beyond this is a compromised or broken accd, not a person.
ISSUE_LIMIT = 20
# Machine identities: a distinct subject (OU) the edge routes on, and a shorter bounded life
# that nodes rotate automatically over their signed link.
MACHINE_OU = 'acc-machine'
MACHINE_DAYS = 30
BACKUP_MAGIC = b'ACC-DEVICE-CA-BACKUP-1\n'


def private_directory(directory):
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('device authority directory must be a private directory owned by the signer')


def private_read(directory, name):
    try:
        fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:  # a symlink (ELOOP) or a missing file
        raise ValueError('device authority files must be private to the signer') from error
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('device authority files must be private to the signer')
        return stream.read()


def issuance_lock(method):
    """One issuance, or one rollover step, at a time. An issuance reads ca.key and ca.pem, signs and records under it,
    and a rollover step renames those files under it, so no request reads one authority's key with the other's
    certificate (edge integration review, EI-5). The lock is the authority directory itself (flock on its descriptor):
    it adds no file. Taken before the ledger's own lock, never inside it."""
    @functools.wraps(method)
    def locked(directory, *args):
        private_directory(directory)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('device authority directory must be a private directory owned by the signer')
            fcntl.flock(fd, fcntl.LOCK_EX)
            return method(directory, *args)
        finally:
            os.close(fd)
    return locked


def record(directory, entry):
    """Append-only issuance ledger outside accd's reach, bounded per trailing hour."""
    fd = os.open(directory / 'issued.log', os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as stream:
        fcntl.flock(fd, fcntl.LOCK_EX)
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('device authority ledger must be private to the signer')
        stream.seek(0)
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        if sum(1 for line in stream if json.loads(line)['issued_at'] > since) >= ISSUE_LIMIT:
            raise ValueError('issuance rate exceeded')
        stream.write(json.dumps(entry) + '\n')
        stream.flush()
        os.fsync(fd)


def initialize(directory, name='Rynhult device CA'):
    """Owner-only provisioning primitive; deliberately absent from stdin protocol."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
            .sign(key, hashes.SHA256()))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_directory(directory)
    # Exclusive creation: provisioning must never silently replace an authority.
    for name, data in [('ca.key', key.private_bytes(serialization.Encoding.PEM,
                       serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
                       ('ca.pem', cert.public_bytes(serialization.Encoding.PEM))]:
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)


def authority(directory, days):
    private_directory(directory)
    ca_key = serialization.load_pem_private_key(private_read(directory, 'ca.key'), password=None)
    ca = x509.load_pem_x509_certificate(private_read(directory, 'ca.pem'))
    # The two files are read separately and a rollover promote renames them one at a time: never sign with one
    # authority's key under the other's name (edge integration review, EI-1).
    spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if ca_key.public_key().public_bytes(*spki) != ca.public_key().public_bytes(*spki):
        raise ValueError('device authority is changing; try again')
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=days)
    if ca.not_valid_after_utc <= expiry or ca.not_valid_before_utc > now:
        raise ValueError('device authority needs owner maintenance')
    return ca_key, ca, now, expiry


def issue_machine(directory, request):
    """Sign only the public key of a node-held P-256 key; every requested name or extension is ignored."""
    if (not isinstance(request, dict) or set(request) != {'operation', 'node_id', 'csr', 'days'}
            or request['operation'] != 'issue_machine' or not isinstance(request['node_id'], str)
            or not re.fullmatch('[0-9a-f]{16,32}', request['node_id']) or not isinstance(request['csr'], str)
            or len(request['csr']) > 2048 or type(request['days']) is not int or not 1 <= request['days'] <= MACHINE_DAYS):
        raise ValueError('invalid signing request')
    csr = x509.load_pem_x509_csr(request['csr'].encode())
    public = csr.public_key()
    if not csr.is_signature_valid or not isinstance(public, ec.EllipticCurvePublicKey) or public.curve.name != 'secp256r1':
        raise ValueError('invalid signing request')
    ca_key, ca, now, expiry = authority(directory, request['days'])
    node_id = request['node_id']
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id),
                                     x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, MACHINE_OU)]))
            .issuer_name(ca.subject).public_key(public).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(expiry)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(f'urn:acc:machine:{node_id}')]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    record(directory, {'issued_at': now.isoformat(), 'machine_id': node_id,
                       'serial': str(cert.serial_number), 'not_after': expiry.isoformat()})
    return {'certificate': cert.public_bytes(serialization.Encoding.PEM).decode()}


def issue_native(directory, request):
    """Native app-held P-256 key. No requested subject, extension or path is honored."""
    if (set(request) != {'operation', 'device_id', 'csr', 'days'}
            or not isinstance(request['device_id'], str) or not re.fullmatch('[0-9a-f]{32}', request['device_id'])
            or not isinstance(request['csr'], str) or len(request['csr']) > 2048
            or type(request['days']) is not int or not 1 <= request['days'] <= 90):
        raise ValueError('invalid signing request')
    csr = x509.load_pem_x509_csr(request['csr'].encode())
    public = csr.public_key()
    if not csr.is_signature_valid or not isinstance(public, ec.EllipticCurvePublicKey) or public.curve.name != 'secp256r1':
        raise ValueError('invalid signing request')
    ca_key, ca, now, expiry = authority(directory, request['days'])
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, request['device_id']),
                                     x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, 'acc-native')]))
            .issuer_name(ca.subject).public_key(public).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(expiry)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
            .sign(ca_key, hashes.SHA256()))
    record(directory, {'issued_at': now.isoformat(), 'native_device_id': request['device_id'],
                       'serial': str(cert.serial_number), 'not_after': expiry.isoformat()})
    return {'certificate': cert.public_bytes(serialization.Encoding.PEM).decode()}


def current_authority(directory):
    """The public certificate new identities are issued under, so accd can tell which leaves predate a rollover,
    and the previous authority's fingerprint and whether its key still exists, so accd can audit both."""
    private_directory(directory)
    ca_pem = private_read(directory, 'ca.pem')
    previous = directory / 'previous'
    return {'certificate': ca_pem.decode(), 'fingerprint': fingerprint(x509.load_pem_x509_certificate(ca_pem)),
            'previous': (fingerprint(x509.load_pem_x509_certificate(private_read(previous, 'ca.pem')))
                         if os.path.lexists(previous / 'ca.pem') else None),
            'previous_key': os.path.lexists(previous / 'ca.key')}


@issuance_lock
def issue(directory, request):
    if isinstance(request, dict) and request.get('operation') == 'issue_native':
        return issue_native(directory, request)
    if isinstance(request, dict) and request.get('operation') == 'issue_machine':
        return issue_machine(directory, request)
    if isinstance(request, dict) and request.get('operation') == 'authority':
        if set(request) != {'operation'}:
            raise ValueError('invalid signing request')
        return current_authority(directory)
    if (not isinstance(request, dict) or set(request) != {'operation', 'device_id', 'days'}
            or request['operation'] != 'issue' or not isinstance(request['device_id'], str)
            or not re.fullmatch('[0-9a-f]{32}', request['device_id'])
            or type(request['days']) is not int or not 1 <= request['days'] <= 90):
        raise ValueError('invalid signing request')
    ca_key, ca, now, expiry = authority(directory, request['days'])
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, request['device_id'])]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(expiry)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
            .sign(ca_key, hashes.SHA256()))
    record(directory, {'issued_at': now.isoformat(), 'device_id': request['device_id'],
                       'serial': str(cert.serial_number), 'not_after': expiry.isoformat()})
    password = secrets.token_urlsafe(24)
    bundle = pkcs12.serialize_key_and_certificates(request['device_id'].encode(), key, cert, [ca],
                                                 serialization.BestAvailableEncryption(password.encode()))
    return {'certificate': cert.public_bytes(serialization.Encoding.PEM).decode(),
            'pkcs12': base64.b64encode(bundle).decode(), 'password': password}


def fingerprint(cert):
    return cert.fingerprint(hashes.SHA256()).hex()


# Planned rollover (owner-only, through the recovery tool; never the stdin contract). The edge trusts the staged
# authority before it issues anything, and each step either completes, resumes or refuses: a half-moved authority
# leaves no ca.pem at the top, so the signer refuses to issue until promote is run again.
EDGE_CURRENT, EDGE_PREVIOUS = 'device-ca.pem', 'device-ca-previous.pem'


def edge_file(helper_dir, name):
    """The edge's trust file, or None when it is absent. A link or anything but a plain file is refused, as the edge
    helper refuses it, never read as absent (edge integration review, EI-5)."""
    try:
        fd = os.open(Path(helper_dir) / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(f'the edge trust file {name} is unsafe (a link?): replace it with a plain file') from None
    info = os.fstat(fd)  # before fdopen, which can't open a folder
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise ValueError(f'the edge trust file {name} is unsafe (a link?): replace it with a plain file')
    with os.fdopen(fd, 'rb') as stream:
        return stream.read(65537)


def rollover_log(directory):
    """The record's descriptor, once it is known to be private. Each step opens it before changing anything, so an
    unusable record refuses the step instead of leaving a change it can't record."""
    fd = os.open(directory / 'rollover.log', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError('device authority rollover record must be private to the signer')
    return fd


def rollover_record(directory, entry):
    """Root-only append-only record of rollover steps, beside the issuance ledger (which counts every line)."""
    fd = rollover_log(directory)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(), **entry}) + '\n')
        stream.flush()
        os.fsync(fd)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def drop_previous_key(directory):
    """Nothing issues from the old key after promote and there is no abort path, so keeping it only adds risk;
    the old certificate stays until finish so the old leaves still verify."""
    previous = directory / 'previous'
    private_directory(previous)
    if not os.path.lexists(previous / 'ca.key'):
        return
    os.unlink(previous / 'ca.key')
    sync_directory(previous)
    rollover_record(directory, {'event': 'promote',
                                'authority': fingerprint(x509.load_pem_x509_certificate(private_read(directory, 'ca.pem'))),
                                'previous': fingerprint(x509.load_pem_x509_certificate(private_read(previous, 'ca.pem'))),
                                'previous_key_deleted': True})


@issuance_lock
def rollover_stage(directory):
    """Create the next authority beside the current one. Nothing issues from it until promote."""
    private_directory(directory)
    os.close(rollover_log(directory))
    if os.path.lexists(directory / 'previous'):
        raise ValueError('finish the previous authority rollover first')
    if os.path.lexists(directory / 'next'):
        raise ValueError('an authority is already staged')
    current = x509.load_pem_x509_certificate(private_read(directory, 'ca.pem'))
    name = 'Rynhult device CA ' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    if current.subject.rfc4514_string() == 'CN=' + name:
        raise ValueError('stage again in a second')
    os.mkdir(directory / 'next', 0o700)
    initialize(directory / 'next', name)
    staged = fingerprint(x509.load_pem_x509_certificate(private_read(directory / 'next', 'ca.pem')))
    rollover_record(directory, {'event': 'stage', 'authority': staged})
    return staged


@issuance_lock
def rollover_promote(directory, helper_dir):
    """Issue from the staged authority; the current one moves to previous/ and its key is deleted.
    Resumable; idempotent once done."""
    private_directory(directory)
    os.close(rollover_log(directory))
    staged, previous = directory / 'next', directory / 'previous'
    top = os.path.lexists(directory / 'ca.pem')
    if not os.path.lexists(staged):
        if top and os.path.lexists(previous):
            drop_previous_key(directory)  # an earlier promote died after the swap
            return fingerprint(x509.load_pem_x509_certificate(private_read(directory, 'ca.pem')))
        raise ValueError('nothing staged')
    private_directory(staged)
    new_pem = private_read(staged, 'ca.pem')
    private_read(staged, 'ca.key')
    if top and not os.path.lexists(previous):
        old_pem = private_read(directory, 'ca.pem')
    elif not top and os.path.lexists(previous):
        private_directory(previous)
        old_pem = private_read(previous, 'ca.pem')
    else:
        raise ValueError('unexpected authority state; resolve it by hand from the encrypted backup')
    if edge_file(helper_dir, EDGE_CURRENT) != new_pem or edge_file(helper_dir, EDGE_PREVIOUS) != old_pem:
        raise ValueError('the edge must trust both authorities first: install the staged ca.pem as '
                         f'{EDGE_CURRENT} and the current one as {EDGE_PREVIOUS}')
    if top:
        os.mkdir(previous, 0o700)
        for name in ('ca.key', 'ca.pem'):
            os.rename(directory / name, previous / name)
    for name in ('ca.key', 'ca.pem'):
        os.rename(staged / name, directory / name)
    os.rmdir(staged)
    sync_directory(directory)
    drop_previous_key(directory)
    return fingerprint(x509.load_pem_x509_certificate(new_pem))


@issuance_lock
def rollover_finish(directory, helper_dir):
    """Delete the previous authority's certificate (its key went at promote) once the edge no longer trusts it."""
    private_directory(directory)
    os.close(rollover_log(directory))
    previous = directory / 'previous'
    if not os.path.lexists(previous):
        raise ValueError('nothing to finish')
    if edge_file(helper_dir, EDGE_PREVIOUS) is not None:
        raise ValueError(f'the edge still trusts the previous authority: remove its {EDGE_PREVIOUS} once Remote access '
                         'shows no identity left on it')
    private_directory(previous)
    retired = (fingerprint(x509.load_pem_x509_certificate(private_read(previous, 'ca.pem')))
               if os.path.lexists(previous / 'ca.pem') else None)
    for name in ('ca.key', 'ca.pem'):
        if os.path.lexists(previous / name):
            os.unlink(previous / name)
    os.rmdir(previous)
    current = fingerprint(x509.load_pem_x509_certificate(private_read(directory, 'ca.pem')))
    rollover_record(directory, {'event': 'finish', 'authority': current, 'previous': retired})
    return current


def sealing_key(passphrase, salt):
    if len(passphrase) < 16:
        raise ValueError('backup passphrase is too short')
    return Scrypt(salt=salt, length=32, n=2 ** 17, r=8, p=1).derive(passphrase)


def export_backup(directory, passphrase):
    """Authenticated encryption of the authority and its ledger. Returns bytes; nothing touches disk here."""
    private_directory(directory)
    ca_pem = private_read(directory, 'ca.pem')
    try:
        ledger = private_read(directory, 'issued.log').decode()
    except ValueError:
        if (directory / 'issued.log').exists():
            raise
        ledger = ''
    plain = json.dumps({'format': 1, 'ca_pem': ca_pem.decode(), 'ca_key_pem': private_read(directory, 'ca.key').decode(),
                        'ledger': ledger, 'fingerprint': fingerprint(x509.load_pem_x509_certificate(ca_pem))}).encode()
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    return BACKUP_MAGIC + salt + nonce + AESGCM(sealing_key(passphrase, salt)).encrypt(nonce, plain, BACKUP_MAGIC)


def restore_backup(blob, passphrase, directory, expected_fingerprint):
    """Restore into a new private directory only after the owner's independently recorded fingerprint matches."""
    if not re.fullmatch('[0-9a-f]{64}', expected_fingerprint or ''):
        raise ValueError('an independently recorded authority fingerprint is required')
    if not blob.startswith(BACKUP_MAGIC) or len(blob) > (1 << 24):
        raise ValueError('not a device authority backup')
    salt, nonce, sealed = blob[len(BACKUP_MAGIC):][:16], blob[len(BACKUP_MAGIC) + 16:][:12], blob[len(BACKUP_MAGIC) + 28:]
    try:
        plain = json.loads(AESGCM(sealing_key(passphrase, salt)).decrypt(nonce, sealed, BACKUP_MAGIC))
    except Exception:
        raise ValueError('backup cannot be decrypted with this passphrase or was altered') from None
    cert = x509.load_pem_x509_certificate(plain['ca_pem'].encode())
    key = serialization.load_pem_private_key(plain['ca_key_pem'].encode(), password=None)
    if fingerprint(cert) != expected_fingerprint:
        raise ValueError('backup authority fingerprint differs from the recorded fingerprint')
    if (key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            != cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            or not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            or set(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) != {ExtendedKeyUsageOID.CLIENT_AUTH}):
        raise ValueError('backup is not a client-only device authority')
    # Never merge into, or replace, an existing authority directory.
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    private_directory(directory)
    for name, data in [('ca.key', plain['ca_key_pem'].encode()), ('ca.pem', plain['ca_pem'].encode()),
                       ('issued.log', plain['ledger'].encode())]:
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    return fingerprint(cert)


def main():
    try:
        if len(sys.argv) != 1:
            raise ValueError('arguments forbidden')
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise ValueError('request too large')
        result = issue(STATE, json.loads(raw))
        sys.stdout.write(json.dumps(result))
    except Exception:
        # Never print key material, input or a traceback at this privilege boundary.
        sys.stderr.write('device signing refused\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
