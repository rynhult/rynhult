# ruff: noqa: F811
"""Supervised end cannot overtake buffered normalized message publication."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_session_relay_native_write import Writer
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.models import RelayEvent, RelaySession, SessionRun
from acc.session_relay.adapters.codex import StdioRPC

SID = 'abc123def456'


@pytest.mark.parametrize('release_message', [True, False])
async def test_exit_drains_received_messages_or_marks_incomplete(runtime_case, release_message):
    c = runtime_case
    await c.launch.launch(SID)
    state = next(iter(c.factory.active.values()))
    # This artificial owner has no stdio reader. Supply an explicit completed
    # reader-EOF fact; the message and publication path remain production code.
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    c.owners[0].rpc = SimpleNamespace(task=task, eof=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.peer.call
    async def blocked(method, params):
        if method == 'publication' and params.get('kind') == 'message':
            entered.set()
            await release.wait()
        return await original(method, params)
    state.peer.call = blocked
    await state.events.put({'method': 'item/completed', 'params': {'threadId': 'fixture-native-id',
        'item': {'id': 'last-message', 'type': 'agentMessage', 'text': 'Last complete artificial block'}}})
    await asyncio.wait_for(entered.wait(), 3)
    c.owners[0].exit.set()
    try:
        if release_message:
            for _ in range(100):
                if state.final_exit_task is not None:
                    break
                await asyncio.sleep(.01)
            await asyncio.sleep(.05)
            with c.db() as db:
                assert not db.get(RelaySession, SID).sealed
            release.set()
        for _ in range(600):
            with c.db() as db:
                ended = db.get(SessionRun, c.launch.run_id).ended_at is not None
            if ended:
                break
            await asyncio.sleep(.01)
        assert ended
        with c.db() as db:
            events = [r.event for r in db.scalars(select(RelayEvent).where(RelayEvent.session_id == SID)
                .order_by(RelayEvent.seq))]
            final = next(event for event in events if 'exitOutcome' in event['payload'])
            assert final['payload']['exitOutcome'] == 'normal'
            assert final['payload']['finalEventsDrained'] is release_message
            messages = [event for event in events if event['kind'] == 'message.upsert']
            if release_message:
                assert len(messages) == 1 and messages[0]['eventSeq'] < final['eventSeq']
                assert messages[0]['payload']['text'] == 'Last complete artificial block'
            else:
                assert not messages
    finally:
        release.set()


@pytest.mark.parametrize('data,eof', [(b'', True), (b'not-json\n', False), (b'[]\n', False)])
async def test_reader_eof_is_not_fabricated_after_invalid_native_data(data, eof):
    reader = asyncio.StreamReader()
    async def observe(event):
        pass
    rpc = StdioRPC(reader, Writer(), observe)
    reader.feed_data(data)
    reader.feed_eof()
    await rpc.task
    assert rpc.eof is eof
    await rpc.close()
