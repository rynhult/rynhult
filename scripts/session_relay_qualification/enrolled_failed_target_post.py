"""Known partial native target cleanup, using its own authenticated tree proof."""
from enrolled_failed_target import prepare as prepare_failure


async def prepare(root,work,c):
    case=await prepare_failure(root,work,c)
    case.after_fork=True
    return case
