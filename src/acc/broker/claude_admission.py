"""Pinned Claude transcript evidence for one already registered native input.

The owned CLI and frozen hook/guard placement are the writer trust boundary.
This reader grants no actor authority. It accepts only a new exact user prompt
and its completed provider assistant response after a pre-input cursor. Native
Stop, prompt echo and transcript history alone are never admission evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from functools import wraps
from pathlib import Path

from acc.session_relay import protocol as p

MAX_HISTORY = 16 << 20
MAX_TAIL = 1 << 20
MAX_RECORDS = 4096


class _PendingEvidence(p.RelayError):
    """The protected transcript has not yet flushed the complete native turn."""


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


def _open_directory(path):
    if not path.is_absolute():
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in ('.', '..'):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class NativeAdmission:
    def __init__(self, root, path, native_id):
        self.lock = threading.RLock()
        self.root, self.path, self.native_id = Path(root), Path(path), native_id
        self.directory = self.project = self.fd = None
        self.offset = self.prefix = None
        self.stable_size = self.stable_prefix = None
        self.used = set()
        relative = self.path.relative_to(self.root)
        if (len(relative.parts) != 2 or any(part in ('.', '..') for part in relative.parts)
                or not p.valid_opaque(native_id) or relative.name != native_id + '.jsonl'):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        self.project_name, self.filename = relative.parts
        try:
            self.directory = _open_directory(self.root)
            self.project = os.open(self.project_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.directory)
            self.fd = os.open(self.filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.project)
            self.identity = os.fstat(self.fd)
            self.root_identity, self.project_identity = os.fstat(self.directory), os.fstat(self.project)
            self._validate()
        except BaseException:
            self.close()
            raise

    def _validate(self):
        if self.fd is None:
            raise p.RelayError(p.ErrorCode.OFFLINE)
        root_fd = _open_directory(self.root)
        try:
            root = os.fstat(root_fd)
        finally:
            os.close(root_fd)
        project = os.stat(self.project_name, dir_fd=self.directory, follow_symlinks=False)
        current = os.stat(self.filename, dir_fd=self.project, follow_symlinks=False)
        info = os.fstat(self.fd)
        for value, expected in ((root, self.root_identity), (project, self.project_identity), (current, self.identity)):
            if ((value.st_dev, value.st_ino) != (expected.st_dev, expected.st_ino)
                    or value.st_uid != os.getuid() or value.st_mode & 0o022):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        if ((info.st_dev, info.st_ino) != (self.identity.st_dev, self.identity.st_ino)
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_HISTORY):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        return info.st_size

    def _hash(self, size):
        digest = hashlib.sha256()
        for start in range(0, size, 65536):
            value = os.pread(self.fd, min(65536, size - start), start)
            if not value:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            digest.update(value)
        return digest.digest()

    @_locked
    def begin(self):
        size = self._validate()
        # A new input cursor must not erase the last trusted history epoch.
        # Native compaction/rewrite needs explicit source recovery, never a new
        # begin() that silently legitimizes changed accepted evidence.
        if self.stable_size is not None and (size < self.stable_size
                or self._hash(self.stable_size) != self.stable_prefix):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        if size and os.pread(self.fd, 1, size - 1) != b'\n':
            raise p.RelayError(p.ErrorCode.BUSY)
        self.offset, self.prefix = size, self._hash(size)
        self._validate()
        self.stable_size, self.stable_prefix = self.offset, self.prefix

    def _chain(self, operation):
        if (self.offset is None or operation.method != 'turn.submit'
                or operation.payload_digest != p.operation_digest(operation.method, operation.params)):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        size = self._validate()
        if size < self.offset or size - self.offset > MAX_TAIL or self._hash(self.offset) != self.prefix:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        raw = os.pread(self.fd, size - self.offset, self.offset)
        lines = raw[:raw.rfind(b'\n') + 1].splitlines()
        if len(lines) > MAX_RECORDS:
            raise p.RelayError(p.ErrorCode.LIMIT)
        nodes, users, candidates = {}, [], []
        for line in lines:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            identifier = row.get('uuid')
            if identifier is None:
                continue
            if (not p.valid_opaque(identifier) or identifier in nodes or row.get('sessionId') != self.native_id
                    or row.get('isSidechain') is not False
                    or row.get('type') not in ('user', 'assistant', 'attachment', 'progress', 'system')):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            nodes[identifier] = row
            if row.get('parentUuid') is not None and not p.valid_opaque(row['parentUuid']):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            message = row.get('message', {})
            if not isinstance(message, dict):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            if row['type'] == 'user' and message.get('role') == 'user' and isinstance(message.get('content'), str):
                users.append(row)
            if (row['type'] == 'assistant' and message.get('role') == 'assistant'
                    and message.get('stop_reason') == 'end_turn' and p.valid_opaque(message.get('id'))
                    and row.get('isApiErrorMessage') is not True):
                candidates.append(row)
        if not users and not candidates:
            raise _PendingEvidence(p.ErrorCode.INPUT_CONFLICT)
        if len(users) != 1 or users[0].get('message', {}).get('content') != operation.params.get('text'):
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        target = users[0]['uuid']
        accepted = set()
        for row in candidates:
            parent, visited = row.get('parentUuid'), set()
            while parent in nodes and parent not in visited:
                if parent == target:
                    accepted.add(row['message']['id'])
                    break
                visited.add(parent)
                parent = nodes[parent].get('parentUuid')
        if not accepted and not candidates:
            raise _PendingEvidence(p.ErrorCode.INPUT_CONFLICT)
        if len(accepted) != 1:
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        native_ref = self.native_id + ':' + target + ':' + next(iter(accepted))
        self._validate()
        if self._hash(self.offset) != self.prefix or os.pread(self.fd, len(raw), self.offset) != raw:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        branches = set()
        for row in candidates:
            if row['message']['id'] not in accepted:
                continue
            identifier, visited = row['uuid'], set()
            while identifier in nodes and identifier not in visited:
                visited.add(identifier)
                if identifier == target:
                    branches.update(visited)
                    break
                identifier = nodes[identifier].get('parentUuid')
        # Preserve the full validated accepted chain across the next cursor,
        # including evidence appended after the previous begin() offset.
        self.stable_size, self.stable_prefix = size, self._hash(size)
        return native_ref, [row for identifier, row in nodes.items() if identifier in branches]

    @_locked
    def evidence(self, operation):
        native_ref, _ = self._chain(operation)
        if native_ref in self.used or len(self.used) >= 512:
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        self.used.add(native_ref)
        return p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, operation.operation_id, operation.run_id, native_ref)

    def wait_evidence(self, operation):
        # Native Stop can precede asynchronous transcript flush. Retry only the
        # absence of records, under the same cursor/inode/prefix and bounded time.
        # Wrong prompts, contradictory chains and integrity errors fail at once.
        deadline = time.monotonic() + 1
        while True:
            try:
                return self.evidence(operation)
            except _PendingEvidence:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(.025, remaining))

    @_locked
    def denied_application(self, operation, tool, inputs, marker):
        # A completed provider chain plus a private per-invocation marker is
        # required. Arbitrary tool errors, Stop and a delivered reply are not proof.
        from acc.session_relay.claude_hook import denial_message
        message = denial_message(marker)
        native_ref, branch = self._chain(operation)
        uses, results = [], []
        for row in branch:
            content = row.get('message', {}).get('content')
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if (row['type'] == 'assistant' and row['message'].get('role') == 'assistant'
                        and row.get('isApiErrorMessage') is not True and block.get('type') == 'tool_use'
                        and block.get('name') == tool and p.canonical_json(block.get('input')) == p.canonical_json(inputs)
                        and p.valid_opaque(block.get('id'))):
                    uses.append(block['id'])
                if (row['type'] == 'user' and row['message'].get('role') == 'user' and row.get('toolUseResult') == 'Error: ' + message
                        and block.get('type') == 'tool_result' and block.get('is_error') is True
                        and block.get('content') == message and p.valid_opaque(block.get('tool_use_id'))):
                    results.append(block['tool_use_id'])
        if len(results) != 1 or uses.count(results[0]) != 1:
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        return {'nativeRef': native_ref + ':' + marker + ':' + results[0], 'nativeToolId': results[0]}

    @_locked
    def allowed_application(self, operation, tool, inputs, marker, tool_id):
        # tool_id was uniquely bound by authenticated PreToolUse before the
        # answer. The caller also requires its matching authenticated PostToolUse.
        if not p.valid_opaque(tool_id) or not p.valid_opaque(marker):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        native_ref, branch = self._chain(operation)
        uses, results = 0, 0
        for row in branch:
            content = row.get('message', {}).get('content')
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if (row['type'] == 'assistant' and row['message'].get('role') == 'assistant'
                        and block.get('type') == 'tool_use' and block.get('id') == tool_id
                        and block.get('name') == tool and p.canonical_json(block.get('input')) == p.canonical_json(inputs)):
                    uses += 1
                if (row['type'] == 'user' and row['message'].get('role') == 'user'
                        and isinstance(row.get('toolUseResult'), dict) and block.get('type') == 'tool_result'
                        and block.get('tool_use_id') == tool_id and (block.get('is_error') is None or block.get('is_error') is False)):
                    results += 1
        if uses != 1 or results != 1:
            raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
        return {'nativeRef': native_ref + ':' + marker + ':' + tool_id, 'nativeToolId': tool_id}

    @_locked
    def close(self):
        for name in ('fd', 'project', 'directory'):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)
