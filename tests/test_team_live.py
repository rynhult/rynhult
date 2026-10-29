"""Slice 2: isolated snapshots, transactional events, bounded polling and no agent authority."""
import asyncio
from datetime import timedelta

import pytest
from helpers import pause_supervisor, write_db
from sqlalchemy import select
from test_passkeys import setup
from test_team import grant

from acc.server import auth
from acc.server.models import Event, LaunchSpec, Session, Team

ROOT = 'a' * 32
CHILD = 'b' * 32
GRANDCHILD = 'c' * 32


@pytest.fixture(autouse=True)
def preview(monkeypatch):
    """The orchestrator v2 preview on (ACC_ORCHESTRATOR=1), before the app is built: without it, no v2 route exists."""
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')


def member(id, project_id, parent=None, depth=0, owner='casey', team=ROOT):
    return Session(id=id, project_id=project_id, provider='claude', name='Lead' if parent is None else 'Helper ' + id[0],
        tmux_name='live-' + id, run_as='agent', management='external', owner=owner, team_id=team,
        parent_session_id=parent, team_depth=depth, observed_state='starting',
        launch_spec=LaunchSpec(provider='claude', inputs={'model': 'example-model', 'profile': 'backend'}, files={}, digest=id))


@pytest.fixture
async def tree(api, project, env, monkeypatch):
    await pause_supervisor(api)
    headers, _ = await setup(api, env, monkeypatch)
    with write_db(api.app.state.db) as db:
        db.add(Team(id=ROOT, limits=grant(), expires_at=auth.now() + timedelta(hours=1)))
        db.add_all([member(ROOT, project['id']), member(CHILD, project['id'], ROOT, 1),
                    member(GRANDCHILD, project['id'], CHILD, 2)])
        db.commit()
    return headers


async def test_snapshot_requires_grant_and_person_and_includes_starting_descendants(api, tree):
    response = await api.get(f'/api/teams/{ROOT}/tree')
    assert response.status_code == 200, response.text
    result = response.json()
    assert [m['id'] for m in result['members']] == [ROOT, CHILD, GRANDCHILD]
    assert result['members'][2]['parent'] == CHILD and result['members'][2]['depth'] == 2
    assert result['members'][0]['observed'] == 'starting'
    assert result['cursor'] > 0
    assert all('team_task' not in m and 'launch_spec' not in m for m in result['members'])
    assert (await api.get('/api/teams/' + 'f' * 32 + '/tree')).status_code == 404
    assert (await api.get(f'/api/teams/{CHILD}/tree')).status_code == 404
    assert (await api.get(f'/api/teams/{ROOT}/tree', headers={'Authorization': 'Bearer ' + api.app.state.ctx.token})).status_code == 403


async def test_committed_state_events_replay_once_and_rollback_emits_nothing(api, tree):
    before = (await api.get(f'/api/teams/{ROOT}/tree')).json()
    with write_db(api.app.state.db) as db:
        row = db.get(Session, CHILD)
        api.app.state.supervisor._set_state(db, row, 'waiting_input', 'screen')
        db.commit()
    response = await api.get(f'/api/teams/{ROOT}/events', params={'after': before['cursor'], 'wait': 0})
    assert response.status_code == 200, response.text
    events = response.json()
    states = [e for e in events['events'] if e['kind'] == 'team.member.state']
    assert len(states) == 1 and states[0]['payload']['needs_you'] is True
    again = await api.get(f'/api/teams/{ROOT}/events', params={'after': events['cursor'], 'wait': 0})
    assert again.json()['events'] == []
    with write_db(api.app.state.db) as db:
        api.app.state.supervisor._set_state(db, db.get(Session, CHILD), 'active', 'screen')
        db.flush()
        db.rollback()
    assert (await api.get(f'/api/teams/{ROOT}/events', params={'after': events['cursor'], 'wait': 0})).json()['events'] == []
    snapshot = (await api.get(f'/api/teams/{ROOT}/tree')).json()
    assert next(m for m in snapshot['members'] if m['id'] == CHILD)['observed'] == 'waiting_input'


