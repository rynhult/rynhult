"""Orchestrator v2 gate sandbox review (docs/security/2026-10-27-v2-gate-sandbox-review.md): one test per finding."""
import dataclasses

import pytest
from helpers import write_db
from test_orchestrator_gates import FakeMachine, add, jobs, preview, queue  # noqa: F401

from acc.server.models import Project
from acc.server.orchestrator import gates


# GS-1: with ACC_CONTAINERS off, a container project's sessions are refused (never run natively), so its branches
# hold code only a container ran. The gate counted it as a host project then and ran that code on the host.
async def test_gs_1_a_container_project_is_never_gated_on_the_host_even_with_containers_off(queue, monkeypatch):  # noqa: F811
    api, project, machine, scheduler = queue
    supervisor = api.app.state.supervisor
    queued = add(api, project, 'acc/team-earlier')
    with write_db(api.app.state.db) as s:
        s.get(Project, project['id']).containers = True
        s.commit()
    off = dataclasses.replace(supervisor.settings, containers_enabled=False)
    monkeypatch.setattr(supervisor, 'settings', off)
    monkeypatch.setattr(api.app.state.ctx, 'settings', off)
    with pytest.raises(gates.LaunchRefused, match='run in containers'):
        add(api, project, 'acc/team-later')
    refused = await api.post('/api/gates', json={'project': project['id'], 'branch': 'acc/team-later'})
    assert refused.status_code == 409
    await scheduler.tick()
    assert jobs(api)[queued][0] == 'refused' and machine.started == []


# GS-2: the container check ran once, before the broker prepared the job; a switch to containers during preparation
# (or before a targeted rerun) still started branch code on the host.
async def test_gs_2_a_switch_to_containers_during_preparation_stops_the_start(queue):  # noqa: F811
    api, project, machine, scheduler = queue
    queued = add(api, project, 'acc/team-racing')
    prepare = machine.call

    async def switching(method, **params):
        if method == 'gate_prepare':  # the owner switches the project while the broker prepares the checkout
            with write_db(api.app.state.db) as s:
                s.get(Project, project['id']).containers = True
                s.commit()
        return await prepare(method, **params)
    api.app.state.supervisor.broker.call = switching
    await scheduler.tick()
    assert jobs(api)[queued][0] == 'refused' and machine.started == []


async def test_gs_2_a_targeted_rerun_after_the_switch_is_refused(queue):  # noqa: F811
    api, project, machine, scheduler = queue
    queued = add(api, project, 'acc/team-flaky')
    await scheduler.tick()
    assert len(machine.started) == 1
    with write_db(api.app.state.db) as s:
        s.get(Project, project['id']).containers = True
        s.commit()
    machine.results[queued] = {'exit': 1, 'counts': {'tests': 2}, 'failed': ['tests/test_x.py::test_a'], 'rerunnable': True}
    machine.finished.add(queued)
    await scheduler.tick()
    assert len(machine.started) == 1  # the rerun never started on the host
    assert jobs(api)[queued][0] in ('refused', 'failed')
