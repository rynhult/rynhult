# ruff: noqa: F811
"""Bounded current-authorized run choices reveal no native execution identity."""
import httpx
from test_session_relay_past_history import past_case  # noqa: F401
from test_session_relay_public_writer import public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayRun, SessionRun, utcnow

SID = 'abc123def456'


async def test_retained_list_excludes_live_current_and_foreign_runs(past_case):
    c = past_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        response = await client.get(f'/api/sessions/{SID}/relay/runs')
        assert response.status_code == 200, response.text
        value = response.json()
        assert value['currentRunId'] == '900' and value['more'] is False
        assert [run['runId'] for run in value['runs']] == [str(c.old_run)]
        assert value['runs'][0]['exitOutcome'] == 'unknown'
        assert 'fixture-native-id' not in response.text and 'binding' not in response.text
        c.state['revoked'] = True
        denied = await client.get(f'/api/sessions/{SID}/relay/runs')
        assert denied.status_code == 401 and 'endedAt' not in denied.text
    assert len(c.owners) == 1 and not c.launch.grants


async def test_run_choices_are_bounded_and_read_never_launches(past_case):
    c = past_case
    @write_intent
    def retain():
        with c.db() as db:
            for identifier in range(1000, 1060):
                db.add(SessionRun(id=identifier, session_id=SID, reason='manual_resume', verified_at=utcnow(),
                    ended_at=utcnow(), end_reason='native_exit_normal', exit_status=0))
                db.flush()
                db.add(RelayRun(run_id=identifier, session_id=SID, binding_id=f'old-{identifier}',
                    identity={}, frozen_policy={}, policy_revision=0))
            db.commit()
    retain()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        value = (await client.get(f'/api/sessions/{SID}/relay/runs')).json()
        assert len(value['runs']) == 50 and value['more'] is True
        assert value['runs'][0]['runId'] == '1059' and value['runs'][-1]['runId'] == '1010'
    assert len(c.owners) == 1 and c.launch.terminal_grants.record is None