async def test_poll_wakes_on_committed_update_and_cancels_without_holding_database(api, tree):
    cursor = (await api.get(f'/api/teams/{ROOT}/tree')).json()['cursor']
    task = asyncio.create_task(api.get(f'/api/teams/{ROOT}/events', params={'after': cursor, 'wait': 2}))
    async def update():
        with write_db(api.app.state.db) as db:
            api.app.state.supervisor._set_state(db, db.get(Session, CHILD), 'active', 'screen')
            db.commit()
    await update()
    result = await asyncio.wait_for(task, 3)
    assert result.status_code == 200 and result.json()['events']
    task = asyncio.create_task(api.get(f'/api/teams/{ROOT}/events', params={'after': result.json()['cursor'], 'wait': 25}))
    await asyncio.sleep(0)  # yield to request admission, not a readiness delay
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert (await api.get(f'/api/teams/{ROOT}/events', params={'after': result.json()['cursor'], 'wait': 0})).status_code == 200


async def test_foreign_members_events_and_unrelated_provider_payloads_are_not_disclosed(api, tree, project):
    with write_db(api.app.state.db) as db:
        db.add(member('d' * 32, project['id'], ROOT, 1, owner='another-person'))
        db.add(Event(session_id=CHILD, source='claude_hook', kind='secret', payload={'token': 'DO-NOT-RETURN'}))
        db.commit()
    snapshot = (await api.get(f'/api/teams/{ROOT}/tree')).json()
    assert 'd' * 32 not in [m['id'] for m in snapshot['members']]
    result = await api.get(f'/api/teams/{ROOT}/events', params={'after': 0, 'wait': 0})
    assert 'DO-NOT-RETURN' not in result.text and 'another-person' not in result.text
    assert all(e['session'] != 'd' * 32 for e in result.json()['events'])


@pytest.mark.parametrize('query', [{'after': -1}, {'after': 2**64}, {'wait': 26}, {'wait': -1}])
async def test_poll_input_is_bounded(api, tree, query):
    assert (await api.get(f'/api/teams/{ROOT}/events', params=query)).status_code == 422


async def test_finish_and_activity_changes_are_transactional_events(api, tree):
    before = (await api.get(f'/api/teams/{ROOT}/tree')).json()['cursor']
    with write_db(api.app.state.db) as db:
        row = db.get(Session, CHILD)
        row.activity = 'Running example tests'
        db.commit()
    with write_db(api.app.state.db) as db:
        row = db.get(Session, CHILD)
        row.desired_state = 'archived'
        api.app.state.supervisor._set_state(db, row, 'archived', 'api')
        db.commit()
    events = (await api.get(f'/api/teams/{ROOT}/events', params={'after': before, 'wait': 0})).json()['events']
    assert any(e['kind'] == 'team.member.state' and e['payload']['activity'] == 'Running example tests' for e in events)
    assert any(e['kind'] == 'team.member.finished' and e['payload']['archived'] for e in events)
    with api.app.state.db() as db:
        assert list(db.scalars(select(Event).where(Event.source == 'orchestrator')))


async def test_merge_and_future_slice_events_are_in_snapshot_and_replay(api, tree):
    from acc.server.models import AuditLog
    with write_db(api.app.state.db) as db:
        db.add(AuditLog(actor='session:' + ROOT, action='team.merge', target=ROOT,
                        detail={'helper': CHILD, 'merged': True}))
        db.add_all([
            Event(session_id=CHILD, source='orchestrator', kind='team.selection',
                  payload={'id': 7, 'chosen': 'claude', 'reason': 'Allowed review profile', 'state': 'selected', 'secret': 'PRIVATE'}),
            Event(session_id=CHILD, source='orchestrator', kind='team.member.report', payload={'id': CHILD, 'status': 'done'}),
            Event(session_id=CHILD, source='orchestrator', kind='team.delivery', payload={'id': 9, 'session': CHILD, 'state': 'delivered'}),
        ])
        db.commit()
    snapshot = (await api.get(f'/api/teams/{ROOT}/tree')).json()
    node = next(row for row in snapshot['members'] if row['id'] == CHILD)
    assert node['merged'] is True
    assert node['selection_reason'] == 'Allowed review profile'
    assert node['report_status'] == 'done'
    assert node['deliveries'] == [{'id': 9, 'session': CHILD, 'state': 'delivered'}]
    assert 'PRIVATE' not in str(snapshot)


