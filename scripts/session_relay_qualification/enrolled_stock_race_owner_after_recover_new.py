from enrolled_stock_race_loss import prepare_mode


async def prepare(root,work,c):
    return await prepare_mode(root,work,c,'owner_after')
