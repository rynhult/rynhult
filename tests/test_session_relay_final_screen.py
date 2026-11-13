"""Retained final screens remain bounded, freshly authorized and read-only."""
from types import SimpleNamespace

import httpx
import pytest
from test_session_relay_public_writer import SID, attach, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayRun, RelaySession, SessionRun, utcnow
from acc.server.session_relay import final_screen, store
from acc.session_relay import protocol as p


@pytest.fixture
async def ended_case(public_case):  # noqa: F811
    c = public_case
    c.launch.publication({'kind': 'exited', 'returnCode': 0,
        'finalScreen': {'text': 'Final secret screen', 'truncated': False}})
    await c.launch.peer.close()
    await c.manager.close()
    assert SID not in c.manager.runs
    return c


async def read(client, c, view, run=None):
    return await client.get(f'/api/sessions/{SID}/relay/terminal', params={
        'attachmentId': view['attachmentId'], 'expectedRunId': run or str(c.launch.run_id)})


async def test_ended_screen_uses_no_live_broker_grant_or_launch_and_reloads_redaction(ended_case):
    c = ended_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'ended-screen', [])
        first = await read(client, c, view)
        assert first.status_code == 200 and first.json()['text'] == 'Final secret screen'
        c.ctx.supervisor.vault.redactor = lambda *_: SimpleNamespace(text=lambda text: text.replace('secret', '[REDACTED]'))
        second = await read(client, c, view)
        assert second.status_code == 200 and second.json()['text'] == 'Final [REDACTED] screen'
    assert len(c.owners) == 1 and not c.launch.grants
    assert c.launch.terminal_grants.record is None


@pytest.mark.parametrize('change', ['revoke', 'redaction', 'epoch'])
async def test_retained_screen_rechecks_after_await(ended_case, monkeypatch, change):
    c = ended_case
    original = final_screen.read
    calls = 0
    def changed(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 1:
            if change == 'revoke':
                c.state['revoked'] = True
            elif change == 'redaction':
                c.ctx.supervisor.vault.redactor = lambda *_: SimpleNamespace(text=lambda text: '[MASKED]')
            else:
                @write_intent
                def reset():
                    with c.db() as db:
                        db.get(RelaySession, SID).history_epoch += 1
                        db.commit()
                reset()
        return result
    monkeypatch.setattr(final_screen, 'read', changed)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'awaited-ended-screen', [])
        response = await read(client, c, view)
        if change == 'redaction':
            assert response.status_code == 200 and response.json()['text'] == '[MASKED]'
        else:
            assert response.status_code in (401, 404, 409)
            assert 'Final secret screen' not in response.text


async def test_replacement_observer_can_explicitly_select_old_screen_but_old_connection_cannot(ended_case):
    c = ended_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as old, httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as fresh:
        old_view = await attach(old, 'old-reader', [])
        @write_intent
        def replace_run():
            with c.db() as db:
                db.add(SessionRun(id=900, session_id=SID, reason='manual_resume', verified_at=utcnow()))
                db.flush()
                db.add(RelayRun(run_id=900, session_id=SID, binding_id='replacement', identity={}, frozen_policy={}, policy_revision=0))
                db.flush()
                row = db.get(RelaySession, SID)
                row.current_run_id, row.binding_id, row.sealed = 900, 'replacement', False
                db.commit()
        replace_run()
        denied = await read(old, c, old_view)
        assert denied.status_code == 404 and 'Final secret' not in denied.text
        view = await attach(fresh, 'replacement-reader', [])
        retained = await read(fresh, c, view)
        assert retained.status_code == 200 and retained.json()['runId'] == str(c.launch.run_id)
        current = await read(fresh, c, view, run='900')
        assert current.status_code == 409 and 'Final secret' not in current.text
        unknown = await read(fresh, c, view, run='901')
        assert unknown.status_code == 404 and 'Final secret' not in unknown.text
    assert len(c.owners) == 1 and not c.launch.grants


async def test_unavailable_final_screen_never_falls_back_to_an_older_capture(ended_case):
    c = ended_case
    @write_intent
    def unavailable():
        with c.db() as db:
            store.append(db, store.lock(db, SID), 'session.stateChanged', {'finalScreen': None}, priority=True)
            db.commit()
    unavailable()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'unavailable-final', [])
        response = await read(client, c, view)
        assert response.status_code == 409 and 'Final secret' not in response.text


@pytest.mark.parametrize('text,mask', [('\x00' * (120 << 10), lambda text: text), ('x' * 20000, lambda text: text * 10)], ids=['json-escaping', 'redaction-expansion'])
async def test_encoding_or_redaction_expansion_cannot_lose_exit_record(public_case, text, mask):  # noqa: F811
    c = public_case
    c.ctx.supervisor.vault.redactor = lambda *_: SimpleNamespace(text=mask)
    c.launch.publication({'kind': 'exited', 'returnCode': 0, 'finalScreen': {'text': text, 'truncated': False}})
    with c.db() as db:
        assert db.get(RelaySession, SID).sealed
        assert db.get(SessionRun, c.launch.run_id).exit_status == 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'expanded-final', [])
        response = await read(client, c, view)
        assert response.status_code == 409


def test_malformed_capture_is_not_a_final_screen():
    for value in ({'text': 'x', 'truncated': 0}, {'text': 'x', 'truncated': False, 'extra': True}):
        with pytest.raises(p.RelayError):
            final_screen.sanitize(value, lambda text: text)
