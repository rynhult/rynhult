# ruff: noqa: F811
"""A delivered input frame cannot acknowledge authority lost during Host await.

Composed public/main/broker path and synthetic Host; no native CLI attestation.
"""
import asyncio
import time
from dataclasses import replace

import pytest
from test_session_relay_public_writer import SID, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_terminal_grants import claim, frame, stream_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import Observation, RelayOwnership, RelaySession, SecurityRevision, Session, Setting


@pytest.mark.parametrize('change', ['caller', 'policy', 'persisted-budget', 'held', 'stop', 'assignment',
                                    'revision', 'flag', 'capability', 'deadline', 'owner'])
async def test_input_delivered_before_authority_loss_never_acknowledges_or_replays(stream_case, change):
    c = stream_case
    grant = (await claim(c))['result']
    entered, release = asyncio.Event(), asyncio.Event()
    c.host.block = entered, release
    pending = asyncio.create_task(call(c.client, 'terminal.input', frame(c, grant)))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert len(c.host.writes) == 1 and not pending.done()
        if change == 'caller':
            c.state['revoked'] = True
        elif change == 'flag':
            c.ctx.settings = replace(c.ctx.settings, feature_overrides={'session_relay': False})
        elif change == 'deadline':
            c.launch.terminal_grants.record.expires = time.monotonic() - 1
        else:
            @write_intent
            def revoke():
                with c.db() as db:
                    row = db.get(RelaySession, SID)
                    if change == 'policy':
                        db.get(SecurityRevision, 1).revision += 1
                    elif change == 'persisted-budget':
                        db.add(Setting(key='budget.alex', value={'rules': {
                            'codex.primary': {'mode': 'cap', 'limit': 80, 'action': 'pause'}}}))
                        db.add(Observation(key='codex.rate_limits', source='fixture', observed_at=time.time(),
                            value={'primary': {'used_percent': 99, 'resets_at': time.time() + 3600, 'window_minutes': 300}}))
                    elif change == 'held':
                        db.get(Session, SID).desired_state = 'archived'
                        db.add(Setting(key='backup.replacement', value={'sessions': [SID]}))
                    elif change == 'stop':
                        db.get(Session, SID).desired_state = 'stopped'
                    elif change == 'assignment':
                        db.get(Session, SID).automation = {'mode': 'fixture-assignment'}
                    elif change == 'revision':
                        row.control_revision += 1
                    elif change == 'capability':
                        row.capabilities = {**row.capabilities, 'terminal.input': 'unsupported'}
                    elif change == 'owner':
                        db.get(RelayOwnership, SID).view = 'chat'
                    db.commit()
            revoke()
        release.set()
        result = await asyncio.wait_for(pending, 10)
        assert 'error' in result and 'watermark' not in result, result
        assert c.launch.terminal_grants.record is None
        with c.db() as db:
            assert db.get(RelaySession, SID).sealed
        if change == 'caller':
            replay = await c.client.post(f'/api/sessions/{SID}/relay', json={
                'jsonrpc': '2.0', 'id': 'revoked-replay', 'method': 'ry/terminal.input',
                'params': {'sessionId': SID, **frame(c, grant)}})
            assert replay.status_code == 401 and 'watermark' not in replay.text
        else:
            replay = await call(c.client, 'terminal.input', frame(c, grant))
            assert 'error' in replay and 'watermark' not in replay
        assert len(c.host.writes) == 1 and len(c.host.claims) == 1 and not c.launch.grants
        assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
