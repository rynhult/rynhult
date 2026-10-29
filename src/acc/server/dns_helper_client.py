"""Private bounded root-helper channel; never return import fields through a public API."""
import asyncio
import json

from acc.peercred import peer_uid


async def request(message):
    async with asyncio.timeout(30):
        reader, writer = await asyncio.open_unix_connection('/run/acc-dns/helper.sock', limit=16385)
        try:
            if peer_uid(writer.get_extra_info('socket')) != 0:
                raise OSError('untrusted DNS helper')
            writer.write(json.dumps(message).encode() + b'\n')
            await writer.drain()
            response = await reader.readline()
            if len(response) > 16384:
                raise OSError('DNS helper response refused')
            return json.loads(response)
        except (ValueError, asyncio.LimitOverrunError) as error:
            raise OSError('DNS helper response refused') from error
        finally:
            writer.close()
            await writer.wait_closed()
