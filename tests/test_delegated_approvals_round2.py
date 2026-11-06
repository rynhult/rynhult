"""Round-two security findings: provenance, discovery, de-duplication and rollback."""
from datetime import timedelta

import pytest
from helpers import write_db
from migration_revisions import DELEGATED_APPROVALS
from sqlalchemy import select
from test_delegated_approvals import issue, world

from acc.server.models import ApprovalConsent, AuditLog, Project, Session, User, utcnow


async def sign_in(api, username, *, verified=False):
    from acc.server import auth
    from acc.server.models import AuthSession
    with write_db(api.app.state.db) as db:
        user = db.scalar(select(User).where(User.username == username))
        token, csrf = auth.create_session(db, user, 'round-two')
        if verified:
            db.get(AuthSession, auth.digest(token)).reauthenticated_at = auth.now()
        db.commit()
    api.headers.pop('authorization', None)
    api.cookies.set('acc_session', token)
    api.headers.update({'x-acc-csrf': csrf, 'origin': 'http://acc'})


async def test_bind_audits_verified_person_and_keeps_target_lead_separate(api, project):
    sid = await world(api, project)
    await sign_in(api, 'alex', verified=True)
    response = await api.post('/api/approvals/consents', json={
        'project_id': project['id'], 'team_id': sid, 'action': 'tool.install', 'duration': 'project',
        'constraints': {'manager': 'npm', 'name': 'prettier', 'version': '3.6.2', 'registry': 'https://registry.npmjs.org'}})
    assert response.status_code == 201, response.text
    parent = response.json()['id']
    bound = await api.post(f'/api/approvals/consents/{parent}/bind', json={'team_id': sid})
    assert bound.status_code == 201, bound.text
    with api.app.state.db() as db:
        entry = db.scalar(select(AuditLog).where(AuditLog.action == 'approval.bind', AuditLog.target == bound.json()['id']))
        assert entry.actor == 'alex'
        assert entry.detail['lead_id'] == sid
        assert entry.detail['parent_id'] == parent


