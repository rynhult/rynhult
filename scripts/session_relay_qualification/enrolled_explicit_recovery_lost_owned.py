from enrolled_explicit_recovery import prepare as prepare_recovery


async def prepare(root,work,c):
    case=await prepare_recovery(root,work,c)
    case.lose_owned_ack=True
    return case
