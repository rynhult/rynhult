"""Live checkout identity, explicit source trust and transactional group capacity."""
# ruff: noqa: F811
import asyncio

import pytest
from helpers import pause_supervisor, write_db
from race_helpers import during_broker_call, guard_database, transaction_rendezvous
from test_group_exchange import exchange_case, grant, transfer  # noqa: F401
from test_groups import grouped, two  # noqa: F401
from test_users import csrf


async def checkout(case, sid):
    import base64

    from acc import git_exchange
    minted = await grant(case)
    pack = base64.b64encode(git_exchange.export(case['source'] / '.git', case['sha'])).decode()
    assert (await transfer(case, minted.json()['token'], pack)).status_code == 200
    provisioned = await case['owner'].post(case['base'] + f"/placements/{case['placement']}/provision")
    assert provisioned.status_code == 200, provisioned.text
    response = await case['member'].post(case['base'] + '/exchange/checkout', json={
        'placement_id': case['placement'], 'session_id': sid, 'commit': case['sha']}, headers=csrf(case['member']))
    assert response.status_code == 200, response.text


async def test_launch_requires_checkout_trust_and_binds_id(exchange_case):
    case = exchange_case
    sid = 'abcd1234abcd'
    await checkout(case, sid)
    url = case['base'] + '/sessions'
    body = {'checkout_id': sid, 'task': 'Review validation', 'provider': 'claude', 'name': 'Group validation',
            'prompt': 'private prompt canary', 'trust_source': False}
    assert (await case['member'].post(url, json=body, headers=csrf(case['member']))).status_code == 409
    body['trust_source'] = True
    launched = await case['member'].post(url, json=body, headers=csrf(case['member']))
    assert launched.status_code == 201, launched.text
    assert launched.json()['id'] == sid
    repeated = await case['member'].post(url, json=body, headers=csrf(case['member']))
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['id'] == sid
    board = (await case['member'].get(f"/api/groups/{case['group']['id']}/board")).json()
    assert board['rows'][0]['task'] == 'Review validation'
    assert 'private prompt canary' not in str(board)
    from acc.server.models import Session
    with case['owner'].app.state.db() as s:
        row = s.get(Session, sid)
        assert row.cwd.endswith('/sessions/' + sid)
        assert row.run_as == row.placement.account
        assert row.placement.person == 'alex'


async def test_quota_pause_and_authorized_override(exchange_case, backend_url):
    case = exchange_case
    await checkout(case, 'abcd1234abcd')
    base = f"/api/groups/{case['group']['id']}"
    revision = (await case['owner'].get(base)).json()['revision']
    policy = await case['member'].post(base + '/policy', json={'revision': revision, 'concurrent': 1, 'daily_launches': 1}, headers=csrf(case['member']))
    assert policy.status_code == 200
    body = {'checkout_id': 'abcd1234abcd', 'task': 'Review validation', 'provider': 'claude', 'name': 'Group validation', 'trust_source': True}
    results = await asyncio.gather(*(case['member'].post(case['base'] + '/sessions', json=body, headers=csrf(case['member'])) for _ in range(2)))
    assert sorted(r.status_code for r in results) == [200, 201]
    usage = (await case['member'].get(base + '/usage')).json()
    assert usage['daily_launches'] == 1
    assert usage['concurrent'] == 1
    assert usage['cost'] is None


async def test_removal_stops_group_sessions_and_refuses_resume(exchange_case):
    case = exchange_case
    sid = 'abcd1234abcd'
    await checkout(case, sid)
    base = f"/api/groups/{case['group']['id']}"
    actor = case['member']
    result = await actor.post(case['base'] + '/sessions', json={'checkout_id': sid, 'task': 'Review validation',
        'provider': 'claude', 'name': 'Group validation', 'trust_source': True}, headers=csrf(actor))
    assert result.status_code == 201, result.text
    rev = (await case['owner'].get(base)).json()['revision']
    assert (await case['owner'].post(base + '/members/1', json={'revision': rev, 'role': 'maintainer'})).status_code == 200
    rev += 1
    assert (await case['owner'].post(case['base'] + '/owners', json={'revision': rev, 'owners': [1]})).status_code == 200
    rev += 1
    removed = await case['owner'].post(base + '/members/2', json={'revision': rev, 'role': 'remove'})
    assert removed.status_code == 200, removed.text
    from acc.server.models import Session
    with case['owner'].app.state.db() as s:
        assert s.get(Session, sid).desired_state == 'stopped'
    assert (await actor.get('/api/sessions/' + sid)).status_code == 404
    assert (await actor.post('/api/sessions/' + sid + '/resume', headers=csrf(actor))).status_code == 404


