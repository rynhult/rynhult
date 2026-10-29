"""Own-machine authority stays scoped to an approved person/node/account tuple."""
# ruff: noqa: F811
from dataclasses import replace

import pytest
from helpers import write_db
from test_auth import live  # noqa: F401
from test_groups import grouped, two  # noqa: F401
from test_users import csrf


async def test_remote_group_routes_are_off_by_default(grouped):
    member = grouped['member']
    response = await member.get('/api/group-machines')
    assert response.status_code == 404


@pytest.fixture
async def remote_case(env, grouped):
    settings = replace(env['settings'], groups_remote_enabled=True)
    grouped['owner'].headers.pop('Authorization', None)
    grouped['owner'].headers.update(csrf(grouped['owner']))
    ctx = grouped['owner'].app.state.ctx
    ctx.settings = settings
    ctx.supervisor.settings = settings
    return grouped


async def test_machine_account_request_needs_owner_approval_and_never_self_approves(remote_case):
    from acc.server.models import Node
    case = remote_case
    with write_db(case['owner'].app.state.db) as s:
        s.add(Node(id='a' * 32, name='alex-box', status='active', created_by='casey'))
        s.commit()
    member = case['member']
    response = await member.post('/api/group-machines', json={'node_id': 'a' * 32, 'account': 'agent'}, headers=csrf(member))
    assert response.status_code == 201, response.text
    binding = response.json()
    assert binding['person'] == 'alex' and binding['status'] == 'pending'
    denied = await member.post(f"/api/group-machines/{binding['id']}/approve", headers=csrf(member))
    assert denied.status_code == 403
    assert not (await member.get('/api/group-machines')).json()[0]['approved_by']


@pytest.fixture
async def machines(remote_case, live, env, tmp_path):
    import asyncio

    from helpers import wait_for
    from test_multihost import StandInNode, enroll

    from acc.broker.server import serve
    case = remote_case
    ctx = case['owner'].app.state.ctx
    tasks = []
    boxes = {}
    try:
        for name in ('alex-box', 'casey-box'):
            root = tmp_path / name
            root.mkdir()
            work = root / 'work'
            work.mkdir()
            config = replace(env['broker'], socket_path=root / 'b.sock', run_dir=root / 'run',
                             allowed_roots=(work,), tmux_socket=env['broker'].tmux_socket + name)
            config.run_dir.mkdir()
            tasks.append(asyncio.create_task(serve(config)))
            await wait_for(lambda: asyncio.sleep(0, result=config.socket_path.exists()))
            key, node_id = await enroll(case['owner'], name)
            node = StandInNode(config.socket_path, key, node_id)
            task = asyncio.create_task(node.run(live))
            tasks.append(task)
            await wait_for(lambda: asyncio.sleep(0, result=name in ctx.broker.hub.links))
            boxes[name] = {'config': config, 'node': node, 'id': node_id, 'task': task, 'root': root}
        yield {**case, 'boxes': boxes, 'live': live, 'tasks': tasks,
               'base': f"/api/groups/{case['group']['id']}/projects/{case['group_project']['id']}"}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def revision(case):
    return (await case['owner'].get(f"/api/groups/{case['group']['id']}")).json()['revision']


async def bind(case, client, host):
    requested = await client.post('/api/group-machines', json={'node_id': case['boxes'][host]['id'], 'account': 'agent'}, headers=csrf(client))
    assert requested.status_code == 201, requested.text
    binding = requested.json()
    proof, _ = await prove(case, client, binding, host=host)
    assert proof.status_code == 200, proof.text
    approved = await case['owner'].post(f"/api/group-machines/{binding['id']}/approve")
    assert approved.status_code == 200, approved.text
    requested = await client.post(case['base'] + '/remote-placements', json={
        'binding_id': binding['id'], 'revision': await revision(case)}, headers=csrf(client))
    assert requested.status_code == 201, requested.text
    placement = requested.json()['id']
    approved = await case['member'].post(case['base'] + f'/remote-placements/{placement}/approve',
        json={'revision': await revision(case)}, headers=csrf(case['member']))
    assert approved.status_code == 200, approved.text
    return binding['id'], placement


