from enrolled_codex_ack import Case
from enrolled_codex_file_inventory import prepare as prepare_inventory


async def prepare(root,work,c):
    inventory=await prepare_inventory(root,work,c)
    case=Case(inventory.base)
    case.request_method='item/fileChange/requestApproval';case.item_type='fileChange'
    case.accepted=True;case.selected_choice='accept'
    return case
