"""Owner consent for public git backups is separate from global remote-access enablement."""
import pytest
from helpers import verified_post, write_db
from sqlalchemy import select
from test_auth import do_setup
from test_machine_identities import register
from test_roaming_git_backup import NODE_KEY, enable, grant_headers

from acc.node.identity import public_key_b64
from acc.server.models import AuditLog, Node, Project

FRESH = 'f' * 32
LEAF = 'f' * 64


def prepare(api, project):
    enable(api, project['id'])
    with write_db(api.app.state.db) as db:
        db.add(Node(id=FRESH, name='consent-test', status='active', public_key=public_key_b64(NODE_KEY)))
        db.get(Project, project['id']).host = 'consent-test'
        register(db, 'node', LEAF, person='', node_id=FRESH)
        db.commit()


async def test_global_remote_access_does_not_opt_a_new_machine_into_public_backups(api, project):
    prepare(api, project)
    grant = api.app.state.supervisor.backup_grants.mint(project['id'], FRESH)
    response = await api.post(f"/api/git/{project['id']}.git/token",
                              headers=grant_headers(grant, project['id'], edge=LEAF))
    assert response.status_code == 403, response.text
    row = next(row for row in (await api.get('/api/nodes')).json() if row['id'] == FRESH)
    assert row['public_git_backup'] is False


async def test_owner_opt_in_is_revision_checked_audited_and_can_be_disabled(api, env, project):
    prepare(api, project)
    await do_setup(api, env)
    async def row():
        return next(row for row in (await api.get('/api/nodes')).json() if row['id'] == FRESH)
    before = await row()
    payload = {'role': 'always_on', 'public_git_backup': True, 'expected_revision': before['policy_revision']}
    response = await verified_post(api, '/api/nodes/consent-test/policy', payload)
    assert response.status_code == 200 and response.json()['public_git_backup'] is True, response.text
    assert response.json()['policy_revision'] != before['policy_revision']
    assert (await verified_post(api, '/api/nodes/consent-test/policy', payload)).status_code == 409
    grant = api.app.state.supervisor.backup_grants.mint(project['id'], FRESH)
    headers = grant_headers(grant, project['id'], edge=LEAF)
    assert (await api.post(f"/api/git/{project['id']}.git/token", headers=headers)).status_code == 200
    response = await api.post('/api/nodes/consent-test/policy', json={
        'role': 'always_on', 'public_git_backup': False, 'expected_revision': (await row())['policy_revision']})
    assert response.status_code == 200
    assert (await api.post(f"/api/git/{project['id']}.git/token", headers=headers)).status_code == 403
    with api.app.state.db() as db:
        audits = list(db.scalars(select(AuditLog).where(AuditLog.action == 'node.public_git_backup').order_by(AuditLog.id)))
        assert [entry.detail['enabled'] for entry in audits] == [True, False]


@pytest.mark.parametrize('value', [None, 'true', 1, [], {}])
async def test_consent_requires_a_strict_boolean(api, project, value):
    prepare(api, project)
    response = await api.post('/api/nodes/consent-test/policy', json={'role': 'always_on', 'public_git_backup': value})
    assert response.status_code == 422, response.text


async def test_setting_consent_requires_a_revision_even_for_owner_cli(api, project):
    prepare(api, project)
    response = await api.post('/api/nodes/consent-test/policy', json={'role': 'always_on', 'public_git_backup': True})
    assert response.status_code == 409


async def test_opt_out_closes_public_git_and_revokes_grants_but_preserves_private(api, project):
    from acc.server.edge import Connection

    prepare(api, project)
    with write_db(api.app.state.db) as db:
        db.get(Node, FRESH).public_git_backup = True
        db.commit()
    grants = api.app.state.supervisor.backup_grants
    pending, public, private = [grants.mint(project['id'], FRESH) for _ in range(3)]
    grants.lookup(public, project['id'])['via'] = 'public'
    grants.lookup(private, project['id'])['via'] = 'private'
    token = grants.exchange(public, project['id'])
    edge = api.app.state.ctx.edge
    git = Connection(LEAF, f"/api/git/{project['id']}.git/git-receive-pack")
    control = Connection(LEAF, '/api/nodes/connect')
    other = Connection('a' * 64, git.path)
    edge.open.update([git, control, other])
    try:
        row = next(row for row in (await api.get('/api/nodes')).json() if row['id'] == FRESH)
        response = await api.post('/api/nodes/consent-test/policy', json={
            'role': 'always_on', 'public_git_backup': False, 'expected_revision': row['policy_revision']})
        assert response.status_code == 200, response.text
        assert git.closed.is_set()
        assert not control.closed.is_set() and not other.closed.is_set()
        assert grants.lookup(pending, project['id']) is None
        assert grants.lookup(public, project['id']) is None
        assert grants.lookup(private, project['id']) is not None
        assert not api.app.state.supervisor.backup_tokens.check(token, project['id'])
        response = await api.get(f"/api/git/{project['id']}.git/info/refs?service=git-receive-pack",
                                headers={'X-Acc-Edge-Client': LEAF, 'Authorization': 'Bearer ' + token})
        assert response.status_code == 403
    finally:
        edge.open.difference_update([git, control, other])


async def test_edge_sweep_rechecks_public_backup_consent(api, project):
    from acc.server.edge import Connection

    prepare(api, project)
    edge = api.app.state.ctx.edge
    git = Connection(LEAF, f"/api/git/{project['id']}.git/git-receive-pack")
    control = Connection(LEAF, '/api/nodes/connect')
    edge.open.update([git, control])
    try:
        await edge.sweep()
        assert git.closed.is_set()
        assert not control.closed.is_set()
    finally:
        edge.open.difference_update([git, control])


