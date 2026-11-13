from enrolled_codex_ack_failed import prepare as prepare_failed


async def prepare(root,work,c):
    case=await prepare_failed(root,work,c)
    case.loss='after'
    return case
