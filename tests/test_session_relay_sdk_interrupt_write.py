"""Real nonblocking pipe: no buffered/late interrupt beyond final authority."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from acc.session_relay import claude_sdk_interrupt as guarded
from acc.session_relay.protocol import ErrorCode, RelayError

DATA = json.dumps({'type': 'control_request', 'request_id': 'fixture-interrupt', 'request': {'subtype': 'interrupt'}}) + '\n'


@pytest.fixture
def pipe_case():
    read, write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    transport = SimpleNamespace(get_write_buffer_size=lambda: 0,
        get_extra_info=lambda name: SimpleNamespace(fileno=lambda: write))
    stream = SimpleNamespace(is_closing=lambda: False, transport=transport)
    base = SimpleNamespace(_write_lock=asyncio.Lock(), _ready=True, _exit_error=None,
        _process=SimpleNamespace(returncode=None),
        _stdin_stream=SimpleNamespace(transport_stream=SimpleNamespace(_stream=stream)))
    yield SimpleNamespace(base=base, closed=False), read, write, transport
    os.close(read)
    os.close(write)


async def test_current_guard_writes_exact_bytes_once(pipe_case):
    wire, read, _, _ = pipe_case
    calls = []
    async def authorize():
        calls.append(True)
    with guarded.scoped(wire, authorize):
        await guarded.write(wire, DATA)
    assert os.read(read, 4096) == DATA.encode() and calls == [True]
    with pytest.raises(RelayError):
        await guarded.write(wire, DATA)
    with pytest.raises(BlockingIOError):
        os.read(read, 1)


async def test_revoke_while_waiting_for_sdk_lock_never_writes(pipe_case):
    wire, read, _, _ = pipe_case
    allowed = True
    async def authorize():
        if not allowed:
            raise RelayError(ErrorCode.STALE_AUTHORITY)
    async def issue():
        with guarded.scoped(wire, authorize):
            await guarded.write(wire, DATA)
    await wire.base._write_lock.acquire()
    task = asyncio.create_task(issue())
    await asyncio.sleep(0)
    allowed = False
    wire.base._write_lock.release()
    with pytest.raises(RelayError):
        await task
    with pytest.raises(BlockingIOError):
        os.read(read, 1)


@pytest.mark.parametrize('change', ['buffer', 'closed', 'exit', 'full'])
async def test_no_deferred_write_after_pipe_or_owner_refusal(pipe_case, change):
    wire, read, write, transport = pipe_case
    filled = 0
    if change == 'full':
        while True:
            try:
                filled += os.write(write, b'x' * 4096)
            except BlockingIOError:
                break
    async def authorize():
        if change == 'buffer':
            transport.get_write_buffer_size = lambda: 1
        elif change == 'closed':
            wire.closed = True
        elif change == 'exit':
            wire.base._process.returncode = -9
    with guarded.scoped(wire, authorize), pytest.raises((RelayError, BlockingIOError)):
        await guarded.write(wire, DATA)
    received = b''
    while True:
        try:
            received += os.read(read, 8192)
        except BlockingIOError:
            break
    assert received == b'x' * filled
    await asyncio.sleep(.01)
    with pytest.raises(BlockingIOError):
        os.read(read, 1)
