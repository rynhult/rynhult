"""Framed read attachment to one existing owned TUI; never input authority."""
from __future__ import annotations

import asyncio
import json
import socket

from acc.session_relay import protocol as p

from .attach import attach


class OwnedObserver:
    def __init__(self, config, name, run, authorize, same_process):
        self.config, self.name, self.run = config, name, run
        self.authorize, self.same_process = authorize, same_process
        self.closed = False
        self.writers, self.tasks = set(), set()
        self.lock = asyncio.Lock()

    async def current(self, run):
        return await self._current(run, self.authorize)

    async def _current(self, run, authorize):
        if self.closed or run != self.run or await authorize() is not True:
            return False
        try:
            owned = await asyncio.to_thread(self.same_process) is True
        except (OSError, ValueError):
            return False
        return owned and await authorize() is True and not self.closed

    async def capture(self, run, *, authorize=None, history=True):
        from . import ops
        # Only the internal screen-read composition may supply its fresh read
        # grant. Stream opening (including input) always uses self.authorize.
        authorize = self.authorize if authorize is None else authorize
        if not await self._current(run, authorize):
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        value = await asyncio.to_thread(ops.tmux, self.config, 'capture-pane', '-p', '-J',
                                       *(('-S', '-200') if history else ()),
                                       '-t', '=' + self.name + ':')
        if value.returncode or not await self._current(run, authorize):
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        raw = value.stdout.encode()
        truncated = len(raw) > 128 << 10
        text = raw[-(128 << 10):].decode(errors='replace')
        if truncated:
            text = text.partition('\n')[2]  # no partial-line secret/redaction fragment
        return {'text': text, 'truncated': truncated}

    def readonly(self):
        return True

    async def open(self, run):
        async with self.lock:
            if not await self.current(run):
                raise p.RelayError(p.ErrorCode.STALE_RUN)
            if len(self.tasks) >= 16:
                raise p.RelayError(p.ErrorCode.LIMIT)
            left, right = socket.socketpair()
            left.setblocking(False)
            right.setblocking(False)
            reader, writer = await asyncio.open_connection(sock=left)
            remote_reader, remote_writer = await asyncio.open_connection(sock=right)
            async def serve():
                try:
                    # Read-only at the actual broker byte boundary as well as
                    # the Host interface. A handshake/read cannot mint input.
                    await attach(self.config, {'tmux_name': self.name, 'readonly': self.readonly()}, remote_reader, remote_writer)
                finally:
                    remote_writer.close()
                    self.writers.discard(writer)
            task = asyncio.create_task(serve())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            self.writers.add(writer)
            try:
                response = json.loads(await asyncio.wait_for(reader.readline(), 5))
                if response.get('ok') is not True or response.get('readonly') is not self.readonly():
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                if not await self.current(run):
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                return reader, writer
            except BaseException:
                writer.close()
                raise

    async def close(self):
        self.closed = True
        # Wait for an in-flight handshake to observe closure before collecting
        # its streams. This lock is never held by the long-lived output reader.
        async with self.lock:
            for writer in tuple(self.writers):
                writer.close()
            tasks = tuple(self.tasks)
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=5)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


class OwnedInputAttachment(OwnedObserver):
    """Internal Host composition only; authorize must validate its opaque grant.

    This class is never selected by an attach payload or read preference.
    """
    def readonly(self):
        return False
