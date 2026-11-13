from enrolled_stock_public import prepare as prepare_public


async def prepare(root,work,c):
    case=await prepare_public(root,work,c)
    case.base.command='exit 7'
    case.expected_exit=7
    return case
