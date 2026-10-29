"""Pre-freeze careless-use regressions; temporary databases and fixture brokers only."""
from datetime import timedelta

import pytest
from helpers import write_db
from sqlalchemy import select
from test_orchestrator_gates import add, jobs, queue  # noqa: F401

from acc.server.models import AuditLog, Team, utcnow
from acc.server.orchestrator import gates


@pytest.fixture(autouse=True)
def preview(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')


@pytest.mark.parametrize('change', ['stopped', 'expired', 'removed'])
@pytest.mark.parametrize('boundary', ['prepare', 'rerun'])
async def test_team_revocation_at_broker_wait_does_not_launch_a_gate(queue, monkeypatch, change, boundary):  # noqa: F811
    api, project, machine, scheduler = queue
    team_id = 'b' * 32
    with write_db(api.app.state.db) as db:
        db.add(Team(id=team_id, limits={}, expires_at=utcnow() + timedelta(hours=1), stopped=False, started=0))
        db.commit()
    job = add(api, project, 'acc/team-revoked', team_id=team_id)
    if boundary == 'rerun':
        await scheduler.tick()
        machine.finished.add(job)
        machine.results[job] = {'exit': 1, 'counts': {'tests': 1, 'failed': 1},
                                'failed': ['tests/test_demo.py::test_failed'], 'rerunnable': True}
        machine.started.clear()
    call = machine.call
    cleaned = []

    async def interleaved(method, **params):
        result = await call(method, **params)
        if method == 'gate_cleanup':
            cleaned.append(params['job'])
        if method == ('gate_prepare' if boundary == 'prepare' else 'gate_status'):
            with write_db(api.app.state.db) as db:
                team = db.get(Team, team_id)
                if change == 'removed':
                    db.delete(team)
                elif change == 'expired':
                    team.expires_at = utcnow() - timedelta(seconds=1)
                else:
                    team.stopped = True
                db.commit()
        return result

    monkeypatch.setattr(api.app.state.supervisor.broker, 'call', interleaved)
    await scheduler.tick()
    assert machine.started == []  # a previous admission cannot authorize fresh code execution
    assert jobs(api)[job][0] == 'cancelled'
    assert cleaned == [job]
    with api.app.state.db() as db:
        row = db.get(gates.GateJob, job)
        assert 'team' in row.reason and ('stopped' in row.reason or 'expired' in row.reason)
        assert len(list(db.scalars(select(AuditLog).where(AuditLog.action == 'gate.start',
                                                        AuditLog.target == job)))) == (boundary == 'rerun')

