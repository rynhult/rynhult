"""Authenticated primary control frames to the fixed target-local folder helper."""
import json

from acc import folder_channel


async def handle(ws, message, node_id, socket=None):
    rid = message.get('id')
    payload = message.get('payload')
    if (not isinstance(rid, str) or len(rid) > 64 or not isinstance(payload, dict)
            or payload.get('machine_id') != node_id):
        return
    try:
        result = await folder_channel.request(payload, socket=socket)
        response = {'t': 'folder-policy-result', 'id': rid, 'ok': True, 'result': result}
    except (OSError, ValueError, TimeoutError):
        response = {'t': 'folder-policy-result', 'id': rid, 'ok': False}
    await ws.send(json.dumps(response))