async def seed(case, tmp_path):
    import base64

    from test_gitbackup import git, make_repo

    from acc import git_exchange
    source = make_repo(tmp_path / 'source')
    sha = git(source, 'rev-parse', 'HEAD').stdout.strip()
    grant = await case['member'].post(case['base'] + '/exchange/grants', json={
        'operation': 'fetch', 'session_id': 'a' * 12, 'old_sha': '0' * 40, 'new_sha': sha}, headers=csrf(case['member']))
    # Seed as explicit publication through a reviewed placement, using the normal transfer boundary.
    # The member's remote placement is already approved by bind().
    placement = (await case['member'].get(case['base'] + '/placements')).json()[0]['id']
    grant = await case['member'].post(case['base'] + '/exchange/grants', json={
        'operation': 'publish', 'placement_id': placement, 'session_id': 'a' * 12,
        'old_sha': '0' * 40, 'new_sha': sha}, headers=csrf(case['member']))
    assert grant.status_code == 201, grant.text
    result = await case['member'].post(case['base'] + '/exchange/transfer', headers={
        'Authorization': 'Bearer ' + grant.json()['token']}, json={
        'data': base64.b64encode(git_exchange.export(source / '.git', sha)).decode()})
    assert result.status_code == 200, result.text
    return sha


async def launch(case, client, placement, sha, sid, provider='claude'):
    checked = await client.post(case['base'] + '/exchange/checkout', json={
        'placement_id': placement, 'session_id': sid, 'commit': sha}, headers=csrf(client))
    assert checked.status_code == 200, checked.text
    result = await client.post(case['base'] + '/sessions', json={
        'checkout_id': sid, 'task': 'Review validation', 'name': 'Own machine', 'trust_source': True, 'provider': provider}, headers=csrf(client))
    assert result.status_code == 201, result.text
    return result


async def test_two_people_two_machines_launch_and_wrong_account_refusal(machines, tmp_path):
    from helpers import wait_for
    from test_supervisor import state_is

    from acc.server.models import Session
    case = machines
    await case['owner'].post(f"/api/groups/{case['group']['id']}/members/1", json={
        'revision': await revision(case), 'role': 'member'})
    _, alex = await bind(case, case['member'], 'alex-box')
    _, casey = await bind(case, case['owner'], 'casey-box')
    sha = await seed(case, tmp_path)
    await launch(case, case['member'], alex, sha, 'a1' * 6)
    await launch(case, case['owner'], casey, sha, 'b2' * 6, provider='codex')
    for sid, host, person in [('a1' * 6, 'alex-box', 'alex'), ('b2' * 6, 'casey-box', 'casey')]:
        try:
            await wait_for(state_is(case['owner'], sid, 'idle'), timeout=20)
        except AssertionError:
            detail = (await case['owner'].get('/api/sessions/' + sid)).json()
            events = (await case['owner'].get('/api/sessions/' + sid + '/events')).json()
            pytest.fail(str({k: detail.get(k) for k in ('last_error', 'observed_state', 'desired_state', 'runs')}) + str([e for e in events if e['kind'] == 'launch_failed']))
        with case['owner'].app.state.db() as s:
            row = s.get(Session, sid)
            assert (row.host, row.run_as, row.owner) == (host, 'agent', person)
            assert row.cwd.startswith(str(case['boxes'][host]['root']))
    denied = await case['member'].post(case['base'] + '/exchange/checkout', json={
        'placement_id': casey, 'session_id': 'c3' * 6, 'commit': sha}, headers=csrf(case['member']))
    assert denied.status_code == 404
    with write_db(case['owner'].app.state.db) as s:
        row = s.get(Session, 'a1' * 6)
        row.run_as = 'wrong-agent'
        s.commit()
    denied = await case['member'].post('/api/sessions/' + 'a1' * 6 + '/resume', headers=csrf(case['member']))
    assert denied.status_code in (404, 409)


