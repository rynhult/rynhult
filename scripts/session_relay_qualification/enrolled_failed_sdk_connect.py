from enrolled_failed_sdk_target import prepare as prepare_failure


async def prepare(root,work,c):
    case=await prepare_failure(root,work,c);case.connect_failure=True
    return case
