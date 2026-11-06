"""Loopback-only fake PTY. No subprocess, broker, session, or real terminal."""
import asyncio
import json

from websockets.asyncio.server import serve


def event(**value):
    print(json.dumps(value), flush=True)


async def main():
    async def connection(ws):
        event(kind="connection")
        await ws.send("SCROLLBACK-SENTINEL\r\n" + "".join(f"initial-{n:03}\r\n" for n in range(120)))

        async def output():
            n = 0
            while True:
                await asyncio.sleep(0.025)
                await ws.send(f"stream-{n:06}\r\n")
                n += 1
                event(kind="output", sequence=n)

        stream = asyncio.create_task(output())
        try:
            async for message in ws:
                event(kind="client", message=json.loads(message))
        finally:
            stream.cancel()
            await asyncio.gather(stream, return_exceptions=True)
            event(kind="closed")

    async with serve(connection, "127.0.0.1", 0) as server:
        event(kind="ready", port=server.sockets[0].getsockname()[1])
        await asyncio.Future()


asyncio.run(main())