@pytest.mark.parametrize('revocation', ['binding', 'placement', 'membership', 'node'])
async def test_partition_revocation_reconnect_stops_without_replay(machines, tmp_path, revocation):
    import asyncio
    from pathlib import Path

    from helpers import wait_for
    from test_multihost import StandInNode

    from acc.server import vault
    from acc.server.models import GroupMachineAccount, Observation, Session, VaultGrant
    case = machines
    binding, placement = await bind(case, case['member'], 'alex-box')
    sha = await seed(case, tmp_path)
    sid = 'c4' * 6
    supervisor = case['owner'].app.state.ctx.supervisor
    with write_db(case['owner'].app.state.db) as s:
        key = vault.create_key(s, supervisor.vault, name='Alex own key', variable='DEMO_KEY',
                               value='own-remote-canary', owner='alex', remote=True)
        s.flush()
        s.add(VaultGrant(key_id=key.id, project_id=case['group_project']['id'], given_by='alex'))
        s.commit()
    await launch(case, case['member'], placement, sha, sid)
    box = case['boxes']['alex-box']
    envfile = box['config'].run_dir / sid / 'session.env'
    await wait_for(lambda: envfile.exists())
    assert 'own-remote-canary' in envfile.read_text()
    box['task'].cancel()
    await asyncio.gather(box['task'], return_exceptions=True)
    hub = supervisor.broker.hub
    await wait_for(lambda: 'alex-box' not in hub.links)
    fresh = await case['member'].post(case['base'] + '/exchange/checkout', json={
        'placement_id': placement, 'session_id': 'e5' * 6, 'commit': sha}, headers=csrf(case['member']))
    assert fresh.status_code in (404, 409), fresh.text
    if revocation == 'binding':
        response = await case['owner'].post(f'/api/group-machines/{binding}/revoke')
    elif revocation == 'placement':
        response = await case['member'].post(case['base'] + f'/remote-placements/{placement}/revoke',
            json={'revision': await revision(case)}, headers=csrf(case['member']))
    elif revocation == 'membership':
        groupbase = f"/api/groups/{case['group']['id']}"
        # Retain a named maintainer before removing Alex.
        await case['owner'].post(groupbase + '/members/1', json={'revision': await revision(case), 'role': 'maintainer'})
        await case['owner'].post(case['base'] + '/owners', json={'revision': await revision(case), 'owners': [1]})
        response = await case['owner'].post(groupbase + '/members/2', json={
            'revision': await revision(case), 'role': 'remove'})
    else:
        response = await case['owner'].post('/api/nodes/alex-box/remove')
    assert response.status_code == 200, response.text
    if revocation == 'node':
        # A revoked enrollment key cannot reconnect. The standard node authentication tests
        # exercise the signed handshake; don't restore its authority to drain sessions here.
        with case['owner'].app.state.db() as s:
            from acc.server.models import Node
            assert s.get(Node, box['id']).status == 'revoked'
            assert s.get(Session, sid).desired_state == 'stopped'
            assert sid in s.get(Observation, 'env_cleanup').value
        from websockets.exceptions import ConnectionClosed
        revoked = StandInNode(box['config'].socket_path, box['node'].key, box['id'])
        with pytest.raises(ConnectionClosed) as refused:
            await asyncio.wait_for(revoked.run(case['live']), 5)
        assert refused.value.rcvd.code == 4401
        return
    with case['owner'].app.state.db() as s:
        row = s.get(Session, sid)
        assert row.desired_state == 'stopped' and row.team_stop_pending
        assert sid in s.get(Observation, 'env_cleanup').value
        # Personal session and the other machine keep their authority.
        assert s.get(Session, case['session']['id']).desired_state == 'running'
        assert s.get(GroupMachineAccount, binding).person == 'alex'
        assert Path(row.run_dir) == box['config'].run_dir / sid
    assert 'own-remote-canary' in envfile.read_text()  # disconnected cleanup is honestly pending
    replacement = StandInNode(box['config'].socket_path, box['node'].key, box['id'])
    task = asyncio.create_task(replacement.run(case['live']))
    case['tasks'].append(task)
    await wait_for(lambda: 'alex-box' in hub.links)
    async def cleaned():
        with case['owner'].app.state.db() as s:
            row = s.get(Session, sid)
            return not row.team_stop_pending and sid not in s.get(Observation, 'env_cleanup').value
    await wait_for(cleaned, timeout=20)
    assert not envfile.exists() or 'own-remote-canary' not in envfile.read_text()
    denied = await case['member'].post('/api/sessions/' + sid + '/resume', headers=csrf(case['member']))
    assert denied.status_code in (404, 409)


