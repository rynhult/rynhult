"""Exact private resolution switches native modal without answering either tool."""
from enrolled_stock_decoder import Case
from installed_security import prepare as prepare_security


async def prepare(root, work, c):
    return Case(await prepare_security(root, work, c), concurrent=True, withdraw=True)
