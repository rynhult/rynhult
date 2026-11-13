from enrolled_stock_public import prepare as prepare_public


async def prepare(root,work,c):
    case=await prepare_public(root,work,c)
    case.accepted=False
    case.loss='after'
    return case