async def test_waiting_polls_are_bounded_and_a_new_view_evicts_the_oldest(api, tree, monkeypatch):
    """Review 2026-10-25 (OV-10): a closed tab's poll can't lock the person out; at most four still wait."""
    from acc.server.api import team_live
    monkeypatch.setattr(team_live, 'POLL_INTERVAL', .01)
    cursor = (await api.get(f'/api/teams/{ROOT}/tree')).json()['cursor']
    # A poll joins the waiting list only after its first read, which runs in a worker thread, so polls started together
    # join in whatever order those reads finish (the race that made tasks[0] not the oldest). Start them one at a
    # time instead: a poll re-reads with the same request object, so its second principal() call means it is waiting.
    calls: dict[int, int] = {}
    original = api.app.state.ctx.principal
    loop = asyncio.get_running_loop()
    joined = asyncio.Queue()
    def principal(request):
        result = original(request)
        calls[id(request)] = calls.get(id(request), 0) + 1
        if calls[id(request)] == 2:
            loop.call_soon_threadsafe(joined.put_nowait, id(request))
        return result
    monkeypatch.setattr(api.app.state.ctx, 'principal', principal)
    tasks = []

    async def start_waiting_poll():
        tasks.append(asyncio.create_task(api.get(f'/api/teams/{ROOT}/events', params={'after': cursor, 'wait': 25})))
        await asyncio.wait_for(joined.get(), 5)
    try:
        for _ in range(4):
            await start_waiting_poll()
        assert not any(task.done() for task in tasks)
        tasks.append(asyncio.create_task(api.get(f'/api/teams/{ROOT}/events', params={'after': cursor, 'wait': 25})))
        oldest = await asyncio.wait_for(tasks[0], 5)  # answered at once, not after its 25 s
        assert oldest.status_code == 200 and oldest.json() == {'events': [], 'cursor': cursor, 'has_more': False,
                                                              'evicted': True}
        await asyncio.sleep(.1)
        assert not any(task.done() for task in tasks[1:])  # the other three and the newest keep waiting
        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        assert (await api.get(f'/api/teams/{ROOT}/events', params={'after': cursor, 'wait': .01})).status_code == 200
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_event_pages_advance_only_over_delivered_rows(api, tree):
    before = (await api.get(f'/api/teams/{ROOT}/tree')).json()['cursor']
    with write_db(api.app.state.db) as db:
        for revision in range(205):
            db.add(Event(session_id=ROOT, source='orchestrator', kind='worklist.now', payload={'revision': revision}))
        db.commit()
    first = (await api.get(f'/api/teams/{ROOT}/events', params={'after': before, 'wait': 0})).json()
    assert len(first['events']) == 200 and first['has_more']
    second = (await api.get(f'/api/teams/{ROOT}/events', params={'after': first['cursor'], 'wait': 0})).json()
    assert len(second['events']) == 5 and not second['has_more']
    assert [e['payload']['revision'] for e in first['events'] + second['events']] == list(range(205))
    assert (await api.get(f'/api/teams/{ROOT}/events', params={'after': second['cursor'] + 1, 'wait': 0})).status_code == 409


