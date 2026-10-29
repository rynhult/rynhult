"""Orchestrator v2 review L-7: every helper launch re-checks what the team was started with, and fails closed."""
from helpers import write_db
from sqlalchemy import select
from test_team import lead, request

from acc.server.models import AuditLog, Project, Session, User


def refused_audit(api):
    with api.app.state.db() as s:
        return [a.detail['reason'] for a in s.scalars(select(AuditLog).where(AuditLog.action == 'team.start_refused'))]


def change(api, project, **fields):
    with write_db(api.app.state.db) as s:
        row = s.get(Project, project['id'])
        for key, value in fields.items():
            setattr(row, key, value)
        s.commit()


async def test_l7_a_helper_starts_while_nothing_changed(api, project, env):
    x = await lead(api, project, env)
    assert (await request(api, env, x, 'start', name='A', agent='claude', task='work'))['ok']
    assert refused_audit(api) == []


async def test_l7_the_owners_removal_or_lost_access_stops_helper_launches(api, project, env):
    x = await lead(api, project, env)
    with write_db(api.app.state.db) as s:
        s.get(Session, x['id']).owner = 'riley'  # a member's team; riley has no account (removed)
        s.commit()
    reply = await request(api, env, x, 'start', name='A', agent='claude', task='work')
    assert not reply['ok'] and 'no longer has an account' in reply['error']
    with write_db(api.app.state.db) as s:
        s.add(User(username='riley', password_hash='', totp_secret='', role='member'))
        s.commit()
    reply = await request(api, env, x, 'start', name='A', agent='claude', task='work')
    assert not reply['ok'] and 'no longer has access' in reply['error']  # the project isn't riley's
    change(api, project, owner='riley')
    assert (await request(api, env, x, 'start', name='A', agent='claude', task='work'))['ok']
    assert refused_audit(api) == ['owner_removed', 'owner_access']


async def test_l7_a_project_switched_to_containers_gets_no_native_helper(api, project, env):
    x = await lead(api, project, env)
    change(api, project, containers=True)
    reply = await request(api, env, x, 'start', name='A', agent='claude', task='work')
    assert not reply['ok'] and 'containers' in reply['error']
    with api.app.state.db() as s:
        assert s.scalar(select(Session).where(Session.parent_session_id == x['id'])) is None
    assert refused_audit(api) == ['project_containers']


async def test_l7_a_changed_project_account_or_archived_project_refuses(api, project, env):
    x = await lead(api, project, env)
    change(api, project, account='someone-else-agent')
    reply = await request(api, env, x, 'start', name='A', agent='claude', task='work')
    assert not reply['ok'] and 'account changed' in reply['error']
    change(api, project, account=x['run_as'], archived=True)
    reply = await request(api, env, x, 'start', name='A', agent='claude', task='work')
    assert not reply['ok'] and 'archived' in reply['error']
    assert refused_audit(api) == ['account_changed', 'project_unavailable']


async def test_l7_a_granted_profile_removed_from_the_catalog_refuses(api, project, env, monkeypatch):
    catalog = api.app.state.supervisor.catalog
    name = next(iter(catalog.profiles))
    x = await lead(api, project, env, agents=[{'agent': 'claude', 'profile': name}])
    monkeypatch.setattr(catalog, 'profiles', {k: v for k, v in catalog.profiles.items() if k != name})
    reply = await request(api, env, x, 'start', name='A', agent='claude', profile=name, task='work')
    assert not reply['ok'] and 'no longer exists' in reply['error']
    assert refused_audit(api) == ['profile_removed']
