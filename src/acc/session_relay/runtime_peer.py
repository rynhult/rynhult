"""Bounded bidirectional RPC inside the existing authenticated broker stream.

No listener, retries, authority cache or client-supplied actor. Authority requests
travel back to main over the same pinned connection that carries dispatch.
"""
from __future__ import annotations

import asyncio
import json
import secrets

from .protocol import MAX_FRAME_BYTES


class RuntimePeer:
    def __init__(self, reader, writer, handle, *, lost=lambda: None, capacity=16, timeout=30):
        self.reader, self.writer, self.handle = reader, writer, handle
        self.lost, self.capacity, self.timeout = lost, capacity, timeout
        self.pending, self.handlers = {}, set()
        self.closed = False
        self.lock = asyncio.Lock()
        self.task = asyncio.create_task(self._read())

    async def write(self, value):
        data = json.dumps(value, separators=(',', ':')).encode() + b'\n'
        if len(data) > MAX_FRAME_BYTES:
            raise ValueError('Runtime frame exceeds its bound')
        async with self.lock:
            if self.closed:
                raise ConnectionError('Runtime channel closed')
            self.writer.write(data)
            await self.writer.drain()

    async def call(self, method, params):
        if self.closed:
            raise ConnectionError('Runtime channel closed')
        if len(self.pending) >= self.capacity:
            raise ValueError('Runtime request capacity exceeded')
        identifier = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.write({'relayVersion': 1, 'id': identifier, 'method': method, 'params': params})
            return await asyncio.wait_for(future, self.timeout)
        except (TimeoutError, asyncio.CancelledError):
            self.invalidate()
            raise
        finally:
            self.pending.pop(identifier, None)

    def invalidate(self):
        if self.closed:
            return
        self.closed = True
        self.lost()
        self.writer.close()
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError('Runtime authority lost'))

    async def _serve(self, request):
        try:
            result = await self.handle(request['method'], request['params'])
            await self.write({'relayVersion': 1, 'id': request['id'], 'result': result})
        except Exception:
            # Never send raw native errors, prompts, paths or authentication data.
            if not self.closed:
                await self.write({'relayVersion': 1, 'id': request['id'], 'error': 'Runtime request refused'})

    def _done(self, task):
        self.handlers.discard(task)
        if not task.cancelled():
            task.exception()

    async def _read(self):
        try:
            while line := await self.reader.readline():
                if len(line) > MAX_FRAME_BYTES:
                    break
                value = json.loads(line)
                if (not isinstance(value, dict) or type(value.get('relayVersion')) is not int
                        or value['relayVersion'] != 1 or type(value.get('id')) is not str
                        or len(value['id']) != 32):
                    break
                if 'method' in value:
                    if (value.keys() != {'relayVersion', 'id', 'method', 'params'}
                            or not isinstance(value['params'], dict) or type(value['method']) is not str
                            or len(self.handlers) >= self.capacity):
                        break
                    task = asyncio.create_task(self._serve(value))
                    self.handlers.add(task)
                    task.add_done_callback(self._done)
                elif value.keys() in ({'relayVersion', 'id', 'result'}, {'relayVersion', 'id', 'error'}):
                    future = self.pending.get(value['id'])
                    if future is None or future.done():
                        break
                    if 'error' in value:
                        future.set_exception(ConnectionError('Runtime authority refused'))
                    else:
                        future.set_result(value['result'])
                else:
                    break
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            self.invalidate()

    async def close(self):
        self.invalidate()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        # Accepted dispatches finish journalling before the channel is reclaimed.
        await asyncio.gather(*self.handlers, return_exceptions=True)
