"""Pinned Escape through original public frame and chosen Cancel application."""
from enrolled_stock_public import prepare as prepare_public


async def prepare(root,work,c):
    case=await prepare_public(root,work,c)
    case.accepted=False
    return case
