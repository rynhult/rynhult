"""acc-node configuration: an env file (primary URL, how to trust it, node id, socket dir) plus a state dir (key, CA,
status)."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from acc.env import Environment

DEFAULT_CONFIG = Path("/etc/acc-node.env")
DEFAULT_STATE_DIR = Path("/var/lib/acc-node")
DEFAULT_SOCKET_DIR = Path("/run/acc-broker")
NODE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
FINGERPRINT = re.compile(r"[0-9a-f]{64}")
KEY_FILE = "node.key"
CA_FILE = "ca.pem"
TRUST_MODES = ("system", "ca")  # system CAs, or the private CA saved at enrollment
STATUS_FILE = "status.json"


class ConfigError(Exception):
    pass


def normalize_fingerprint(value: str) -> str:
    """SHA-256 hex, with or without colons, any case → 64 lowercase hex digits."""
    fingerprint = value.replace(":", "").strip().lower()
    if not FINGERPRINT.fullmatch(fingerprint):
        raise ConfigError(f"bad fingerprint {value!r}: expected the SHA-256 of the primary's CA certificate in hex")
    return fingerprint


def read_env_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class NodeConfig:
    primary: str  # https://acc.example.lan
    node_id: str
    trust: str = "system"
    ca_file: Path | None = None  # trust "ca": the primary's private CA, checked against ca_fingerprint at enrollment
    ca_fingerprint: str = ""
    name: str = ""
    socket_dir: Path = DEFAULT_SOCKET_DIR

    folder_policy_socket: Path | None = None  # trusted local config; absent means not qualified

    alternate_primaries: tuple[str, ...] = ()  # owner-provisioned; same verified TLS trust

    def __post_init__(self):
        for primary in self.alternate_primaries:
            parsed = urlsplit(primary)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ConfigError("Alternate mains must be approved HTTPS URLs without credentials, queries or fragments")
        if self.trust not in TRUST_MODES:
            raise ConfigError(f"bad trust mode {self.trust!r}: expected system or ca")
        if self.trust == "ca" and self.ca_file is None:
            raise ConfigError("trust mode ca needs the CA file")

    @classmethod
    def load(cls, path: Path) -> "NodeConfig":
        try:
            env = Environment(read_env_file(path))
        except OSError as error:
            raise ConfigError(f"cannot read {path}: {error} (run `acc-node enroll` first)") from error
        required = ["ACC_NODE_PRIMARY", "ACC_NODE_ID", "ACC_NODE_TRUST"]
        if env.get("ACC_NODE_TRUST") == "ca":
            required.append("ACC_NODE_CA_FILE")
        missing = [k for k in required if not env.get(k)]
        if missing:
            raise ConfigError(f"{path} lacks {', '.join(missing)} (run `acc-node enroll` again)")
        return cls(primary=env["ACC_NODE_PRIMARY"].rstrip("/"), node_id=env["ACC_NODE_ID"],
                   trust=env["ACC_NODE_TRUST"],
                   ca_file=Path(env["ACC_NODE_CA_FILE"]) if env.get("ACC_NODE_CA_FILE") else None,
                   ca_fingerprint=env.get("ACC_NODE_CA_FINGERPRINT", ""), name=env.get("ACC_NODE_NAME", ""),
                   socket_dir=Path(env.get("ACC_NODE_SOCKET_DIR") or DEFAULT_SOCKET_DIR),
                   folder_policy_socket=Path(env["ACC_FOLDER_POLICY_SOCKET"]) if env.get("ACC_FOLDER_POLICY_SOCKET") else None,
                   alternate_primaries=tuple(p.rstrip("/") for p in env.get("ACC_NODE_ALTERNATE_PRIMARIES", "").split()))

    def write(self, path: Path) -> None:
        lines = ["# Written by `acc-node enroll`.", f"ACC_NODE_PRIMARY={self.primary}", f"ACC_NODE_ID={self.node_id}",
                 f"ACC_NODE_TRUST={self.trust}"]
        if self.trust == "ca":
            lines += [f"ACC_NODE_CA_FILE={self.ca_file}", f"ACC_NODE_CA_FINGERPRINT={self.ca_fingerprint}"]
        lines += [f"ACC_NODE_NAME={self.name}", f"ACC_NODE_SOCKET_DIR={self.socket_dir}"]
        if self.folder_policy_socket is not None:
            lines += [f"ACC_FOLDER_POLICY_SOCKET={self.folder_policy_socket}"]
        if self.alternate_primaries:
            lines += [f"ACC_NODE_ALTERNATE_PRIMARIES={' '.join(self.alternate_primaries)}"]
        write_atomic(path, ("\n".join(lines) + "\n").encode(), 0o644)


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    """Replace `path` with `data`: a new file under a random name (O_EXCL), renamed over it. A link at `path` is
    replaced, never followed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        try:
            os.fchmod(fd, mode)
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