async def test_another_person_and_an_agent_cannot_read_either_route(api, tree, member_accounts):
    from test_users import browser, csrf, join
    owner = browser(api)
    owner.cookies.update(api.cookies)
    invited = await owner.post('/api/users/invites', json={'role': 'member', 'account': 'dana-agent'}, headers=csrf(owner))
    assert invited.status_code == 201, invited.text
    other = browser(api)
    try:
        joined = await join(other, invited.json()['link'], 'alex')
        assert joined.status_code == 200, joined.text
        for suffix in ('tree', 'events?wait=0'):
            assert (await other.get(f'/api/teams/{ROOT}/{suffix}')).status_code == 404
            assert (await api.get(f'/api/teams/{ROOT}/{suffix}', headers={'Authorization': 'Bearer ' + api.app.state.ctx.token})).status_code == 403
    finally:
        await other.aclose()
        await owner.aclose()


async def test_waiting_poll_rechecks_revoked_login(api, tree, monkeypatch):
    from sqlalchemy import delete

    from acc.server.api import team_live
    from acc.server.models import AuthSession
    monkeypatch.setattr(team_live, 'POLL_INTERVAL', .01)
    cursor = (await api.get(f'/api/teams/{ROOT}/tree')).json()['cursor']
    scanning = asyncio.Event()
    original = api.app.state.ctx.principal
    loop = asyncio.get_running_loop()
    def principal(request):
        result = original(request)
        loop.call_soon_threadsafe(scanning.set)
        return result
    monkeypatch.setattr(api.app.state.ctx, 'principal', principal)
    pending = asyncio.create_task(api.get(f'/api/teams/{ROOT}/events', params={'after': cursor, 'wait': 25}))
    try:
        await asyncio.wait_for(scanning.wait(), 3)
        with write_db(api.app.state.db) as db:
            db.execute(delete(AuthSession))
            db.commit()
        assert (await asyncio.wait_for(pending, 3)).status_code == 401
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_event_text_is_vault_redacted_before_storage_and_on_read(api, tree):
    from acc.server.models import VaultKey
    canary = 'secret-canary-no-echo'
    id = '00112233aabbccdd'
    vault = api.app.state.supervisor.vault
    with write_db(api.app.state.db) as db:
        db.add(VaultKey(id=id, name='Example API', variable='API_KEY', owner='casey',
                        ciphertext=vault.cipher.seal(id, 'API_KEY', canary, create=True)))
        row = db.get(Session, CHILD)
        row.vault_keys = [id]
        db.commit()
    before = (await api.get(f'/api/teams/{ROOT}/tree')).json()['cursor']
    with write_db(api.app.state.db) as db:
        row = db.get(Session, CHILD)
        row.activity = 'Using ' + canary
        row.name = 'Worker ' + canary
        api.app.state.supervisor._set_state(db, row, 'active', 'api')
        db.commit()
    with api.app.state.db() as db:
        assert canary not in str([e.payload for e in db.scalars(select(Event).where(Event.source == 'orchestrator'))])
    snapshot = await api.get(f'/api/teams/{ROOT}/tree')
    replay = await api.get(f'/api/teams/{ROOT}/events', params={'after': before, 'wait': 0})
    assert canary not in snapshot.text + replay.text
    assert 'Example API' in snapshot.text + replay.text


async def test_archiving_after_merge_preserves_the_merge_mark(api, tree):
    from acc.server.models import AuditLog
    with write_db(api.app.state.db) as db:
        db.add(AuditLog(actor='session:' + ROOT, action='team.merge', target=ROOT,
                        detail={'helper': CHILD, 'merged': True}))
        db.commit()
    with write_db(api.app.state.db) as db:
        db.get(Session, CHILD).desired_state = 'archived'
        db.commit()
    node = next(node for node in (await api.get(f'/api/teams/{ROOT}/tree')).json()['members'] if node['id'] == CHILD)
    assert node['merged'] and node['archived']


async def test_ordinary_sessions_emit_no_orchestrator_events(api, tree, project):
    plain = 'e' * 32
    with write_db(api.app.state.db) as db:
        row = member(plain, project['id'], team=None)
        db.add(row)
        db.commit()
        api.app.state.supervisor._set_state(db, row, 'active', 'api')
        db.commit()
    with api.app.state.db() as db:
        assert not list(db.scalars(select(Event).where(Event.session_id == plain, Event.source == 'orchestrator')))


