"""`acc-node enroll`: make a key pair, register its public half with the primary using a one-time code.

Trust is either the system CAs (`--trust system`, e.g. Let's Encrypt), or the primary's private CA (`--ca-fingerprint`,
e.g. Caddy's internal CA): its root is fetched from /api/nodes/ca without verification, accepted only if its SHA-256
matches, saved as ca.pem, and every connection after that (the enrollment POST included) verifies against it.
"""
from __future__ import annotations

import http.client
import json
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from .config import CA_FILE, KEY_FILE, NODE_NAME, ConfigError, NodeConfig, normalize_fingerprint, write_atomic
from .identity import check_ca, generate_key, node_info, public_key_b64, save_key, tls_context, unverified_context

MAX_CA_SIZE = 64 * 1024


class EnrollError(Exception):
    pass


def primary_origin(primary: str) -> tuple[str, int | None, str]:
    parts = urlsplit(primary)
    if parts.scheme != "https" or not parts.hostname:
        raise ConfigError(f"bad primary URL {primary!r}: expected https://host[:port]")
    return parts.hostname, parts.port, parts.path.rstrip("/")


def request(primary: str, context: ssl.SSLContext, method: str, path: str, body: dict | None = None,
            limit: int | None = None) -> tuple[int, bytes]:
    host, port, prefix = primary_origin(primary)
    connection = http.client.HTTPSConnection(host, port, timeout=30, context=context)
    try:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        connection.request(method, prefix + path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(limit) if limit else response.read()
    finally:
        connection.close()


def fetch_ca(primary: str, fingerprint: str) -> str:
    status, data = request(primary, unverified_context(), "GET", "/api/nodes/ca", limit=MAX_CA_SIZE)
    if status != 200:
        raise EnrollError(f"cannot fetch the primary's CA: HTTP {status}")
    return check_ca(data, fingerprint)


def enroll(*, primary: str, code: str, name: str, config_path: Path, state_dir: Path, socket_dir: Path,
           trust: str = "system", ca_fingerprint: str | None = None,
           installed_state_dir: Path | None = None) -> NodeConfig:
    """Writes the key (and CA) to `state_dir`, the config to `config_path`. `installed_state_dir`: where the key and
    CA will be once handed over (deploy/node/acc-node enrolls into a staging folder, then acc-node adopts them)."""
    primary = primary.rstrip("/")
    primary_origin(primary)
    if not NODE_NAME.fullmatch(name):
        raise ConfigError(f"bad node name {name!r}: lowercase letters, digits and dashes, up to 40")
    ca_pem = None
    if trust == "ca":
        ca_fingerprint = normalize_fingerprint(ca_fingerprint or "")
        ca_pem = fetch_ca(primary, ca_fingerprint)
    elif trust != "system":
        raise ConfigError(f"bad trust mode {trust!r}: expected system or ca")
    key = generate_key()
    status, data = request(primary, tls_context(trust, ca_pem=ca_pem), "POST", "/api/nodes/enroll",
                           {"name": name, "code": code, "public_key": public_key_b64(key), "info": node_info()})
    try:
        answer = json.loads(data) if data else {}
    except ValueError:
        answer = {}
    if status != 200:
        detail = answer.get("detail") if isinstance(answer, dict) else None
        raise EnrollError(f"the primary refused enrollment: HTTP {status} {detail or data[:200]!r}")
    node_id = answer.get("node_id") if isinstance(answer, dict) else None
    if node_id in (None, ""):
        raise EnrollError("the primary's answer has no node_id")
    # Only now replace an earlier key and CA: a refused enrollment leaves a working node as it was.
    save_key(key, state_dir / KEY_FILE)
    ca_file = None
    if ca_pem is not None:
        write_atomic(state_dir / CA_FILE, ca_pem.encode(), 0o644)
        ca_file = ((installed_state_dir or state_dir) / CA_FILE).absolute()
    config = NodeConfig(primary=primary, node_id=str(node_id), trust=trust, ca_file=ca_file,
                        ca_fingerprint=ca_fingerprint if trust == "ca" else "", name=name, socket_dir=socket_dir)
    config.write(config_path)
    return config
