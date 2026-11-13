"""Frozen per-view permission ownership; never parallel SDK/native reviewers."""
import copy

from acc.session_relay import protocol as p


def split(settings, legacy_command, guard_command=None):
    tui = copy.deepcopy(settings)
    groups = tui.setdefault('hooks', {}).pop('PermissionRequest', [])
    ordinary = {'hooks': [{'type': 'command', 'command': legacy_command}]}
    if groups and groups != [ordinary]:
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    # The ordinary PermissionRequest sink owns a legacy answer socket. It cannot
    # accompany either hub reviewer. Its other activity hooks remain installed.
    if guard_command is not None:
        known_guard = {'hooks': [{'type': 'command', 'command': guard_command}]}
        tui['hooks']['PreToolUse'] = [group for group in tui['hooks'].get('PreToolUse', []) if group != known_guard]
    sdk = copy.deepcopy(tui)
    return tui, sdk
