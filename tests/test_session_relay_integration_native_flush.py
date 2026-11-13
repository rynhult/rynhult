"""Exact late native facts never revive input after authority or integrity loss."""
import asyncio
import json

import pytest
from test_session_relay_claude_input_host import enter, native_host, transcript  # noqa: F401
from test_session_relay_claude_terminal_grants import claude_stream  # noqa: F401
from test_session_relay_ownership import SID, ownership_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.models import RelayOperation
from acc.session_relay import protocol as p


@pytest.mark.parametrize('change', ['none', 'revoke', 'replace', 'wrong-prompt'])
async def test_native_stop_waits_for_exact_records_and_keeps_final_fences(native_host, monkeypatch, change):  # noqa: F811
    c = native_host
    await enter(c)
    operation = c.native_controller.pending['operation']
    loop = asyncio.get_running_loop()
    missing = asyncio.Event()
    original = c.native_host.admission.evidence

    def observe(operation):
        try:
            return original(operation)
        except p.RelayError:
            loop.call_soon_threadsafe(missing.set)
            raise

    monkeypatch.setattr(c.native_host.admission, 'evidence', observe)
    stopped = asyncio.create_task(c.broker_state.observe(
        {'session_id': 'fixture-conversation', 'hook_event_name': 'Stop'}))
    try:
        await asyncio.wait_for(missing.wait(), 2)
        assert not stopped.done()
        assert c.native_host.processed == 0 and not c.native_host.boundary.verified_empty
        with c.db() as db:
            assert db.get(RelayOperation, operation.operation_id).receipt['state'] == 'submitting'
        if change == 'revoke':
            c.state['revoked'] = True
        elif change == 'replace':
            c.native_path.unlink()
        transcript(c)
        if change == 'wrong-prompt':
            rows = [json.loads(line) for line in c.native_path.read_text().splitlines()]
            rows[0]['message']['content'] = 'A different native prompt'
            c.native_path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        if change == 'none':
            await stopped
            assert c.native_host.boundary.verified_empty and c.native_host.processed == 1
            assert c.native_controller.pending is None
        else:
            with pytest.raises(p.RelayError):
                await stopped
            assert c.native_host.processed == 0 and c.native_host.boundary.partial
        with c.db() as db:
            assert db.get(RelayOperation, operation.operation_id).receipt['state'] == (
                'accepted' if change in ('none', 'revoke') else 'submitting')
        assert len(c.native_writes) == 1 and c.native_host.attested is False
    finally:
        if not stopped.done():
            stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)
