"""Approval routes refuse private/group subjects before validating request fields."""
import pytest
from helpers import write_db
from sqlalchemy import select
from test_delegated_approvals import world
from test_delegated_approvals_round2 import sign_in

from acc.server import approvals
from acc.server.models import ApprovalConsent, Session


async def test_grant_and_bind_require_recent_verification_before_fields(api, project):
    await world(api, project)
    await sign_in(api, 'alex')
    for path in ('/api/approvals/consents', f"/api/approvals/consents/{'a' * 32}/bind"):
        response = await api.post(path, json={})
        assert response.status_code == 403, (path, response.text)


async def test_unknown_approval_resources_refuse_before_field_validation(api, project):
    await world(api, project)
    await sign_in(api, 'alex', verified=True)
    for path in (f"/api/approvals/consents/{'a' * 32}/revoke",
                 f"/api/approvals/consents/{'a' * 32}/bind",
                 f"/api/approvals/batches/{'b' * 32}/approve"):
        response = await api.post(path, json={})
        assert response.status_code == 404, (path, response.text)


@pytest.mark.parametrize('role', ['member', 'owner'])
async def test_group_approval_subject_is_refused_before_invalid_fields_even_for_issuer(api, project, role):
    from acc.server.models import ApprovalBatch, Group, ToolRequest, User
    sid = await world(api, project)
    tools = api.app.state.supervisor.tools
    rid = tools.request_session(sid, manager='npm', name='prettier', reason='Format')
    with write_db(api.app.state.db) as db:
        db.scalar(select(User).where(User.username == 'alex')).role = role
        policy = approvals.grant(db, actor='alex', project_id=project['id'], team_id=sid,
            action='tool.install', constraints={'manager': 'npm', 'name': 'prettier', 'version': '3.6.2',
                'registry': 'https://registry.npmjs.org'}, duration='project')
        cid = policy.id
        batch = approvals.preview_batch(db, 'alex', [rid])
        bid = batch.id
        db.add(Group(id='demo-group', name='Demo group', created_by='alex'))
        db.flush()
        p = db.get(Session, sid).project
        p.owner, p.group_id = None, 'demo-group'
        db.commit()
    await sign_in(api, 'alex', verified=True)
    targets = [
        ('/api/approvals/consents', {'project_id': project['id']}),
        ('/api/approvals/consents', {'team_id': sid}),
        (f'/api/approvals/consents/{cid}/bind', {}),
        (f'/api/approvals/consents/{cid}/revoke', {}),
        ('/api/approvals/batches/preview', {'requests': [rid]}),
        (f'/api/approvals/batches/{bid}/approve', {}),
    ]
    for path, body in targets:
        response = await api.post(path, json=body)
        assert response.status_code == 404, (role, path, response.text)
    listed = await api.get('/api/approvals/consents', params={'project_id': project['id'], 'limit': 0})
    assert listed.status_code == 404
    with api.app.state.db() as db:
        assert db.get(ApprovalConsent, cid).state == 'active'
        assert db.get(ApprovalBatch, bid).used is False
        assert db.get(ToolRequest, rid).status == 'pending_approval'
