from enrolled_claude_tool_ack import prepare_tool


async def prepare(root,work,c):
    return await prepare_tool(root,work,c,'Read',loss='before')