async def test_members_and_agents_cannot_change_public_backup_consent(api, project, monkeypatch):
    from acc.server.api.context import Principal

    prepare(api, project)
    row = next(row for row in (await api.get('/api/nodes')).json() if row['id'] == FRESH)
    monkeypatch.setattr(api.app.state.ctx, 'principal', lambda request: Principal('blair', 'member', 'agent'))
    response = await api.post('/api/nodes/consent-test/policy', json={
        'role': 'always_on', 'public_git_backup': True, 'expected_revision': row['policy_revision']})
    assert response.status_code == 403
    with api.app.state.db() as db:
        assert db.get(Node, FRESH).public_git_backup is False
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'node.public_git_backup')) is None


def test_upgrade_existing_machine_defaults_to_no_public_backups(tmp_path):
    from alembic import command
    from alembic.script import ScriptDirectory

    from acc.server.db import alembic_config, make_engine

    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    config = alembic_config(url)
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision('0068').down_revision == '0067'
    chain = list(scripts.walk_revisions())
    assert all(revision.down_revision == following.revision for revision, following in zip(chain, chain[1:]))
    assert chain[-1].down_revision is None
    command.upgrade(config, '0067')
    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO node (id, name, status, info, created_at) VALUES ('legacy', 'demo-legacy', 'active', '{}', '2026-10-25 00:00:00')")
        command.upgrade(config, '0068')
        with engine.connect() as connection:
            assert connection.exec_driver_sql('SELECT public_git_backup FROM node').scalar() == 0
            assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize('who', ['api_token', 'stale_browser'])
async def test_opting_in_needs_a_freshly_verified_owner_but_opting_out_never_does(api, env, project, who):
    # Turning public git transport on widens what the public address accepts: only an owner who just verified their
    # sign-in may do it, never the installation API token or a copied cookie. Turning it off is always allowed.
    from acc.server import auth
    from acc.server.models import AuthSession
    prepare(api, project)
    await do_setup(api, env)
    revision = next(row for row in (await api.get('/api/nodes')).json() if row['id'] == FRESH)['policy_revision']
    payload = {'role': 'always_on', 'public_git_backup': True, 'expected_revision': revision}
    if who == 'api_token':
        response = await api.post('/api/nodes/consent-test/policy', json=payload)
    else:
        with write_db(api.app.state.db) as s:
            s.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = None
            s.commit()
        response = await api.post('/api/nodes/consent-test/policy', json=payload, headers={
            'Authorization': '', 'Origin': api.app.state.ctx.settings.allowed_origins[0],
            'X-ACC-CSRF': api.cookies.get('acc_csrf') or ''})
    assert response.status_code == 403, response.text
    with api.app.state.db() as db:
        assert db.get(Node, FRESH).public_git_backup is False
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'node.public_git_backup')) is None
        refused = db.scalar(select(AuditLog).where(AuditLog.action == 'node.public_git_backup_refused'))
        assert refused is not None and refused.detail == {'node_id': FRESH, 'reason': 'verification_required'}
    # A same-value save (another setting changed) and an opt-out need no fresh verification.
    with write_db(api.app.state.db) as s:
        s.get(Node, FRESH).public_git_backup = True
        s.commit()
    revision = next(row for row in (await api.get('/api/nodes')).json() if row['id'] == FRESH)['policy_revision']
    same = await api.post('/api/nodes/consent-test/policy', json={**payload, 'expected_revision': revision})
    assert same.status_code == 200, same.text
    off = await api.post('/api/nodes/consent-test/policy', json={
        'role': 'always_on', 'public_git_backup': False, 'expected_revision': same.json()['policy_revision']})
    assert off.status_code == 200 and off.json()['public_git_backup'] is False


@pytest.mark.parametrize('where', ['public_hostname', 'through_edge'])
async def test_opting_in_needs_the_private_address_too(api, env, project, where, monkeypatch):
    # Owner decision 2026-10-25 (review F-3): turning public git transport on is done from the LAN, like Apply.
    from acc.server.api import nodes
    prepare(api, project)
    await do_setup(api, env)
    revision = next(row for row in (await api.get('/api/nodes')).json() if row['id'] == FRESH)['policy_revision']
    payload = {'role': 'always_on', 'public_git_backup': True, 'expected_revision': revision}
    if where == 'public_hostname':
        monkeypatch.setattr(nodes, 'request_host', lambda request: 'remote.example.com')
    else:
        monkeypatch.setattr(nodes, 'through_edge', lambda request: True)
    response = await verified_post(api, '/api/nodes/consent-test/policy', payload)
    assert response.status_code == 403 and 'private' in response.text, response.text
    with api.app.state.db() as db:
        assert db.get(Node, FRESH).public_git_backup is False
        refused = db.scalar(select(AuditLog).where(AuditLog.action == 'node.public_git_backup_refused'))
        assert refused.detail == {'node_id': FRESH, 'reason': 'private_address_required'}
    # Opting out works from anywhere.
    with write_db(api.app.state.db) as s:
        s.get(Node, FRESH).public_git_backup = True
        s.commit()
    revision = next(row for row in (await api.get('/api/nodes')).json() if row['id'] == FRESH)['policy_revision']
    off = await api.post('/api/nodes/consent-test/policy', json={
        'role': 'always_on', 'public_git_backup': False, 'expected_revision': revision})
    assert off.status_code == 200
