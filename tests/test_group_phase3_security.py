"""Phase 3 security review regressions: Unix identity, override ceilings and audit subjects."""
# ruff: noqa: F811
import asyncio
from datetime import timedelta

import pytest
from helpers import pause_supervisor, write_db
from race_helpers import during_broker_call, guard_database, transaction_rendezvous
from test_group_board import working  # noqa: F401
from test_group_exchange import exchange_case, grant, transfer  # noqa: F401
from test_group_launch import checkout, reserve_checkout
from test_groups import grouped, two  # noqa: F401
from test_users import csrf

from acc.server.models import AuditLog, Group, GroupKeyGrant, GroupOperation, Placement, Session, User, VaultKey

BODY = {'task': 'Review validation', 'provider': 'claude', 'name': 'Group validation', 'trust_source': True}


def share_account(case, username='sam'):
    """Another ACC person whose sessions run as the same agent account as the member's placement."""
    with write_db(case['owner'].app.state.db) as s:
        account = s.get(Placement, case['placement']).account
        s.add(User(username=username, role='member', account=account, password_hash='unused', totp_secret='unused'))
        s.commit()


async def test_group_launch_refuses_agent_account_shared_with_another_person(exchange_case):
    case = exchange_case
    sid = 'abcd1234abcd'
    await checkout(case, sid)
    share_account(case)
    launched = await case['member'].post(case['base'] + '/sessions', json={'checkout_id': sid, **BODY}, headers=csrf(case['member']))
    assert launched.status_code in (404, 409), launched.text
    assert 'shared with another' in launched.json()['detail']
    assert 'dedicated agent account' in launched.json()['detail']
    with case['owner'].app.state.db() as s:
        assert s.get(Session, sid) is None


async def test_removed_person_no_longer_blocks_dedicated_group_account(exchange_case):
    case = exchange_case
    sid = 'abcd1234abcd'
    await checkout(case, sid)
    share_account(case)
    with case['owner'].app.state.db() as s:
        removed_id = s.query(User).filter_by(username='sam').one().id
    removed = await case['owner'].post(f'/api/users/{removed_id}/remove', json={'resources': 'archive'},
                                     headers=csrf(case['owner']))
    assert removed.status_code == 200, removed.text
    launched = await case['member'].post(case['base'] + '/sessions', json={'checkout_id': sid, **BODY}, headers=csrf(case['member']))
    assert launched.status_code == 201, launched.text


async def test_running_group_session_loses_authority_when_its_account_becomes_shared(exchange_case):
    from acc.server import group_board
    case = exchange_case
    sid = 'abcd1234abcd'
    await checkout(case, sid)
    launched = await case['member'].post(case['base'] + '/sessions', json={'checkout_id': sid, **BODY}, headers=csrf(case['member']))
    assert launched.status_code == 201, launched.text
    share_account(case)
    with case['owner'].app.state.db() as s:
        assert not group_board.authorized(s, s.get(Session, sid))
    assert (await case['member'].get('/api/sessions/' + sid)).status_code == 404


async def test_maintainer_pause_override_cannot_exceed_owner_ceilings(exchange_case, backend_url):
    case = exchange_case
    actor = case['member']  # the group's maintainer
    await checkout(case, 'abcd1234abcd')
    second = await actor.post(case['base'] + '/exchange/checkout', json={'placement_id': case['placement'], 'session_id': 'eeee1234eeee', 'commit': case['sha']}, headers=csrf(actor))
    assert second.status_code == 200, second.text
    with write_db(case['owner'].app.state.db) as s:
        group = s.get(Group, case['group']['id'])
        group.concurrent = group.concurrent_ceiling = 1
        s.commit()
    first = await actor.post(case['base'] + '/sessions', json={'checkout_id': 'abcd1234abcd', **BODY}, headers=csrf(actor))
    assert first.status_code == 201, first.text
    base = f"/api/groups/{case['group']['id']}"
    body = {'checkout_id': 'eeee1234eeee', **BODY}
    assert (await actor.post(case['base'] + '/sessions', json=body, headers=csrf(actor))).status_code == 409
    rev = (await actor.get(base)).json()['revision']
    assert (await actor.post(base + '/unpause', json={'revision': rev}, headers=csrf(actor))).status_code == 200
    over = await actor.post(case['base'] + '/sessions', json=body, headers=csrf(actor))
    assert over.status_code == 409, over.text
    assert (await actor.get(base + '/usage')).json()['concurrent'] == 1


