"""Frozen relay-native metadata write boundary.

The native writer needs to append its own history; model tools must not forge
that history. Shell subprocesses need the OS sandbox, file tools need Edit deny
rules, and mutable account/repository settings cannot select exceptions. The pinned native positive/escape matrix is recorded in the group9 handoff;
capability and public writer qualification remain separate.
"""
from copy import deepcopy
from pathlib import PurePosixPath

from acc.session_relay import protocol as p

METADATA_ROOT = '~/.claude/projects'


def freeze(settings, run_dir):
    if not isinstance(run_dir, str):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    path = PurePosixPath(run_dir)
    if (not isinstance(settings, dict) or not path.is_absolute()
            or '..' in path.parts or any(char in run_dir for char in '\n\r\0*?[]!')):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    result = deepcopy(settings)
    if result.get('disableAllHooks') is not False:
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    # No filesystem access: all paths are derived from the reviewed run and the
    # account HOME pinned by ClaudeOwnerFactory, never caller-provided roots.
    result['sandbox'] = {'enabled':True, 'failIfUnavailable':True,
        'allowUnsandboxedCommands':False, 'autoAllowBashIfSandboxed':False,
        'excludedCommands':[], 'filesystem':{'disabled':False,
            'denyWrite':[METADATA_ROOT, str(path)], 'denyRead':[str(path)]}}
    permissions = result.setdefault('permissions', {})
    if not isinstance(permissions, dict):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    existing = permissions.get('deny', [])
    if not isinstance(existing, list) or not all(isinstance(item,str) for item in existing):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    # Edit rules also govern Write/NotebookEdit in the pinned CLI. Write(path)
    # rules alone do not protect those tools. // is the absolute-path anchor.
    denies = [f'Edit({METADATA_ROOT}/**)', f'Edit(/{path}/**)', f'Read(/{path}/**)']
    permissions['deny'] = list(dict.fromkeys([*existing,*denies]))
    return result


def validate(settings, run_dir):
    expected = freeze(settings, run_dir)
    if settings != expected:
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
