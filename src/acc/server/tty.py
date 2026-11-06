"""Browser terminal: websocket <-> broker PTY stream, with one writer per session.

Client -> server text frames: {"t": "i", "d": "<input>"} | {"t": "r", "rows": n, "cols": n}
Server -> client: binary frames are terminal output; text frames are JSON control
messages {"t": "mode", "write": bool, "partial": bool, "reason": str | None} | {"t": "closed", "reason": "..."}.
"""
from __future__ import annotations

import asyncio
import json
import re
import struct
from types import SimpleNamespace
from typing import Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect

from ..broker_protocol import response_from

TAKE_REASONS = {'manual_handover', 'controller_unavailable', 'team_intervention'}

HEADER = struct.Struct(">cI")
# Recognized terminal replies, never keystrokes: focus, primary/secondary
# device attributes and cursor position. Strip replies only for classification;
# forward the original frame exactly once, including mixed reply + human data.
# Undecorated row 1 is ambiguous with modified F3 (CSI 1;<modifier> R).
# Treat it and all unknown/malformed replies as human: cancellation is safer.
_TERMINAL_REPLY = re.compile(
    r"\x1b\[(?:[IO]|[?>][0-9]+(?:;[0-9]+)*c|"
    r"(?:\?[1-9][0-9]*|(?!1;)[1-9][0-9]*);[1-9][0-9]*R)"
)


def human_input(data: str) -> bool:
    return bool(_TERMINAL_REPLY.sub("", data))


class Writers:
    """Tracks the single websocket per session allowed to type."""

    def __init__(self):
        self.by_session: dict[str, WebSocket] = {}
        self.epochs: dict[str, int] = {}
        self.partial: dict[str, bool] = {}
        self.partial_actors: dict[str, int] = {}
        self.runs: dict[str, str] = {}
        self.controllers: dict[object, object] = {}
        self.control_locks: dict[str, asyncio.Lock] = {}

    def bind_run(self, session_id: str, run_id: str) -> None:
        if self.runs.get(session_id) != run_id:
            self.partial.pop(session_id, None)
            self.partial_actors.pop(session_id, None)
            self.runs[session_id] = run_id

    def record_partial(self, session_id: str, partial: bool, actor_id: int | None) -> None:
        self.partial[session_id] = partial
        if partial and actor_id is not None:
            self.partial_actors[session_id] = actor_id
        else:
            self.partial_actors.pop(session_id, None)

    def may_reclaim(self, session_id: str, actor_id: int | None) -> bool:
        """The same person's reconnect, never a second live window or another person."""
        return (actor_id is not None and bool(self.partial.get(session_id)) and session_id not in self.by_session
                and self.partial_actors.get(session_id) == actor_id)

    def epoch(self, session_id: str) -> int:
        return self.epochs.get(session_id, 0)

    def claim(self, session_id: str, ws: WebSocket, take: bool, *, human: bool = False) -> tuple[bool, WebSocket | None]:
        current = self.by_session.get(session_id)
        if not human and self.partial.get(session_id):
            return False, None
        if current is not None and current is not ws and getattr(self.controllers.get(current), 'busy', False):
            return False, None
        if current is None or take:
            self.by_session[session_id] = ws
            if current is not ws:
                self.epochs[session_id] = self.epoch(session_id) + 1
            return True, current if current is not ws else None
        return False, None

    def release(self, session_id: str, ws: WebSocket) -> None:
        if self.by_session.get(session_id) is ws:
            del self.by_session[session_id]
            self.epochs[session_id] = self.epoch(session_id) + 1


async def _read_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    kind, length = HEADER.unpack(await reader.readexactly(HEADER.size))
    return kind, await reader.readexactly(length)


def _frame(kind: bytes, payload: bytes) -> bytes:
    return HEADER.pack(kind, len(payload)) + payload


