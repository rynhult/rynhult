"""Browser terminal: websocket <-> broker PTY stream, with one writer per session.

Client -> server text frames: {"t": "i", "d": "<input>"} | {"t": "r", "rows": n, "cols": n}
Server -> client: binary frames are terminal output; text frames are JSON control
messages {"t": "mode", "write": bool} | {"t": "closed", "reason": "..."}.
"""
from __future__ import annotations

import asyncio
import json
import struct
from typing import Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect

from ..broker_protocol import response_from

HEADER = struct.Struct(">cI")


class Writers:
    """Tracks the single websocket per session allowed to type."""

    def __init__(self):
        self.by_session: dict[str, WebSocket] = {}

    def claim(self, session_id: str, ws: WebSocket, take: bool) -> tuple[bool, WebSocket | None]:
        current = self.by_session.get(session_id)
        if current is None or take:
            self.by_session[session_id] = ws
            return True, current if current is not ws else None
        return False, None

    def release(self, session_id: str, ws: WebSocket) -> None:
        if self.by_session.get(session_id) is ws:
            del self.by_session[session_id]


async def _read_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    kind, length = HEADER.unpack(await reader.readexactly(HEADER.size))
    return kind, await reader.readexactly(length)


def _frame(kind: bytes, payload: bytes) -> bytes:
    return HEADER.pack(kind, len(payload)) + payload


async def bridge(ws: WebSocket, connect, session_id: str, tmux_name: str, writers: Writers,
                 want_write: bool, take: bool, rows: int, cols: int,
                 signed_in: Callable[[], Awaitable[bool]] | None = None, recheck: float = 30.0,
                 on_input: Callable[[], Awaitable[None]] | None = None) -> None:
    """`connect()` opens a connection to the session's broker (a local socket, or a channel on its node).
    `signed_in` reauthorizes every `recheck` seconds; ended logins or lost access close the terminal."""
    can_write, displaced = writers.claim(session_id, ws, take) if want_write else (False, None)
    writer = None
    tasks = set()
    try:
        if displaced is not None:
            try:
                await displaced.close(code=4001, reason="control taken by another window")
            except RuntimeError:
                pass
        reader, writer = await connect()
        params = {"tmux_name": tmux_name, "readonly": not can_write, "rows": rows, "cols": cols}
        writer.write(json.dumps({"id": 1, "method": "attach", "params": params}).encode() + b"\n")
        await writer.drain()
        try:
            hello = response_from(await reader.readline(), 1, attach=True)
        except ValueError as error:
            raise ConnectionError(str(error)) from error
        if not hello.get("ok"):
            await ws.send_text(json.dumps({"t": "closed", "reason": hello.get("error", "attach failed")}))
            await ws.close(code=4404)
            return
        await ws.send_text(json.dumps({"t": "mode", "write": can_write}))

        async def to_browser() -> None:
            while True:
                kind, payload = await _read_frame(reader)
                if kind == b"d":
                    await ws.send_bytes(payload)
                elif kind == b"x":
                    await ws.send_text(json.dumps({"t": "closed", "reason": "terminal detached"}))
                    return

        async def from_browser() -> None:
            while True:
                message = json.loads(await ws.receive_text())
                if message.get("t") == "i" and can_write and writers.by_session.get(session_id) is ws:
                    if message.get("d") and on_input:
                        await on_input()
                    writer.write(_frame(b"d", str(message.get("d", "")).encode()))
                elif message.get("t") == "r":
                    size = {"rows": int(message["rows"]), "cols": int(message["cols"])}
                    writer.write(_frame(b"r", json.dumps(size).encode()))
                await writer.drain()

        async def login_lasts() -> None:
            while signed_in is not None:
                await asyncio.sleep(recheck)
                if not await signed_in():
                    await ws.send_text(json.dumps({"t": "closed", "reason": "terminal access ended"}))
                    await ws.close(code=4401)
                    return
            await asyncio.Event().wait()

        tasks = {asyncio.create_task(to_browser()), asyncio.create_task(from_browser()), asyncio.create_task(login_lasts())}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.exception() and not isinstance(task.exception(), (WebSocketDisconnect, asyncio.IncompleteReadError,
                                                                        ConnectionError, RuntimeError)):
                raise task.exception()
    finally:
        writers.release(session_id, ws)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if writer is not None:
                writer.write(_frame(b"x", b""))
                await writer.drain()
        except (ConnectionError, RuntimeError):
            pass
        finally:
            if writer is not None:
                writer.close()
