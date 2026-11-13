from enrolled_stock_lifecycle import prepare_mode


async def prepare(root,work,c):
    return await prepare_mode(root,work,c,'chat')