async def bridge(ws: WebSocket, connect, session_id: str, tmux_name: str, writers: Writers,
                 want_write: bool, take: bool, rows: int, cols: int,
                 signed_in: Callable[[], Awaitable[bool]] | None = None, recheck: float = 30.0,
                 on_input: Callable[[], Awaitable[None]] | None = None, *, take_reason=None,
                 confirm_partial=False, actor=None, actor_id=None, on_control=None, bound_run: str | None = None, bound_instance: str | None = None, observer_geometry=None) -> None:
    """`connect()` opens a connection to the session's broker (a local socket, or a channel on its node).
    `signed_in` reauthorizes every `recheck` seconds; ended logins or lost access close the terminal."""
    if bound_run is not None and (want_write or take or signed_in is None):
        raise ValueError('Run-bound attachments must be authorized observers.')
    previous = writers.by_session.get(session_id)
    holder = writers.controllers.get(previous)
    reason = take_reason if isinstance(take_reason, str) and take_reason in TAKE_REASONS else None
    refusal = None
    if take and reason is None:
        refusal = 'Choose a supported reason to take control.'
    reclaim = want_write and not take and writers.may_reclaim(session_id, actor_id)
    if not refusal and want_write and writers.partial.get(session_id) and not confirm_partial and not reclaim and (take or previous is None):
        refusal = 'Partial input may remain. Confirm handover without clearing or replaying it.'
    can_write, displaced = writers.claim(session_id, ws, take, human=True) if want_write and not refusal else (False, None)
    if take and not can_write and not refusal:
        refusal = 'The current controller has pending input or active composition. Try again after it finishes.'
    if can_write and writers.partial.get(session_id):
        writers.record_partial(session_id, True, actor_id)  # whoever writes now answers for this text
        refusal = 'Partial input may remain. Inspect it before typing.'
    elif not can_write and not refusal:
        refusal = 'another window has control' if want_write else 'this terminal is read-only here'
    async def demote():
        await ws.close(code=4001, reason="control taken by another window")
    async def notify():
        pass
    controller = SimpleNamespace(busy=False, actor=actor, channel='terminal', demote=demote, notify=notify, session_id=session_id)
    writers.controllers[ws] = controller
    writer = None
    tasks = set()
    try:
        if take and on_control:
            await on_control({'take': True, 'reason': reason, 'previous_actor': getattr(holder, 'actor', None),
                              'previous_channel': getattr(holder, 'channel', 'automation' if previous else None),
                              'new_channel': 'terminal', 'outcome': 'granted' if can_write else 'refused'})
        if reclaim and can_write and on_control:
            await on_control({'take': False, 'reason': 'same_person_reconnect', 'previous_actor': actor,
                              'previous_channel': 'terminal', 'new_channel': 'terminal', 'outcome': 'granted'})
        if displaced is not None:
            try:
                await displaced.close(code=4001, reason="control taken by another window")
            except RuntimeError:
                pass
        reader, writer = await connect()
        # Broker connection can await: revalidate the captured incarnation before attach.
        if bound_run is not None and not await signed_in():
            # No attach handshake was sent: the broker still expects JSON,
            # not the binary detach frame used by an established attachment.
            writer.close()
            writer = None
            await ws.close(code=4404)
            return
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
        if bound_run is not None and not await signed_in():
            await ws.close(code=4404)
            return
        await ws.send_text(json.dumps({"t": "mode", "write": can_write,
            **({"wheel": hello.get("wheel") is True} if bound_run is None else {}),
            **({key: hello[key] for key in ("rows", "cols") if key in hello} if not can_write else {}),
            **({"partial": bool(writers.partial.get(session_id)), "reason": refusal} if bound_run is None else {}),
            **({'run': bound_run, 'instance': bound_instance, 'protocol': 'run-observer-v1', **({'rows': observer_geometry[0], 'cols': observer_geometry[1]} if observer_geometry else {})} if bound_run is not None else {})}))

        async def to_browser() -> None:
            checked_until = 0.0
            while True:
                kind, payload = await _read_frame(reader)
                if bound_run is not None and asyncio.get_running_loop().time() >= checked_until:
                    if not await signed_in():
                        await ws.close(code=4404)
                        return
                    checked_until = asyncio.get_running_loop().time() + .25
                if kind == b"d":
                    await ws.send_bytes(payload)
                elif kind == b"g":
                    await ws.send_text(json.dumps({"t": "geometry", **json.loads(payload)}))
                elif kind == b"x":
                    await ws.send_text(json.dumps({"t": "closed", "reason": "terminal detached"}))
                    return

        async def from_browser() -> None:
            while True:
                message = json.loads(await ws.receive_text())
                if message.get('t') == 'pins' and writers.by_session.get(session_id) is ws:
                    controller.busy = message.get('busy') is not False
                    writers.record_partial(session_id, message.get('partial') is not False, actor_id)
                if message.get("t") == "i" and can_write and writers.by_session.get(session_id) is ws:
                    if human_input(str(message.get("d", ""))) and on_input:
                        await on_input()
                    if writers.by_session.get(session_id) is ws:
                        writer.write(_frame(b"d", str(message.get("d", "")).encode()))
                elif message.get("t") == "wheel" and can_write and writers.by_session.get(session_id) is ws:
                    lines = message.get('lines')
                    if hello.get('wheel') is True and type(lines) is int and 1 <= abs(lines) <= 200:
                        writer.write(_frame(b'w', json.dumps(lines).encode()))
                elif message.get("t") == "r" and bound_run is None:
                    size = {"rows": int(message["rows"]), "cols": int(message["cols"])}
                    writer.write(_frame(b"r", json.dumps(size).encode()))
                await writer.drain()
                if message.get('t') == 'i':
                    await ws.send_text(json.dumps({'t': 'ack'}))

        async def login_lasts() -> None:
            while signed_in is not None:
                await asyncio.sleep(recheck)
                if not await signed_in():
                    await ws.send_text(json.dumps({"t": "closed", "reason": "terminal access ended"}))
                    await ws.close(code=4404 if bound_run is not None else 4401)
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
        writers.controllers.pop(ws, None)
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
