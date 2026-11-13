"""TerminalHost v1 over an existing broker tmux attachment's framed byte path.

The opener is the normal authorized attach path, never launch or send-keys. A
socket drain proves delivery to the bridge, not tmux decoding: without a qualified
processing acknowledgement, handover stays blocked. No synthetic watermark.
"""
from __future__ import annotations

import asyncio
import json

from acc.broker.attach import frame, read_frame

from .adapters.base import (
    Dimensions,
    ExitObservation,
    ExitState,
    GeometryGrant,
    InputGrant,
    TerminalHost,
    TerminalRun,
    Watermark,
    WriteResult,
)
from .protocol import MAX_TERMINAL_INPUT_BYTES, ErrorCode, RelayError


class TmuxTerminalHost(TerminalHost):
    def __init__(self, run: TerminalRun, opener, validate_input, validate_geometry, *, validate_read=None, consume_output=False):
        self.run, self.opener = run, opener
        self.validate_input, self.validate_geometry = validate_input, validate_geometry
        self.validate_read = validate_read
        self.reader = self.writer = None
        self.consume_output, self.output_task = consume_output, None
        self.grant = None
        self.admitted = self.processed = 0
        self.partial = False
        self.lock = asyncio.Lock()
        self.exit = ExitObservation(ExitState.UNKNOWN)

    def _run(self, run):
        if run != self.run:
            raise RelayError(ErrorCode.STALE_RUN)

    async def _connect(self):
        if self.writer is None:
            self.reader, self.writer = await self.opener(self.run)
            self.exit = ExitObservation(ExitState.RUNNING)
            if self.consume_output:
                self.output_task = asyncio.create_task(self._consume_output(self.reader))

    async def _consume_output(self, reader):
        # Input-only bridges still own the other half of the duplex tmux stream.
        # Leaving it unread backpressures the pane and strands client retirement.
        # Screen delivery uses the separate authorized observer; these duplicate
        # frames never prove native processing or execution exit.
        try:
            while True:
                kind, _data = await read_frame(reader)
                if kind == b'x' or kind not in (b'd', b'g', b'm'):
                    break
        except asyncio.CancelledError:
            return
        except (asyncio.IncompleteReadError, OSError, ConnectionError, ValueError):
            pass
        self.partial = True
        self.exit = ExitObservation(ExitState.UNKNOWN)

    async def attach_read(self, run):
        if self.consume_output:
            raise RelayError(ErrorCode.UNSUPPORTED)
        self._run(run)
        if self.validate_read is None or not await self.validate_read(run):
            raise RelayError(ErrorCode.NOT_FOUND_OR_DENIED)
        async with self.lock:
            await self._connect()
        while self.reader is not None:
            try:
                kind, data = await read_frame(self.reader)
            except (asyncio.IncompleteReadError, ConnectionError):
                self.exit = ExitObservation(ExitState.UNKNOWN)
                return
            if not await self.validate_read(run):
                raise RelayError(ErrorCode.NOT_FOUND_OR_DENIED)
            if kind == b'd':
                yield data
            elif kind == b'x':
                self.exit = ExitObservation(ExitState.EXITED)
                return

    async def claim_input(self, grant: InputGrant):
        self._run(grant.run)
        if not await self.validate_input(grant):
            raise RelayError(ErrorCode.STALE_AUTHORITY)
        async with self.lock:
            if not await self.validate_input(grant):
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            if self.grant == grant:
                return
            if self.partial or self.admitted != self.processed:
                raise RelayError(ErrorCode.INPUT_CONFLICT)
            if self.grant and grant.epoch <= self.grant.epoch:
                raise RelayError(ErrorCode.INPUT_CONFLICT)
            self.grant, self.admitted, self.processed = grant, 0, 0

    async def write_frames(self, grant: InputGrant, sequence: int, data: bytes):
        self._run(grant.run)
        if type(data) is not bytes or not data or len(data) > MAX_TERMINAL_INPUT_BYTES:
            raise RelayError(ErrorCode.LIMIT)
        async with self.lock:
            await self._connect()
            if not await self.validate_input(grant) or self.grant != grant:
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            if self.partial or type(sequence) is not int or sequence != self.admitted + 1:
                raise RelayError(ErrorCode.INPUT_CONFLICT)
            self.admitted = sequence
            try:
                self.writer.write(frame(b'd', data))
                await self.writer.drain()
            except (OSError, ConnectionError, asyncio.CancelledError):
                self.partial = True
                # Cancellation still propagates, while poisoning the stream against resend.
                if asyncio.current_task().cancelling():
                    raise
            return WriteResult(Watermark(grant.epoch, self.admitted, self.processed), self.partial)

    async def drain(self, grant: InputGrant, watermark: int):
        async with self.lock:
            if self.grant != grant or not await self.validate_input(grant):
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            if type(watermark) is not int or not 0 <= watermark <= self.admitted:
                raise RelayError(ErrorCode.INPUT_CONFLICT)
            if self.partial or watermark > self.processed:
                raise RelayError(ErrorCode.INPUT_CONFLICT)
            return Watermark(grant.epoch, self.admitted, self.processed)

    async def resize(self, grant: GeometryGrant, dimensions: Dimensions):
        self._run(grant.run)
        async with self.lock:
            await self._connect()
            if not await self.validate_geometry(grant):
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            if type(dimensions.rows) is not int or type(dimensions.cols) is not int:
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
            actual = Dimensions(max(20, min(dimensions.cols, 1000)), max(5, min(dimensions.rows, 500)))
            self.writer.write(frame(b'r', json.dumps({'cols': actual.cols, 'rows': actual.rows}).encode()))
            await self.writer.drain()
            return actual

    async def detach(self, run):
        self._run(run)
        async with self.lock:
            if self.output_task:
                self.output_task.cancel()
                await asyncio.gather(self.output_task, return_exceptions=True)
                self.output_task = None
            if self.writer:
                self.writer.close()
                await self.writer.wait_closed()
            self.reader = self.writer = None
            self.grant = None

    async def observe_exit(self, run):
        self._run(run)
        return self.exit


class ObserverTerminalHost(TmuxTerminalHost):
    """An owned read opener does not qualify input, geometry or native drain."""
    async def claim_input(self, grant):
        raise RelayError(ErrorCode.UNSUPPORTED)

    async def write_frames(self, grant, sequence, data):
        raise RelayError(ErrorCode.UNSUPPORTED)

    async def resize(self, grant, dimensions):
        raise RelayError(ErrorCode.UNSUPPORTED)
