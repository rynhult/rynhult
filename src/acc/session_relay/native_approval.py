"""Bounded ordinary-command review context; never a policy-amendment grant."""
from .protocol import ErrorCode, RelayError


def describe_command(params):
    command, cwd, reason = params.get('command'), params.get('cwd'), params.get('reason')
    for value, limit, required in ((command, 4096, True), (cwd, 1024, True), (reason, 1024, False)):
        if value is None and not required:
            continue
        if not isinstance(value, str) or (required and not value.strip()) or len(value.encode()) > limit:
            raise RelayError(ErrorCode.UNSUPPORTED)
    return 'Command:\n' + command + '\nDirectory:\n' + cwd + ('\nReason:\n' + reason if reason else '')



def describe_tool(tool, inputs, workspace):
    """Review the exact bounded tool input; never silently omit input fields."""
    from .claude_hook import TOOL_APPROVALS
    from .protocol import canonical_json
    if tool not in TOOL_APPROVALS or not isinstance(inputs, dict):
        return None
    if not isinstance(workspace, str) or not workspace or len(workspace.encode()) > 1024:
        return None
    rendered=canonical_json(inputs).decode()
    if len(rendered.encode()) > 5120:
        return None  # only denial can be offered without complete review context
    return 'Tool: '+tool+'\nDirectory:\n'+workspace+'\nExact input:\n'+rendered


def describe_file(changes,reason=None):
    from pathlib import PurePosixPath

    from .protocol import canonical_json
    if not isinstance(changes,list) or not 0<len(changes)<=16:
        raise RelayError(ErrorCode.UNSUPPORTED)
    for change in changes:
        if (not isinstance(change,dict) or change.keys()!={'path','kind','diff'}
                or not isinstance(change['path'],str) or not PurePosixPath(change['path']).is_absolute()
                or '..' in PurePosixPath(change['path']).parts or not isinstance(change['diff'],str)
                or not isinstance(change['kind'],dict) or change['kind'].get('type') not in ('add','update','delete')):
            raise RelayError(ErrorCode.UNSUPPORTED)
    if reason is not None and (not isinstance(reason,str) or len(reason.encode())>1024):
        raise RelayError(ErrorCode.UNSUPPORTED)
    rendered=canonical_json({'changes':changes,'reason':reason}).decode()
    if len(rendered.encode())>5120:raise RelayError(ErrorCode.LIMIT)
    return 'Exact file changes (this action only):\n'+rendered
