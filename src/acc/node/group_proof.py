"""Local account-control challenges. Only issue() returns a code, never a broker RPC."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path

TTL = 300
MAX_ATTEMPTS = 5
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def _identity(node_id, account, request_id):
    if (not re.fullmatch(r'[a-f0-9]{16,32}', node_id)
            or not re.fullmatch(r'[a-f0-9]{32}', request_id)
            or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', account)):
        raise ValueError('invalid proof identity')
    return [node_id, account, request_id]


@contextmanager
def _state(run_dir):
    directory = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError('proof requires an account-owned private run directory')
        fd = os.open('.group-proof.json', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        fcntl.flock(fd, fcntl.LOCK_EX)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError('unsafe proof state')
        data = os.read(fd, 16385)
        if len(data) > 16384:
            raise ValueError('invalid proof state')
        state = json.loads(data) if data else {}
        if not isinstance(state, dict):
            raise ValueError('invalid proof state')
        yield state
        encoded = json.dumps(state).encode()
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, encoded)
        os.ftruncate(fd, len(encoded))
        os.fsync(fd)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _audit(state, action, identity, now):
    state['audit'] = (state.get('audit', []) + [{'action': action, 'identity': identity, 'at': now}])[-32:]


def issue(run_dir: Path, node_id: str, account: str, request_id: str, *, now=None):
    identity = _identity(node_id, account, request_id)
    now = time.time() if now is None else now
    with _state(run_dir) as state:
        if state and now - state['issued_at'] < 60:
            raise ValueError('wait one minute before generating another code')
        code = ''.join(secrets.choice(ALPHABET) for _ in range(10))
        state.update(identity=identity, issued_at=now, expires_at=now + TTL,
                     digest=hashlib.sha256(code.encode()).hexdigest(), attempts=0, used=False)
        _audit(state, 'issued', identity, now)
    return code


def verify(run_dir: Path, node_id: str, account: str, request_id: str, code: str, *, now=None):
    identity = _identity(node_id, account, request_id)
    now = time.time() if now is None else now
    with _state(run_dir) as state:
        if not state:
            return False
        valid = (state['identity'] == identity and state['issued_at'] <= now < state['expires_at']
                 and not state['used'] and state['attempts'] < MAX_ATTEMPTS
                 and hmac.compare_digest(state['digest'], hashlib.sha256(code.encode()).hexdigest()))
        if state['identity'] == identity:
            state['attempts'] += 1
        if valid:
            state['used'] = True
        _audit(state, 'verified' if valid else 'refused', identity, now)
        return valid


def show(args):
    from ..server.agent_setup import valid_agent_account
    account = pwd.getpwuid(os.geteuid()).pw_name
    if not valid_agent_account(account):
        raise ValueError('run as the ordinary agent account requested, not root or a service account')
    code = issue(args.run_dir, args.node_id, account, args.request)
    print(f'Confirmation code: {code}\nExpires in five minutes. Enter it only in your matching machine-account request.')
    return 0


def arguments(parser: argparse.ArgumentParser):
    parser.add_argument('--request', required=True, help='the pending machine-account request ID')
    parser.add_argument('--node-id', required=True, help='the enrolled machine ID shown on that request')
    parser.add_argument('--run-dir', type=Path, required=True, help="this account's configured broker run directory")