async def test_remote_delivery_excludes_other_peoples_keys_even_with_legacy_group_consent(machines, tmp_path):
    from helpers import wait_for

    from acc.server import vault
    from acc.server.models import GroupKeyGrant, Session, VaultGrant
    case = machines
    _, placement = await bind(case, case['member'], 'alex-box')
    sha = await seed(case, tmp_path)
    supervisor = case['owner'].app.state.ctx.supervisor
    with write_db(case['owner'].app.state.db) as s:
        keys = [vault.create_key(s, supervisor.vault, name=name, variable=variable, value=value,
                                owner=person, remote=remote) for name, variable, value, person, remote in [
            ('Alex remote', 'OWN_REMOTE', 'alex-remote-canary', 'alex', True),
            ('Alex local', 'OWN_LOCAL', 'alex-local-canary', 'alex', False),
            ('Private teammate name', 'FOREIGN_KEY', 'casey-private-canary', 'casey', True)]]
        s.flush()
        for key in keys:
            s.add(VaultGrant(key_id=key.id, project_id=case['group_project']['id'], given_by=key.owner))
        s.add(GroupKeyGrant(key_id=keys[2].id, group_id=case['group']['id'], given_by='casey'))
        s.commit()
    sid = 'd6' * 6
    await launch(case, case['member'], placement, sha, sid)
    envfile = case['boxes']['alex-box']['config'].run_dir / sid / 'session.env'
    await wait_for(lambda: envfile.exists())
    content = envfile.read_text()
    assert 'alex-remote-canary' in content
    assert 'alex-local-canary' not in content and 'casey-private-canary' not in content
    with case['owner'].app.state.db() as s:
        row = s.get(Session, sid)
        assert 'casey-private-canary' not in str(row.launch_spec.files)
        assert 'Private teammate name' not in str(row.launch_spec.files)
    board = await case['member'].get(f"/api/groups/{case['group']['id']}/board")
    assert 'casey-private-canary' not in board.text and 'Private teammate name' not in board.text
    assert not (case['boxes']['casey-box']['config'].run_dir / sid).exists()


async def test_machine_identity_substitution_and_unreported_account_refused(machines):
    from helpers import write_db

    from acc.server.models import GroupMachineAccount, Node
    case = machines
    requested = await case['member'].post('/api/group-machines', json={
        'node_id': case['boxes']['alex-box']['id'], 'account': 'other-account'}, headers=csrf(case['member']))
    assert requested.status_code == 201
    denied = await case['owner'].post('/api/group-machines/' + requested.json()['id'] + '/approve')
    assert denied.status_code == 409
    binding, placement = await bind(case, case['member'], 'alex-box')
    # An approved account cannot be claimed by another person, even an instance owner.
    denied = await case['owner'].post('/api/group-machines', json={
        'node_id': case['boxes']['alex-box']['id'], 'account': 'agent'})
    assert denied.status_code == 409
    assert all(b['person'] == 'alex' for b in (await case['member'].get('/api/group-machines')).json())
    # The display name alone cannot authenticate a replacement enrollment.
    with write_db(case['owner'].app.state.db) as s:
        old = s.get(Node, case['boxes']['alex-box']['id'])
        old.name, old.status = 'retired-box', 'revoked'
        s.add(Node(id='f' * 16, name='alex-box', status='active', created_by='casey'))
        s.commit()
        assert s.get(GroupMachineAccount, binding).node_id != 'f' * 16
    denied = await case['member'].post(case['base'] + '/exchange/grants', json={
        'operation': 'fetch', 'placement_id': placement, 'session_id': 'a' * 12,
        'old_sha': '0' * 40, 'new_sha': 'a' * 40}, headers=csrf(case['member']))
    assert denied.status_code == 404


async def test_remote_account_binding_blocks_legacy_personal_account_name_collision(machines):
    from acc.server.models import User
    case = machines
    await case['owner'].post(f"/api/groups/{case['group']['id']}/members/1", json={
        'revision': await revision(case), 'role': 'member'})
    await bind(case, case['owner'], 'casey-box')
    # Same Unix name on a node is not a person's identity. Alex's local account
    # changing to that spelling must not grant Casey's broker login or files.
    with write_db(case['owner'].app.state.db) as s:
        s.get(User, 2).account = 'agent'
        s.commit()
    response = await case['member'].get('/api/accounts?machine=casey-box')
    assert response.status_code == 404
    response = await case['member'].post('/api/projects', json={
        'name': 'Foreign account', 'slug': 'foreign-account', 'host': 'casey-box',
        'workspace_path': str(case['boxes']['casey-box']['config'].allowed_roots[0])}, headers=csrf(case['member']))
    assert response.status_code == 404
    from acc.server.models import Project, Session
    with write_db(case['owner'].app.state.db) as s:
        # Represent a stale registration racing owner approval: recheck on use too.
        stale = Project(slug='stale-foreign', name='Stale record', owner='alex', account='agent',
                        host='casey-box', workspace_path=str(case['boxes']['casey-box']['config'].allowed_roots[0]))
        s.add(stale)
        s.flush()
        stale_id = stale.id
        row = s.get(Session, case['session']['id'])
        row.owner, row.run_as = 'alex', 'agent'
        row.placement.host, row.placement.account = 'casey-box', 'agent'
        s.commit()
    assert (await case['member'].get(f'/api/projects/{stale_id}/knowledge')).status_code == 404
    assert (await case['member'].get('/api/sessions/' + case['session']['id'])).status_code == 404



