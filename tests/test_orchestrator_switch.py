"""Orchestrator v2 availability: explicit Off closes every v2 entry and drains work."""
import pytest
from helpers import write_db
from sqlalchemy import select
from test_passkeys import setup
from test_team import lead, request

from acc.server.models import Event, Project, Setting
from acc.server.orchestrator import gates
from acc.server.supervisor_common import LaunchRefused

V2_PATHS = ('/api/gates', '/api/settings/orchestrator', '/api/teams/{lead}/tree', '/api/teams/{lead}/events',
            '/api/teams/{lead}/selections', '/api/teams/{lead}/worklist', '/api/teams/{lead}/plan',
            '/api/projects/{project_id}/brief', '/api/project-brief-references/', '/api/teams/{team_id}/brief')


@pytest.fixture
def slices_on(monkeypatch):
    """Every slice's own switch on, set before the app is built."""
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')


@pytest.fixture
def orchestrator_off(slices_on, monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '0')
    monkeypatch.delenv('RYNHULT_ORCHESTRATOR', raising=False)


@pytest.fixture
def orchestrator_on(slices_on, monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')


def v2_routes(app):
    return sorted(path for path in app.openapi()['paths'] if any(path.startswith(p) for p in V2_PATHS))


async def test_off_means_no_v2_route_tool_hook_or_job(orchestrator_off, api, project, env, monkeypatch):
    assert v2_routes(api.app)  # registered for live owner enable; the shared boundary refuses every off request
    x = await lead(api, project, env)
    with write_db(api.app.state.db) as s:  # even with the gate queue turned on in the database
        s.merge(Setting(key=gates.BUDGET_KEY, value={**gates.DEFAULT_BUDGET, 'enabled': True}))
        s.commit()
    for path in (f"/api/teams/{x['id']}/tree", f"/api/teams/{x['id']}/events", f"/api/teams/{x['id']}/worklist",
                 f"/api/teams/{x['id']}/plan", f"/api/teams/{x['id']}/plan/versions/1",
                 f"/api/teams/{x['id']}/selections", '/api/gates', '/api/gates/budget',
                 '/api/settings/orchestrator/selection', '/api/deliveries/' + 'a' * 32):
        assert (await api.get(path)).status_code == 404, path
    for method, path in (
            ('get', f'/api/projects/{project["id"]}/brief'),
            ('get', f'/api/projects/{project["id"]}/brief/references/REF-1'),
            ('post', f'/api/projects/{project["id"]}/brief/documents'),
            ('post', f'/api/projects/{project["id"]}/brief/import'),
            ('post', f'/api/projects/{project["id"]}/brief/versions/1/accept'),
            ('post', f'/api/project-brief-references/{project["id"]}'),
            ('get', f'/api/teams/{x["id"]}/brief'), ('post', f'/api/teams/{x["id"]}/brief/pin')):
        response = await getattr(api, method)(path, **({'json': {}} if method == 'post' else {}))
        assert response.status_code == 404, (method, path, response.text)
    assert (await api.post('/api/gates', json={'project': project['id'], 'branch': 'acc/team-a'})).status_code == 404
    for action, args in (('worklist', {}), ('plan', {}), ('plan_update', {'reason': 'x', 'idea': 'x'}),
                         ('select', {'task': 'x', 'need': {'task_type': 'docs'}}),
                         ('report', {'status': 'done', 'summary': 'x'}), ('gate', {}), ('gate_status', {'job': 'a' * 32}),
                         ('restack', {'helper': 'a' * 32}), ('reserve', {'kind': 'migration'}),
                         ('start', {'selection': 'a' * 32, 'name': 'A', 'task': 'x'})):
        reply = await request(api, env, x, action, **args)
        assert reply == {'ok': False, 'error': 'invalid team action or arguments'}, (action, reply)
    helper = await request(api, env, x, 'start', name='A', agent='claude', task='work')  # M17 itself is unchanged
    assert helper['ok'], helper
    with api.app.state.db() as s:
        assert not list(s.scalars(select(Event).where(Event.source == 'orchestrator')))
    with api.app.state.db() as s, pytest.raises(LaunchRefused, match='Orchestrator v2 is off'):
        gates.enqueue(s, s.get(Project, project['id']), 'acc/team-a', 'related', 'check', 'test',
                      settings=api.app.state.supervisor.settings)
    await setup(api, env, monkeypatch)  # a person, whose page learns from this that v2 is absent
    assert (await api.get('/api/auth/state')).json()['instance']['orchestrator'] is False


async def test_on_brings_the_slices_back(orchestrator_on, api, project, env, monkeypatch):
    assert v2_routes(api.app) == sorted([
        '/api/gates', '/api/gates/budget', '/api/gates/projects/{project_id}/commands', '/api/gates/{job_id}/cancel',
        '/api/gates/projects/{project_id}/flakes', '/api/gates/{job_id}/accept-retry', '/api/gates/wrapup',
        '/api/settings/orchestrator/selection', '/api/teams/{lead}/selections',
        '/api/teams/{lead}/selections/{selection_id}/override', '/api/teams/{lead}/worklist', '/api/teams/{lead}/worklist/items', '/api/teams/{lead}/worklist/items/{item_id}', '/api/teams/{lead}/worklist/items/{item_id}/discuss',
        '/api/teams/{lead}/worklist/now', '/api/teams/{lead}/plan', '/api/teams/{lead}/plan/versions/{number}',
        '/api/projects/{project_id}/brief', '/api/projects/{project_id}/brief/import',
        '/api/projects/{project_id}/brief/documents', '/api/projects/{project_id}/brief/versions/{number}/accept',
        '/api/projects/{project_id}/brief/references/{reference_id}', '/api/project-brief-references/{project_id}',
        '/api/teams/{team_id}/brief', '/api/teams/{team_id}/brief/pin'])
    x = await lead(api, project, env)
    assert (await api.get(f"/api/teams/{x['id']}/worklist")).status_code == 200
    reply = await request(api, env, x, 'gate_status', job='a' * 32)  # dispatched to slice 5, which answers itself
    assert not reply['ok'] and 'no gate job' in reply['error'], reply
    for path in ('tree', 'events?wait=0'):  # the live tree is there (it answers people only)
        assert (await api.get(f"/api/teams/{x['id']}/{path}")).status_code == 403, path
    await setup(api, env, monkeypatch)
    assert (await api.get('/api/auth/state')).json()['instance']['orchestrator'] is True


async def test_turning_the_orchestrator_off_ends_what_it_left_running(orchestrator_on, api, project, env, monkeypatch):
    """Owner decision 2026-10-26: after a restart with Orchestrator v2 off, nothing it started still looks alive. Each item is
    closed as stopped with the reason in its history, so it reads correctly if Orchestrator v2 is turned back on; the
    working list's questions leave the Inbox, and come back with Orchestrator v2."""
    from datetime import timedelta

    from acc.server import orchestrator
    from acc.server.models import AttentionItem, AuditLog, Session, utcnow
    from acc.server.orchestrator import delivery, selection, worklist
    x = await lead(api, project, env)
    with write_db(api.app.state.db) as s:
        lead_row = s.get(Session, x['id'])
        card = worklist.add(s, x['id'], f"session:{x['id']}", 'lead', {
            'kind': 'decision', 'title': 'Which database?', 'options': [{'id': 'a', 'label': 'A'}, {'id': 'b', 'label': 'B'}]})
        s.add(gates.GateJob(id='g' * 32, project_id=project['id'], team_id=x['id'], branch='acc/team-a', suite='related',
                            priority='check', state='queued', account=lead_row.run_as, requested_by='test', results={}))
        s.add(delivery.MessageDelivery(id='d' * 32, session_id=x['id'], actor='@api', preview='hello', text_sha256='0' * 64,
                                       bytes=5, body='hello', state='queued', expires_at=utcnow() + timedelta(minutes=15)))
        s.add(selection.Selection(id='s' * 32, team_id=x['id'], requested_by=x['id'], request_id='r' * 32, need={},
                                  task_digest='0' * 64, candidates=[], chosen=None, reason='proposed', budget={},
                                  state='proposed'))
        s.commit()
        key = f'worklist:{card.id}'

    def inbox_open():
        with api.app.state.db() as s:
            return s.scalar(select(AttentionItem).where(AttentionItem.dedupe_key == key,
                                                        AttentionItem.resolved_at.is_(None))) is not None
    assert inbox_open()

    monkeypatch.setenv('ACC_ORCHESTRATOR', '0')  # restarted with Orchestrator v2 off
    done = await orchestrator.reconcile_availability(api.app.state.supervisor)
    assert done == {'gates': 1, 'deliveries': 1, 'selections': 1, 'inbox': 1}
    with api.app.state.db() as s:
        job = s.get(gates.GateJob, 'g' * 32)
        assert job.state == 'cancelled' and job.reason == 'Orchestrator v2 was turned off'
        assert s.scalar(select(AuditLog).where(AuditLog.action == 'gate.cancel', AuditLog.target == job.id)) is not None
        row = s.get(delivery.MessageDelivery, 'd' * 32)
        assert row.state == 'failed' and row.error == 'orchestrator_off' and row.body == ''
        picked = s.get(selection.Selection, 's' * 32)
        assert picked.state == 'refused' and picked.reason == 'Orchestrator v2 was turned off'
    assert not inbox_open()
    assert (await orchestrator.reconcile_availability(api.app.state.supervisor)) == dict.fromkeys(done, 0)  # once is enough

    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')  # and back on: the open question is in the Inbox again
    assert (await orchestrator.reconcile_availability(api.app.state.supervisor))['inbox'] == 1
    assert inbox_open()
