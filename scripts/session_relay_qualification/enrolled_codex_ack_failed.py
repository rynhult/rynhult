"""Actual granted command exits nonzero; permission application is separate."""
from enrolled_codex_ack import prepare as prepare_ack


async def prepare(root,work,c):
    case=await prepare_ack(root,work,c)
    case.accepted=True
    case.base.command='exit 7'
    case.expect_application=True
    return case
