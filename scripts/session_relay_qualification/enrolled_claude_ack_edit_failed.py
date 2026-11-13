from enrolled_claude_tool_failed import prepare_failed


async def prepare(root,work,c):
    return await prepare_failed(root,work,c,'Edit',loss=None)
