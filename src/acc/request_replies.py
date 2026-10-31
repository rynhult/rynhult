"""Server-only request acknowledgements. Request IDs are read capabilities, not approvals."""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path

ROOT = Path('/var/lib/acc-request-replies')
SESSION = re.compile(r'[a-z0-9]{8,32}')
REQUEST = re.compile(r'[0-9a-f]{32}')


def publish(root: Path, session_id: str, replies: dict) -> None:
    if not isinstance(session_id, str) or not SESSION.fullmatch(session_id) or not isinstance(replies, dict) or len(replies) > 24:
        raise ValueError('Invalid request acknowledgements')
    payloads = {}
    for rid, row in replies.items():
        if not isinstance(rid, str) or not REQUEST.fullmatch(rid) or not isinstance(row, dict) or row.get('status') not in ('accepted', 'refused'):
            raise ValueError('Invalid request acknowledgement')
        content = json.dumps({'session_id': session_id, 'request_id': rid, 'reply': row}).encode()
        if len(content) > 4096:
            raise ValueError('Request acknowledgement is too large')
        payloads[rid + '.json'] = content
    # Provisioned by the installer, never by an agent broker or an agent-chosen path.
    anchor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(anchor)
        if info.st_uid != os.geteuid() or info.st_mode & 0o066:
            raise ValueError('Unsafe acknowledgement store')
        try:
            os.mkdir(session_id, mode=0o711, dir_fd=anchor)
        except FileExistsError:
            pass
        folder = os.open(session_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=anchor)
    finally:
        os.close(anchor)
    try:
        info = os.fstat(folder)
        if info.st_uid != os.geteuid() or info.st_mode & 0o066:
            raise ValueError('Unsafe acknowledgement directory')
        if stat.S_IMODE(info.st_mode) != 0o711:
            os.fchmod(folder, 0o711)  # service UMask=0077 must not remove agent traversal
        for name, content in payloads.items():
            try:
                current = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            except OSError:
                current = None
            if current is not None:
                try:
                    info = os.fstat(current)
                    if (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                            and not info.st_mode & 0o022
                            and info.st_size <= 4096 and os.read(current, 4097) == content):
                        if stat.S_IMODE(info.st_mode) != 0o644:
                            os.fchmod(current, 0o644)
                        continue  # reconnect retries must not churn unchanged files
                finally:
                    os.close(current)
            temporary = '.reply-' + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=folder)
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(content)
                    output.flush()
                    os.fchmod(output.fileno(), 0o644)  # readable-only after the complete envelope is written
                os.rename(temporary, name, src_dir_fd=folder, dst_dir_fd=folder)
            finally:
                try:
                    os.unlink(temporary, dir_fd=folder)
                except FileNotFoundError:
                    pass
        for name in os.listdir(folder):
            if name.endswith('.json') and REQUEST.fullmatch(name[:-5]) and name not in payloads:
                os.unlink(name, dir_fd=folder)  # only this server-owned session's expired acknowledgements
    finally:
        os.close(folder)
