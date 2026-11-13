# ruff: noqa: F811
"""Actual reviewed SDK/TUI owner transitions with artificial CLI, not native qualification."""
import httpx
from fastapi import FastAPI, Request
from test_session_relay_ownership import ownership_case, select_owner  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.api.session_relay import router
from acc.server.models import RelaySession, SessionRun

SID = 'abc123def456'


async def test_reviewed_replacements_retain_run_events_and_reset_cross_run_cursor(ownership_case):
    c = ownership_case
    original = c.launch.run_id

    def principal(request: Request):
        request.state.cookie_authenticated = True
        request.state.session_hash = 'fixture-login'
        return c.resolve()[0]

    c.ctx.principal = principal
    c.ctx.relay_principal = lambda _db, request: principal(request)
    c.ctx.relay_runtime = c.manager
    app = FastAPI()
    app.include_router(router(c.ctx))
    first = c.owned_factory.current
    await select_owner(c, 'terminal', 'retained-terminal')
    terminal_run = c.launch.run_id
    terminal = c.owned_factory.current
    await select_owner(c, 'chat', 'retained-chat')
    current = c.launch.run_id
    assert len({original, terminal_run, current}) == 3
    assert first.process.returncode is not None and terminal.process.returncode is not None
    count = len(c.owned_factory.owners)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
        response = await client.get(f'/api/sessions/{SID}/relay/runs')
        assert response.status_code == 200, response.text
        runs = response.json()
        assert runs['currentRunId'] == str(current)
        assert {run['runId'] for run in runs['runs']} == {str(original), str(terminal_run)}
        assert all(run['exitOutcome'] == 'unknown' for run in runs['runs'])
        assert 'fixture-conversation' not in response.text
        for identifier in (original, terminal_run):
            params = {'runId': str(identifier), 'limit': 1}
            response = await client.get(f'/api/sessions/{SID}/relay/events', params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            first_cursor = page['cursor']
            events = list(page['events'])
            for _ in range(64):
                if not page['more']:
                    break
                response = await client.get(f'/api/sessions/{SID}/relay/events', params={**params, 'cursor': page['cursor']})
                assert response.status_code == 200, response.text
                page = response.json()
                events.extend(page['events'])
            assert not page['more'] and events
            assert {event['runId'] for event in events} == {str(identifier)}
            assert any(event['kind'] == 'switch.changed' for event in events)
            for selection in ({'runId': str(current)}, {}, {'runId': str(terminal_run if identifier == original else original)}):
                refused = await client.get(f'/api/sessions/{SID}/relay/events', params={**selection, 'cursor': first_cursor})
                assert refused.status_code == 409 and refused.json()['error']['data']['code'] == 'CURSOR_RESET'
        c.state['revoked'] = True
        for suffix in ('runs', 'events'):
            refused = await client.get(f'/api/sessions/{SID}/relay/{suffix}', params={'runId': str(original)})
            assert refused.status_code == 401 and 'fixture-conversation' not in refused.text
    with c.db() as db:
        assert db.get(SessionRun, original).ended_at and db.get(SessionRun, terminal_run).ended_at
        assert db.get(SessionRun, current).ended_at is None
        assert db.get(RelaySession, SID).current_run_id == current
    assert len(c.owned_factory.owners) == count == 3
    assert not c.launch.grants and c.launch.terminal_grants.record is None


async def test_retired_screen_is_redacted_at_checkpoint_and_again_at_read(ownership_case, monkeypatch):
    from types import SimpleNamespace

    from acc.server.session_relay import final_screen

    c = ownership_case
    await select_owner(c, 'terminal', 'screen-terminal')
    retired_run = c.launch.run_id
    process = c.owned_factory.current.process
    process.snapshot = 'Retired secret native view'
    # Artificial process type, explicitly not installed-TUI qualification.
    monkeypatch.setattr('acc.broker.claude_owner.TmuxProcess', type(process))
    c.ctx.supervisor.vault.redactor = lambda *_a: SimpleNamespace(text=lambda text: text.replace('secret', '[REDACTED]'))
    await select_owner(c, 'chat', 'screen-chat')
    _, screen = final_screen.read(c.ctx, c.resolve, SID, str(retired_run))
    assert screen == {'runId': str(retired_run), 'text': 'Retired [REDACTED] native view', 'truncated': True}
    c.ctx.supervisor.vault.redactor = lambda *_a: SimpleNamespace(text=lambda text: text.replace('native', '[CURRENT MASK]'))
    _, screen = final_screen.read(c.ctx, c.resolve, SID, str(retired_run))
    assert screen['text'] == 'Retired [REDACTED] [CURRENT MASK] view'
    assert len(c.owned_factory.owners) == 3


async def test_sdk_exit_cannot_supply_a_retained_terminal_screen(ownership_case):
    import pytest
    from sqlalchemy import select

    from acc.server.models import RelayEvent

    c = ownership_case
    original = c.launch.peer.handle

    async def checkpoint(method, params):
        if method == 'authority.switch_checkpoint' and params['phase'] == 'starting':
            params = {**params, 'evidence': {**params['evidence'],
                'retained_screen': {'text': 'Not a terminal view', 'truncated': False}}}
        return await original(method, params)

    c.launch.peer.handle = checkpoint
    with pytest.raises(ConnectionError, match='Runtime authority refused'):
        await select_owner(c, 'terminal', 'invalid-sdk-screen')
    with c.db() as db:
        assert not any('finalScreen' in event.event.get('payload', {}) for event in db.scalars(select(RelayEvent)))
    assert len(c.owned_factory.owners) == 1
