"""Exact native API-key team exit facts survive admission loss, never placement change."""

import pytest
from test_session_relay_claude_service_native import run_native_service
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_in_thread, write_intent
from acc.server.models import Session, Team
from acc.server.session_relay import delivery, stop_proof
from acc.server.session_relay.hub import seal_for_stop

SID = 'abc123def456'


pytestmark = pytest.mark.relay_native


async def test_native_api_key_team_stop_is_conclusive_after_team_admission_is_withdrawn(runtime_case, monkeypatch, record_property):  # noqa: F811
    c = runtime_case
    original_close = c.manager.close
    observations = []
    async def close():
        if SID in c.manager.runs and not observations:
            observations.append('started')
            @write_intent
            def stop_intent():
                seal_for_stop(c.ctx, SID)
                with c.db() as db:
                    session = db.get(Session, SID)
                    assert session.claude_auth == 'api_key' and session.team_id
                    session.desired_state = 'stopped'
                    db.get(Team, session.team_id).stopped = True
                    db.commit()
            await write_in_thread(stop_intent)
            monkeypatch.setattr(delivery, 'DELIVERY_QUALIFIED', False)
            monkeypatch.setattr(delivery, 'CLAUDE_SERVICE_QUALIFIED', False)
            assert await c.manager.stop(SID) is True
            assert SID not in c.manager.runs
            assert await c.manager.stop(SID) is True
            assert await write_in_thread(lambda: stop_proof.verified(c.manager, SID))
            @write_intent
            def change_binding():
                with c.db() as db:
                    team = db.get(Team, db.get(Session, SID).team_id)
                    team.limits = {**team.limits, 'total': team.limits['total'] + 1}
                    db.commit()
            await write_in_thread(change_binding)
            assert await c.manager.stop(SID) is False
            observations.append('exact-native-exit-reused-only-before-binding-change')
        await original_close()
    monkeypatch.setattr(c.manager, 'close', close)
    await run_native_service(c, monkeypatch, record_property, team=True)
    assert observations == ['started', 'exact-native-exit-reused-only-before-binding-change']
    record_property('integration_team_stop', 'Actual pinned API-key native SDK team; exact Stop persists after team stopped and qualification off; changed immutable placement binding refuses reuse; no launch/input/replay.')
