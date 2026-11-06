"""Source identity review regressions (P2–P4)."""
# ruff: noqa: F811
from test_groups import grouped, two  # noqa: F401

from acc.server.models import Placement, Project


def test_scp_locator_redacts_password_with_at_sign():
    from acc.server.source_schema_v1 import sanitize_locator
    assert sanitize_locator('user:p@ss@host.example:a/b') == 'host.example:a/b'


async def test_group_location_response_has_only_reviewed_provenance_fields(grouped):
    from helpers import write_db
    w = grouped
    with write_db(w['owner'].app.state.db) as db:
        db.add(Placement(project_id=w['group_project']['id'], host='local', account='agent',
                         path='/member', person='alex', approved_by='owner-private-name'))
        db.commit()
    result = await w['member'].get(f"/api/groups/{w['group']['id']}/projects/{w['group_project']['id']}/locations")
    assert result.status_code == 200, result.text
    assert set(result.json()[0]['provenance']) <= {'import_mode', 'managed_checkout'}
    assert 'owner-private-name' not in result.text


def test_legacy_enrollment_rollover_does_not_strand_projects_or_sessions(api):
    from acc.server.models import LaunchSpec, Node
    from acc.server.models import Session as AgentSession
    from acc.server.sources import compatibility_target, freeze_session, session_problem
    with api.app.state.db() as db:
        node = Node(id='old-enrollment', name='edge', status='active', public_key='old-key')
        db.add(node)
        db.flush()
        project = Project(slug='rollover', name='Rollover', host='edge', account='alex-agent',
                          workspace_path='/work/repo', owner='alex')
        db.add(project)
        db.flush()
        placement = Placement(project_id=project.id, person='alex', host='edge', account='alex-agent', path='/work/repo')
        db.add(placement)
        db.flush()
        session = AgentSession(id='rollover-session', name='Rollover', provider='claude', project=project,
            placement=placement, run_as='alex-agent', tmux_name='rollover',
            launch_spec=LaunchSpec(provider='claude', inputs={}, files={}, digest='fixed'))
        db.add(session)
        db.flush()
        freeze_session(db, session, compatibility_target(db, project))
        db.commit()
        db.delete(node)
        db.flush()
        db.add(Node(id='new-enrollment', name='edge', status='active', public_key='new-key'))
        db.commit()
        assert compatibility_target(db, project)['host'] == 'edge'
        assert session_problem(db, session) is None


async def test_shared_resume_preflight_and_post_use_same_frozen_target_guard(api, project):
    import pytest
    from helpers import pause_supervisor, write_db

    from acc.server.models import Session as AgentSession
    from acc.server.supervisor_common import LaunchRefused
    await pause_supervisor(api)
    launched = await api.post('/api/sessions', json={'project_id': project['id'], 'name': 'Shared preflight', 'provider': 'claude'})
    assert launched.status_code == 201, launched.text
    ident = launched.json()['id']
    with write_db(api.app.state.db) as db:
        db.get(AgentSession, ident).placement.path = '/changed-after-launch'
        db.commit()
    with api.app.state.db() as db:
        session = db.get(AgentSession, ident)
        before = (session.desired_state, session.observed_state, session.recovery_attempts)
        with pytest.raises(LaunchRefused) as refusal:
            api.app.state.supervisor._check_resume(db, session, '@api')
        assert (session.desired_state, session.observed_state, session.recovery_attempts) == before
    checked = await api.get(f'/api/sessions/{ident}/resume-check')
    assert checked.status_code == 200, checked.text
    assert checked.json() == {'eligible': False, 'reason': str(refusal.value)}
    response = await api.post(f'/api/sessions/{ident}/resume')
    assert response.status_code == 409, response.text
    assert response.json()['detail'] == str(refusal.value)
