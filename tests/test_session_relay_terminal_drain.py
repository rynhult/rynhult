"""Read-only holder watermarks; buffered delivery never becomes processing proof."""
import asyncio
import time

import httpx
from test_session_relay_public_writer import SID, attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_terminal_grants import claim, frame, stream_case  # noqa: F401

from acc.server.models import RelaySession


def request(c, grant):
    return {'attachmentId': c.attachment, 'inputGrant': grant['inputGrant']}


async def test_drain_cannot_mint_input_and_admitted_prefix_is_not_processed(stream_case):  # noqa: F811
    c = stream_case
    assert 'error' in await call(c.client, 'terminal.drain', {'attachmentId': c.attachment, 'inputGrant': 'missing'})
    assert not c.host.claims
    grant = (await claim(c))['result']
    await call(c.client, 'terminal.input', frame(c, grant))
    for _ in range(2):
        result = await call(c.client, 'terminal.drain', request(c, grant))
        assert result['result'] == {'epoch': 2, 'admitted': 1, 'processed': 0}
    assert len(c.host.claims) == len(c.host.writes) == 1


async def test_pending_frame_cannot_be_reported_as_admitted(stream_case):  # noqa: F811
    c = stream_case
    grant = (await claim(c))['result']
    reached, release = asyncio.Event(), asyncio.Event()
    c.host.block = reached, release
    pending = asyncio.create_task(call(c.client, 'terminal.input', frame(c, grant)))
    await asyncio.wait_for(reached.wait(), 5)
    try:
        reply = await call(c.client, 'terminal.drain', request(c, grant))
        assert reply['error']['data']['code'] == 'BUSY'
    finally:
        release.set()
    assert 'result' in await pending
    assert (await call(c.client, 'terminal.drain', request(c, grant)))['result']['admitted'] == 1


async def test_other_attachment_and_expired_or_detached_grant_cannot_read_watermarks(stream_case):  # noqa: F811
    c = stream_case
    grant = (await claim(c))['result']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as other:
        view = await attach(other, 'other-drain-reader', ['terminal'])
        assert 'error' in await call(other, 'terminal.drain', {**request(c, grant), 'attachmentId': view['attachmentId']})
    c.launch.terminal_grants.record.expires = time.monotonic() - 1
    assert 'error' in await call(c.client, 'terminal.drain', request(c, grant))
    assert not c.host.writes and len(c.host.claims) == 1
    with c.db() as db:
        assert not db.get(RelaySession, SID).sealed  # denied read cannot fence execution
