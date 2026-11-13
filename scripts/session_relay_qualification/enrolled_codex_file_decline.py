from enrolled_codex_file_ack import prepare as prepare_ack


async def prepare(root,work,c):
    case=await prepare_ack(root,work,c)
    case.accepted=False;case.selected_choice='decline'
    return case
