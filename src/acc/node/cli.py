"""acc-node command line: enroll, run, status."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from acc.env import environment

from .client import Node, broker_accounts
from .config import (
    CA_FILE,
    DEFAULT_CONFIG,
    DEFAULT_SOCKET_DIR,
    DEFAULT_STATE_DIR,
    KEY_FILE,
    STATUS_FILE,
    ConfigError,
    NodeConfig,
    write_atomic,
)
from .enroll import EnrollError, enroll
from .identity import TrustError, load_key


def env_path(variable: str, fallback: Path) -> Path:
    """Defaults for the path options can come from the environment (the macOS launchd job sets them); the option wins."""
    return Path(environment.get(variable) or fallback)


def parser() -> argparse.ArgumentParser:
    config, state_dir = env_path("ACC_NODE_CONFIG", DEFAULT_CONFIG), env_path("ACC_NODE_STATE_DIR", DEFAULT_STATE_DIR)
    socket_dir = env_path("ACC_NODE_SOCKET_DIR", DEFAULT_SOCKET_DIR)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=config,
                        help=f"env file (default $ACC_NODE_CONFIG, else {DEFAULT_CONFIG}; now {config})")
    common.add_argument("--state-dir", type=Path, default=state_dir,
                        help=f"key, CA and status (default $ACC_NODE_STATE_DIR, else {DEFAULT_STATE_DIR}; now {state_dir})")
    top = argparse.ArgumentParser(prog="rynhult-node", description="Connects this machine's Rynhult brokers to a Rynhult primary.")
    commands = top.add_subparsers(dest="command", required=True)
    enroll_cmd = commands.add_parser("enroll", parents=[common], help="register this node with the primary")
    enroll_cmd.add_argument("--primary", required=True, help="the primary's URL, https://…")
    trust = enroll_cmd.add_mutually_exclusive_group(required=True)
    trust.add_argument("--trust", choices=["system"], help="verify the primary with the system CAs (public certificate)")
    trust.add_argument("--ca-fingerprint", metavar="HEX",
                       help="trust the primary's private CA, whose certificate has this SHA-256")
    trust.add_argument("--fingerprint", metavar="HEX", help="deprecated: same as --ca-fingerprint")
    enroll_cmd.add_argument("--code", required=True,
                            help="the one-time code from Hosts → Add host, or - to read it from stdin")
    enroll_cmd.add_argument("--name", required=True, help="this node's name, as entered on the primary")
    enroll_cmd.add_argument("--socket-dir", type=Path, default=socket_dir,
                            help=f"where the brokers' sockets are (default $ACC_NODE_SOCKET_DIR, else "
                                 f"{DEFAULT_SOCKET_DIR}; now {socket_dir})")
    enroll_cmd.add_argument("--installed-state-dir", type=Path, help=argparse.SUPPRESS)  # deploy/node/acc-node
    from . import group_proof
    group_proof.arguments(commands.add_parser("group-proof", help="show a local machine-account confirmation code"))
    commands.add_parser("run", parents=[common], help="stay connected to the primary and relay broker connections")
    commands.add_parser("status", parents=[common], help="show the configuration and connection state")
    # deploy/node/acc-node: the acc-node user stores the key or CA root just enrolled (on stdin) in its own state dir.
    adopt = commands.add_parser("adopt", parents=[common])
    adopt.add_argument("file", choices=sorted(ADOPTABLE))
    return top


MAX_CODE = 128


def enroll_code(value: str) -> str:
    """The code itself, or with "-" one line of stdin: from-source commands pipe it in, so it is on no command line
    (process list, sudo's log)."""
    if value != "-":
        return value
    code = sys.stdin.readline(MAX_CODE + 1)
    if len(code) > MAX_CODE or not code.strip():
        raise ValueError("--code -: expected the one-time code on one line of stdin")
    return code.strip()


def cmd_enroll(args) -> int:
    if args.fingerprint:
        print("acc-node: --fingerprint is deprecated; use --ca-fingerprint (the SHA-256 of the primary's CA)",
              file=sys.stderr)
    ca_fingerprint = args.ca_fingerprint or args.fingerprint
    config = enroll(primary=args.primary, code=enroll_code(args.code), name=args.name, config_path=args.config,
                    state_dir=args.state_dir, socket_dir=args.socket_dir,
                    trust="ca" if ca_fingerprint else "system", ca_fingerprint=ca_fingerprint,
                    installed_state_dir=args.installed_state_dir)
    print(f"Enrolled as {config.name} (node {config.node_id}). Configuration written to {args.config}.")
    print("Start it with `acc-node run` (or its service).")
    return 0


ADOPTABLE = {KEY_FILE: 0o600, CA_FILE: 0o644}
MAX_ADOPT = 64 * 1024


def cmd_adopt(args) -> int:
    data = sys.stdin.buffer.read(MAX_ADOPT + 1)
    if len(data) > MAX_ADOPT:
        raise ValueError(f"{args.file}: over {MAX_ADOPT} bytes")
    if args.file == KEY_FILE:
        if not isinstance(serialization.load_pem_private_key(data, password=None), Ed25519PrivateKey):
            raise ValueError("not an Ed25519 key")
    else:
        x509.load_pem_x509_certificate(data)
    write_atomic(args.state_dir / args.file, data, ADOPTABLE[args.file])
    return 0


def cmd_run(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = NodeConfig.load(args.config)
    key = load_key(args.state_dir / KEY_FILE)
    try:
        asyncio.run(Node(config, key, args.state_dir).run_forever())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_status(args) -> int:
    config = NodeConfig.load(args.config)
    key_path = args.state_dir / KEY_FILE
    print(f"node:        {config.name or '?'} ({config.node_id})")
    print(f"primary:     {config.primary}")
    if config.trust == "ca":
        print(f"trust:       private CA {config.ca_file} (SHA-256 {config.ca_fingerprint or '?'})")
    else:
        print("trust:       system CAs")
    print(f"key:         {key_path} ({'present' if key_path.exists() else 'MISSING'})")
    accounts = broker_accounts(config.socket_dir)
    print(f"brokers:     {', '.join(accounts) or 'none'} in {config.socket_dir}")
    try:
        status = json.loads((args.state_dir / STATUS_FILE).read_text())
    except (OSError, ValueError):
        print("connection:  unknown (acc-node run has not written a status)")
        return 0
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(status.get("since", 0)))
    line = f"connection:  {status.get('state')} since {since}"
    if status.get("error"):
        line += f" ({status['error']})"
    print(line)
    return 0 if status.get("state") == "connected" else 3


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    from . import group_proof
    handler = {"enroll": cmd_enroll, "run": cmd_run, "status": cmd_status, "adopt": cmd_adopt, "group-proof": group_proof.show}[args.command]
    try:
        return handler(args)
    except (ConfigError, EnrollError, TrustError, OSError, ValueError) as error:
        print(f"acc-node: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