async def test_paused_group_requires_group_override_and_counts_concurrent_new_checkouts(exchange_case):
    from helpers import write_db

    from acc.server.models import Group
    case = exchange_case
    await checkout(case, 'abcd1234abcd')
    base = f"/api/groups/{case['group']['id']}"
    actor = case['member']
    with write_db(case['owner'].app.state.db) as s:
        s.get(Group, case['group']['id']).paused = True
        s.commit()
    body = {'checkout_id': 'abcd1234abcd', 'task': 'Review tests', 'provider': 'claude', 'name': 'Demo', 'trust_source': True, 'budget_ok': True}
    paused = await actor.post(case['base'] + '/sessions', json=body, headers=csrf(actor))
    assert paused.status_code == 409, paused.text
    rev = (await case['owner'].get(base)).json()['revision']
    override = await actor.post(base + '/unpause', json={'revision': rev}, headers=csrf(actor))
    assert override.status_code == 200, override.text
    assert (await actor.post(case['base'] + '/sessions', json=body, headers=csrf(actor))).status_code == 201
    assert (await actor.get(base + '/usage')).json()['daily_launches'] == 1


def reserve_checkout(case, db, sid):
    """Exercise reserve's contract: hold its transaction through session + ledger insertion."""
    from acc.server import group_board
    from acc.server.models import GroupLaunch, GroupWork, Project, Session
    from acc.server.supervisor_common import LaunchRefused

    with write_db(db) as s:
        group, checkout, placement = group_board.launch_binding(
            s, s.get(Project, case['group_project']['id']), sid, 'alex')
        try:
            group_board.reserve(s, group)
        except LaunchRefused:
            return 'refused'
        template = s.get(Session, case['session']['id'])
        s.add(Session(id=sid, project_id=placement.project_id, provider='claude', name='Quota race',
                      tmux_name='quota-' + sid, launch_spec_id=template.launch_spec_id,
                      owner='alex', run_as=placement.account, placement_id=placement.id,
                      desired_state='running', observed_state='starting'))
        s.flush()
        s.add(GroupWork(session_id=sid, checkout_id=sid, task='Review tests', base_sha=checkout.base_sha))
        s.add(GroupLaunch(id=sid, session_id=sid))
        s.commit()
    return 'reserved'


@pytest.mark.parametrize('limit', ['concurrent', 'daily_launches'])
async def test_two_checkout_launch_race_reserves_only_one_slot(exchange_case, backend_url, monkeypatch, limit):
    from acc.server.models import Group
    case = exchange_case
    await pause_supervisor(case['owner'])
    await checkout(case, 'abcd1234abcd')
    actor = case['member']
    second = await actor.post(case['base'] + '/exchange/checkout', json={'placement_id': case['placement'], 'session_id': 'eeee1234eeee', 'commit': case['sha']}, headers=csrf(actor))
    assert second.status_code == 200
    with write_db(case['owner'].app.state.db) as s:
        setattr(s.get(Group, case['group']['id']), limit, 1)
        s.commit()
    # HTTP launches have a process-local asyncio lock; race the database reservation
    # itself on two worker connections to protect the cross-controller fence too.
    with guard_database(case['owner'], monkeypatch) as db, transaction_rendezvous(db, 'UPDATE acc_group'):
        results = await asyncio.gather(*(asyncio.to_thread(reserve_checkout, case, db, sid)
                                         for sid in ('abcd1234abcd', 'eeee1234eeee')))
    assert sorted(results) == ['refused', 'reserved']
    usage = (await actor.get(f"/api/groups/{case['group']['id']}/usage")).json()
    assert usage['concurrent'] == 1 and usage['daily_launches'] == 1 and usage['paused']


async def test_resume_is_charged_once_and_cannot_bypass_group_pause(exchange_case):
    case = exchange_case
    await checkout(case, 'abcd1234abcd')
    actor = case['member']
    base = f"/api/groups/{case['group']['id']}"
    body = {'task': 'Review tests', 'provider': 'claude', 'name': 'Demo', 'trust_source': True, 'checkout_id': 'abcd1234abcd'}
    assert (await actor.post(case['base'] + '/sessions', json=body, headers=csrf(actor))).status_code == 201
    url = '/api/sessions/abcd1234abcd'
    assert (await actor.post(url + '/stop', headers=csrf(actor))).status_code == 200
    assert (await actor.post(url + '/resume', headers=csrf(actor))).status_code == 200
    assert (await actor.get(base + '/usage')).json()['daily_launches'] == 2


