"""Pinned SDK tool application lifecycle; no authority or dispatch here."""
from . import protocol as p

CLAUDE_ACK_QUALIFIED=False


def applied(identity,response,result,*,current,native_id,behavior,tool_error,post_tool,tool_seen,post_failed=False):
    if (not current or identity.tool not in {'Bash','Read','Write','Edit'} or not isinstance(response,dict)
            or response.get('behavior')!=behavior or response.get('writeCompleted') is not True
            or result.get('sessionId')!=native_id or result.get('subtype')!='success'
            or result.get('isError') is not False or not p.valid_opaque(result.get('uuid'))):
        return False
    denials=result.get('permissionDenials')
    if not isinstance(denials,tuple):return False  # absent report is not an explicit empty report
    denied=(identity.tool,identity.tool_use_id) in denials
    if behavior=='deny':return tool_seen is True and denied and tool_error is True
    successful=(tool_error is False or (tool_error is None and identity.tool in {'Read','Write','Edit'}))
    completed=(successful and post_tool is True and post_failed is False)
    failed=(identity.tool in {'Read','Write','Edit'} and tool_error is True and post_failed is True and post_tool is False)
    return tool_seen is True and behavior=='allow' and not denied and (completed or failed)
