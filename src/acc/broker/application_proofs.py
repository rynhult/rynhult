"""Bounded immutable application facts; no payload/credential or replay authority."""
import fcntl
import json
import os
import pwd
import re
import secrets
import stat
import threading
from pathlib import Path

from acc.session_relay import protocol as p

from .session_relay import private_dir

MAX_RECORDS = 4096
MAX_BYTES = 16 << 10
NAME = 'application-proofs'


def filename(identifier):
    if not isinstance(identifier, str) or re.fullmatch('[a-f0-9]{32}', identifier) is None:
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    return identifier + '.json'


def read_fd(fd, identifier):
    file = os.open(filename(identifier), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        before = os.fstat(file)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1 or before.st_size > MAX_BYTES):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        value = os.read(file, MAX_BYTES + 1)
        after = os.fstat(file)
        if len(value) > MAX_BYTES or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        return json.loads(value)
    finally:
        os.close(file)


class ApplicationProofs:
    def __init__(self, directory):
        self.root = Path(directory) / NAME
        self.fd = private_dir(self.root)
        self.identity = os.fstat(self.fd)
        self.lock = threading.Lock()

    def remember(self, binding, value, application):
        operation = value['operation']
        identity = {key: operation[key] for key in ('operationId', 'sessionId', 'runId', 'method', 'payloadDigest', 'actor', 'fence')}
        fact = {'version': 1, 'binding': dict(vars(binding)), 'operation': identity, 'application': application}
        raw = p.canonical_json(fact)
        if len(raw) > MAX_BYTES:
            raise p.RelayError(p.ErrorCode.LIMIT)
        name = filename(operation['operationId'])
        with self.lock:
            if self.fd is None:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            temporary = None
            try:
                current = self.root.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (self.identity.st_dev, self.identity.st_ino):
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                try:
                    previous = read_fd(self.fd, operation['operationId'])
                except FileNotFoundError:
                    previous = None
                if previous is not None:
                    if p.canonical_json(previous) != raw:
                        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                    return
                with os.scandir(self.fd) as entries:
                    if sum(1 for _, _entry in zip(range(MAX_RECORDS), entries)) >= MAX_RECORDS:
                        raise p.RelayError(p.ErrorCode.LIMIT)
                temporary = '.pending-' + secrets.token_hex(16)
                file = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
                try:
                    with os.fdopen(file, 'wb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
                    os.unlink(temporary, dir_fd=self.fd)
                    temporary = None
                    os.fsync(self.fd)
                finally:
                    if temporary is not None:
                        os.unlink(temporary, dir_fd=self.fd)
            finally:
                fcntl.flock(self.fd, fcntl.LOCK_UN)

    def forget(self, identifier):
        with self.lock:
            if self.fd is not None:
                try:
                    os.unlink(filename(identifier), dir_fd=self.fd)
                except FileNotFoundError:
                    pass
                os.fsync(self.fd)

    def close(self):
        with self.lock:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None


def read(config, scope):
    session = scope['operation']['sessionId']
    binding = scope['binding']
    if (not isinstance(session, str) or re.fullmatch('[a-f0-9]{12}', session) is None
            or binding['account'] != pwd.getpwuid(os.getuid()).pw_name
            or Path(scope['directory']) != Path(config.run_dir) / session):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    from . import ops
    ops._under_roots(config, binding['workspace'])
    fd = os.open(config.run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in (session, 'relay', NAME):
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        fact = read_fd(fd, scope['operation']['operationId'])
        if (not isinstance(fact, dict) or fact.keys() != {'version', 'binding', 'operation', 'application'}
                or type(fact['version']) is not int or fact['version'] != 1
                or p.canonical_json(fact['binding']) != p.canonical_json(binding)
                or p.canonical_json(fact['operation']) != p.canonical_json(scope['operation'])):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        return fact
    except FileNotFoundError:
        return None
    finally:
        os.close(fd)
