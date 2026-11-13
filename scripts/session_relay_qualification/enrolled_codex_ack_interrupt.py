from enrolled_codex_ack import prepare as prepare_ack


async def prepare(root,work,c):
    case=await prepare_ack(root,work,c);case.interrupted=True
    return case
