"""Known background work outlives a ResultMessage; EOF is not task completion."""
import asyncio
from dataclasses import make_dataclass
from types import SimpleNamespace

import pytest
from test_session_relay_claude_agent import operation, spec

from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter
from acc.session_relay.protocol import ErrorCode, RelayError

ResultMessage = make_dataclass('ResultMessage', [('subtype', str), ('session_id', str)])


@pytest.mark.parametrize('end', ['notification', 'eof'])
async def test_result_settles_once_but_background_drain_still_fences_idle(tmp_path, end):
    queue = asyncio.Queue()
    events = []
    result_seen, notification_seen, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Client:
        async def receive_messages(self):
            while (message := await queue.get()) is not None:
                yield message
    async def publish(event):
        events.append(event)
        if event['kind'] == 'turn.completed':
            result_seen.set()
        if event.get('message', {}).get('subtype') == 'task_notification':
            notification_seen.set()
            await release.wait()
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=publish)
    adapter.spec, adapter.client = spec(tmp_path), Client()
    adapter.attestation = SimpleNamespace(process_identity='fixture-process', native_conversation='fixture-conversation')
    adapter.active = True
    adapter.reader = asyncio.create_task(adapter._read(operation()))
    try:
        await queue.put({'subtype': 'task_started', 'task_id': 'bg', 'session_id': 'fixture-conversation'})
        await queue.put(ResultMessage('success', 'fixture-conversation'))
        await asyncio.wait_for(result_seen.wait(), 3)
        assert not adapter.active and adapter.background == {'bg'}
        assert not (await adapter.prepare_switch()).idle
        assert not await adapter.idle_stop()
        if end == 'notification':
            await queue.put({'subtype': 'task_notification', 'task_id': 'bg', 'session_id': 'fixture-conversation'})
            await asyncio.wait_for(notification_seen.wait(), 3)
            assert not adapter.background and adapter.draining_background()
            assert not (await adapter.prepare_switch()).idle
            with pytest.raises(RelayError) as refused:
                await adapter.submit(operation())
            assert refused.value.code is ErrorCode.BUSY
            release.set()
        else:
            await queue.put(None)
        await asyncio.wait_for(adapter.reader, 3)
        assert adapter.failure is (end == 'eof')
        assert (await adapter.prepare_switch()).idle is (end == 'notification')
        assert [e['kind'] for e in events if e['kind'] != 'provider_message'] == ['operation.accepted', 'turn.completed']
    finally:
        release.set()
        adapter.reader.cancel()
        await asyncio.gather(adapter.reader, return_exceptions=True)