@pytest.mark.parametrize('prefix', ['ACC_', 'RYNHULT_'])
def test_remote_groups_deployment_setting_is_explicit_and_alias_compatible(monkeypatch, prefix):
    from acc.server.config import Settings
    monkeypatch.delenv('ACC_GROUPS_REMOTE', raising=False)
    monkeypatch.delenv('RYNHULT_GROUPS_REMOTE', raising=False)
    assert not Settings.from_env().groups_remote_enabled
    monkeypatch.setenv(prefix + 'GROUPS_REMOTE', '1')
    assert Settings.from_env().groups_remote_enabled


async def test_unapproved_account_request_cannot_deny_another_person_existing_access(machines):
    from acc.server.models import User
    case = machines
    # Casey's unreviewed claim must not lock Alex out of an ordinary remote account.
    requested = await case['owner'].post('/api/group-machines', json={
        'node_id': case['boxes']['alex-box']['id'], 'account': 'agent'})
    assert requested.status_code == 201
    with write_db(case['owner'].app.state.db) as s:
        s.get(User, 2).account = 'agent'
        s.commit()
    assert (await case['member'].get('/api/accounts?machine=alex-box')).status_code == 200
    assert (await case['owner'].post('/api/group-machines/' + requested.json()['id'] + '/revoke')).status_code == 200
    requested = await case['member'].post('/api/group-machines', json={
        'node_id': case['boxes']['alex-box']['id'], 'account': 'agent'}, headers=csrf(case['member']))
    assert requested.status_code == 201, requested.text


async def test_group_cleanup_errors_remain_pending_until_broker_confirms(machines, tmp_path, monkeypatch):
    from helpers import pause_supervisor

    from acc.server.broker_client import BrokerError
    from acc.server.models import Observation
    case = machines
    binding, placement = await bind(case, case['member'], 'alex-box')
    sha = await seed(case, tmp_path)
    sid = 'ef' * 6
    await launch(case, case['member'], placement, sha, sid)
    await pause_supervisor(case['owner'])
    supervisor = case['owner'].app.state.ctx.supervisor
    original = supervisor.broker.call
    async def failing_clear(method, *args, **kwargs):
        if method == 'clear_env' and kwargs.get('acc_id') == sid:
            raise BrokerError('fixture refused cleanup')
        return await original(method, *args, **kwargs)
    monkeypatch.setattr(supervisor.broker, 'call', failing_clear)
    response = await case['owner'].post(f'/api/group-machines/{binding}/revoke')
    assert response.status_code == 200
    await supervisor.clear_envs()
    with case['owner'].app.state.db() as s:
        assert sid in s.get(Observation, 'env_cleanup').value
    monkeypatch.setattr(supervisor.broker, 'call', original)
    await supervisor.clear_envs()
    with case['owner'].app.state.db() as s:
        assert sid not in s.get(Observation, 'env_cleanup').value


@pytest.mark.parametrize('account', ['root', 'acc-node'])
async def test_own_machine_binding_refuses_privileged_and_service_accounts(remote_case, account):
    from acc.server.models import Node
    case = remote_case
    with write_db(case['owner'].app.state.db) as s:
        s.add(Node(id='b' * 32, name='own-box', status='active', created_by='casey'))
        s.commit()
    response = await case['member'].post('/api/group-machines', json={
        'node_id': 'b' * 32, 'account': account}, headers=csrf(case['member']))
    assert response.status_code == 422


async def test_group_machine_project_approvals_have_safe_scoped_audit_receipts(machines):
    case = machines
    _, placement = await bind(case, case['member'], 'alex-box')
    groupbase = f"/api/groups/{case['group']['id']}"
    result = await case['member'].get(groupbase + '/audit')
    actions = {row['action'] for row in result.json()}
    assert {'group.remote_placement_request', 'group.remote_placement_approve'} <= actions
    assert all(set(row) == {'id', 'ts', 'actor', 'action'} for row in result.json())
    revoked = await case['member'].post(case['base'] + f'/remote-placements/{placement}/revoke',
        json={'revision': await revision(case)}, headers=csrf(case['member']))
    assert revoked.status_code == 200
    assert 'group.remote_placement_revoke' in {row['action'] for row in (await case['member'].get(groupbase + '/audit')).json()}


