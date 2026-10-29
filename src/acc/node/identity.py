"""The node's Ed25519 identity, and how it trusts the primary's TLS: the system CAs, or a private CA whose
certificate was checked by its SHA-256 at enrollment."""
from __future__ import annotations

import base64
import hashlib
import os
import platform
import re
import socket
import ssl
import subprocess
from functools import lru_cache
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .. import __version__
from ..bundle import installed_identity
from ..wsl import interop_enabled, isolation_state, wsl_version
from .config import write_atomic


class TrustError(Exception):
    pass


def generate_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def save_key(key: Ed25519PrivateKey, path: Path) -> None:
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    write_atomic(path, pem, 0o600)  # as whoever runs this: on Linux, root enrolls into a staging folder (acc-node)


def load_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 key")
    return key


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign_challenge(key: Ed25519PrivateKey, nonce_b64: str, primary_id: str) -> str:
    nonce = base64.b64decode(nonce_b64, validate=True)
    return base64.b64encode(key.sign(nonce + primary_id.encode())).decode()


@lru_cache(maxsize=1)
def build_identity():
    return installed_identity(Path(__file__).resolve().parents[3])


WINDOWS_VER = re.compile(r"\[Version (\d+)\.(\d+)\.(\d+)(?:\.(\d+))?\]")
CMD = Path("/mnt/c/Windows/System32/cmd.exe")


def windows_version(run=subprocess.run, cmd: Path = CMD) -> str | None:
    """The Windows that hosts this WSL, as "Windows 11 (build 22631)", through WSL's interop (cmd.exe /d /c ver; registry AutoRun disabled); None
    when interop is off or doesn't answer quickly."""
    if not cmd.exists():
        return None
    env = dict(os.environ)
    if not env.get("WSL_INTEROP"):  # a systemd service has no WSL_INTEROP: use the interop socket WSL made
        sockets = sorted(Path("/run/WSL").glob("*_interop")) if Path("/run/WSL").is_dir() else []
        if sockets:
            env["WSL_INTEROP"] = str(sockets[-1])
    try:
        out = run([str(cmd), "/d", "/c", "ver"], capture_output=True, text=True, timeout=3, env=env, cwd="/mnt/c").stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = WINDOWS_VER.search(out or "")
    if not match:
        return None
    major, build = int(match.group(1)), int(match.group(3))
    name = "Windows 11" if major == 10 and build >= 22000 else f"Windows {major}" if major != 10 else "Windows 10"
    return f"{name} (build {build})"


@lru_cache(maxsize=1)
def host_system() -> dict:
    """What the primary shows for this machine's system: whether it is WSL (and which), and its Windows host."""
    if platform.system() != "Linux":
        return {}
    wsl = wsl_version()
    windows = windows_version() if wsl != "0" else None
    distro = os.environ.get("WSL_DISTRO_NAME") if wsl != "0" else None
    return {"wsl": wsl, **({"windows": windows} if windows else {}), **({"wsl_distro": distro} if distro else {})}


def node_info() -> dict:
    system = host_system()
    return {"os": platform.system().lower(), "release": platform.release(), "arch": platform.machine(), "hostname": socket.gethostname(),
            "version": __version__, "build": build_identity(), **system,
            **({"wsl_isolation": isolation_state(interop_enabled(), {})} if system.get("wsl") in ("1", "2") else {})}


def tls_context(trust: str, ca_file: Path | None = None, ca_pem: str | None = None,
                identity: Path | None = None) -> ssl.SSLContext:
    """Full verification (chain, hostname, CERT_REQUIRED): against the system CAs, or only against the primary's
    private CA (a file once enrolled, PEM text during enrollment). `identity`, the node's machine identity (certificate
    and key in one file), is presented as the client certificate; it never changes which server is trusted."""
    if trust == "system":
        context = ssl.create_default_context()
    elif trust != "ca" or (ca_file is None and ca_pem is None):
        raise TrustError(f"cannot build a TLS context for trust mode {trust!r}")
    else:
        context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None, cadata=ca_pem)
    if identity is not None:
        context.load_cert_chain(identity)
    return context


def unverified_context() -> ssl.SSLContext:
    """Only for fetching the CA at enrollment, which is then checked by its fingerprint."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def check_ca(pem: bytes, fingerprint: str) -> str:
    """The primary's CA certificate (PEM) if the SHA-256 of its DER is `fingerprint`; else TrustError."""
    try:
        certificate = x509.load_pem_x509_certificate(pem)
    except ValueError as error:
        raise TrustError(f"the primary's CA is not a PEM certificate: {error}") from None
    seen = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
    if seen != fingerprint:
        raise TrustError(f"the primary's CA certificate has fingerprint {seen}, expected {fingerprint}; refusing it")
    return certificate.public_bytes(serialization.Encoding.PEM).decode()
