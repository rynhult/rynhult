"""Synthetic browser writes respect concurrent application snapshots."""
import asyncio
import threading

import httpx
from e2e_team_fixtures import install
from fastapi import FastAPI
from helpers import write_db
from sqlalchemy import func, select

from acc.server.db import _transaction_gate
from acc.server.models import LaunchSpec, Project, Session, Team
from acc.server.orchestrator.assignments import Assignment, Attempt
from acc.server.orchestrator.delivery import TaskReport


async def test_team_arrangements_require_capability_and_wait_for_readers(api, project, monkeypatch):
    factory = api.app.state.db
    with write_db(factory) as db:
        spec = LaunchSpec(provider='claude', inputs={}, files={}, digest='fixture')
        db.add(spec)
        db.flush()
        for name in ('fixture-lead', 'fixture-helper'):
            db.add(Session(id=name, project_id=project['id'], provider='claude', name=name,
                tmux_name=name, launch_spec_id=spec.id, run_as='agent', desired_state='stopped',
                team_id=name if name == 'fixture-lead' else None,
                parent_session_id=None if name == 'fixture-lead' else 'fixture-lead'))
        db.add(Team(id='fixture-lead', limits={}))
        db.commit()
    app = FastAPI()
    app.state.db = factory
    install(app, 'team-fixture-capability')
    url = '/e2e-test/teams/fixture-lead/fixtures/member/fixture-helper'
    headers = {'x-e2e-isolation': 'team-fixture-capability'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://example.lan') as client:
        assert (await client.post(url)).status_code == 403
        with factory() as db:
            assert db.get(Session, 'fixture-helper').team_id is None
        assert (await client.post(url, headers=headers)).status_code == 200
        assert (await client.post(url.replace('fixture-helper', 'missing'), headers=headers)).status_code == 404
        gate = _transaction_gate(factory.kw['bind'])
        enter = gate.enter
        attempted = threading.Event()

        def observe(worker, timeout):
            if worker:
                attempted.set()
            return enter(worker, timeout)

        monkeypatch.setattr(gate, 'enter', observe)
        with factory() as db:
            row = db.get(Project, project['id'])
            task = asyncio.create_task(client.post(url.replace('/member/', '/history/'), headers=headers))
            try:
                assert await asyncio.to_thread(attempted.wait, 2)
                assert not task.done()
                row.notes = 'Concurrent owner change survives fixture seeding'
                db.commit()
            finally:
                db.close()
                response = await asyncio.wait_for(task, 10)
        assert response.status_code == 200, response.text
        with factory() as db:
            helper = db.get(Session, 'fixture-helper')
            assert helper.team_id == helper.parent_session_id == 'fixture-lead'
            assert db.get(Project, project['id']).notes == 'Concurrent owner change survives fixture seeding'
            assert db.scalar(select(func.count()).select_from(Assignment)) == 49
            assert db.scalar(select(func.count()).select_from(Attempt)) == 490
            assert db.scalar(select(func.count()).select_from(TaskReport)) == 1470


async def test_helper_fixture_binds_parent_at_managed_creation(api, project, env):
    from test_team import lead

    from acc.server.child_caps import FILE

    root = await lead(api, project, env)
    install(api.app, 'create-helper-capability')
    path = f"/e2e-test/teams/{root['id']}/helpers"
    assert (await api.post(path)).status_code == 403
    headers = {'x-e2e-isolation': 'create-helper-capability'}
    response = await api.post(path, headers=headers)
    assert response.status_code == 200, response.text
    sid = response.json()['id']
    with api.app.state.db() as db:
        helper = db.get(Session, sid)
        assert helper.parent_session_id == root['id']
        assert helper.parent_chain == [root['id']]
        assert FILE in helper.launch_spec.files
    member = await api.post(f"/e2e-test/teams/{root['id']}/fixtures/member/{sid}", headers=headers)
    assert member.status_code == 200, member.text
    # A second root cannot be converted to a child, even with fixture authority.
    made = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Other root'})
    assert made.status_code == 201, made.text
    other = made.json()
    refused = await api.post(f"/e2e-test/teams/{root['id']}/fixtures/member/{other['id']}", headers=headers)
    assert refused.status_code == 409, refused.text
    with api.app.state.db() as db:
        assert db.get(Session, other['id']).parent_session_id is None
