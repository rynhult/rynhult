"""Bounded service-to-root folder channel; no agent-facing route or arbitrary socket."""
import asyncio
import json

from acc.peercred import peer_uid

SOCKET = '/run/acc-folder-policy/helper.sock'
MAX_MESSAGE = 65536


async def request(message, socket=SOCKET):
    if socket is None:
        raise OSError("Folder helper is not configured.")
    raw = json.dumps(message).encode() + b'\n'
    if len(raw) > MAX_MESSAGE - 1024:
        raise ValueError('The folder request is too large. Choose fewer folders.')
    async with asyncio.timeout(40):
        reader, writer = await asyncio.open_unix_connection(socket, limit=MAX_MESSAGE + 1)
        try:
            if peer_uid(writer.get_extra_info('socket')) != 0:
                raise OSError('Untrusted folder helper.')
            writer.write(raw)
            await writer.drain()
            reply = await reader.readline()
            if len(reply) > MAX_MESSAGE:
                raise OSError('Invalid folder helper response.')
            result = json.loads(reply)
            if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                raise OSError('Invalid folder helper response.')
            if not result['ok']:
                raise ValueError('The target refused this folder operation. Check its status and review again.')
            if not isinstance(result.get('result'), dict):
                raise OSError('Invalid folder helper response.')
            return result['result']
        finally:
            writer.close()
            await writer.wait_closed()