@pytest.mark.parametrize('ceiling', ['concurrent_ceiling', 'daily_ceiling'])
async def test_concurrent_override_launches_cannot_exceed_owner_ceilings(
        exchange_case, backend_url, monkeypatch, ceiling):
    from acc.server.models import utcnow
    case = exchange_case
    await pause_supervisor(case['owner'])
    await checkout(case, 'abcd1234abcd')
    actor = case['member']
    second = await actor.post(case['base'] + '/exchange/checkout', json={
        'placement_id': case['placement'], 'session_id': 'eeee1234eeee', 'commit': case['sha']}, headers=csrf(actor))
    assert second.status_code == 200, second.text
    with write_db(case['owner'].app.state.db) as s:
        group = s.get(Group, case['group']['id'])
        group.paused = True
        group.override_until = utcnow() + timedelta(minutes=15)
        setattr(group, ceiling, 1)
        s.commit()
    with guard_database(case['owner'], monkeypatch) as db, transaction_rendezvous(db, 'UPDATE acc_group'):
        results = await asyncio.gather(*(asyncio.to_thread(reserve_checkout, case, db, sid)
                                         for sid in ('abcd1234abcd', 'eeee1234eeee')))
    assert sorted(results) == ['refused', 'reserved']
    usage = (await actor.get(f"/api/groups/{case['group']['id']}/usage")).json()
    assert usage['concurrent'] == usage['daily_launches'] == 1


@pytest.mark.parametrize('change', ['shared-account', 'viewer', 'removed', 'group-archived', 'project-archived'])
async def test_dispatch_rechecks_group_authority_after_concurrent_revocation(
        exchange_case, backend_url, monkeypatch, change):
    from pathlib import Path

    from acc.server.models import GroupMembership, Project
    case = exchange_case
    await pause_supervisor(case['owner'])
    sid = 'abcd1234abcd'
    await checkout(case, sid)

    def revoke(db):
        with write_db(db) as s:
            if change == 'shared-account':
                account = s.get(Placement, case['placement']).account
                s.add(User(username='sam', role='member', account=account,
                           password_hash='unused', totp_secret='unused'))
            elif change == 'group-archived':
                s.get(Group, case['group']['id']).archived = True
            elif change == 'project-archived':
                s.get(Project, case['group_project']['id']).archived = True
            else:
                member = s.get(GroupMembership, (case['group']['id'], 2))
                if change == 'viewer':
                    member.role = 'viewer'
                else:
                    s.delete(member)
            # Do not set stop intent here: the post-dispatch authority guard must
            # independently catch stale pre-dispatch authorization.
            s.commit()

    response = await during_broker_call(case['owner'], monkeypatch, 'launch', sid,
        case['member'].post(case['base'] + '/sessions', json={'checkout_id': sid, **BODY},
                            headers=csrf(case['member'])), revoke)
    assert response.status_code == 201, response.text
    with case['owner'].app.state.db() as s:
        row = s.get(Session, sid)
        assert row.desired_state == row.observed_state == 'stopped'
        assert not (Path(row.run_dir) / 'session.env').exists()
        tmux_name, account = row.tmux_name, row.run_as
    panes = await case['owner'].app.state.supervisor.broker.call('list', account=account)
    assert tmux_name not in {pane['tmux_name'] for pane in panes}


async def test_pre_dispatch_discards_authority_from_before_a_concurrent_revocation(
        working, backend_url, monkeypatch):
    from sqlalchemy import create_engine, delete

    from acc.server import group_board
    from acc.server.models import GroupMembership
    w = working
    api = w['owner']
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    sid = w['session']['id']
    launched = []
    original = supervisor.broker.call

    async def observed(method, **kwargs):
        if method == 'launch' and kwargs.get('acc_id') == sid:
            launched.append(sid)
        return await original(method, **kwargs)

    monkeypatch.setattr(supervisor.broker, 'call', observed)
    # Model another controller: it has no in-process SQLite transaction gate.
    writer = create_engine(api.app.state.db.kw['bind'].url, hide_parameters=True)
    try:
        with api.app.state.db() as s:
            row = s.get(Session, sid)
            assert group_board.authorized(s, row)
            reader_connection = s.connection().connection.driver_connection

            def revoke():
                with writer.begin() as connection:
                    assert connection.connection.driver_connection is not reader_connection
                    connection.execute(delete(GroupMembership).where(
                        GroupMembership.group_id == w['group']['id'], GroupMembership.user_id == 2))

            # Commit the removal while the dispatcher's transaction and ORM cache
            # still contain the old membership (REPEATABLE READ on PostgreSQL).
            await asyncio.to_thread(revoke)
            await supervisor._start(s, row, reason='launch')
            s.commit()
        assert launched == []
        with api.app.state.db() as s:
            assert s.get(Session, sid).desired_state == 'stopped'
    finally:
        writer.dispose()


