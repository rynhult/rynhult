"""A single launch failure must remain isolated from independent multi-run choices."""
from helpers import wait_for
from sqlalchemy import select
from test_multirun import start
from test_supervisor import state_is

from acc.server.broker_client import BrokerError
from acc.server.models import Event


async def test_multirun_broker_launch_failure_does_not_stop_siblings(api, project, env, monkeypatch):
    supervisor = api.app.state.supervisor
    original = supervisor.broker.call

    async def call(method, **kwargs):
        if method == 'launch' and kwargs.get('provider') == 'codex':
            raise BrokerError('demo provider failed to start')
        return await original(method, **kwargs)

    monkeypatch.setattr(supervisor.broker, 'call', call)
    group = await start(api, project, env)
    claude, codex, copilot = [row['session'] for row in group['runs']]
    await wait_for(state_is(api, claude['id'], 'idle'))
    await wait_for(state_is(api, copilot['id'], 'idle'))

    # Reconciliation can replace last_error with recovery advice. The persisted
    # launch failure, rather than a transient message, proves the injected fault.
    with api.app.state.db() as db:
        errors = list(db.scalars(select(Event).where(Event.session_id == codex['id'], Event.kind == 'launch_failed')))
        assert any('demo provider failed to start' in row.payload['error'] for row in errors)
    failed_run = (await api.get(f"/api/sessions/{codex['id']}")).json()
    assert failed_run['last_error']
    assert not failed_run['runs']
    saved = (await api.get(f"/api/multiruns/{group['id']}")).json()
    assert len(saved['runs']) == 3
    assert saved['runs'][0]['session']['id'] == claude['id']
    assert saved['runs'][2]['session']['id'] == copilot['id']