async def test_all_active_consents_remain_pageable_behind_newer_history(api, project):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        original = issue(db, sid)
        original.created_at = utcnow() - timedelta(days=1)
        oldest = original.id
        created = utcnow()
        for n in range(410):
            db.add(ApprovalConsent(id=f'{n:032x}', issuer='alex', project_id=project['id'], team_id=sid,
                action=original.action, constraints=original.constraints, authority=original.authority,
                duration='team', state='active' if n < 205 else 'revoked', created_at=created))
        db.commit()
    await sign_in(api, 'alex')
    found = set()
    cursor = None
    pages = 0
    while True:
        response = await api.get('/api/approvals/consents', params={
            'limit': 200, **({'cursor': cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        data = response.json()
        ids = {r['id'] for r in data['consents']}
        assert not ids & found
        assert all(r['state'] == 'active' for r in data['consents'])
        found.update(ids)
        pages += 1
        cursor = data['next_cursor']
        if cursor is None:
            break
        if pages == 1:
            # Paging must survive a concurrent revocation of its anchor.
            revoked_anchor = await api.post(f'/api/approvals/consents/{cursor}/revoke', json={'revision': 1})
            assert revoked_anchor.status_code == 200
        assert pages < 5
    assert len(found) == 206 and oldest in found
    assert pages == 2
    history = await api.get('/api/approvals/consents', params={'state': 'revoked', 'limit': 200})
    assert history.status_code == 200
    assert len(history.json()['consents']) == 200
    assert history.json()['next_cursor'] is not None
    revoked = await api.post(f'/api/approvals/consents/{oldest}/revoke', json={'revision': 1})
    assert revoked.status_code == 200 and revoked.json()['state'] == 'revoked'


async def test_owner_discovers_other_person_consent_only_in_selected_authorized_project(api, project):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        own = issue(db, sid)
        consent_id = own.id
        db.add(User(username='blair', role='owner', account='blair-agent', password_hash='unused', totp_secret='unused'))
        other = Project(slug='other-demo', name='Other demo', workspace_path='/tmp/other-demo',
                        account='alex-agent', owner='alex', trusted=True)
        db.add(other)
        db.flush()
        other_id = other.id
        db.commit()
    await sign_in(api, 'blair')
    assert (await api.get('/api/approvals/consents')).json()['consents'] == []
    visible = await api.get('/api/approvals/consents', params={'project_id': project['id']})
    assert visible.status_code == 200
    assert [r['id'] for r in visible.json()['consents']] == [consent_id]
    assert (await api.get('/api/approvals/consents', params={'project_id': other_id})).json()['consents'] == []
    assert (await api.get('/api/approvals/consents', params={'project_id': 987654})).status_code == 404
    assert (await api.get('/api/approvals/consents', params={'project_id': other_id, 'cursor': consent_id})).status_code == 422
    revoked = await api.post(f'/api/approvals/consents/{consent_id}/revoke', json={'revision': 1})
    assert revoked.status_code == 200


async def test_member_list_scope_cannot_discover_other_issuer_or_project(api, project):
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        own = issue(db, sid)
        own_id = own.id
        other = Project(slug='private-demo', name='Private demo', workspace_path='/tmp/private-demo',
                        owner='blair', account='blair-agent', trusted=True)
        db.add(other)
        db.flush()
        other_id = other.id
        db.add(ApprovalConsent(id='f' * 32, issuer='blair', project_id=project['id'], team_id=sid,
            action=own.action, constraints=own.constraints, authority=own.authority, duration='team'))
        db.commit()
    await sign_in(api, 'alex')
    visible = await api.get('/api/approvals/consents', params={'project_id': project['id']})
    assert [r['id'] for r in visible.json()['consents']] == [own_id]
    assert (await api.get('/api/approvals/consents', params={'project_id': other_id})).status_code == 404
    assert (await api.get('/api/approvals/consents', params={'cursor': 'f' * 32})).status_code == 422
    assert (await api.get('/api/approvals/consents', params={'limit': 201})).status_code == 422


async def test_solo_sessions_do_not_share_an_approval_as_if_they_were_a_team(api, project):
    from acc.server.tools import ToolRefused
    sid = await world(api, project)
    with write_db(api.app.state.db) as db:
        first = db.get(Session, sid)
        first.team_id = None
        db.add(Session(id='solo-second', project_id=first.project_id, provider=first.provider, name='Solo second',
            tmux_name='solo-second', run_as=first.run_as, owner=first.owner, launch_spec_id=first.launch_spec_id))
        db.commit()
    tools = api.app.state.supervisor.tools
    tools.request_session(sid, manager='npm', name='prettier', reason='Format one')
    with pytest.raises(ToolRefused, match='already'):
        tools.request_session('solo-second', manager='npm', name='prettier', reason='Format two')


async def test_helper_coordination_audits_actor_without_calling_helper_the_root_lead(api, project, env):
    from test_team import lead, request
    root = await lead(api, project, env)
    first = await request(api, env, root, 'start', name='Demo helper', agent='claude', profile=None, task='Build demo')
    assert first['ok'], first
    helper = first['result']
    second = await request(api, env, helper, 'start', name='Demo child', agent='claude', profile=None, task='Check demo')
    assert second['ok'], second
    with api.app.state.db() as db:
        entry = db.scalar(select(AuditLog).where(AuditLog.action == 'team.start', AuditLog.actor == f"session:{helper['id']}"))
        assert entry.detail['decision']['lead_id'] == root['id']
        assert entry.detail['decision']['acting_session_id'] == helper['id']
        assert entry.detail['decision']['acting_role'] == 'helper'
        root_entry = db.scalar(select(AuditLog).where(AuditLog.action == 'team.start', AuditLog.actor == f"session:{root['id']}"))
        assert root_entry.detail['decision']['acting_session_id'] == root['id']
        assert root_entry.detail['decision']['acting_role'] == 'lead'


@pytest.mark.parametrize('with_decision', [True, False])
def test_downgrade_preserves_person_decision_even_without_consent_records(tmp_path, with_decision):
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect, null, text
    from test_team_lifetime_migration import seed

    from acc.server.db import alembic_config, make_engine
    config = alembic_config(f'sqlite:///{tmp_path / "downgrade.db"}')
    command.upgrade(config, DELEGATED_APPROVALS)
    previous = ScriptDirectory.from_config(config).get_revision(DELEGATED_APPROVALS).down_revision
    engine = make_engine(config.get_main_option('sqlalchemy.url'))
    seed(engine, 'tool_request', {'id': 'tool-123456789abc',
        'decision': {'source': 'P', 'issuer': 'alex'} if with_decision else null()})
    try:
        if with_decision:
            with pytest.raises(RuntimeError, match='Delegated approval records'):
                command.downgrade(config, previous)
            with engine.connect() as db:
                assert 'decision' in {c['name'] for c in inspect(db).get_columns('tool_request')}
                assert db.scalar(text('SELECT count(*) FROM tool_request WHERE decision IS NOT NULL')) == 1
        else:
            command.downgrade(config, previous)
            with engine.connect() as db:
                assert db.scalar(text('SELECT count(*) FROM tool_request')) == 1
    finally:
        engine.dispose()
