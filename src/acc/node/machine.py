"""This node's machine identity for the public address: a P-256 key that never leaves the node, and the client
certificate the main's device authority signed for it. The certificate and key share one 0600 file, so a switch to
a successor is one atomic rename; the successor waits beside it until the main says the edge admits it."""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .config import write_atomic

IDENTITY = "machine.pem"
NEXT_KEY = "machine-next.key"
NEXT = "machine-next.pem"
SPKI = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo


def _read(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(fd)
        # Owned like its state folder (the node's service account; the root update helper reads it too).
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.lstat(path.parent).st_uid:
            return None  # a key others can read or replace is not this node's identity
        return file.read(65536)


def current(state_dir: Path | None) -> Path | None:
    """The identity file to present, when there is a usable one."""
    if state_dir is None:
        return None
    path = state_dir / IDENTITY
    return path if _read(path) else None


def present(context, state_dir: Path | None, private_dir: Path) -> bool:
    """Load the checked identity into `context` from a 0600 copy in `private_dir`, for a caller (the root update
    helper) that must not re-open a file in a folder another account controls by name. True when one was loaded."""
    raw = _read(state_dir / IDENTITY) if state_dir else None
    if not raw:
        return False
    fd, copy = tempfile.mkstemp(dir=private_dir, prefix=".machine.")
    try:
        with os.fdopen(fd, "wb") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(raw)
        context.load_cert_chain(copy)
    finally:
        os.unlink(copy)
    return True


def fingerprint(state_dir: Path | None, name: str = IDENTITY) -> str | None:
    raw = _read(state_dir / name) if state_dir else None
    if not raw:
        return None
    try:
        return x509.load_pem_x509_certificate(raw).fingerprint(hashes.SHA256()).hex()
    except ValueError:
        return None


def new_request(state_dir: Path, node_id: str) -> str:
    """A fresh successor key (kept here) and its CSR. The signer uses only the public key."""
    key = ec.generate_private_key(ec.SECP256R1())
    write_atomic(state_dir / NEXT_KEY, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()), 0o600)
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])).sign(key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def accept(state_dir: Path, pem: str) -> bool:
    """Keep a signed successor beside the current identity, if it certifies the successor key held here."""
    raw = _read(state_dir / NEXT_KEY)
    if not raw or not isinstance(pem, str) or len(pem) > 8192:
        return False
    try:
        key = serialization.load_pem_private_key(raw, password=None)
        cert = x509.load_pem_x509_certificate(pem.encode())
    except ValueError:
        return False
    if cert.public_key().public_bytes(*SPKI) != key.public_key().public_bytes(*SPKI):
        return False
    write_atomic(state_dir / NEXT, cert.public_bytes(serialization.Encoding.PEM) + raw, 0o600)
    (state_dir / NEXT_KEY).unlink(missing_ok=True)
    return True


def switch(state_dir: Path, expected: str) -> bool:
    """Make the successor current, only when it is the one the main says the edge now admits."""
    if fingerprint(state_dir, NEXT) != expected:
        return False
    os.replace(state_dir / NEXT, state_dir / IDENTITY)
    return True
