"""Opt-in timing probes for the disposable E2E stack; never installed in accd.

Wrap existing calls without changing transport bytes, authorization, locks or
scheduling decisions. Records lengths/hashes, not terminal contents or secrets.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import sys
import time


def install(supervisor, path):
    from acc.broker import attach
    from acc.server import tty
    from acc.server.api import terminal

    output = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    current = contextvars.ContextVar('terminal_trace_session', default=None)
    mapping = {}

    def mark(stage, **fields):
        os.write(output, (json.dumps({'ns': time.time_ns(), 'stage': stage,
                                     'task': asyncio.current_task().get_name() if asyncio.current_task() else None,
                                     'coroutine': asyncio.current_task().get_coro().__qualname__ if asyncio.current_task() else None,
                                     'session': current.get(), **fields}) + '\n').encode())

    def payload(data):
        return {'bytes': len(data), 'hash': hashlib.sha256(data).hexdigest()[:16]}

    # Observe Uvicorn's complete-frame enqueue separately from the bridge's
    # later receive call, including backpressure while on_input is awaited.
    from uvicorn.protocols.websockets.auto import AutoWebSocketsProtocol
    original_enqueue = AutoWebSocketsProtocol.send_receive_event_to_app

    def enqueue(protocol):
        path = protocol.scope.get('path', '')
        if path.startswith('/api/sessions/') and path.endswith('/tty') and protocol.curr_msg_data_type == 'text':
            frame = json.loads(b''.join(protocol.frames))
            if frame.get('t') == 'i':
                token = current.set(path.split('/')[3])
                try:
                    mark('server.protocol_input', queued=protocol.queue.qsize(),
                         **payload(str(frame.get('d', '')).encode()))
                finally:
                    current.reset(token)
        return original_enqueue(protocol)

    AutoWebSocketsProtocol.send_receive_event_to_app = enqueue

    original_bridge = terminal.bridge

    async def bridge(ws, connect, session_id, tmux_name, *args, **kwargs):
        mapping[tmux_name] = session_id
        token = current.set(session_id)
        original_receive = ws.receive_text

        async def receive():
            text = await original_receive()
            frame = json.loads(text)
            if frame.get('t') == 'i':
                mark('server.receive', **payload(str(frame.get('d', '')).encode()))
            return text

        ws.receive_text = receive
        mark('server.attach_begin')
        try:
            return await original_bridge(ws, connect, session_id, tmux_name, *args, **kwargs)
        finally:
            mark('server.attach_end')
            current.reset(token)

    terminal.bridge = bridge
    original_frame = tty._frame

    def frame(kind, data):
        if kind == b'd':
            mark('server.broker_write', **payload(data))
        return original_frame(kind, data)

    tty._frame = frame
    original_cancel = supervisor.cancel_automation

    async def cancel(session_id):
        token = current.set(session_id)
        mark('server.on_input_begin')
        try:
            return await original_cancel(session_id)
        finally:
            mark('server.on_input_end')
            current.reset(token)

    supervisor.cancel_automation = cancel
    original_lock = supervisor._lock

    class Lock:
        async def acquire(self):
            caller = sys._getframe(2)
            site = f'{caller.f_code.co_filename.rsplit("/", 1)[-1]}:{caller.f_lineno}:{caller.f_code.co_name}'
            del caller
            mark('supervisor.lock_wait', site=site)
            result = await original_lock.acquire()
            self.site = site
            mark('supervisor.lock_acquired', site=site)
            return result

        def release(self):
            mark('supervisor.lock_release', site=self.site)
            original_lock.release()

        def locked(self):
            return original_lock.locked()

        async def __aenter__(self):
            await self.acquire()
            return self

        async def __aexit__(self, *_):
            self.release()

    supervisor._lock = Lock()
    original_attach = attach.attach

    async def broker_attach(config, params, *args):
        token = current.set(mapping.get(params.get('tmux_name')))
        mark('broker.attach_begin')
        try:
            return await original_attach(config, params, *args)
        finally:
            mark('broker.attach_end')
            current.reset(token)

    attach.attach = broker_attach
    original_read = attach.read_frame

    async def read_frame(reader):
        kind, data = await original_read(reader)
        if kind == b'd':
            mark('broker.receive', **payload(data))
        return kind, data

    attach.read_frame = read_frame
    original_os = attach.os

    class OS:
        def __getattr__(self, name):
            return getattr(original_os, name)

        def write(self, fd, data):
            mark('broker.pty_write_begin', **payload(data))
            count = original_os.write(fd, data)
            mark('broker.pty_write_end', bytes=count)
            return count

        def read(self, fd, count):
            data = original_os.read(fd, count)
            mark('broker.pty_read', **payload(data))
            return data

    attach.os = OS()

    async def heartbeat():
        previous = time.perf_counter()
        while True:
            await asyncio.sleep(.1)
            now = time.perf_counter()
            mark('event_loop.lag', ms=max(0, (now - previous - .1) * 1000))
            previous = now

    return asyncio.create_task(heartbeat())
