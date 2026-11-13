"""Actual tmp-file IO failure after an exact person Allow; not a policy exception."""
from enrolled_claude_tool_ack import prepare_tool


async def prepare_failed(root,work,c,tool,loss=None):
    case=await prepare_tool(root,work,c,tool,choice='accept',loss=loss)
    target=work/'sdk-tool-target.txt'
    async def fail_io():
        if tool=='Read':target.unlink()
        else:
            target.chmod(0o400)
            work.chmod(0o500)
    case.before_answer=fail_io
    original=case.exercise
    async def exercise(*args,**kwargs):
        try:return await original(*args,**kwargs)
        finally:
            work.chmod(0o700)
            if target.exists():target.chmod(0o600)
    case.exercise=exercise
    return case