async def test_a_removed_machine_name_with_bindings_is_refused_not_a_crash(remote_case):
    """The old node keeps its bindings (and their account reservation) after removal, so its name can't be reused for
    a new enrollment: a clear refusal, not a failed foreign key."""
    from acc.server.models import GroupMachineAccount, Node
    case = remote_case
    with write_db(case['owner'].app.state.db) as s:
        s.add(Node(id='b' * 32, name='old-box', status='revoked', created_by='casey'))
        s.flush()
        s.add(GroupMachineAccount(id='c' * 32, node_id='b' * 32, person='alex', account='agent',
                                  status='revoked', approved_by='casey'))
        s.add(Node(id='d' * 32, name='plain-box', status='revoked', created_by='casey'))
        s.commit()
    response = await case['owner'].post('/api/nodes', json={'name': 'old-box'})
    assert response.status_code == 409 and 'new name' in response.json()['detail'], response.text
    with case['owner'].app.state.db() as s:
        assert s.get(GroupMachineAccount, 'c' * 32).status == 'revoked'  # history and reservation stay
        assert s.get(Node, 'b' * 32).name == 'old-box'
    # a removed machine without own-machine history is re-enrolled as before
    assert (await case['owner'].post('/api/nodes', json={'name': 'plain-box'})).status_code == 201


async def test_a_pending_request_cannot_block_the_rightful_person(machines):
    """Requests don't reserve a machine/account; only an owner's approval does (review L1)."""
    case = machines
    pair = {'node_id': case['boxes']['alex-box']['id'], 'account': 'agent'}
    squat = await case['owner'].post('/api/group-machines', json=pair)
    assert squat.status_code == 201, squat.text
    mine = await case['member'].post('/api/group-machines', json=pair, headers=csrf(case['member']))
    assert mine.status_code == 201, mine.text
    again = await case['member'].post('/api/group-machines', json=pair, headers=csrf(case['member']))
    assert again.status_code == 409  # one pending request per person
    proof, _ = await prove(case, case['member'], mine.json())
    assert proof.status_code == 200, proof.text
    assert (await case['owner'].post(f"/api/group-machines/{mine.json()['id']}/approve")).status_code == 200
    # Approval reserves the pair: the other request can't be approved, and no new one is accepted.
    assert (await case['owner'].post(f"/api/group-machines/{squat.json()['id']}/approve")).status_code == 409
    assert (await case['owner'].post('/api/group-machines', json=pair)).status_code == 409
    assert (await case['owner'].post(f"/api/group-machines/{squat.json()['id']}/revoke")).status_code == 200


async def test_only_a_signed_in_owner_approves_a_machine_account(machines):
    """The installation API token is automation, not the person who verified the machine (review L6)."""
    case = machines
    requested = await case['member'].post('/api/group-machines', json={
        'node_id': case['boxes']['alex-box']['id'], 'account': 'agent'}, headers=csrf(case['member']))
    token = case['owner'].app.state.ctx.settings.api_token()
    denied = await case['owner'].post(f"/api/group-machines/{requested.json()['id']}/approve",
                                      headers={'Authorization': f'Bearer {token}'})
    assert denied.status_code == 403, denied.text
    proof, _ = await prove(case, case['member'], requested.json())
    assert proof.status_code == 200, proof.text
    assert (await case['owner'].post(f"/api/group-machines/{requested.json()['id']}/approve")).status_code == 200


async def test_removing_a_person_revokes_their_machine_accounts(remote_case):
    """Their bindings stay as reservations, but are no longer shown as approved (review L2)."""
    from acc.server.models import GroupMachineAccount, Node, User
    case = remote_case
    with write_db(case['owner'].app.state.db) as s:
        s.add(Node(id='e' * 32, name='blair-box', status='active', created_by='casey'))
        person = User(username='blair', password_hash='x', totp_secret='x', role='member')
        s.add(person)
        s.flush()
        s.add(GroupMachineAccount(id='f' * 32, node_id='e' * 32, person='blair', account='agent',
                                  status='approved', approved_by='casey'))
        s.commit()
        blair = person.id
    removed = await case['owner'].post(f'/api/users/{blair}/remove', json={'resources': 'archive'})
    assert removed.status_code == 200, removed.text
    with case['owner'].app.state.db() as s:
        row = s.get(GroupMachineAccount, 'f' * 32)
        assert row.status == 'revoked' and row.approved_by == 'casey'  # still reserves the account
    audit = (await case['owner'].get('/api/audit?action=group.machine_revoke')).json()
    assert any(a['target'] == 'f' * 32 and (a['detail'] or {}).get('reason') == 'user_removed' for a in audit)