async def test_key_attach_and_operation_audit_name_their_subjects(grouped):
    w = grouped
    with write_db(w['owner'].app.state.db) as s:
        s.add(VaultKey(id='demo-key', name='private canary', variable='DEMO_KEY', ciphertext='sealed', owner='casey'))
        s.flush()
        s.add(GroupKeyGrant(key_id='demo-key', group_id=w['group']['id'], host='local', given_by='casey'))
        s.add(GroupOperation(id='example-operation', group_id=w['group']['id'], kind='quota', summary='Review launch quota'))
        s.commit()
    base = f"/api/groups/{w['group']['id']}"
    project_id = w['group_project']['id']
    actor = w['member']
    assert (await actor.post(base + f'/projects/{project_id}/keys/demo-key/attach', headers=csrf(actor))).status_code == 200
    assert (await actor.post(base + '/operations/example-operation/update', json={'revision': 1, 'action': 'assign', 'user_id': 2}, headers=csrf(actor))).status_code == 200
    with w['owner'].app.state.db() as s:
        attach = s.query(AuditLog).filter_by(action='group.key_attach').one()
        assert attach.detail['key_id'] == 'demo-key' and attach.detail['project_id'] == project_id
        assign = s.query(AuditLog).filter_by(action='group.operation_assign').one()
        assert assign.detail['operation_id'] == 'example-operation' and assign.detail['user_id'] == 2


async def test_archived_group_project_refuses_key_attach(grouped):
    from acc.server.models import Project
    w = grouped
    with write_db(w['owner'].app.state.db) as s:
        s.add(VaultKey(id='demo-key', name='private canary', variable='DEMO_KEY', ciphertext='sealed', owner='casey'))
        s.flush()
        s.add(GroupKeyGrant(key_id='demo-key', group_id=w['group']['id'], host='local', given_by='casey'))
        s.get(Project, w['group_project']['id']).archived = True
        s.commit()
    base = f"/api/groups/{w['group']['id']}"
    project_id = w['group_project']['id']
    response = await w['member'].post(base + f'/projects/{project_id}/keys/demo-key/attach', headers=csrf(w['member']))
    assert response.status_code == 409, response.text
    with w['owner'].app.state.db() as s:
        assert not s.query(AuditLog).filter_by(action='group.key_attach').count()


async def test_remote_fact_lookup_rechecks_revoked_group_membership(working, monkeypatch):
    from sqlalchemy import delete

    from acc.server.models import GroupMembership

    api = working['owner']
    await pause_supervisor(api)
    sup = api.app.state.supervisor
    sid = working['session']['id']
    with write_db(sup.db) as db:
        db.get(Session, sid).team_id = sid  # exercise a group lead's remote-fact lookup
        db.commit()
    original = sup.broker.call
    lookups, launches = [], []

    async def call(method, **params):
        if method == 'git_refs':
            with write_db(sup.db) as fresh:
                fresh.execute(delete(GroupMembership).where(
                    GroupMembership.group_id == working['group']['id'], GroupMembership.user_id == 2))
                fresh.commit()
            lookups.append(method)
            return {'git': True, 'has_remote': False}
        if method == 'launch':
            launches.append(params)
            return {'pid': 123, 'command': ['fixture-only; not executed']}
        return await original(method, **params)

    monkeypatch.setattr(sup.broker, 'call', call)
    with sup.db() as db:
        await sup._start(db, db.get(Session, sid), reason='launch')
        db.commit()
    assert lookups == ['git_refs']
    assert not launches
    with sup.db() as db:
        assert db.get(Session, sid).desired_state == 'stopped'