@pytest.mark.parametrize('role', ['owner', 'maintainer', 'member', 'viewer', 'nonmember', 'removed'])
async def test_group_launch_route_role_and_checkout_matrix(exchange_case, role):
    from helpers import write_db

    from acc.server.models import GroupMembership
    case = exchange_case
    await checkout(case, 'abcd1234abcd')
    actor = case['owner'] if role == 'owner' else case['member']
    with write_db(case['owner'].app.state.db) as s:
        membership = s.get(GroupMembership, (case['group']['id'], 2))
        if role in ('nonmember', 'removed'):
            s.delete(membership)
        elif role != 'owner':
            membership.role = role
        s.commit()
    response = await actor.post(case['base'] + '/sessions', json={'checkout_id': 'abcd1234abcd', 'task': 'Review validation',
        'provider': 'claude', 'name': 'Demo', 'trust_source': True}, headers=csrf(actor))
    # Even a trusted owner cannot launch a teammate's checkout under that teammate's login.
    assert response.status_code == (201 if role in ('maintainer', 'member') else 404), response.text


async def test_key_consent_revoked_during_dispatch_kills_and_cleans_the_session(
        exchange_case, monkeypatch, backend_url):
    from pathlib import Path

    from helpers import write_db

    from acc.server import vault
    from acc.server.models import GroupKeyGrant, Session, VaultGrant
    case = exchange_case
    await pause_supervisor(case['owner'])
    sid = 'abcd1234abcd'
    await checkout(case, sid)
    supervisor = case['owner'].app.state.ctx.supervisor
    with write_db(case['owner'].app.state.db) as s:
        key = vault.create_key(s, supervisor.vault, name='Private canary name', variable='DEMO_KEY', value='opaque-dispatch-canary', owner='casey')
        s.flush()
        key_id = key.id
        grant = GroupKeyGrant(key_id=key.id, group_id=case['group']['id'], given_by='casey', host='local')
        s.add(grant)
        s.add(VaultGrant(key_id=key.id, project_id=case['group_project']['id'], given_by='casey'))
        s.flush()
        grant_id = grant.id
        s.commit()
    def revoke(db):
        with write_db(db) as s:
            row = s.get(Session, sid)
            assert 'opaque-dispatch-canary' in (Path(row.run_dir) / 'session.env').read_text()
            s.delete(s.get(GroupKeyGrant, grant_id))
            s.commit()

    response = await during_broker_call(case['owner'], monkeypatch, 'launch', sid,
        case['member'].post(case['base'] + '/sessions', json={
        'checkout_id': sid, 'task': 'Review contracts', 'provider': 'claude', 'name': 'Demo', 'trust_source': True},
        headers=csrf(case['member'])), revoke)
    assert response.status_code == 201, response.text
    with case['owner'].app.state.db() as s:
        row = s.get(Session, sid)
        assert row.desired_state == 'stopped'
        assert key_id in row.vault_keys
        assert not (Path(row.run_dir) / 'session.env').exists()


@pytest.mark.parametrize('change', ['viewer', 'removed', 'project-archived'])
async def test_checkout_rechecks_authority_after_broker_receive(exchange_case, backend_url, monkeypatch, change):
    from acc.server.models import GroupCheckout, GroupMembership, Project, Session
    case = exchange_case
    await pause_supervisor(case['owner'])
    await checkout(case, 'abcd1234abcd')
    sid = 'eeee1234eeee'

    def revoke(db):
        with write_db(db) as s:
            member = s.get(GroupMembership, (case['group']['id'], 2))
            if change == 'viewer':
                member.role = 'viewer'
            elif change == 'removed':
                s.delete(member)
            else:
                s.get(Project, case['group_project']['id']).archived = True
            s.commit()

    response = await during_broker_call(case['owner'], monkeypatch, 'group_receive', sid,
        case['member'].post(case['base'] + '/exchange/checkout', json={
            'placement_id': case['placement'], 'session_id': sid, 'commit': case['sha']},
            headers=csrf(case['member'])), revoke)
    assert response.status_code == 404, response.text
    with case['owner'].app.state.db() as s:
        assert s.get(GroupCheckout, sid) is None
        assert s.get(Session, sid) is None
