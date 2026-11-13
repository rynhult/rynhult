"""A duplex framed stream has exactly one output reader; never processing proof."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from acc.broker.attach import frame
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import TerminalRun
from acc.session_relay.native_input_host import NativeInputHost


@pytest.mark.parametrize('owned_read', [False, True])
async def test_native_host_output_reader_ownership_is_explicit(owned_read):
    run = TerminalRun('fixture-session', 'fixture-run')
    reader = asyncio.StreamReader()
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    opener = AsyncMock(return_value=(reader, writer))
    allow = AsyncMock(return_value=True)
    options = {'consume_output': False} if owned_read else {}  # default production input-only drainer
    host = NativeInputHost(run, opener, allow, allow, validate_read=allow,
        routes=SimpleNamespace(), idle=allow, processed=allow, **options)
    try:
        await host._connect()
        drainer = host.output_task
        if owned_read:
            assert drainer is None
            reader.feed_data(frame(b'd', b'one complete native screen block'))
            reader.feed_data(frame(b'x', b''))
            assert [data async for data in host.attach_read(run)] == [b'one complete native screen block']
        else:
            assert drainer is not None and not drainer.done()
            with pytest.raises(p.RelayError) as refused:
                await anext(host.attach_read(run))
            assert refused.value.code is p.ErrorCode.UNSUPPORTED
            assert host.output_task is drainer and host.reader is reader
            reader.feed_data(frame(b'd', b'drained duplicate screen bytes'))
            reader.feed_data(frame(b'x', b''))
            await asyncio.wait_for(drainer, 1)
        assert opener.await_count == 1
        assert host.grant is None and host.admitted == host.processed == 0
        assert host.ticket is None and host.attested is False
    finally:
        await host.detach(run)
    writer.close.assert_called_once()