async def test_the_board_honours_the_setting_for_remote_sessions(machines, tmp_path):
    """Turning own-machine groups off stops a still-running remote session from showing claims or posting
    observations at once, before reconcile stops it (review L3)."""
    import datetime

    from helpers import pause_supervisor

    from acc.server import group_board
    from acc.server.models import GroupClaim, Observation, Session
    case = machines
    binding, placement = await bind(case, case['member'], 'alex-box')
    sha = await seed(case, tmp_path)
    sid = 'd6' * 6
    await launch(case, case['member'], placement, sha, sid)
    await pause_supervisor(case['owner'])  # this case tests the board before reconciliation stops the session
    with write_db(case['owner'].app.state.db) as s:
        s.add(GroupClaim(session_id=sid, path='src/claimed.py',
                         expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)))
        s.commit()
    board = f"/api/groups/{case['group']['id']}/board"
    def claims(rows):
        return [c['path'] for row in rows['rows'] if row['session_id'] == sid for c in row['claims']]
    assert claims((await case['member'].get(board)).json()) == ['src/claimed.py']
    ctx = case['owner'].app.state.ctx
    ctx.settings = replace(ctx.settings, groups_remote_enabled=False)
    ctx.supervisor.settings = ctx.settings
    assert claims((await case['member'].get(board)).json()) == []
    with write_db(case['owner'].app.state.db) as s:
        session = s.get(Session, sid)
        if session.desired_state == 'running':
            group_board.observe_work(s, session, {'paths': ['src/after-off.py']})
            s.commit()
        observed = s.get(Observation, 'group.paths.' + sid)
        assert not observed or 'src/after-off.py' not in observed.value.get('paths', [])


async def test_indirect_revocations_name_what_they_revoked_in_the_audit(machines):
    """Membership and node removal list the bindings and placements they revoked; cancelling a request is a
    cancellation, not a revocation (review L4)."""
    case = machines
    binding, placement = await bind(case, case['member'], 'alex-box')
    groupbase = f"/api/groups/{case['group']['id']}"
    await case['owner'].post(groupbase + '/members/1', json={'revision': await revision(case), 'role': 'maintainer'})
    await case['owner'].post(case['base'] + '/owners', json={'revision': await revision(case), 'owners': [1]})
    removed = await case['owner'].post(groupbase + '/members/2', json={'revision': await revision(case), 'role': 'remove'})
    assert removed.status_code == 200, removed.text
    audit = (await case['owner'].get('/api/audit?action=group.membership')).json()
    assert any((a['detail'] or {}).get('remote_placements_revoked') == [placement] for a in audit), audit
    gone = await case['owner'].post('/api/nodes/alex-box/remove')
    assert gone.status_code == 200, gone.text
    node = (await case['owner'].get('/api/audit?action=node.remove')).json()[0]
    assert node['detail']['group_bindings_revoked'] == [binding]
    pending = await case['owner'].post('/api/group-machines', json={
        'node_id': case['boxes']['casey-box']['id'], 'account': 'agent'})
    assert (await case['owner'].post(f"/api/group-machines/{pending.json()['id']}/revoke")).status_code == 200
    actions = {a['action'] for a in (await case['owner'].get('/api/audit?action=group.machine_')).json()
               if a['target'] == pending.json()['id']}
    assert actions == {'group.machine_request', 'group.machine_cancel'}


async def prove(case, client, binding, *, host='alex-box', node_id=None, account='agent', request_id=None, now=None):
    from acc.node.group_proof import issue
    box = case['boxes'][host]
    code = issue(box['config'].run_dir, node_id or box['id'], account, request_id or binding['id'], now=now)
    response = await client.post(f"/api/group-machines/{binding['id']}/verify",
                                 json={'code': code}, headers=csrf(client))
    return response, code


async def test_proof_required_before_owner_approval_and_only_requester_verifies(machines):
    from sqlalchemy import select

    from acc.node.group_proof import issue
    from acc.server.models import AuditLog
    case = machines
    box = case['boxes']['alex-box']
    binding = (await case['member'].post('/api/group-machines', json={
        'node_id': box['id'], 'account': 'agent', 'proof_verified_at': '2099-01-01T00:00:00Z'},
        headers=csrf(case['member']))).json()
    assert binding['proof_verified_at'] is None
    url = f"/api/group-machines/{binding['id']}"
    denied = await case['owner'].post(url + '/approve')
    assert denied.status_code == 409, denied.text
    code = issue(box['config'].run_dir, box['id'], 'agent', binding['id'])
    assert (await case['owner'].post(url + '/verify', json={'code': code})).status_code == 404
    verified = await case['member'].post(url + '/verify', json={'code': code}, headers=csrf(case['member']))
    assert verified.status_code == 200, verified.text
    assert verified.json()['proof_verified_at']
    replay = await case['member'].post(url + '/verify', json={'code': code}, headers=csrf(case['member']))
    assert replay.status_code == 409
    assert (await case['owner'].post(url + '/approve')).status_code == 200
    with case['owner'].app.state.db() as s:
        logs = list(s.scalars(select(AuditLog).where(AuditLog.target == binding['id'])))
        assert any(log.action == 'group.machine_verified' for log in logs)
        assert code not in str([(log.action, log.detail) for log in logs])


