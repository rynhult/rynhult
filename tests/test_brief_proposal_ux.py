"""Proposal discovery and person-reviewed merges preserve current project scope."""
import pytest

from acc.server.orchestrator import project_briefs as brief

pytest_plugins = ['project_brief_migrations']


@pytest.fixture(autouse=True)
def v2(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')


async def test_pending_proposal_is_independent_of_history_and_accept_merges_edits(api, project):
    url = f"/api/projects/{project['id']}/brief"
    assert (await api.post(url, json={'version': 0, 'fields': {'product': 'Current', 'constraints': 'Keep privacy', 'run_mode': 'prepare'}})).status_code == 200
    proposal = (await api.post(url + '/import', json={'markdown': brief.markdown(project['id'], 1, {'product': 'Proposed', 'design': 'Blue'})})).json()
    view = (await api.get(url, params={'history_before': 2})).json()
    assert view['pending_proposal']['version'] == proposal['version']
    accepted = await api.post(url + f"/versions/{proposal['version']}/accept", json={
        'version': 1, 'fields': ['product', 'design'], 'edits': {'design': 'Calm blue'}})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['submitted'] == {'product': 'Proposed', 'design': 'Calm blue', 'constraints': 'Keep privacy', 'run_mode': 'prepare'}
    assert (await api.get(url)).json()['pending_proposal'] is None


async def test_review_edits_must_be_selected_and_version_fenced(api, project):
    url = f"/api/projects/{project['id']}/brief"
    proposal = (await api.post(url + '/import', json={'markdown': '# Product\nAn idea', 'base_version': 0})).json()
    route = url + f"/versions/{proposal['version']}/accept"
    assert (await api.post(route, json={'version': 0, 'fields': ['product'], 'edits': {'constraints': 'injected'}})).status_code == 422
    assert (await api.post(route, json={'version': 0, 'fields': ['product'], 'edits': {'product': ''}})).status_code == 422
    assert (await api.post(url, json={'version': 0, 'fields': {'product': 'New current', 'success': 'Keep this'}})).status_code == 200
    assert (await api.post(route, json={'version': 0, 'fields': ['product'], 'edits': {}})).status_code == 409


async def test_stale_proposal_review_and_accept_are_available_after_new_current(api, project):
    url = f"/api/projects/{project['id']}/brief"
    proposal = (await api.post(url + '/import', json={'markdown': '# Old proposal', 'base_version': 0})).json()
    assert proposal['version'] == 1
    assert (await api.post(url, json={'version': 0, 'fields': {'product': 'One line', 'constraints': 'Private'}})).json()['version'] == 2
    review = (await api.get(url, params={'review_version': 1, 'history_before': 1})).json()
    assert review['pending_proposal'] is None
    assert review['review_proposal']['base_version'] == 0
    assert review['current_version'] == 2 and not review['history']
    accepted = await api.post(url + '/versions/1/accept', json={'version': 2, 'fields': ['product', 'design']})
    assert accepted.status_code == 200
    assert accepted.json()['submitted']['constraints'] == 'Private'
    assert (await api.get(url, params={'review_version': 999})).status_code == 404


async def test_acceptance_queues_a_redacted_notice_for_active_project_lead(api, project, env):
    from sqlalchemy import select
    from test_changes import repo
    from test_team import grant

    from acc.server.orchestrator.delivery import MessageDelivery
    repo(env['workspaces'] / 'demo')
    launch = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                 'name': 'Lead', 'worktree': True, 'orchestrator': grant()})
    assert launch.status_code == 201, launch.text
    lead = launch.json()['id']
    url = f"/api/projects/{project['id']}/brief"
    proposal = (await api.post(url + '/import', json={'markdown': '# Proposed app', 'base_version': 0})).json()
    assert (await api.post(url + f"/versions/{proposal['version']}/accept", json={'version': 0, 'fields': ['product']})).status_code == 200
    with api.app.state.db() as db:
        rows = list(db.scalars(select(MessageDelivery).where(MessageDelivery.session_id == lead,
            MessageDelivery.actor == 'supervisor')))
        assert len(rows) == 1
        assert 'accepted project brief version 2' in rows[0].preview
        assert rows[0].text_sha256 and rows[0].bytes > 0
        assert 'brief_review=1' in brief.documents_kickoff(db, project['id'])


async def test_notice_backpressure_rolls_back_acceptance(api, project, monkeypatch):
    from acc.server.supervisor_common import LaunchRefused
    url = f"/api/projects/{project['id']}/brief"
    proposal = (await api.post(url + '/import', json={'markdown': '# Proposed app', 'base_version': 0})).json()
    def full(*args):
        raise LaunchRefused('session delivery queue is full')
    monkeypatch.setattr(brief, 'notify_accept', full)
    response = await api.post(url + f"/versions/{proposal['version']}/accept", json={'version': 0, 'fields': ['product']})
    assert response.status_code == 409 and 'no fields accepted' in response.text
    assert (await api.get(url)).json()['current_version'] == 0


async def test_brief_question_links_to_pending_review(api, project, env):
    from test_team import lead
    url = f"/api/projects/{project['id']}/brief"
    assert (await api.post(url, json={'version': 0, 'fields': {'product': 'Initial'}})).status_code == 200
    session = await lead(api, project, env)
    assert (await api.post(url + '/import', json={'markdown': '# Proposed', 'base_version': 1})).status_code == 200
    with api.app.state.db() as db:
        from acc.server.orchestrator.planning import PlanPhase
        db.add(PlanPhase(team_id=session['id'], key='design', position=1, title='Design'))
        db.flush()
        item = brief.question(db, session['id'], {'field': 'design', 'question': 'Which style?', 'blocks': ['design'],
            'options': [{'id': 'calm', 'label': 'Calm'}, {'id': 'bold', 'label': 'Bold'}], 'recommendation': {'option': 'calm', 'reason': 'Readable'}})
        assert brief.review_url(project['id'], 2) in item.steps
