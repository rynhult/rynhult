"""Pinned Linux SDK interrupt write: final authority after the SDK pipe lock.

One bounded control frame fits PIPE_BUF. No asyncio transport buffering, partial
frame retry or control-response inference is permitted on this path.
"""
import fcntl
import os
import stat
from contextlib import contextmanager
from contextvars import ContextVar

from .protocol import ErrorCode, RelayError

_scope = ContextVar('relay_sdk_interrupt', default=None)


@contextmanager
def scoped(wire, authorize):
    scope = {'wire': wire, 'authorize': authorize, 'record': None}
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


async def write(wire, data):
    value = _scope.get()
    if value is None or value['wire'] is not wire:
        raise RelayError(ErrorCode.POLICY_REFUSED)
    base = wire.base
    async with base._write_lock:
        await value['authorize']()
        if (wire.closed or not base._ready or base._exit_error or not base._stdin_stream
                or not base._process or base._process.returncode is not None):
            raise RelayError(ErrorCode.OFFLINE)
        # Pinned anyio asyncio subprocess pipe, not a generic byte transport.
        stream = base._stdin_stream.transport_stream._stream
        if stream.is_closing() or stream.transport.get_write_buffer_size():
            raise RelayError(ErrorCode.BUSY)
        pipe = stream.transport.get_extra_info('pipe')
        fd = os.dup(pipe.fileno())
        try:
            raw = data.encode('utf-8')
            if (not stat.S_ISFIFO(os.fstat(fd).st_mode)
                    or not fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK
                    or not 0 < len(raw) <= os.fpathconf(fd, 'PC_PIPE_BUF')):
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
            # No await from the final callback/target check to this atomic write.
            # EAGAIN writes zero bytes; refuse, never buffer or retry it later.
            if os.write(fd, raw) != len(raw):
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        finally:
            os.close(fd)