@pytest.mark.parametrize('kind', ['account', 'machine', 'request', 'expired'])
async def test_wrong_or_expired_proof_never_authorizes(machines, kind):
    import time
    case = machines
    box = case['boxes']['alex-box']
    binding = (await case['member'].post('/api/group-machines', json={
        'node_id': box['id'], 'account': 'agent'}, headers=csrf(case['member']))).json()
    response, _ = await prove(case, case['member'], binding,
        host='casey-box' if kind == 'machine' else 'alex-box',
        account='other-agent' if kind == 'account' else 'agent',
        request_id='d' * 32 if kind == 'request' else None,
        now=time.time() - 301 if kind == 'expired' else None)
    assert response.status_code == 409, response.text
    assert (await case['owner'].post(f"/api/group-machines/{binding['id']}/approve")).status_code == 409


async def test_proof_attempts_are_rate_limited_and_audited(machines):
    from sqlalchemy import select

    from acc.node.group_proof import issue
    from acc.server.models import AuditLog
    case = machines
    box = case['boxes']['alex-box']
    binding = (await case['member'].post('/api/group-machines', json={
        'node_id': box['id'], 'account': 'agent'}, headers=csrf(case['member']))).json()
    url = f"/api/group-machines/{binding['id']}/verify"
    code = issue(box['config'].run_dir, box['id'], 'agent', binding['id'])
    for _ in range(10):
        assert (await case['member'].post(url, json={'code': 'WRONGCODE1'}, headers=csrf(case['member']))).status_code == 409
    assert (await case['member'].post(url, json={'code': code}, headers=csrf(case['member']))).status_code == 429
    with case['owner'].app.state.db() as s:
        logs = list(s.scalars(select(AuditLog).where(AuditLog.target == binding['id'])))
        assert sum(row.action == 'group.machine_proof_refused' for row in logs) == 10
        assert code not in str([row.detail for row in logs])


async def test_revocation_during_proof_cannot_restore_authority(machines, monkeypatch):
    case = machines
    ctx = case['owner'].app.state.ctx
    box = case['boxes']['alex-box']
    binding = (await case['member'].post('/api/group-machines', json={
        'node_id': box['id'], 'account': 'agent'}, headers=csrf(case['member']))).json()
    original = ctx.broker.call
    async def race(method, **params):
        result = await original(method, **params)
        if method == 'group_machine_verify':
            assert (await case['member'].post(f"/api/group-machines/{binding['id']}/revoke", headers=csrf(case['member']))).status_code == 200
        return result
    monkeypatch.setattr(ctx.broker, 'call', race)
    response, _ = await prove(case, case['member'], binding)
    assert response.status_code == 409
    assert (await case['owner'].post(f"/api/group-machines/{binding['id']}/approve")).status_code == 404


async def test_pre_proof_approved_binding_is_fenced_until_its_person_proves_control(machines):
    from acc.server import group_remote
    from acc.server.models import GroupMachineAccount, Placement
    case = machines
    box = case['boxes']['alex-box']
    with write_db(case['owner'].app.state.db) as s:
        s.add(GroupMachineAccount(id='e' * 32, node_id=box['id'], person='alex', account='agent',
                                 status='approved', approved_by='casey'))
        legacy = Placement(project_id=case['group_project']['id'], machine_account_id='e' * 32,
                           person='alex', host='alex-box', account='agent', path=str(box['config'].allowed_roots[0]),
                           trust='approved', sync_status='ready')
        s.add(legacy)
        s.flush()
        assert not group_remote.valid(s, legacy)
        placement_id = legacy.id
        s.commit()
    requested = await case['member'].post(case['base'] + '/remote-placements', json={
        'binding_id': 'e' * 32, 'revision': await revision(case)}, headers=csrf(case['member']))
    assert requested.status_code == 404
    proof, _ = await prove(case, case['member'], {'id': 'e' * 32})
    assert proof.status_code == 200, proof.text
    with case['owner'].app.state.db() as s:
        assert group_remote.valid(s, s.get(Placement, placement_id))
