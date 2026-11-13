"""Native Claude hook client. Fixed private channel; every failure denies.

Invoked only by reviewed --settings command groups. No native TUI fallback on
permission timeout, no cached allow and no raw command/action in error output.
"""
from __future__ import annotations

import json
import os
import re
import socket
import stat
import sys
from pathlib import Path

LIMIT = 1 << 20
TOOL_APPROVALS = frozenset({'Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep'})


def _valid_digest(value):
    # This installed hook also runs as a standalone file without a package root.
    return type(value) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', value) is not None


def background_task_id(payload):
    """Pinned native result evidence; absence is not a completion observation."""
    response = payload.get('tool_response')
    if not isinstance(response, dict) or response.get('backgroundTaskId') is None:
        return None
    identifier = response['backgroundTaskId']
    if not isinstance(identifier, str) or not 0 < len(identifier) <= 256:
        raise ValueError('Invalid native background identity')
    return identifier


def denial_message(marker):
    if _valid_digest(marker):
        return 'This action was denied by the session hub. Reference ' + marker
    if not isinstance(marker, str) or not re.fullmatch('[0-9a-f]{32}', marker):
        raise ValueError('Invalid native decision marker')
    return 'This action was denied by the session hub. [relay-decision:' + marker + ']'


def exchange(channel_path, payload):
    path = Path(channel_path)
    if path.name != 'owner-channel.json':
        raise ValueError('Wrong hook channel')
    parent = path.parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError('Unsafe hook directory')
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        pinned = os.fstat(directory_fd)
        if ((pinned.st_dev, pinned.st_ino) != (parent.st_dev, parent.st_ino)
                or pinned.st_uid != os.getuid() or stat.S_IMODE(pinned.st_mode) != 0o700):
            raise ValueError('Hook directory changed')
        return _exchange(directory_fd, payload)
    finally:
        os.close(directory_fd)


def _exchange(directory_fd, payload):
    fd = os.open('owner-channel.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 8192):
            raise ValueError('Unsafe hook channel')
        channel = json.loads(os.read(fd, 8193))
    finally:
        os.close(fd)
    if payload.get('session_id') != channel['nativeId']:
        raise ValueError('Wrong native conversation')
    info = os.stat('native-hook.sock', dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('Unsafe hook socket')
    request = json.dumps({'token': channel['token'], 'runId': channel['runId'], 'payload': payload}).encode() + b'\n'
    if len(request) > LIMIT:
        raise ValueError('Hook payload too large')
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(20)
        connection.connect(f'/proc/self/fd/{directory_fd}/native-hook.sock')
        connection.sendall(request)
        data = b''
        while b'\n' not in data:
            chunk = connection.recv(min(8192, LIMIT + 1 - len(data)))
            if not chunk or len(data) + len(chunk) > LIMIT:
                raise ValueError('Hook reply missing or oversized')
            data += chunk
    reply = json.loads(data.split(b'\n', 1)[0])
    referenced_deny = (isinstance(reply, dict) and reply.keys() == {'behavior', 'denyReference'}
                       and reply['behavior'] == 'deny' and _valid_digest(reply['denyReference']))
    if not referenced_deny and reply not in ({'behavior': 'allow'}, {'behavior': 'deny'}, {'observed': True}, {'sdkPermissionOwner': True}):
        if not isinstance(reply, dict) or reply.keys() != {'behavior', 'applicationMarker'} or reply['behavior'] != 'deny':
            raise ValueError('Invalid hook reply')
        denial_message(reply['applicationMarker'])
    return reply


def main():
    payload = {}
    try:
        raw = sys.stdin.buffer.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('Hook too large')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('Hook is not an object')
        reply = exchange(sys.argv[1], payload)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        reply = {'behavior': 'deny'}
    if payload.get('hook_event_name') == 'PermissionRequest':
        if reply == {'sdkPermissionOwner': True}:
            # Only the authenticated broker can select this path for its owned
            # SDK stdio callback. No native TUI/unmanaged approval fallback.
            return 0
        # Returning an explicit deny prevents native approval UI from becoming
        # the answer path when the hub/socket is absent or stale.
        behavior = reply.get('behavior', 'deny')
        decision = {'behavior': behavior}
        # This is consent to the existing exact call, not a tool-input rewrite.
        # Echoing updatedInput re-evaluates ask rules and can re-open native UI.
        if behavior != 'allow':
            decision['message'] = (denial_message(reply['denyReference']) if 'denyReference' in reply else denial_message(reply['applicationMarker']) if reply.get('applicationMarker')
                                   else 'This action was denied by the session hub.')
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': decision}}))
        return 0
    if payload.get('hook_event_name') in ('Stop', 'SubagentStop', 'SessionEnd'):
        # These are observations, not permission decisions. Exit 2 on Stop
        # tells Claude to continue the turn, creating retries after revocation
        # or broker loss. Missing observation must never certify broker idle.
        return 0
    if reply != {'observed': True}:
        print('The session hub could not verify this native action.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