async def test_snapshot_is_not_overwhelmed_by_old_delivery_history(api, tree):
    with write_db(api.app.state.db) as db:
        db.add(Event(session_id=CHILD, source='orchestrator', kind='team.selection',
                     payload={'id': 7, 'reason': 'Keep this old reason', 'chosen': 'claude', 'state': 'selected'}))
        for revision in range(4001):
            db.add(Event(session_id=ROOT, source='orchestrator', kind='worklist.now', payload={'revision': revision}))
        db.commit()
    node = next(row for row in (await api.get(f'/api/teams/{ROOT}/tree')).json()['members'] if row['id'] == CHILD)
    assert node['selection_reason'] == 'Keep this old reason'


def test_event_commit_order_and_rollback_on_database_backends(backend_url):
    """The PostgreSQL lane uses only the suite's throwaway container harness."""
    from types import SimpleNamespace

    from acc.server.db import WRITE_INTENT, make_engine, make_sessionmaker, migrate
    from acc.server.models import Project
    from acc.server.orchestrator import live

    migrate(backend_url)
    engine = make_engine(backend_url)
    factory = make_sessionmaker(engine)
    redactor = SimpleNamespace(text=lambda value: value)
    # The Orchestrator v2 admin feature is on for this context (live.install's listener checks it on every flush).
    context = SimpleNamespace(db=factory, settings=SimpleNamespace(features=SimpleNamespace(enabled=lambda key: True)),
                              supervisor=SimpleNamespace(vault=SimpleNamespace(redactor=lambda *args: redactor)))
    live.install(context)
    token = WRITE_INTENT.set(True)
    try:
        with factory() as db:
            project = Project(slug='example', name='Example', workspace_path='/example', account='agent')
            db.add(project)
            db.flush()
            db.add(Team(id=ROOT, limits=grant(), expires_at=auth.now() + timedelta(hours=1)))
            db.add(member(ROOT, project.id))
            db.commit()
            first = db.scalar(select(Event.id).where(Event.source == 'orchestrator'))
            live.on_state(db, db.get(Session, ROOT), 'starting', 'active')
            db.flush()
            db.rollback()
            live.on_state(db, db.get(Session, ROOT), 'starting', 'idle')
            db.commit()
        with factory() as db:
            ids = list(db.scalars(select(Event.id).where(Event.source == 'orchestrator').order_by(Event.id)))
            assert len(ids) == 2 and ids[0] == first and ids[1] > first
    finally:
        WRITE_INTENT.reset(token)
        engine.dispose()


async def test_malformed_sibling_payload_does_not_break_tree_or_poll(api, tree):
    before = (await api.get(f'/api/teams/{ROOT}/tree')).json()['cursor']
    with write_db(api.app.state.db) as db:
        db.add(Event(session_id=CHILD, source='orchestrator', kind='team.selection', payload=['untrusted']))
        db.commit()
    assert (await api.get(f'/api/teams/{ROOT}/tree')).status_code == 200
    response = await api.get(f'/api/teams/{ROOT}/events', params={'after': before, 'wait': 0})
    assert response.status_code == 200
    assert response.json()['events'][0]['payload'] == {}


async def test_stopped_member_publishes_a_finish_without_being_archived(api, tree):
    before = (await api.get(f'/api/teams/{ROOT}/tree')).json()['cursor']
    with write_db(api.app.state.db) as db:
        api.app.state.supervisor._set_state(db, db.get(Session, CHILD), 'stopped', 'api')
        db.commit()
    result = (await api.get(f'/api/teams/{ROOT}/events', params={'after': before, 'wait': 0})).json()
    assert any(event['kind'] == 'team.member.finished' and event['payload']['archived'] is False for event in result['events'])


def test_event_order_lock_is_separate_from_ha_activity_and_leader_locks():
    from acc.server import ha
    from acc.server.db import LEADER_LOCK
    from acc.server.orchestrator import live

    assert live.EVENT_ORDER_LOCK not in {ha.ACTIVITY_LOCK, LEADER_LOCK, 1094927181, 1094927182}
