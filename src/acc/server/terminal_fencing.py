"""Run-bound P2 observer attachments and explicit control on the same socket/PTY."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi import WebSocketDisconnect

from ..broker_protocol import response_from
from .tty import TAKE_REASONS, _frame, _read_frame


async def fenced_bridge(ws, connect, session_id, tmux_name, writers, *, run_id, rows, cols,
                        valid, can_control=None, recheck=30.0, on_input=None, on_control=None, actor=None, actor_id=None):
    writable = can_control or valid
    reader, writer = await connect()
    tasks = set()
    pending_mode = None
    controller = SimpleNamespace(busy=False, actor=actor, channel="terminal", demote=None, session_id=session_id, notify=None)
    wheel = False
    lease_epoch = None
    geometry = {}

    async def mode(reason=None):
        owns = writers.by_session.get(session_id) is ws and lease_epoch == writers.epoch(session_id)
        await ws.send_text(json.dumps({'t': 'mode', 'write': owns, 'wheel': wheel, 'run_id': run_id,
            **(geometry if not owns else {}),
            'epoch': writers.epoch(session_id), 'held': writers.by_session.get(session_id) is not None,
            'reason': reason, 'partial': bool(writers.partial.get(session_id)),
            # The same person's own unsent text with no current holder: claim without the handover dialog.
            'reclaim': not owns and writers.may_reclaim(session_id, actor_id)}))

    async def broker_mode(write):
        nonlocal pending_mode
        pending_mode = asyncio.get_running_loop().create_future()
        writer.write(_frame(b'm', json.dumps({'write': write}).encode()))
        await writer.drain()
        result = await asyncio.wait_for(pending_mode, 5)
        pending_mode = None
        if result.get('write') is not write:
            raise ConnectionError('terminal control unavailable')

    async def demote():
        nonlocal lease_epoch
        lease_epoch = None
        await broker_mode(False)
        await mode('Control moved to another view.')

    controller.demote = demote
    controller.notify = mode

    async def notify_observers():
        for other, state in list(writers.controllers.items()):
            if other is not ws and state.session_id == session_id:
                try:
                    await state.notify()
                except (RuntimeError, ConnectionError, WebSocketDisconnect):
                    pass
    try:
        if not await valid():
            await ws.close(code=4409)
            return
        writer.write(json.dumps({'id': 1, 'method': 'attach', 'params': {
            'tmux_name': tmux_name, 'readonly': True, 'fenced_control': True, 'rows': rows, 'cols': cols,
        }}).encode() + b'\n')
        await writer.drain()
        hello = response_from(await reader.readline(), 1, attach=True)
        if not hello.get('ok') or hello.get('fenced_control') is not True or not await valid():
            await ws.send_text(json.dumps({'t': 'closed', 'reason': 'Run-bound terminal attachment unavailable. Recheck the session.'}))
            await ws.close(code=4409)
            return
        wheel = hello.get('wheel') is True
        geometry = {key: hello[key] for key in ("rows", "cols") if key in hello}
        writers.controllers[ws] = controller
        await mode()

        async def output():
            nonlocal geometry
            while True:
                kind, payload = await _read_frame(reader)
                if kind == b'd':
                    await ws.send_bytes(payload)
                elif kind == b'g':
                    geometry = {key: json.loads(payload)[key] for key in ('rows', 'cols')}
                    await ws.send_text(json.dumps({'t': 'geometry', **geometry}))
                elif kind == b'm' and pending_mode is not None and not pending_mode.done():
                    pending_mode.set_result(json.loads(payload))
                elif kind == b'x':
                    await ws.send_text(json.dumps({'t': 'closed', 'reason': 'Terminal detached. Recheck the session.'}))
                    return

        async def control(message):
            nonlocal lease_epoch
            async with writers.control_locks.setdefault(session_id, asyncio.Lock()):
                current = writers.by_session.get(session_id)
                previous = writers.controllers.get(current)
                reason = message.get('reason')
                reason = reason if isinstance(reason, str) and reason in TAKE_REASONS else None
                event = {'take': message.get('take') is True, 'reason': reason,
                         'previous_actor': getattr(previous, 'actor', None),
                         'previous_channel': getattr(previous, 'channel', 'automation' if current else None),
                         'new_channel': 'terminal'}

                async def refuse(code, text):
                    if on_control:
                        await on_control({**event, 'outcome': 'refused', 'code': code})
                    await mode(text)

                if message.get('run_id') != run_id or type(message.get('epoch')) is not int or message['epoch'] != writers.epoch(session_id) or type(message.get('take', False)) is not bool or not await valid():
                    await refuse('stale', 'Control changed or this run ended. Recheck before taking control.')
                    return
                if message.get('take') and reason is None:
                    await refuse('reason', 'Choose a supported reason to take control.')
                    return
                if not await writable():
                    await refuse('unavailable', 'This run cannot accept input. Check its recovery or backup status.')
                    return
                if current is not writers.by_session.get(session_id) or message['epoch'] != writers.epoch(session_id):
                    await refuse('stale', 'Control changed during the access check. Review the current holder before taking control.')
                    return
                if current is ws:
                    await mode()
                    return
                if current is not None:
                    if not message.get('take'):
                        await refuse('held', 'Another window or delivery controls this terminal.')
                        return
                    if previous is None or previous.busy:
                        await refuse('pending', 'The current controller has pending input or active composition. Try again after it finishes.')
                        return
                reclaim = current is None and writers.may_reclaim(session_id, actor_id)
                if (writers.partial.get(session_id) and message.get('confirm_partial') is not True
                        and not reclaim):
                    await refuse('partial', 'Partial input may remain in the terminal. Confirm handover without clearing or replaying it.')
                    return
                claimed, _ = writers.claim(session_id, ws, current is not None, human=True)
                if not claimed:
                    await refuse('pending', 'The current controller has pending input. Try again after it finishes.')
                    return
                lease_epoch = writers.epoch(session_id)
                if current is not None:
                    await previous.demote()
                await broker_mode(True)
                if writers.by_session.get(session_id) is not ws or lease_epoch != writers.epoch(session_id) or not await valid():
                    writers.release(session_id, ws)
                    await demote()
                    await refuse('stale', 'Control changed before handover completed.')
                    return
                if writers.partial.get(session_id):
                    # A confirmed controller now answers for the existing prompt,
                    # including a disconnect before its first pins/input frame.
                    writers.record_partial(session_id, True, actor_id)
                if on_control:
                    if reclaim:
                        event.update(reason='same_person_reconnect', partial=True)
                    await on_control({**event, 'outcome': 'granted', 'code': None})
                await mode()
                await notify_observers()

        async def input_frames():
            nonlocal lease_epoch
            while True:
                raw = await ws.receive_text()
                if len(raw) > 262144:
                    await ws.close(code=4400)
                    return
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                kind = message.get('t')
                owns = (message.get('run_id') == run_id and type(message.get('epoch')) is int
                        and message['epoch'] == lease_epoch == writers.epoch(session_id)
                        and writers.by_session.get(session_id) is ws)
                if kind == 'control':
                    await control(message)
                    continue
                if kind == 'status':
                    await mode()
                    continue
                if kind == 'pins':
                    if owns:
                        controller.busy = message.get('busy') is not False
                        writers.record_partial(session_id, message.get('partial') is not False, actor_id)
                    continue
                if kind == 'release':
                    if controller.busy:
                        await mode('Finish pending input before releasing control.')
                    elif owns and writers.partial.get(session_id) and message.get('confirm_partial') is not True:
                        await mode('Partial input may remain. Confirm release without clearing or replaying it.')
                    elif owns:
                        writers.release(session_id, ws)
                        await demote()
                        await notify_observers()
                    continue
                if kind == 'i':
                    data = message.get('d')
                    if owns and isinstance(data, str) and data and await writable():
                        if on_input:
                            await on_input()
                        # No suspension between the final identity/epoch check and the bytes.
                        if await writable() and lease_epoch == writers.epoch(session_id) and writers.by_session.get(session_id) is ws:
                            writer.write(_frame(b'd', data.encode()))
                            await writer.drain()
                    await ws.send_text(json.dumps({'t': 'ack'}))
                elif kind == 'wheel' and owns and wheel and await writable():
                    lines = message.get('lines')
                    if type(lines) is int and 1 <= abs(lines) <= 200 and lease_epoch == writers.epoch(session_id) and writers.by_session.get(session_id) is ws:
                        writer.write(_frame(b'w', json.dumps(lines).encode()))
                        await writer.drain()
                elif kind == 'r' and owns and await valid() and lease_epoch == writers.epoch(session_id) and writers.by_session.get(session_id) is ws:
                    if type(message.get('rows')) is int and type(message.get('cols')) is int:
                        size = {'rows': max(2, min(300, message['rows'])), 'cols': max(2, min(500, message['cols']))}
                        writer.write(_frame(b'r', json.dumps(size).encode()))
                        await writer.drain()

        async def lifetime():
            while True:
                await asyncio.sleep(recheck)
                if not await valid():
                    await ws.send_text(json.dumps({'t': 'closed', 'reason': 'Terminal access or this run ended.'}))
                    await ws.close(code=4409)
                    return

        tasks = {asyncio.create_task(output()), asyncio.create_task(input_frames()), asyncio.create_task(lifetime())}
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception() and not isinstance(task.exception(), (WebSocketDisconnect, asyncio.IncompleteReadError, ConnectionError, RuntimeError, ValueError)):
                raise task.exception()
    finally:
        writers.release(session_id, ws)
        writers.controllers.pop(ws, None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await notify_observers()
        try:
            writer.write(_frame(b'x', b''))
            await writer.drain()
        except (ConnectionError, RuntimeError):
            pass
        writer.close()
