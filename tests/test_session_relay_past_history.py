"""Authorized retained run selection cannot expose or mutate another run."""
import httpx
import pytest
from test_session_relay_public_writer import attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayRun, RelaySession, Session, SessionRun, utcnow
from acc.server.session_relay import history, store

SID = 'abc123def456'


@pytest.fixture
async def past_case(public_case):  # noqa: F811
    c = public_case
    c.old_run = c.launch.run_id

    @write_intent
    def replace_run():
        with c.db() as db:
            row = db.get(RelaySession, SID)
            for index in range(2):
                history.message(db, row, item_id=f'past-{index}', role='assistant',
                    complete_text=f'Past secret {index}', source_epoch=c.old_run, source_revision=0, mask=lambda text: text)
            db.get(SessionRun, c.old_run).ended_at = utcnow()
            db.add(SessionRun(id=900, session_id=SID, reason='manual_resume', verified_at=utcnow()))
            db.flush()
            db.add(RelayRun(run_id=900, session_id=SID, binding_id='replacement-binding', identity={},
                frozen_policy={}, policy_revision=0))
            db.flush()
            row.current_run_id, row.binding_id = 900, 'replacement-binding'
            history.message(db, row, item_id='new-message', role='assistant', complete_text='Replacement only',
                source_epoch=900, source_revision=0, mask=lambda text: text)
            original = db.get(Session, SID)
            db.add(Session(id='other', provider='codex', name='Other', tmux_name='fixture-other',
                launch_spec_id=original.launch_spec_id, owner='alex', run_as=original.run_as))
            db.flush()
            db.add(SessionRun(id=901, session_id='other', reason='launch', verified_at=utcnow()))
            db.flush()
            store.enroll(db, 'other', 901, 'other-binding', identity={}, frozen_policy={}, policy_revision=0)
            db.commit()
    replace_run()
    return c


async def test_selected_past_run_is_bounded_and_does_not_include_replacement(past_case):
    c = past_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        params = {'runId': c.old_run, 'limit': 1}
        first = await client.get(f'/api/sessions/{SID}/relay/events', params=params)
        assert first.status_code == 200, first.text
        page = first.json()
        assert len(page['events']) == 1 and page['more'] is True
        assert page['events'][0]['runId'] == str(c.old_run)
        events = list(page['events'])
        for _ in range(10):
            response = await client.get(f'/api/sessions/{SID}/relay/events', params={**params, 'cursor': page['cursor']})
            assert response.status_code == 200, response.text
            page = response.json()
            assert len(page['events']) <= 1
            events.extend(page['events'])
            if not page['more']:
                break
        assert page['more'] is False
        assert {event['runId'] for event in events} == {str(c.old_run)}
        assert len([event for event in events if event['kind'] == 'message.upsert']) == 2
        assert 'Replacement only' not in str(events)
        from acc.session_relay import protocol as p
        c.qualify_fixture()
        await attach(client, 'past-run-mutation-check')
        refused = await call(client, 'operation.register', {'expectedRunId': str(c.old_run),
            'registrationKey': 'past-run-mutation', 'method': 'turn.submit',
            'payloadDigest': p.sha256_digest(b'fixture')})
        assert refused['error']['data']['code'] == 'STALE_RUN'
        with c.db() as db:
            assert db.get(RelaySession, SID).current_run_id == 900
        assert not c.launch.grants and c.launch.terminal_grants.record is None
        assert len(c.owners) == 1
        assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 0


async def test_run_cursor_cannot_be_reused_for_current_all_or_foreign_history(past_case):
    c = past_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        page = (await client.get(f'/api/sessions/{SID}/relay/events', params={'runId': c.old_run, 'limit': 1})).json()
        for params in ({'runId': 900}, {}):
            response = await client.get(f'/api/sessions/{SID}/relay/events', params={**params, 'cursor': page['cursor']})
            assert response.status_code == 409
            assert response.json()['error']['data']['code'] == 'CURSOR_RESET'
        for run in (901, 902):
            response = await client.get(f'/api/sessions/{SID}/relay/events', params={'runId': run})
            assert response.status_code == 404
            assert 'Past secret' not in response.text and 'Replacement only' not in response.text


async def test_past_history_replay_reloads_redaction_and_current_login(past_case):
    from types import SimpleNamespace

    c = past_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        page = (await client.get(f'/api/sessions/{SID}/relay/events', params={'runId': c.old_run, 'limit': 1})).json()
        c.ctx.supervisor.vault.redactor = lambda *_: SimpleNamespace(text=lambda text: text.replace('secret', '[REDACTED]'))
        response = await client.get(f'/api/sessions/{SID}/relay/events', params={
            'runId': c.old_run, 'cursor': page['cursor']})
        assert response.status_code == 200 and '[REDACTED]' in response.text and 'secret' not in response.text
        c.state['revoked'] = True
        response = await client.get(f'/api/sessions/{SID}/relay/events', params={'runId': c.old_run})
        assert response.status_code == 401 and 'Past secret' not in response.text
