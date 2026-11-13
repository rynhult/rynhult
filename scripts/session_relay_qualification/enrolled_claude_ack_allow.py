from enrolled_claude_ack import prepare as prepare_ack


async def prepare(root,work,c):
    case=await prepare_ack(root,work,c);case.choice='accept'
    return case
