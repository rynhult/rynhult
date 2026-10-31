"""Phase 3 coordination: never infer shared tasks from private prompts."""
# ruff: noqa: F811
import asyncio

import pytest
from helpers import pause_supervisor, write_db
from isolated_settings import isolated_broker
from race_helpers import guard_database, transaction_rendezvous
from test_groups import grouped, two  # noqa: F401
from test_users import csrf

from acc.server.models import GroupMembership


async def test_board_is_explicit_empty_and_has_unknown_usage(grouped):
    w = grouped
    response = await w['member'].get(f"/api/groups/{w['group']['id']}/board")
    assert response.status_code == 200, response.text
    assert response.json()['rows'] == []
    assert response.json()['usage']['coverage'] == 'unknown'
    assert response.json()['usage']['cost'] is None


async def test_board_policy_revision_matches_its_quota_values(grouped):
    w = grouped
    base = f"/api/groups/{w['group']['id']}"
    before = (await w['member'].get(base + '/board')).json()
    current = (await w['member'].get(base)).json()['revision']
    assert before['policy_revision'] == current
    changed = await w['owner'].post(base + '/policy', json={
        'revision': current, 'concurrent': 2, 'daily_launches': 7,
    })
    assert changed.status_code == 200, changed.text
    after = (await w['member'].get(base + '/board')).json()
    assert after['policy_revision'] == current + 1
    assert after['usage']['limits'] == {'concurrent': 2, 'daily_launches': 7}


@pytest.mark.parametrize('role', ['owner', 'maintainer', 'member', 'viewer', 'nonmember', 'removed'])
async def test_board_policy_permission_matrix(grouped, role):
    w = grouped
    actor = w['owner'] if role == 'owner' else w['member']
    with write_db(w['owner'].app.state.db) as s:
        held = s.get(GroupMembership, (w['group']['id'], 2))
        if role in ('nonmember', 'removed'):
            s.delete(held)
        elif role != 'owner':
            held.role = role
        s.commit()
    base = f"/api/groups/{w['group']['id']}"
    readable = role not in ('nonmember', 'removed')
    for path in ('/board', '/usage', '/operations'):
        assert (await actor.get(base + path)).status_code == (200 if readable else 404)
    changed = await actor.post(base + '/policy', json={'revision': 2, 'concurrent': 2, 'daily_launches': 5}, headers=csrf(actor))
    assert changed.status_code == (200 if role in ('owner', 'maintainer') else 404), changed.text


