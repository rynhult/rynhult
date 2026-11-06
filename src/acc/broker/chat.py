"""Account-local, incremental native Chat reads with cooperative work budgets."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_BYTES = 64 << 20  # per request, not a maximum cursor position
MAX_LINE = 4 << 20
MAX_TEXT = 128 << 10
MAX_PAGE = 256 << 10
CHUNK = 256 << 10
SECONDS = 1.0
RECORD_SECONDS = .05
VERIFY_SECONDS = 30.0
MAX_POSITION = (1 << 63) - 1
NATIVE = re.compile(r'[A-Za-z0-9_-]{1,128}')
FLAGS = getattr(os, 'O_PATH', os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
OPAQUE = {
    'claude': frozenset({'attachment', 'queue-operation', 'atis-latch', 'api-request-shape',
        'api-request-blob', 'api-request', 'last-prompt', 'mode', 'cost-state', 'progress',
        'file-history-snapshot', 'system'}),
    'codex': frozenset({'session_meta', 'turn_context', 'event_msg', 'world_state', 'token_usage_record'}),
}
CONTEXT_TAGS = frozenset({'environment_context', 'user_info', 'system-reminder', 'user_instructions',
    'permissions', 'skill', 'skills_instructions', 'developer_instructions', 'additional_metadata',
    'timestamp', 'bash-stdout', 'bash-stderr', 'bash-input', 'ide_opened_file', 'task-notification', 'user-prompt-submit-hook',
    'ide_selection', 'environment_details', 'instructions', 'turn_aborted',
    'collaboration_mode', 'system'})
CONTEXT_NAME = re.compile(r'/?([A-Za-z][A-Za-z0-9_-]*)(?=[\s/>]|$)')


def _private_tag(tag):
    return tag in CONTEXT_TAGS or tag.startswith(('command-', 'local-command'))


class Budget(Exception):
    pass


def _check(deadline):
    if time.monotonic() >= deadline:
        raise Budget()


def _directory(path):
    fd = os.open('/', FLAGS)
    try:
        for part in Path(path).absolute().parts[1:]:
            new = os.open(part, FLAGS, dir_fd=fd)
            os.close(fd)
            fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_at(root, parts):
    fd = os.dup(root)
    try:
        for part in parts[:-1]:
            new = os.open(part, FLAGS, dir_fd=fd)
            os.close(fd)
            fd = new
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(leaf).st_mode):
            os.close(leaf)
            raise ValueError('unsafe_source')
        return leaf
    finally:
        os.close(fd)


def _workspace_key(workspace):
    if not isinstance(workspace, str) or not workspace.startswith('/') or len(workspace) > 4096:
        raise ValueError('workspace')
    return ''.join(c if c.isascii() and c.isalnum() else '-' for c in workspace)


_LOCK = threading.Lock()
_PATHS: OrderedDict = OrderedDict()
_STATES: OrderedDict = OrderedDict()


def _remember(cache, key, value, maximum):
    with _LOCK:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > maximum:
            cache.popitem(last=False)


def _locate(home, provider, native, workspace, deadline):
    root = Path(home) / ('.claude/projects' if provider == 'claude' else '.codex/sessions')
    key = (str(home), provider, native, workspace)
    with _LOCK:
        cached = _PATHS.get(key)
    directory = _directory(root)
    try:
        if provider == 'claude' and workspace is not None:
            return _open_at(directory, (_workspace_key(workspace), native + '.jsonl'))
        if cached:
            try:
                return _open_at(directory, cached)
            except FileNotFoundError:
                pass
        # Qualified Codex UUIDv7 IDs encode creation time. Try local and UTC
        # layouts, with a one-second creation boundary, before bounded discovery.
        if provider == 'codex':
            candidates = []
            try:
                identifier = uuid.UUID(native)
                if identifier.version == 7:
                    seconds = (identifier.int >> 80) / 1000
                    for zone in (None, timezone.utc):
                        for shift in (-1, 0, 1):
                            at = datetime.fromtimestamp(seconds, zone) + timedelta(seconds=shift)
                            parts = (at.strftime('%Y'), at.strftime('%m'), at.strftime('%d'),
                                     f"rollout-{at.strftime('%Y-%m-%dT%H-%M-%S')}-{native}.jsonl")
                            if parts not in candidates:
                                candidates.append(parts)
            except (ValueError, OverflowError, OSError):
                pass
            for parts in candidates:
                _check(deadline)
                try:
                    leaf = _open_at(directory, parts)
                except FileNotFoundError:
                    continue
                _remember(_PATHS, key, parts, 128)
                return leaf
        found = []
        budget = [4096]

        def walk(fd, depth, prefix=()):
            readable = os.open('.', os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            try:
                with os.scandir(readable) as entries:
                    for entry in entries:
                        _check(deadline)
                        budget[0] -= 1
                        if budget[0] < 0:
                            raise Budget()
                        info = entry.stat(follow_symlinks=False)
                        match = entry.name == f'{native}.jsonl' if provider == 'claude' else (
                            entry.name.startswith('rollout-') and entry.name.endswith(f'-{native}.jsonl'))
                        if match:
                            found.append(prefix + (entry.name,))
                        elif depth and stat.S_ISDIR(info.st_mode):
                            child = os.open(entry.name, FLAGS, dir_fd=fd)
                            try:
                                walk(child, depth - 1, prefix + (entry.name,))
                            finally:
                                os.close(child)
                        # Unrelated links are not sources and cannot deny an account.
            finally:
                os.close(readable)
        walk(directory, 1 if provider == 'claude' else 3)
        if len(found) != 1:
            return None
        leaf = _open_at(directory, found[0])
        _remember(_PATHS, key, found[0], 128)
        return leaf
    finally:
        os.close(directory)


def _user_text(text, deadline):
    """Qualified plain text/wrappers; linear per-block context removal, with gaps."""
    _check(deadline)
    if len(text) > MAX_TEXT or len(text.encode('utf-8')) > MAX_TEXT:
        raise ValueError('text_limit')
    if text.lstrip().startswith('# AGENTS.md instructions for '):
        return '', 1
    # Only known context is private. Comparisons, generics and ordinary markup
    # are the person's text; wrappers delimit text, not a license to drop it.
    chunks, stack = [], []
    position, omitted = 0, 0
    while position < len(text):
        _check(deadline)
        begin = text.find('<', position)
        if begin < 0:
            if not stack:
                chunks.append(text[position:])
            break
        if not stack:
            chunks.append(text[position:begin])
        # Bound tag lookahead so repeated unclosed '<' stays linear.
        end = text.find('>', begin + 1, begin + 258)
        name = CONTEXT_NAME.match(text[begin + 1:begin + 258].lstrip())
        private = name is not None and _private_tag(name.group(1).lower())
        # A malformed known opening cannot turn private context into prose.
        # Refuse the remaining block rather than guessing its closing boundary.
        if private and (end < 0 or text.find('<', begin + 1, end) >= 0):
            omitted += 1
            break
        if end < 0:
            if not stack:
                chunks.append('<')
            position = begin + 1
            continue
        nested = text.find('<', begin + 1, end)
        if nested >= 0:
            if not stack:
                chunks.append(text[begin:nested])
            position = nested
            continue
        token = text[begin + 1:end].strip()
        closing = token.startswith('/')
        parts = token.lstrip('/').rstrip('/').split(None, 1)
        tag = parts[0].lower() if parts else ''
        if _private_tag(tag):
            if closing:
                if stack and stack[-1] == tag:
                    stack.pop()
                elif not stack:
                    omitted += 1
            else:
                if not stack:
                    omitted += 1
                if not token.endswith('/'):
                    stack.append(tag)
        elif tag not in ('user_query', 'user_request') and not stack:
            chunks.append(text[begin:end + 1])
        position = end + 1
    return ''.join(chunks).strip(), omitted


def _message(record, provider, native, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + RECORD_SECONDS
    typ = record.get('type')
    if typ in OPAQUE[provider]:
        return None
    if provider == 'claude':
        if typ not in ('user', 'assistant'):
            raise ValueError('unknown_record')
        if record.get('sessionId') != native:
            raise ValueError('identity')
        if record.get('version') != '2.1.308':
            raise LookupError('version')
        role, payload, identity, turn = typ, record.get('message'), record.get('uuid'), ''
        if record.get('isMeta') or record.get('isSidechain'):
            return None  # Qualified private records, not damaged/missing visible history.
    else:
        if typ != 'response_item':
            raise ValueError('unknown_record')
        payload = record.get('payload')
        if not isinstance(payload, dict):
            raise ValueError('schema')
        if payload.get('type') in ('function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output'):
            return {'text': '', 'tools': 1, 'omitted': 0}
        if payload.get('type') == 'reasoning':
            return None
        if payload.get('type') != 'message':
            raise ValueError('unknown_record')
        role = payload.get('role')
        if role in ('developer', 'system'):
            return None
        if role not in ('user', 'assistant'):
            raise ValueError('role')
        identity = payload.get('id')
        meta = payload.get('internal_chat_message_metadata_passthrough')
        turn = meta.get('turn_id', '') if isinstance(meta, dict) else ''
    if not isinstance(payload, dict):
        raise ValueError('schema')
    content = payload.get('content')
    blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) and provider == 'claude' else content
    if not isinstance(blocks, list) or len(blocks) > 4096:
        raise ValueError('blocks')
    if provider == 'codex' and role == 'user' and isinstance(meta, dict):
        kinds = meta.get('content_item_kinds')
        if kinds is not None and (not isinstance(kinds, list) or len(kinds) != len(blocks)
                or any(kind != 'user.text' for kind in kinds)):
            raise ValueError('unqualified_prompt')
    texts, omitted, tools, size = [], 0, 0, 0
    for block in blocks:
        _check(deadline)
        if not isinstance(block, dict):
            omitted += 1
            continue
        kind = block.get('type')
        if kind in ('thinking', 'redacted_thinking'):
            continue
        if kind in ('tool_use', 'tool_result'):
            tools += 1
            continue
        expected = ('text',) if provider == 'claude' else (('input_text',) if role == 'user' else ('output_text',))
        if kind not in expected or not isinstance(block.get('text'), str):
            omitted += 1
            continue
        text = block['text']
        if len(text) > MAX_TEXT:
            raise ValueError('text_limit')
        size += len(text.encode('utf-8'))
        if size > MAX_TEXT:
            raise ValueError('text_limit')
        if role == 'user':
            text, hidden = _user_text(text, deadline)
            omitted += hidden
        if text.strip():
            texts.append(text)
    if not isinstance(identity, str) or not identity or len(identity) > 256 or not isinstance(turn, str) or len(turn) > 256:
        identity, turn = None, ''
    return {'role': role, 'text': '\n'.join(texts), 'native_key': [turn, identity] if identity else None,
            'omitted': omitted, 'tools': tools}


class Chain:
    """Serializable SHA-256 chain over bounded readline fragments; no raw text."""
    def __init__(self, value=None):
        self.value = value or hashlib.sha256(b'chat-prefix-v2').hexdigest()

    def update(self, fragment):
        self.value = hashlib.sha256(bytes.fromhex(self.value) + fragment).hexdigest()

    def copy(self):
        return Chain(self.value)

    def hexdigest(self):
        return self.value


def export_cache():
    with _LOCK:
        return {'paths': [[list(k), list(v)] for k, v in _PATHS.items()],
                'states': [[list(k), {**vars(v), 'hasher': v.hasher.hexdigest(),
                    'verify_hash': v.verify_hash.hexdigest() if v.verify_hash else None,
                    'head': v.head.hex(), 'tail': v.tail.hex()}] for k, v in _STATES.items()]}


def import_cache(cache):
    # Internal worker channel only; never browser/broker request parameters.
    with _LOCK:
        _PATHS.clear()
        _STATES.clear()
        for key, value in cache.get('paths', []):
            _PATHS[tuple(key)] = tuple(value)
        for key, value in cache.get('states', []):
            value = dict(value)
            value['hasher'] = Chain(value['hasher'])
            value['verify_hash'] = Chain(value['verify_hash']) if value['verify_hash'] else None
            value['head'], value['tail'] = bytes.fromhex(value['head']), bytes.fromhex(value['tail'])
            _STATES[tuple(key)] = State(**value)


@dataclass
class State:
    hasher: object
    known: bool
    unsupported: bool
    checked: float
    head: bytes
    tail: bytes
    size: int
    mtime: int
    ctime: int = 0
    verify_hash: object = None
    verify_position: int = 0


def _anchors(stream, end):
    stream.seek(0)
    head = hashlib.sha256(stream.read(min(end, 4096))).digest()
    stream.seek(max(0, end - 4096))
    tail = hashlib.sha256(stream.read(min(end, 4096))).digest()
    return head, tail


def _saved(key):
    with _LOCK:
        state = _STATES.get(key)
        if state is None:
            return None
        result = copy.copy(state)
        result.hasher = state.hasher.copy()
        result.verify_hash = state.verify_hash.copy() if state.verify_hash is not None else None
        return result


def read_chat(home, provider, native_id, *, limit=100, before=None, after=None, checkpoint=None, workspace=None):
    empty = {'state': 'unknown', 'items': [], 'omitted': 0, 'reset': False, 'source': None,
             'before': None, 'more': False, 'notices': {'tools': 0}, 'reason': None}
    if provider not in ('claude', 'codex'):
        return {**empty, 'state': 'unsupported'}
    if not isinstance(native_id, str) or not NATIVE.fullmatch(native_id):
        return empty
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('invalid_limit')
    for position in (before, after):
        if position is not None and (type(position) is not int or not 0 <= position <= MAX_POSITION):
            raise ValueError('invalid_position')
    if checkpoint is not None and (not isinstance(checkpoint, dict)
            or set(checkpoint) != {'identity', 'end', 'digest'}
            or type(checkpoint['end']) is not int or not 0 <= checkpoint['end'] <= MAX_POSITION
            or any(not isinstance(checkpoint[k], str) or len(checkpoint[k]) != 64
                   or any(c not in '0123456789abcdef' for c in checkpoint[k]) for k in ('identity', 'digest'))):
        raise ValueError('invalid_checkpoint')
    deadline = time.monotonic() + SECONDS
    try:
        fd = _locate(home, provider, native_id, workspace, deadline)
        if fd is None:
            return {**empty, 'state': 'gap', 'reset': True, 'omitted': 1} if checkpoint else empty
        with os.fdopen(fd, 'rb') as stream:
            return _read(stream, provider, native_id, str(home), workspace, limit, before, after, checkpoint, deadline, empty)
    except FileNotFoundError:
        return {**empty, 'state': 'gap', 'reset': True, 'omitted': 1} if checkpoint else empty
    except Budget:
        return {**empty, 'more': True, 'reason': 'discovery_budget'}
    except (OSError, ValueError):
        return {**empty, 'state': 'error'}


def _read(stream, provider, native, home, workspace, limit, before, after, checkpoint, deadline, empty):
    initial = os.fstat(stream.fileno())
    identity = hashlib.sha256(f'{initial.st_dev}:{initial.st_ino}:{native}'.encode()).hexdigest()
    prefix_end = checkpoint.get('end', 0) if checkpoint else 0
    cache_key = (home, provider, native, workspace, identity, prefix_end, checkpoint.get('digest')) if checkpoint else None
    state = _saved(cache_key) if cache_key else None
    if checkpoint:
        if (checkpoint.get('identity') != identity or initial.st_size < prefix_end or state is None
                or _anchors(stream, prefix_end) != (state.head, state.tail)):
            return {**empty, 'state': 'gap', 'reset': True, 'omitted': 1, 'reason': 'source_changed'}
        due = time.monotonic() - state.checked >= VERIFY_SECONDS or (
            initial.st_size != prefix_end or initial.st_mtime_ns != state.mtime or initial.st_ctime_ns != state.ctime)
        if due:
            verification = state.verify_hash or Chain()
            position = state.verify_position
            began_verification = position
            stream.seek(position)
            while position < prefix_end:
                if time.monotonic() >= deadline or position - began_verification + CHUNK > MAX_BYTES:
                    state.verify_hash, state.verify_position = verification, position
                    _remember(_STATES, cache_key, state, 128)
                    return {**empty, 'source': checkpoint, 'more': True, 'reason': 'prefix_verification'}
                data = stream.readline(min(CHUNK, prefix_end - position))
                if not data:
                    return {**empty, 'state': 'gap', 'reset': True, 'omitted': 1}
                verification.update(data)
                position += len(data)
            if verification.hexdigest() != checkpoint.get('digest'):
                return {**empty, 'state': 'gap', 'reset': True, 'omitted': 1}
            state.checked, state.verify_hash, state.verify_position = time.monotonic(), None, 0
        # Old pages must validate continuity too, but need a fresh prefix scan.
        if after != prefix_end:
            state = None
    digest = state.hasher.copy() if state else Chain()
    committed = prefix_end if state else 0
    known, unsupported = (state.known, state.unsupported) if state else (False, False)
    checked = state.checked if state else time.monotonic()
    stream.seek(committed)
    selected = OrderedDict()
    selected_bytes, omitted, tools, scanned = 0, 0, 0, 0
    reason, pending = None, False
    for _ in range(100_000):
        if committed >= initial.st_size:
            break
        if time.monotonic() >= deadline or scanned >= MAX_BYTES:
            reason = 'time_budget' if scanned < MAX_BYTES else 'byte_budget'
            break
        start = committed
        candidate = digest.copy()
        buffer = bytearray()
        oversized = False
        complete = False
        position = committed
        while position < initial.st_size and scanned < MAX_BYTES:
            if time.monotonic() >= deadline:
                reason = 'time_budget'
                break
            part = stream.readline(min(CHUNK, initial.st_size - position, MAX_BYTES - scanned))
            if not part:
                break
            scanned += len(part)
            position += len(part)
            candidate.update(part)
            if not oversized:
                if len(buffer) + len(part) <= MAX_LINE:
                    buffer.extend(part)
                else:
                    buffer.clear()
                    oversized = True
            if part.endswith(b'\n'):
                complete = True
                break
        if not complete:
            pending = True
            reason = reason or ('byte_budget' if scanned >= MAX_BYTES else 'partial_record')
            break
        row, lost, hidden = None, 0, 0
        if oversized:
            lost = 1
        else:
            began = time.monotonic()
            try:
                record = json.loads(buffer.decode('utf-8'))
                if not isinstance(record, dict):
                    raise ValueError('schema')
                if provider == 'codex' and record.get('type') == 'session_meta':
                    meta = record.get('payload')
                    if not isinstance(meta, dict) or meta.get('id') != native:
                        raise ValueError('identity')
                    known = meta.get('cli_version') == '0.163.0'
                    unsupported |= not known
                if provider == 'claude' and record.get('sessionId') == native:
                    known |= record.get('version') == '2.1.308'
                row = _message(record, provider, native, min(deadline, began + RECORD_SECONDS))
                if time.monotonic() - began > RECORD_SECONDS:
                    raise ValueError('record_time')
                if row:
                    lost, hidden = row.pop('omitted', 0), row.pop('tools', 0)
            except LookupError:
                unsupported = True
            except (ValueError, TypeError, RecursionError, Budget):
                row, lost = None, 1
        eligible = (before is None or start < before) and (after is None or start >= after)
        if row and row.get('text') and eligible:
            key = row.pop('native_key') or [start, position]
            item_id = hashlib.sha256(json.dumps([identity, key]).encode()).hexdigest()
            item = {**row, 'id': item_id, 'position': start, 'end': position,
                    'revision': hashlib.sha256(row['text'].encode()).hexdigest()}
            size = len(json.dumps(item).encode('utf-8'))
            old_size = len(json.dumps(selected[item_id]).encode('utf-8')) if item_id in selected else 0
            if size > MAX_PAGE // 2:
                lost += 1
            elif after is not None and ((item_id not in selected and len(selected) >= limit)
                    or selected_bytes - old_size + size > MAX_PAGE // 2):
                # Do not commit this record: next follow starts exactly here.
                reason = 'page_limit'
                break
            else:
                selected[item_id] = item
                selected_bytes += size - old_size
                if after is None:
                    while len(selected) > limit or selected_bytes > MAX_PAGE // 2:
                        _, old = selected.popitem(last=False)
                        selected_bytes -= len(json.dumps(old).encode('utf-8'))
        if eligible:
            omitted += lost
            tools += hidden
        digest, committed = candidate, position
        if before is not None and committed >= before:
            break
    else:
        reason = 'record_budget'
    final = os.fstat(stream.fileno())
    # A growing file is a normal initial-size snapshot. Same-size mutation or
    # truncation is not append continuity. Anchors catch preceding-prefix changes.
    if final.st_size < initial.st_size or (final.st_size == initial.st_size and
            (final.st_mtime_ns, final.st_ctime_ns) != (initial.st_mtime_ns, initial.st_ctime_ns)):
        return {**empty, 'state': 'gap', 'reset': True, 'omitted': 1}
    source = {'identity': identity, 'end': committed, 'digest': digest.hexdigest()}
    head, tail = _anchors(stream, committed)
    stored = State(digest, known, unsupported, checked, head, tail, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
    _remember(_STATES, (home, provider, native, workspace, identity, committed, source['digest']), stored, 128)
    more = committed < initial.st_size and reason != 'partial_record'
    state_name = 'unsupported' if unsupported else 'gap' if omitted or reason in (
        'time_budget', 'byte_budget', 'record_budget') else 'unknown' if pending or not known else (
        'ready' if selected or tools or after is not None else 'empty')
    if unsupported or (provider == 'codex' and not known):
        selected.clear()
    items = list(selected.values())
    return {'state': state_name, 'items': items, 'source': source, 'omitted': omitted,
            'reset': False, 'before': items[0]['position'] if items and items[0]['position'] else None,
            'more': more, 'notices': {'tools': tools}, 'reason': reason}