async def test_policy_updates_compare_revision(grouped, backend_url, monkeypatch):
    w = grouped
    await pause_supervisor(w['owner'])
    url = f"/api/groups/{w['group']['id']}/policy"
    with guard_database(w['owner'], monkeypatch) as db, transaction_rendezvous(db, 'UPDATE acc_group'):
        results = await asyncio.gather(*(w['owner'].post(url, json={
            'revision': 2, 'concurrent': n, 'daily_launches': 5}) for n in (1, 2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    winner = next(n for n, result in zip((1, 2), results) if result.status_code == 200)
    current = (await w['owner'].get(f"/api/groups/{w['group']['id']}")).json()
    assert current['revision'] == 3
    from acc.server.models import Group
    with w['owner'].app.state.db() as s:
        assert s.get(Group, w['group']['id']).concurrent == winner


def test_claim_paths_are_conservative_and_never_secret_paths():
    from acc.server.group_board import overlap, safe_path
    assert overlap('src/api/', 'SRC/api/routes.py')
    assert overlap('src/api', 'src/api/')
    assert not overlap('src/api/', 'src/apis/routes.py')
    for path in ('/home/alex/file', '../x', 'src/../x', '.env', '.git/config', 'keys/token', '.claude/.credentials.json', '.codex/auth.json', '.npmrc', 'server.pem', 'session.env', 'src\\x', 'a//b', 'a\nfile'):
        with pytest.raises(ValueError):
            safe_path(path)


@pytest.fixture
async def working(grouped):
    from acc.server.models import GroupCheckout, GroupWork, Placement, Session, User
    w = grouped
    with write_db(w['owner'].app.state.db) as s:
        session = s.get(Session, w['session']['id'])
        session.project_id = w['group_project']['id']
        session.owner = 'alex'
        session.run_as = s.query(User).filter_by(username='alex').one().account  # alex's own agent account
        binding = s.get(Placement, session.placement_id)
        binding.project_id, binding.person, binding.account = session.project_id, 'alex', session.run_as
        s.add(GroupCheckout(id=session.id, placement_id=binding.id, base_sha='a' * 40))
        s.flush()
        s.add(GroupWork(session_id=session.id, checkout_id=session.id, task='Review validation', base_sha='a' * 40))
        s.commit()
    return w


async def test_claims_task_context_and_racing_requests(working, backend_url, monkeypatch):
    w = working
    await pause_supervisor(w['owner'])
    base = f"/api/groups/{w['group']['id']}"
    url = base + f"/work/{w['session']['id']}"
    actor = w['member']
    body = {'path': 'src/api/'}
    with guard_database(w['owner'], monkeypatch) as db, transaction_rendezvous(db, 'UPDATE acc_group'):
        results = await asyncio.gather(*(actor.post(url + '/claim', json=body, headers=csrf(actor)) for _ in range(2)))
    assert [r.status_code for r in results] == [200, 200]
    board = (await actor.get(base + '/board')).json()
    assert len(board['rows'][0]['claims']) == 1
    assert 'private' not in (await actor.get(url + '/context')).text.lower()
    updated = await actor.post(url + '/task', json={'task': 'Check validation tests'}, headers=csrf(actor))
    assert updated.status_code == 200, updated.text
    assert (await actor.post(url + '/task', json={'task': 'token=abc'}, headers=csrf(actor))).status_code == 422
    released = await actor.post(url + '/release', json=body, headers=csrf(actor))
    assert released.status_code == 200
    assert (await actor.get(base + '/board')).json()['rows'][0]['claims'] == []


async def test_removed_members_cannot_use_existing_board_session(working):
    w = working
    with write_db(w['owner'].app.state.db) as s:
        s.delete(s.get(GroupMembership, (w['group']['id'], 2)))
        s.commit()
    url = f"/api/groups/{w['group']['id']}/work/{w['session']['id']}"
    for suffix, body in [('/claim', {'path': 'src/'}), ('/release', {'path': 'src/'}), ('/task', {'task': 'Safe task'})]:
        assert (await w['member'].post(url + suffix, json=body, headers=csrf(w['member']))).status_code == 404
    assert (await w['member'].get(url + '/context')).status_code == 404


@pytest.mark.parametrize('role', ['owner', 'maintainer', 'member', 'viewer', 'nonmember', 'removed'])
async def test_work_routes_are_self_only_and_require_current_developer_role(working, role):
    w = working
    actor = w['owner'] if role == 'owner' else w['member']
    with write_db(w['owner'].app.state.db) as s:
        held = s.get(GroupMembership, (w['group']['id'], 2))
        if role in ('nonmember', 'removed'):
            s.delete(held)
        elif role != 'owner':
            held.role = role
        s.commit()
    base = f"/api/groups/{w['group']['id']}"
    url = base + f"/work/{w['session']['id']}"
    allowed = role in ('owner', 'maintainer', 'member')
    assert (await actor.get(url + '/context')).status_code == (200 if allowed else 404)
    for suffix, body in [('/task', {'task': 'Review tests'}), ('/claim', {'path': 'src/'}), ('/release', {'path': 'src/'})]:
        result = await actor.post(url + suffix, json=body, headers=csrf(actor))
        assert result.status_code == (200 if allowed else 404), result.text


async def test_usage_keeps_unknown_sessions_unknown_and_deduplicates_observations(working):
    from acc.server.models import Observation
    w = working
    with write_db(w['owner'].app.state.db) as s:
        s.add(Observation(key=f"claude.statusline.{w['session']['id']}", value={'cost': {'total_cost_usd': 1.25}}, source='statusline', observed_at=100))
        s.commit()
    data = (await w['member'].get(f"/api/groups/{w['group']['id']}/usage")).json()
    assert data['cost'] == 1.25
    assert data['coverage'] == 'complete'
    assert data['units'] == 'USD'
    assert data['ledger'][0]['person'] == 'alex'


async def test_claim_overlap_requires_confirmation_and_deduplicates_operations(working, backend_url, monkeypatch):
    from datetime import timedelta

    from acc.server.models import GroupCheckout, GroupClaim, GroupWork, Session, utcnow
    w = working
    await pause_supervisor(w['owner'])
    with write_db(w['owner'].app.state.db) as s:
        first = s.get(Session, w['session']['id'])
        other = Session(id='eeeeeeeeeeee', project_id=first.project_id, provider=first.provider, name='Other',
            tmux_name='other-board-test', launch_spec_id=first.launch_spec_id, owner='alex', run_as=first.run_as,
            placement_id=first.placement_id, desired_state='running', observed_state='working')
        s.add(other)
        s.add(GroupCheckout(id=other.id, placement_id=first.placement_id, base_sha='a' * 40))
        s.flush()
        s.add(GroupWork(session_id=other.id, checkout_id=other.id, task='Review tests', base_sha='a' * 40))
        s.add(GroupClaim(session_id=other.id, path='SRC/api/', expires_at=utcnow() + timedelta(minutes=15)))
        s.commit()
    base = f"/api/groups/{w['group']['id']}"
    url = base + f"/work/{w['session']['id']}/claim"
    actor = w['member']
    refused = await actor.post(url, json={'path': 'src/api/test.py'}, headers=csrf(actor))
    assert refused.status_code == 200 and refused.json()['status'] == 'conflict'
    for _ in range(2):
        accepted = await actor.post(url, json={'path': 'src/api/test.py', 'shared': True}, headers=csrf(actor))
        assert accepted.json()['status'] == 'shared'
    assert len((await actor.get(base + '/operations')).json()) == 1
    with guard_database(w['owner'], monkeypatch) as db, transaction_rendezvous(db, 'UPDATE acc_group'):
        races = await asyncio.gather(*(actor.post(base + f'/work/{sid}/claim', json={
            'path': 'tests/'}, headers=csrf(actor)) for sid in (w['session']['id'], 'eeeeeeeeeeee')))
    assert sorted(response.json()['status'] for response in races) == ['accepted', 'conflict']


def test_board_attachment_refuses_symlink_run_folder_and_never_erases_keys(tmp_path):
    from acc.broker.group_git import refresh_context
    root = tmp_path / 'run'
    folder = root / 'aabbccddeeff'
    folder.mkdir(parents=True)
    (folder / 'session.env').write_text('private-key-canary')
    config = isolated_broker(tmp_path, run_dir=root)
    refresh_context(config, 'aabbccddeeff', '{"revision":1}')
    assert (folder / 'group-context.json').read_text() == '{"revision":1}'
    assert (folder / 'session.env').read_text() == 'private-key-canary'
    (folder / 'group-context.json').unlink()
    (folder / 'group-context.json').symlink_to(folder / 'session.env')
    refresh_context(config, 'aabbccddeeff', '{"revision":2}')
    assert (folder / 'session.env').read_text() == 'private-key-canary'
    with pytest.raises(ValueError):
        refresh_context(config, '../other', '{}')


async def test_group_key_use_requires_consent_attachment_and_current_membership(grouped):
    from acc.server import vault
    from acc.server.models import GroupKeyGrant, VaultGrant, VaultKey
    w = grouped
    db = w['owner'].app.state.db
    with write_db(db) as s:
        s.add(VaultKey(id='demo-key', name='Private key canary', variable='DEMO_KEY', ciphertext='sealed', owner='casey'))
        s.flush()
        s.add(VaultGrant(key_id='demo-key', project_id=w['group_project']['id'], given_by='casey'))
        s.commit()
    with db() as s:
        assert vault.plan(s, w['group_project']['id'], 'alex', False).ids == []
    with write_db(db) as s:
        s.add(GroupKeyGrant(key_id='demo-key', group_id=w['group']['id'], project_id=w['group_project']['id'], host='local', given_by='casey'))
        s.commit()
    with db() as s:
        assert vault.plan(s, w['group_project']['id'], 'alex', False).ids == ['demo-key']
    with write_db(db) as s:
        s.get(GroupMembership, (w['group']['id'], 2)).role = 'viewer'
        s.commit()
    with db() as s:
        with pytest.raises(vault.VaultError):
            vault.plan(s, w['group_project']['id'], 'alex', False)


@pytest.mark.parametrize('role', ['owner', 'maintainer', 'member', 'viewer', 'nonmember', 'removed'])
async def test_key_attachment_and_operation_resolution_permission_matrix(grouped, role):
    w = grouped
    actor = w['owner'] if role == 'owner' else w['member']
    from acc.server.models import GroupKeyGrant, GroupOperation, VaultKey
    with write_db(w['owner'].app.state.db) as s:
        held = s.get(GroupMembership, (w['group']['id'], 2))
        if role in ('nonmember', 'removed'):
            s.delete(held)
        elif role != 'owner':
            held.role = role
        s.add(VaultKey(id='demo-key', name='private canary', variable='DEMO_KEY', ciphertext='sealed', owner='casey'))
        s.flush()
        s.add(GroupKeyGrant(key_id='demo-key', group_id=w['group']['id'], host='local', given_by='casey'))
        s.add(GroupOperation(id='example-operation', group_id=w['group']['id'], kind='quota', summary='Review launch quota'))
        s.commit()
    base = f"/api/groups/{w['group']['id']}"
    keys = base + f"/projects/{w['group_project']['id']}/keys"
    readable = role not in ('nonmember', 'removed')
    assert (await actor.get(keys)).status_code == (200 if readable else 404)
    admin = role in ('owner', 'maintainer')
    update = await actor.post(base + '/operations/example-operation/update', json={'revision': 1, 'action': 'assign', 'user_id': 2}, headers=csrf(actor))
    assert update.status_code == (200 if admin else 404), update.text
    for path in (keys + '/demo-key/attach', base + '/operations/example-operation/resolve'):
        result = await actor.post(path, headers=csrf(actor))
        assert result.status_code == (200 if admin else 404), result.text


async def test_members_cannot_raise_owner_quota_ceilings(grouped):
    w = grouped
    base = f"/api/groups/{w['group']['id']}"
    response = await w['member'].post(base + '/policy', json={'revision': 2, 'concurrent': 100, 'daily_launches': 10000}, headers=csrf(w['member']))
    assert response.status_code == 403
    allowed = await w['owner'].post(base + '/policy', json={'revision': 2, 'concurrent': 100, 'daily_launches': 10000})
    assert allowed.status_code == 200


async def test_session_spool_work_actions_are_bound_deduplicated_and_revoked(working):
    w = working
    handler = w['owner'].app.state.ctx.supervisor.group_work_request
    sid = w['session']['id']
    request = {'type': 'work', 'action': 'task', 'text': 'Review API contracts', 'request_id': 'a' * 32}
    reply = handler(sid, request)
    assert reply['status'] == 'accepted'
    assert handler(sid, request) == reply
    assert handler(sid, {**request, 'request_id': 'b' * 32, 'session_id': 'other'})['status'] == 'refused'
    with write_db(w['owner'].app.state.db) as s:
        s.delete(s.get(GroupMembership, (w['group']['id'], 2)))
        s.commit()
    assert handler(sid, {**request, 'request_id': 'c' * 32})['status'] == 'refused'


async def test_viewers_see_tasks_but_no_claim_paths_or_path_notifications(working):
    from datetime import timedelta

    from acc.server.models import GroupClaim, GroupOperation, utcnow
    w = working
    with write_db(w['owner'].app.state.db) as s:
        s.add(GroupClaim(session_id=w['session']['id'], path='src/private-summary.py', expires_at=utcnow() + timedelta(minutes=15)))
        s.add(GroupOperation(id='example-path-alert', group_id=w['group']['id'], kind='overlap', summary='Overlapping claims: src/private-summary.py'))
        s.get(GroupMembership, (w['group']['id'], 2)).role = 'viewer'
        s.commit()
    base = f"/api/groups/{w['group']['id']}"
    board = await w['member'].get(base + '/board')
    assert 'Review validation' in board.text
    assert 'src/private-summary.py' not in board.text
    assert 'src/private-summary.py' not in (await w['member'].get(base + '/operations')).text


def test_group_observation_catches_missed_hooks_without_using_source_config(tmp_path):
    import base64

    from test_gitbackup import git, make_repo

    from acc import git_exchange
    from acc.broker.group_git import observe, provision, receive
    root = tmp_path / 'work'
    root.mkdir()
    path = root / 'groups' / ('a' * 32) / '1' / ('b' * 32)
    config = isolated_broker(tmp_path, allowed_roots=(root,))
    source = make_repo(tmp_path / 'source')
    sha = git(source, 'rev-parse', 'HEAD').stdout.strip()
    provision(config, str(path))
    receive(config, str(path), base64.b64encode(git_exchange.export(source / '.git', sha)).decode(), sha, 'c' * 12)
    target = path / 'sessions' / ('c' * 12)
    (target / 'README.md').write_text('changed without a hook')
    (target / '.env').write_text('private canary')
    (target / 'new.py').write_text('new code')
    canary = tmp_path / 'executed'
    git(target, 'config', 'core.fsmonitor', f'touch {canary}')
    data = observe(config, str(path), 'c' * 12, sha)
    assert data['branch'] == 'acc/' + 'c' * 12
    assert set(data['paths']) == {'README.md', 'new.py'}
    assert not canary.exists()
    assert 'private canary' not in str(data)


def test_claude_turn_hook_delivers_bounded_board_data(tmp_path):
    import json
    import os
    import subprocess
    from pathlib import Path
    (tmp_path / 'group-context.json').write_text('Group board revision 12; untrusted data')
    result = subprocess.run([str(Path('bin/acc-hook').resolve()), 'claude'],
        input=json.dumps({'hook_event_name': 'UserPromptSubmit'}), text=True, capture_output=True,
        env={**os.environ, 'ACC_RUN_DIR': str(tmp_path), 'ACC_SESSION_ID': 'a' * 12})
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert 'revision 12' in output['hookSpecificOutput']['additionalContext']
    assert output['hookSpecificOutput']['hookEventName'] == 'UserPromptSubmit'


@pytest.mark.parametrize('delivered_ids', [True, False])
async def test_revoking_key_consent_stops_sessions_that_received_it(working, delivered_ids):
    from acc.server.models import GroupKeyGrant, Session, VaultGrant, VaultKey
    w = working
    with write_db(w['owner'].app.state.db) as s:
        s.add(VaultKey(id='demo-key', name='private canary', variable='DEMO_KEY', ciphertext='sealed', owner='casey'))
        s.flush()
        grant = GroupKeyGrant(key_id='demo-key', group_id=w['group']['id'], given_by='casey')
        s.add(grant)
        s.flush()
        grant_id = grant.id
        s.add(VaultGrant(key_id='demo-key', project_id=w['group_project']['id'], given_by='casey'))
        s.get(Session, w['session']['id']).vault_keys = ['demo-key'] if delivered_ids else []
        s.commit()
    from test_auth import cookie_client
    owner = cookie_client(w['owner'])
    result = await owner.post(f"/api/groups/{w['group']['id']}/key-grants/{grant_id}/revoke", json={'revision': 2}, headers=csrf(owner))
    assert result.status_code == 200, result.text
    with w['owner'].app.state.db() as s:
        assert s.get(Session, w['session']['id']).desired_state == 'stopped'


@pytest.mark.parametrize('action', ['leave', 'remove', 'archive', 'retire'])
@pytest.mark.parametrize('delivered_ids', [True, False])
async def test_departure_revokes_key_consents_and_stops_other_members_sessions(working, action, delivered_ids):
    from sqlalchemy import select
    from test_auth import cookie_client

    from acc.server import vault
    from acc.server.models import AuditLog, Group, GroupKeyGrant, Session, VaultGrant, VaultKey
    w = working
    owner, maintainer = cookie_client(w['owner']), w['member']
    owner.headers.update(csrf(owner))
    group_id, project_id = w['group']['id'], w['group_project']['id']
    base = f'/api/groups/{group_id}'
    # Casey supplies keys to Alex's running session, not Casey's own session.
    assert (await owner.post(base + '/members/1', json={'role': 'member', 'revision': 2})).status_code == 200
    with write_db(owner.app.state.db) as s:
        for key_id, scope in [('departing-key', None), ('scoped-key', project_id)]:
            s.add(VaultKey(id=key_id, name='Synthetic key', variable=key_id.replace('-', '_').upper(),
                           ciphertext='sealed', owner='casey'))
            s.flush()
            s.add(GroupKeyGrant(key_id=key_id, group_id=group_id, project_id=scope, host='local', given_by='casey'))
            s.add(VaultGrant(key_id=key_id, project_id=project_id, given_by='casey'))
        s.add(VaultKey(id='remaining-key', name='Other synthetic key', variable='OTHER_KEY',
                       ciphertext='sealed', owner='alex'))
        s.flush()
        s.add(GroupKeyGrant(key_id='remaining-key', group_id=group_id, given_by='alex'))
        s.add(Group(id='unrelated-group', name='Other synthetic group', created_by='casey'))
        s.flush()
        s.add(GroupKeyGrant(key_id='departing-key', group_id='unrelated-group', given_by='casey'))
        s.get(Session, w['session']['id']).vault_keys = ['departing-key', 'scoped-key'] if delivered_ids else []
        s.commit()
    # A refused stale change must not revoke any consent.
    stale = await owner.post(base + '/archive', json={'revision': 2})
    assert stale.status_code == 409
    if action == 'archive':
        response = await owner.post(base + '/archive', json={'revision': 3})
    elif action == 'retire':
        # Retire a non-owner key supplier using Alex as an instance owner.
        from acc.server.models import User
        with write_db(owner.app.state.db) as s:
            s.get(User, 1).role = 'member'
            s.get(User, 2).role = 'owner'
            s.commit()
        response = await maintainer.post('/api/users/1/remove', json={'resources': 'archive'}, headers=csrf(maintainer))
    else:
        actor = owner if action == 'leave' else maintainer
        response = await actor.post(base + '/members/1', json={'role': 'remove', 'revision': 3}, headers=csrf(actor))
    assert response.status_code == 200, response.text
    with owner.app.state.db() as s:
        remaining = list(s.scalars(select(GroupKeyGrant.key_id).where(GroupKeyGrant.group_id == group_id)))
        assert remaining == ([] if action == 'archive' else ['remaining-key'])
        session = s.get(Session, w['session']['id'])
        assert session.desired_state == session.observed_state == 'stopped'
        assert not session.team_stop_pending
        receipts = list(s.scalars(select(AuditLog).where(AuditLog.target == group_id,
                                                        AuditLog.action == 'group.key_revoke')))
        assert {row.detail['key_id'] for row in receipts} == (
            {'departing-key', 'scoped-key', 'remaining-key'} if action == 'archive'
            else {'departing-key', 'scoped-key'})
        assert all(row.detail['grant_id'] and row.detail['reason'] for row in receipts)
        assert all(row.detail['revision'] == 4 for row in receipts)
        if action != 'retire':
            assert list(s.scalars(select(GroupKeyGrant.key_id).where(
                GroupKeyGrant.group_id == 'unrelated-group'))) == ['departing-key']
    if action not in ('archive', 'retire'):
        rejoined = await maintainer.post(base + '/members/1', json={'role': 'member', 'revision': 4}, headers=csrf(maintainer))
        assert rejoined.status_code == 200, rejoined.text
        with owner.app.state.db() as s:
            assert vault.plan(s, project_id, 'alex', False).ids == []
        fresh = await owner.post(base + '/key-grants', json={'revision': 5, 'key_id': 'departing-key'}, headers=csrf(owner))
        assert fresh.status_code == 201, fresh.text
        with owner.app.state.db() as s:
            assert vault.plan(s, project_id, 'alex', False).ids == ['departing-key']
            assert s.get(Group, group_id).revision == 6


async def test_member_can_leave_and_revoke_their_consent(grouped):
    from acc.server.models import GroupKeyGrant, VaultKey
    w = grouped
    base = f"/api/groups/{w['group']['id']}"
    assert (await w['owner'].post(base + '/members/1', json={'role': 'maintainer', 'revision': 2})).status_code == 200
    assert (await w['owner'].post(base + '/projects/' + str(w['group_project']['id']) + '/owners',
                                json={'owners': [1], 'revision': 3})).status_code == 200
    assert (await w['owner'].post(base + '/members/2', json={'role': 'member', 'revision': 4})).status_code == 200
    with write_db(w['owner'].app.state.db) as s:
        s.add(VaultKey(id='leaving-key', name='Synthetic key', variable='LEAVING_KEY', ciphertext='sealed', owner='alex'))
        s.flush()
        s.add(GroupKeyGrant(key_id='leaving-key', group_id=w['group']['id'], given_by='alex'))
        s.commit()
    for user_id, role in [(2, 'maintainer'), (1, 'remove')]:
        denied = await w['member'].post(base + f'/members/{user_id}', json={'role': role, 'revision': 5},
                                       headers=csrf(w['member']))
        assert denied.status_code == 404
    response = await w['member'].post(base + '/members/2', json={'role': 'remove', 'revision': 5}, headers=csrf(w['member']))
    assert response.status_code == 200, response.text
    assert (await w['member'].get(base)).status_code == 404
    assert (await w['member'].get('/api/groups/key-grants/owned')).json() == []


async def test_failed_archive_rolls_back_consent_stop_intent_and_audit(working, monkeypatch):
    from sqlalchemy import select

    from acc.server.models import AuditLog, Group, GroupKeyGrant, Session, VaultGrant, VaultKey
    w = working
    group_id = w['group']['id']
    with write_db(w['owner'].app.state.db) as s:
        s.add(VaultKey(id='atomic-key', name='Synthetic key', variable='ATOMIC_KEY', ciphertext='sealed', owner='casey'))
        s.flush()
        grant = GroupKeyGrant(key_id='atomic-key', group_id=group_id, given_by='casey')
        s.add(grant)
        s.add(VaultGrant(key_id='atomic-key', project_id=w['group_project']['id'], given_by='casey'))
        s.commit()
        grant_id = grant.id

    def fail_state(*args):
        raise RuntimeError('injected state-write failure')

    monkeypatch.setattr(w['owner'].app.state.ctx.supervisor, '_set_state', fail_state)
    with pytest.raises(RuntimeError, match='injected state-write failure'):
        await w['owner'].post(f'/api/groups/{group_id}/archive', json={'revision': 2})
    with w['owner'].app.state.db() as s:
        group = s.get(Group, group_id)
        assert not group.archived and group.revision == 2
        assert s.get(GroupKeyGrant, grant_id) is not None
        assert s.get(Session, w['session']['id']).desired_state == 'running'
        assert s.scalar(select(AuditLog.id).where(
            AuditLog.target == group_id, AuditLog.action == 'group.key_revoke')) is None


async def test_operations_ack_and_assignment_are_revision_fenced(grouped):
    from acc.server.models import GroupOperation
    w = grouped
    with write_db(w['owner'].app.state.db) as s:
        s.add(GroupOperation(id='example-operation', group_id=w['group']['id'], kind='quota', summary='Review launch quota'))
        s.commit()
    base = f"/api/groups/{w['group']['id']}"
    url = base + '/operations/example-operation/update'
    result = await w['member'].post(url, json={'revision': 1, 'action': 'assign', 'user_id': 2}, headers=csrf(w['member']))
    assert result.status_code == 200, result.text
    stale = await w['member'].post(url, json={'revision': 1, 'action': 'resolve'}, headers=csrf(w['member']))
    assert stale.status_code == 409
    rows = (await w['member'].get(base + '/operations')).json()
    assert rows[0]['revision'] == 2 and rows[0]['assignee'] == 'alex'


async def test_usage_warning_notifications_are_deduplicated_and_group_scoped(working):
    from sqlalchemy import select

    from acc.server import group_board
    from acc.server.models import AttentionItem, Group, Observation
    w = working
    with write_db(w['owner'].app.state.db) as s:
        group = s.get(Group, w['group']['id'])
        group.usage_warning_usd = 1
        s.add(Observation(key=f"claude.statusline.{w['session']['id']}", value={'cost': {'total_cost_usd': 2}}, source='statusline', observed_at=100))
        s.flush()
        group_board.maintain(s, group)
        group_board.maintain(s, group)
        s.commit()
        notices = list(s.scalars(select(AttentionItem).where(AttentionItem.source == 'group_board')))
        assert len(notices) == 1
        assert notices[0].owner == 'alex'
        assert notices[0].detail == 'Open Groups → Work board → Group operations for current details.'
    data = (await w['member'].get(f"/api/groups/{w['group']['id']}/usage")).json()
    assert data['usage_warning_reached']


async def test_expired_claims_are_not_renewed_or_reported_as_conflicts(working):
    from datetime import timedelta

    from acc.server.models import GroupClaim, utcnow
    w = working
    with write_db(w['owner'].app.state.db) as s:
        s.add(GroupClaim(session_id=w['session']['id'], path='src/', expires_at=utcnow() - timedelta(minutes=1)))
        s.commit()
    base = f"/api/groups/{w['group']['id']}"
    assert (await w['member'].get(base + '/board')).json()['rows'][0]['claims'] == []
    result = await w['member'].post(base + f"/work/{w['session']['id']}/claim", json={'path': 'src/'}, headers=csrf(w['member']))
    assert result.json()['status'] == 'accepted'


async def test_observed_touches_filter_protected_paths_and_notify_both_people(working):
    from sqlalchemy import select

    from acc.server import group_board
    from acc.server.models import GroupCheckout, GroupOperation, GroupWork, Observation, Session, utcnow
    w = working
    with write_db(w['owner'].app.state.db) as s:
        first = s.get(Session, w['session']['id'])
        other = Session(id='eeeeeeeeeeee', project_id=first.project_id, provider=first.provider, name='Other',
            tmux_name='other-observation-test', launch_spec_id=first.launch_spec_id, owner='alex', run_as=first.run_as,
            placement_id=first.placement_id, desired_state='running', observed_state='working')
        s.add(other)
        s.add(GroupCheckout(id=other.id, placement_id=first.placement_id, base_sha='a' * 40))
        s.flush()
        s.add(GroupWork(session_id=other.id, checkout_id=other.id, task='Review tests', base_sha='a' * 40))
        s.add(Observation(key='group.paths.' + other.id, source='broker_git', observed_at=utcnow().timestamp(), value={'paths': ['src/api.py']}))
        s.flush()
        for _ in range(2):
            group_board.observe_work(s, first, {'branch': 'acc/demo', 'paths': ['src/api.py', '.env', '/home/alex/private', 'keys/token']})
        s.commit()
        assert s.get(Observation, 'group.paths.' + first.id).value['paths'] == ['src/api.py']
        assert len(list(s.scalars(select(GroupOperation)))) == 1


async def test_group_push_recipient_is_rechecked_after_removal(working, monkeypatch):
    from unittest.mock import AsyncMock

    from acc.server.models import AttentionItem
    w = working
    supervisor = w['owner'].app.state.ctx.supervisor
    with write_db(w['owner'].app.state.db) as s:
        s.add(AttentionItem(dedupe_key='group-overlap:example:' + w['session']['id'], session_id=w['session']['id'],
            kind='group_conflict', source='group_board', severity='warn', owner='alex', title='Review overlap'))
        s.delete(s.get(GroupMembership, (w['group']['id'], 2)))
        s.commit()
    notify = AsyncMock()
    monkeypatch.setattr(supervisor, 'notify', notify)
    supervisor.push = object()
    supervisor._push_cursor = 0
    await supervisor.push_attention()
    assert not any(call.kwargs.get('username') == 'alex' for call in notify.call_args_list)


async def test_archiving_one_project_does_not_stop_another_group_project(working):
    from acc.server.group_authority import revoke_execution
    from acc.server.models import Project, Session
    w = working
    with write_db(w['owner'].app.state.db) as s:
        first = s.get(Session, w['session']['id'])
        other_project = Project(group_id=w['group']['id'], name='Other group project', slug='other-group-project', workspace_path='', account='')
        s.add(other_project)
        s.flush()
        other = Session(id='eeeeeeeeeeee', project_id=other_project.id, provider=first.provider, name='Other',
            tmux_name='other-archive-test', launch_spec_id=first.launch_spec_id, owner='alex', run_as=first.run_as,
            placement_id=first.placement_id, desired_state='running', observed_state='working')
        s.add(other)
        s.flush()
        revoke_execution(s, w['group']['id'], project_id=first.project_id)
        assert first.desired_state == 'stopped'
        assert first.team_stop_pending
        assert other.desired_state == 'running'


async def test_shared_task_redacts_key_values_without_exposing_private_key_names(working, monkeypatch):
    from acc.server.vault import Redactor
    w = working
    monkeypatch.setattr(w['owner'].app.state.ctx.supervisor.vault, 'redactor', lambda *args: Redactor([('opaquevaluecanary', 'Private key name canary')]))
    base = f"/api/groups/{w['group']['id']}"
    result = await w['member'].post(base + f"/work/{w['session']['id']}/task", json={'task': 'Review opaquevaluecanary'}, headers=csrf(w['member']))
    assert result.status_code == 200
    board = (await w['member'].get(base + '/board')).text
    assert 'opaquevaluecanary' not in board
    assert 'Private key name canary' not in board


async def test_conflict_acknowledgment_does_not_resolve_or_ack_for_another_person(working):
    import hashlib

    from acc.server.models import GroupOperation, Observation
    w = working
    with write_db(w['owner'].app.state.db) as s:
        s.add(GroupOperation(id='example-conflict', group_id=w['group']['id'], kind='overlap', summary='Review overlap'))
        s.add(Observation(key='group.op.' + hashlib.sha256(b'example-conflict').hexdigest()[:32], source='group_board', observed_at=100,
            value={'revision': 1, 'affected': ['alex', 'casey'], 'acknowledged': [], 'assignee': None}))
        s.commit()
    url = f"/api/groups/{w['group']['id']}/operations/example-conflict/update"
    result = await w['member'].post(url, json={'revision': 1, 'action': 'ack'}, headers=csrf(w['member']))
    assert result.status_code == 200, result.text
    assert result.json()['acknowledged'] == ['alex']
    rows = (await w['member'].get(f"/api/groups/{w['group']['id']}/operations")).json()
    assert not rows[0]['resolved']
    assert rows[0]['acknowledged'] == ['alex']


async def test_usage_warnings_are_evaluated_after_the_last_group_session_stops(working):
    from sqlalchemy import select

    from acc.server.models import AttentionItem, Group, Observation, Session
    w = working
    with write_db(w['owner'].app.state.db) as s:
        s.get(Group, w['group']['id']).usage_warning_usd = 1
        s.get(Session, w['session']['id']).desired_state = 'stopped'
        s.add(Observation(key=f"claude.statusline.{w['session']['id']}", value={'cost': {'total_cost_usd': 2}}, source='statusline', observed_at=100))
        s.commit()
    await w['owner'].app.state.ctx.supervisor.evaluate_budgets()
    with w['owner'].app.state.db() as s:
        assert s.scalar(select(AttentionItem.id).where(AttentionItem.source == 'group_board', AttentionItem.owner == 'alex'))


def test_protected_request_reply_cannot_overwrite_session_keys(tmp_path):
    from acc.request_replies import publish
    root = tmp_path / 'protected'
    root.mkdir(mode=0o711)
    folder = root / 'aabbccddeeff'
    folder.mkdir(mode=0o711)
    secret = tmp_path / 'session.env'
    secret.write_text('canary')
    rid = 'a' * 32
    (folder / (rid + '.json')).symlink_to(secret)
    publish(root, 'aabbccddeeff', {rid: {'status': 'accepted', 'approval_id': 'pub-1'}})
    assert secret.read_text() == 'canary'
    assert not (folder / (rid + '.json')).is_symlink()
    with pytest.raises(ValueError):
        publish(root, '../escape', {})
    folder.rename(root / 'other')
    folder.symlink_to(root / 'other', target_is_directory=True)
    with pytest.raises(OSError):
        publish(root, 'aabbccddeeff', {})
