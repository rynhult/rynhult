"""Downtime policy uses local wall time and never hides age or loses a reconnect."""
from datetime import datetime, timedelta, timezone

import pytest
from isolated_settings import isolated_settings
from sqlalchemy import select

from acc.server.models import AttentionItem, AuditLog, Node


@pytest.fixture
def database(tmp_path):
    from acc.server.db import make_engine, make_sessionmaker, migrate
    url = f"sqlite:///{tmp_path / 'downtime.db'}"
    migrate(url)
    engine = make_engine(url)
    yield make_sessionmaker(engine)
    engine.dispose()


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize('at,active', [
    ('2026-10-24T06:29:59', False), ('2026-10-24T06:30:00', True),
    ('2026-10-24T14:00:00', False),
    # Both occurrences of 01:45 on the fall-back Sunday belong to the window.
    ('2026-11-01T06:45:00', True), ('2026-11-01T07:45:00', True),
    ('2026-11-01T15:00:00', False),
    # Spring forward skips an hour; 09:00 remains the exclusive local end.
    ('2026-03-08T08:30:00', True), ('2026-03-08T14:00:00', False),
])
def test_local_window_boundaries_and_dst(at, active):
    from acc.server.machine_presence import planned_downtime
    node = Node(downtime_policy={'timezone': 'America/Chicago', 'windows': [
        {'days': list(range(7)), 'start': '01:30', 'end': '09:00'}]})
    assert planned_downtime(node, instant(at))['active'] is active


def test_midnight_crossing_and_next_expected_end():
    from acc.server.machine_presence import planned_downtime
    node = Node(downtime_policy={'timezone': 'America/Chicago', 'windows': [
        {'days': [4], 'start': '23:00', 'end': '02:00'}]})
    state = planned_downtime(node, instant('2026-10-24T06:00:00'))
    assert state == {'active': True, 'until_reconnect': False, 'ends_at': instant('2026-10-24T07:00:00')}
    assert not planned_downtime(node, instant('2026-10-25T06:00:00'))['active']


def test_offline_alert_only_suppressed_inside_window(database):
    from acc.server.machine_presence import evaluate, transition
    with database() as db:
        node = Node(id='n', name='demo', status='active', role='always_on', downtime_policy={
            'timezone': 'UTC', 'windows': [{'days': [5], 'start': '01:00', 'end': '09:00'}]})
        db.add(node)
        db.flush()
        transition(db, node, False, instant('2026-10-24T00:00:00'))
        evaluate(db, node, instant('2026-10-24T00:30:00'))
        db.flush()
        assert db.scalar(select(AttentionItem)).resolved_at is None
        evaluate(db, node, instant('2026-10-24T01:00:00'))
        assert db.scalar(select(AttentionItem)).resolved_at is not None
        evaluate(db, node, instant('2026-10-24T09:00:00'))
        assert db.scalar(select(AttentionItem).where(AttentionItem.resolved_at.is_(None))) is not None


def test_until_reconnect_is_consumed_by_real_reconnect(database):
    from acc.server.machine_presence import planned_downtime, transition
    with database() as db:
        node = Node(id='n', name='demo', status='active', downtime_until_reconnect=True)
        db.add(node)
        db.flush()
        transition(db, node, True, instant('2026-10-24T00:00:00'))
        assert planned_downtime(node, instant('2026-10-24T00:01:00'))['active']
        transition(db, node, False, instant('2026-10-24T01:00:00'))
        transition(db, node, True, instant('2026-10-24T09:00:00'))
        assert not planned_downtime(node, instant('2026-10-24T09:00:00'))['active']
        assert node.last_reconnected_at == instant('2026-10-24T09:00:00')


async def test_owner_policy_validated_persisted_and_audited(api):
    await api.post('/api/nodes', json={'name': 'demo'})
    policy = {'timezone': 'America/Chicago', 'windows': [{'days': list(range(7)), 'start': '01:30', 'end': '09:00'}]}
    response = await api.post('/api/nodes/demo/policy', json={'role': 'always_on',
        'downtime_policy': policy, 'downtime_until_reconnect': True})
    assert response.status_code == 200, response.text
    assert response.json()['downtime_policy'] == policy
    assert response.json()['planned_downtime']['active']
    for bad in [{'timezone': 'Bad/Zone', 'windows': []},
                {'timezone': 'UTC', 'windows': [{'days': [7], 'start': '01:30', 'end': '09:00'}]},
                {'timezone': 'UTC', 'windows': [{'days': [0], 'start': '25:00', 'end': '09:00'}]}]:
        assert (await api.post('/api/nodes/demo/policy', json={'role': 'always_on', 'downtime_policy': bad})).status_code == 422
    with api.app.state.db() as db:
        row = db.scalar(select(AuditLog).where(AuditLog.action == 'node.policy'))
        assert row.detail['downtime_policy'] == policy


async def test_stale_machine_policy_cannot_erase_saved_windows(api):
    original = (await api.post('/api/nodes', json={'name': 'demo'})).json()
    assert 'policy_revision' in original
    policy = {'timezone': 'UTC', 'windows': [{'days': [0], 'start': '01:30', 'end': '09:00'}]}
    first = await api.post('/api/nodes/demo/policy', json={
        'role': 'always_on', 'downtime_policy': policy, 'expected_revision': original['policy_revision']})
    assert first.status_code == 200, first.text
    stale = await api.post('/api/nodes/demo/policy', json={
        'role': 'always_on', 'offline_threshold_seconds': 1200,
        'downtime_policy': None, 'expected_revision': original['policy_revision']})
    assert stale.status_code == 409, stale.text
    current = next(n for n in (await api.get('/api/nodes')).json() if n['name'] == 'demo')
    assert current['downtime_policy'] == policy
    assert current['offline_threshold_seconds'] == 600
    updated = await api.post('/api/nodes/demo/policy', json={
        'role': 'always_on', 'offline_threshold_seconds': 1200,
        'expected_revision': first.json()['policy_revision']})
    assert updated.status_code == 200, updated.text
    assert updated.json()['downtime_policy'] == policy


async def test_old_policy_cannot_rearm_until_reconnect_after_it_is_consumed(api):
    from helpers import pause_supervisor, write_db

    from acc.server.machine_presence import transition
    await pause_supervisor(api)
    original = (await api.post('/api/nodes', json={'name': 'demo'})).json()
    policy = {'role': 'always_on', 'downtime_until_reconnect': True}
    armed = (await api.post('/api/nodes/demo/policy', json=policy)).json()
    with write_db(api.app.state.db) as db:
        node = db.get(Node, original['id'])
        transition(db, node, True, instant('2026-10-24T00:00:00'))
        db.commit()
    # Merely observing a connected machine does not invalidate an edit.
    unchanged = await api.post('/api/nodes/demo/policy', json={
        **policy, 'expected_revision': armed['policy_revision']})
    assert unchanged.status_code == 200, unchanged.text
    with write_db(api.app.state.db) as db:
        node = db.get(Node, original['id'])
        transition(db, node, False, instant('2026-10-24T01:00:00'))
        transition(db, node, True, instant('2026-10-24T09:00:00'))
        db.commit()
    stale = await api.post('/api/nodes/demo/policy', json={
        **policy, 'expected_revision': armed['policy_revision']})
    assert stale.status_code == 409, stale.text
    current = next(n for n in (await api.get('/api/nodes')).json() if n['name'] == 'demo')
    assert current['downtime_until_reconnect'] is False


@pytest.fixture
def backup_jobs(database, tmp_path):
    from acc.backup_server import canonical, private_write
    from acc.server.backup_jobs import BackupJobs
    jobs = BackupJobs(isolated_settings(tmp_path), database)
    jobs.service.initialize()
    private_write(jobs.service.root / 'targets.json', canonical([
        {'id': 'a' * 32, 'kind': 'node', 'node_id': 'n', 'repository': 'example.lan',
         'enabled': True, 'created_at': 1, 'setup_verified_at': 1, 'schedule_seconds':3600}]))
    with database() as db:
        db.add(Node(id='n', name='demo', status='active', role='always_on',
            offline_since=instant('2026-10-24T01:00:00'), downtime_policy={
                'timezone': 'UTC', 'windows': [{'days': [5], 'start': '01:00', 'end': '09:00'}]}))
        db.commit()
    return jobs


def test_backup_alerts_suppressed_only_in_target_window(backup_jobs, database, monkeypatch):
    from acc.server import backup_jobs as module
    jobs = backup_jobs
    jobs.store({'id': 'old', 'target_id': 'a' * 32, 'action': 'capture', 'actor': 'system',
        'state': 'failed', 'at': instant('2026-10-24T00:00:00').timestamp()})
    for when, active in [('00:30:00', True), ('01:00:00', False), ('09:00:00', True)]:
        at = instant('2026-10-24T' + when).timestamp()
        monkeypatch.setattr(module.time, 'time', lambda: at)
        jobs.remind(now=at)
        with database() as db:
            open_keys = list(db.scalars(select(AttentionItem.dedupe_key).where(AttentionItem.resolved_at.is_(None))))
        assert ('backup:stale:' + 'a' * 32 in open_keys) is active
        assert ('backup:failed:' + 'a' * 32 in open_keys) is active
    assert jobs.state()['targets'][0]['stale']  # Suppression never makes old data fresh.


def test_catchup_once_per_reconnect_persists_count_and_disabled_never_queues(backup_jobs, database, monkeypatch):
    from acc.backup_server import canonical, private_write
    from acc.server import backup_jobs as module
    from acc.server.machine_presence import transition
    jobs = backup_jobs
    jobs.store({'id': 'old', 'target_id': 'a' * 32, 'action': 'capture', 'actor': 'system',
        'state': 'complete', 'at': instant('2026-10-24T00:30:00').timestamp()})
    reconnect = instant('2026-10-24T08:15:00')
    monkeypatch.setattr(module.time, 'time', lambda: reconnect.timestamp())
    # Tick while offline in planned downtime produces no hourly failures.
    jobs.schedule()
    assert jobs.pending() == []
    with database() as db:
        transition(db, db.get(Node, 'n'), True, reconnect)
        db.commit()
    jobs.schedule()
    assert len(jobs.pending()) == 1
    job = jobs.pending()[0]
    assert job['missed_captures'] == 7
    assert job['note'] == 'caught up after 7 missed'
    jobs.store({**job, 'state': 'complete'})
    # Reconstruct worker to prove durable deduplication after completion/restart.
    again = module.BackupJobs(jobs.settings, database)
    again.schedule()
    assert again.pending() == []
    targets = jobs.service.targets()
    targets[0]['enabled'] = False
    private_write(jobs.service.root / 'targets.json', canonical(targets))
    with database() as db:
        node = db.get(Node, 'n')
        transition(db, node, False, reconnect + timedelta(hours=1))
        transition(db, node, True, reconnect + timedelta(hours=5))
        db.commit()
    again.schedule()
    assert again.pending() == []


def test_backup_state_keeps_last_success_and_expected_capture(backup_jobs, monkeypatch):
    from acc.backup_server import canonical, private_write
    from acc.server import backup_jobs as module
    at = instant('2026-10-24T05:00:00').timestamp()
    monkeypatch.setattr(module.time, 'time', lambda: at)
    private_write(backup_jobs.service.root / ('result-' + 'a' * 32 + '.json'), canonical({
        'captured_at': instant('2026-10-24T00:00:00').timestamp(), 'verified_at': 1}))
    state = backup_jobs.state()['targets'][0]
    assert state['last_successful_off_machine_at'] == instant('2026-10-24T00:00:00').timestamp()
    assert state['planned_downtime']['active']
    assert state['next_capture_expected_at'] == instant('2026-10-24T09:00:00').timestamp()


def test_catchup_ignores_failed_hourly_attempts_and_coalesces_pending(backup_jobs, database, monkeypatch):
    from acc.server import backup_jobs as module
    from acc.server.machine_presence import transition
    jobs = backup_jobs
    for hour, state in [(0, 'complete'), (2, 'failed'), (3, 'failed'), (4, 'failed')]:
        jobs.store({'id': str(hour), 'target_id': 'a' * 32, 'action': 'capture', 'actor': 'system',
            'state': state, 'at': instant(f'2026-10-24T{hour:02d}:30:00').timestamp()})
    reconnect = instant('2026-10-24T08:15:00')
    monkeypatch.setattr(module.time, 'time', lambda: reconnect.timestamp())
    pending = jobs.request('a' * 32, 'capture', 'owner')
    with database() as db:
        transition(db, db.get(Node, 'n'), True, reconnect)
        db.commit()
    jobs.schedule()
    assert len(jobs.pending()) == 1
    assert jobs.pending()[0]['id'] == pending['id']
    assert jobs.pending()[0]['missed_captures'] == 7


def test_short_outage_without_missed_hour_does_not_queue_catchup(backup_jobs, database, monkeypatch):
    from acc.server import backup_jobs as module
    from acc.server.machine_presence import transition
    jobs = backup_jobs
    jobs.store({'id': 'old', 'target_id': 'a' * 32, 'action': 'capture', 'actor': 'system',
        'state': 'complete', 'at': instant('2026-10-24T00:50:00').timestamp()})
    reconnect = instant('2026-10-24T01:15:00')
    monkeypatch.setattr(module.time, 'time', lambda: reconnect.timestamp())
    with database() as db:
        transition(db, db.get(Node, 'n'), True, reconnect)
        db.commit()
    jobs.schedule()
    assert jobs.pending() == []


@pytest.mark.parametrize('until_reconnect', [True, False])
def test_reconnect_between_schedule_reads_preserves_catchup(backup_jobs, database, monkeypatch, until_reconnect):
    from contextlib import contextmanager

    from acc.server import backup_jobs as module
    from acc.server.machine_presence import transition

    jobs = backup_jobs
    reconnect = instant('2026-10-24T08:15:00')
    monkeypatch.setattr(module.time, 'time', lambda: reconnect.timestamp())
    jobs.store({'id': 'old', 'target_id': 'a' * 32, 'action': 'capture', 'actor': 'system',
        'state': 'complete', 'at': instant('2026-10-24T00:30:00').timestamp()})
    with database() as db:
        node = db.get(Node, 'n')
        if until_reconnect:
            node.downtime_policy = None
        node.downtime_until_reconnect = until_reconnect
        db.commit()
    reconnected = False

    @contextmanager
    def reconnect_after_node_read():
        nonlocal reconnected
        with database() as db:
            yield db
            read_node = any(isinstance(row, Node) for row in db.identity_map.values())
        # Commit after the scheduler's offline snapshot, before any second read.
        if read_node and not reconnected:
            reconnected = True
            with database() as db:
                transition(db, db.get(Node, 'n'), True, reconnect)
                db.commit()

    monkeypatch.setattr(jobs, 'db', reconnect_after_node_read)
    jobs.schedule()
    assert reconnected
    assert jobs.pending() == []
    jobs.schedule()
    catchup, = jobs.pending()
    assert catchup['reconnect_at'] == reconnect.timestamp()
    assert catchup['missed_captures'] == 7
    jobs.store({**catchup, 'state': 'complete'})
    jobs.schedule()
    assert jobs.pending() == []


async def test_backup_history_records_successful_catchup(api, monkeypatch):
    jobs = api.app.state.ctx.backup_jobs
    # Only remote listing IO is replaced; API enrichment uses the real durable job history.
    monkeypatch.setattr(jobs.service, 'history', lambda _: {'items': [
        {'backup_id': 'b' * 32, 'snapshot_id': 'c' * 64, 'time': '2026-10-24T09:00:00Z'}], 'verified_missing': False})
    jobs.store({'id': 'catchup', 'target_id': 'a' * 32, 'action': 'capture', 'state': 'complete',
        'at': 1, 'backup_id': 'b' * 32, 'result': {'snapshot_id': 'c' * 64}, 'missed_captures': 7, 'note': 'caught up after 7 missed'})
    result = await api.get('/api/host/backups/' + 'a' * 32 + '/snapshots')
    assert result.status_code == 200
    assert result.json()['items'][0]['note'] == 'caught up after 7 missed'


def test_migration_preserves_machine_and_downtime_roundtrip(tmp_path):
    import sqlite3

    from alembic import command

    from acc.server.db import alembic_config
    path = tmp_path / 'upgrade.db'
    config = alembic_config(f'sqlite:///{path}')
    command.upgrade(config, '0050')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO node (id, name, status, info, created_at) VALUES ('demo', 'demo', 'active', '{}', '2026-01-01')")
    command.upgrade(config, 'head')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT name, downtime_policy, downtime_until_reconnect, last_reconnected_at FROM node').fetchone() == ('demo', None, 0, None)
    command.downgrade(config, '0050')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT name FROM node').fetchone() == ('demo',)
    command.upgrade(config, 'head')


def test_unrelated_backup_target_alerts_remain_active(backup_jobs, database, monkeypatch):
    from acc.backup_server import canonical, private_write
    from acc.server import backup_jobs as module
    jobs = backup_jobs
    at = instant('2026-10-24T05:00:00').timestamp()
    monkeypatch.setattr(module.time, 'time', lambda: at)
    private_write(jobs.service.root / 'targets.json', canonical([*jobs.service.targets(),
        {'id': 'b' * 32, 'kind': 'local', 'enabled': True, 'created_at': at, 'repository': '/example'}]))
    jobs.remind(now=at)
    with database() as db:
        keys = list(db.scalars(select(AttentionItem.dedupe_key).where(AttentionItem.resolved_at.is_(None))))
        assert 'backup:stale:' + 'a' * 32 not in keys
        assert 'backup:stale:' + 'b' * 32 in keys


@pytest.mark.parametrize('at,expected_end', [
    ('2026-03-08T08:29:59', None),
    ('2026-03-08T08:30:00', '2026-03-08T14:00:00'),
    ('2026-11-01T07:40:00', '2026-11-01T07:45:00'),
])
def test_nonexistent_start_shifts_forward_and_repeated_end_is_later(at, expected_end):
    from acc.server.machine_presence import planned_downtime
    spring = at.startswith('2026-03')
    node = Node(downtime_policy={'timezone': 'America/Chicago', 'windows': [
        {'days': [6], 'start': '02:30' if spring else '01:30', 'end': '09:00' if spring else '01:45'}]})
    state = planned_downtime(node, instant(at))
    assert state['active'] is (expected_end is not None)
    assert state['ends_at'] == (instant(expected_end) if expected_end else None)


def test_reconnect_queues_capture_even_with_pending_restore_test(backup_jobs, database, monkeypatch):
    from acc.server import backup_jobs as module
    from acc.server.machine_presence import transition
    jobs = backup_jobs
    jobs.store({'id': 'verify', 'target_id': 'a' * 32, 'action': 'verify', 'actor': 'owner',
        'state': 'queued', 'at': instant('2026-10-24T07:00:00').timestamp()})
    reconnect = instant('2026-10-24T08:15:00')
    monkeypatch.setattr(module.time, 'time', lambda: reconnect.timestamp())
    with database() as db:
        transition(db, db.get(Node, 'n'), True, reconnect)
        db.commit()
    jobs.schedule()
    captures = [j for j in jobs.pending() if j['action'] == 'capture']
    assert len(captures) == 1
    assert captures[0]['missed_captures'] == 7


def test_expected_capture_respects_hourly_cadence_for_short_window(backup_jobs, database, monkeypatch):
    from acc.server import backup_jobs as module
    at = instant('2026-10-24T01:15:00').timestamp()
    monkeypatch.setattr(module.time, 'time', lambda: at)
    backup_jobs.store({'id': 'old', 'target_id': 'a' * 32, 'action': 'capture', 'actor': 'system',
        'state': 'complete', 'at': instant('2026-10-24T00:50:00').timestamp()})
    with database() as db:
        db.get(Node, 'n').downtime_policy = {'timezone': 'UTC', 'windows': [
            {'days': [5], 'start': '01:00', 'end': '01:30'}]}
        db.commit()
    assert backup_jobs.state()['targets'][0]['next_capture_expected_at'] == instant('2026-10-24T01:50:00').timestamp()


def test_windows_covering_entire_week_have_no_invented_return_time():
    from acc.server.machine_presence import planned_downtime
    node = Node(downtime_policy={'timezone': 'UTC', 'windows': [
        {'days': list(range(7)), 'start': '00:00', 'end': '12:00'},
        {'days': list(range(7)), 'start': '12:00', 'end': '00:00'}]})
    state = planned_downtime(node, instant('2026-10-24T05:00:00'))
    assert state['active']
    assert state['ends_at'] is None


async def test_board_and_host_details_report_planned_downtime(api):
    from helpers import write_db
    with write_db(api.app.state.db) as db:
        db.add(Node(id='demo', name='demo', status='active', role='always_on', downtime_until_reconnect=True))
        db.commit()
    board = (await api.get('/api/board')).json()
    row = next(m for m in board['machines'] if m['name'] == 'demo')
    assert row['planned_downtime']['active']
    details = (await api.get('/api/host?machine=demo')).json()
    assert details['planned_downtime']['active']


def test_expected_capture_skips_a_later_window_at_hourly_due_time(backup_jobs, database, monkeypatch):
    from acc.server import backup_jobs as module
    at = instant('2026-10-24T01:10:00').timestamp()
    monkeypatch.setattr(module.time, 'time', lambda: at)
    backup_jobs.store({'id': 'old', 'target_id': 'a' * 32, 'action': 'capture', 'actor': 'system',
        'state': 'complete', 'at': instant('2026-10-24T00:50:00').timestamp()})
    with database() as db:
        db.get(Node, 'n').downtime_policy = {'timezone': 'UTC', 'windows': [
            {'days': [5], 'start': '01:00', 'end': '01:15'},
            {'days': [5], 'start': '01:45', 'end': '03:00'}]}
        db.commit()
    assert backup_jobs.state()['targets'][0]['next_capture_expected_at'] == instant('2026-10-24T03:00:00').timestamp()


from test_auth import live  # noqa: E402, F401, I001


@pytest.mark.parametrize('reconnect_during_tick', [False, True], ids=['idle-worker', 'busy-worker'])
async def test_authenticated_reconnect_wakes_worker_for_one_catchup(api, env, live, monkeypatch, reconnect_during_tick):  # noqa: F811
    import asyncio
    import threading
    import time

    from helpers import pause_supervisor, wait_for, write_db
    from test_multihost import StandInNode, enroll

    from acc.backup_server import canonical, private_write

    await pause_supervisor(api)
    key, node_id = await enroll(api)
    jobs = api.app.state.ctx.backup_jobs
    from acc.server.vault import Cipher
    Cipher(env['settings'].vault_key_path).key(create=True)
    target = jobs.service.add_node_target(node_id, 'pc')
    target_id = target['id']
    jobs.store({'id': 'old', 'target_id': target_id, 'action': 'capture', 'actor': 'system',
        'state': 'complete', 'at': time.time() - 8 * 3600})
    private_write(jobs.service.root / 'targets.json', canonical([
        {**target, 'enabled': True, 'created_at': time.time() - 86400, 'setup_verified_at': 1, 'schedule_seconds':3600}]))
    with write_db(api.app.state.db) as db:
        node = db.get(Node, node_id)
        node.offline_since = datetime.now(timezone.utc) - timedelta(hours=7)
        node.downtime_until_reconnect = True
        db.commit()
    # Real worker capture/intent execution; replace only the off-machine upload IO.
    monkeypatch.setattr(jobs.service, 'back_up', lambda *args, **kwargs: {'snapshot_id': 'c' * 64})
    loop = asyncio.get_running_loop()
    offline_tick = asyncio.Event()
    release_tick = threading.Event()
    tick = jobs.tick

    def observed_tick():
        tick()
        loop.call_soon_threadsafe(offline_tick.set)
        if reconnect_during_tick:
            assert release_tick.wait(timeout=10), 'reconnect did not release the offline tick'

    monkeypatch.setattr(jobs, 'tick', observed_tick)
    worker = asyncio.create_task(jobs.run())  # api fixture starts only the supervisor, not app lifespan workers
    node = StandInNode(env['settings'].broker_socket, key, node_id)
    task = None

    async def captured():
        if task.done():
            await task
            raise AssertionError('node exited before catch-up completed')
        if worker.done():
            await worker
            raise AssertionError('worker exited before catch-up completed')
        values = await asyncio.to_thread(jobs.jobs)
        assert not any(j.get('missed_captures') and j['state'] == 'failed' for j in values), values
        return any(j.get('missed_captures') == 8 and j['state'] == 'complete' for j in values)

    try:
        await asyncio.wait_for(offline_tick.wait(), timeout=5)
        task = asyncio.create_task(node.run(live))
        await wait_for(lambda: hasattr(node, 'ws'))
        release_tick.set()
        await wait_for(captured)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await asyncio.to_thread(jobs.tick)
        captures = [j for j in jobs.jobs() if j.get('missed_captures')]
        assert len(captures) == 1
        assert captures[0]['note'] == 'caught up after 8 missed'
        with api.app.state.db() as db:
            assert not db.get(Node, node_id).downtime_until_reconnect
    finally:
        release_tick.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize('start,days,seen,at,active', [
    ('01:30', list(range(7)), '2026-10-24T05:00:00', '2026-10-24T06:29:59', False),
    ('01:30', list(range(7)), '2026-10-24T05:00:00', '2026-10-24T06:30:00', True),
    ('01:30', list(range(7)), '2026-10-24T05:00:00', '2026-10-24T23:00:00', True),
    ('01:30', list(range(7)), '2026-10-24T15:00:00', '2026-10-24T16:00:00', False),
    ('01:30', list(range(7)), '2026-10-24T06:30:00', '2026-10-24T16:00:00', False),
    ('01:30', list(range(7)), '2026-10-24T15:00:00', '2026-10-25T06:30:00', True),
    # Weekly downtime remains active across days/weeks without a reconnect.
    ('01:30', [5], '2026-10-23T05:00:00', '2026-10-30T23:00:00', True),
    ('01:30', [5], None, '2026-11-13T23:00:00', True),
    # Repeated start uses the first fold; a reconnect in between consumes it.
    ('01:30', [6], '2026-10-31T05:00:00', '2026-11-01T06:29:59', False),
    ('01:30', [6], '2026-10-31T05:00:00', '2026-11-01T06:30:00', True),
    ('01:30', [6], '2026-11-01T06:45:00', '2026-11-01T07:30:00', False),
    # A nonexistent 02:30 start shifts to 03:30, not 03:00 or 01:30.
    ('02:30', [6], '2026-03-07T06:00:00', '2026-03-08T08:29:59', False),
    ('02:30', [6], '2026-03-07T06:00:00', '2026-03-08T08:30:00', True),
])
def test_scheduled_until_reconnect_uses_most_recent_start(start, days, seen, at, active):
    from acc.server.machine_presence import planned_downtime
    node = Node(last_seen=instant(seen) if seen else None, downtime_policy={
        'timezone': 'America/Chicago', 'windows': [{'days': days, 'start': start, 'until_reconnect': True}]})
    assert planned_downtime(node, instant(at)) == {
        'active': active, 'until_reconnect': active, 'ends_at': None}


def test_scheduled_until_reconnect_consumed_and_later_outage_alerts(database):
    from acc.server.machine_presence import evaluate, planned_downtime, transition
    with database() as db:
        node = Node(id='n', name='demo', role='always_on', status='active', downtime_policy={
            'timezone': 'America/Chicago', 'windows': [
                {'days': list(range(7)), 'start': '01:30', 'until_reconnect': True}]})
        db.add(node)
        db.flush()
        transition(db, node, True, instant('2026-10-24T05:00:00'))
        transition(db, node, False, instant('2026-10-24T05:01:00'))
        evaluate(db, node, instant('2026-10-24T05:30:00'))
        db.flush()
        assert db.scalar(select(AttentionItem).where(AttentionItem.resolved_at.is_(None))) is not None
        evaluate(db, node, instant('2026-10-24T20:00:00'))  # long after the former 09:00 end
        db.flush()
        assert db.scalar(select(AttentionItem).where(AttentionItem.resolved_at.is_(None))) is None
        transition(db, node, True, instant('2026-10-24T20:01:00'))
        assert not planned_downtime(node, instant('2026-10-24T20:01:00'))['active']
        # The last-seen fallback must not override the durable reconnect timestamp.
        node.last_seen = instant('2026-10-24T05:00:00')
        assert not planned_downtime(node, instant('2026-10-24T20:02:00'))['active']
        transition(db, node, False, instant('2026-10-24T21:00:00'))
        evaluate(db, node, instant('2026-10-24T21:30:00'))
        db.flush()
        assert db.scalar(select(AttentionItem).where(AttentionItem.resolved_at.is_(None))) is not None
        assert planned_downtime(node, instant('2026-10-25T06:30:00'))['active']


@pytest.mark.parametrize('window,valid', [
    ({'days': [0], 'start': '01:30', 'until_reconnect': True}, True),
    ({'days': [0], 'start': '01:30', 'end': '09:00'}, True),
    ({'days': [0], 'start': '01:30'}, False),
    ({'days': [0], 'start': '01:30', 'until_reconnect': False}, False),
    ({'days': [0], 'start': '01:30', 'end': '09:00', 'until_reconnect': True}, False),
    ({'days': [0], 'start': '01:30', 'end': None, 'until_reconnect': True}, False),
    ({'days': [0], 'start': '01:30', 'until_reconnect': 'yes'}, False),
])
async def test_scheduled_reconnect_window_validation(api, window, valid):
    await api.post('/api/nodes', json={'name': 'demo'})
    response = await api.post('/api/nodes/demo/policy', json={'role': 'always_on',
        'downtime_policy': {'timezone': 'America/Chicago', 'windows': [window]}})
    assert response.status_code == (200 if valid else 422), response.text
    if valid:
        assert response.json()['downtime_policy']['windows'] == [window]


async def test_scheduled_reconnect_flag_changes_revision_and_refuses_stale_save(api):
    node = (await api.post('/api/nodes', json={'name': 'demo'})).json()
    base = {'days': [0], 'start': '01:30'}
    policy = {'timezone': 'America/Chicago', 'windows': [{**base, 'end': '09:00'}]}
    first = await api.post('/api/nodes/demo/policy', json={'role': 'always_on',
        'expected_revision': node['policy_revision'], 'downtime_policy': policy})
    assert first.status_code == 200
    policy['windows'] = [{**base, 'until_reconnect': True}]
    second = await api.post('/api/nodes/demo/policy', json={'role': 'always_on',
        'expected_revision': first.json()['policy_revision'], 'downtime_policy': policy})
    assert second.status_code == 200, second.text
    assert second.json()['policy_revision'] != first.json()['policy_revision']
    assert second.json()['downtime_policy'] == policy
    stale = await api.post('/api/nodes/demo/policy', json={'role': 'always_on',
        'expected_revision': first.json()['policy_revision'], 'downtime_policy': None})
    assert stale.status_code == 409
    with api.app.state.db() as db:
        audit = db.scalars(select(AuditLog).where(AuditLog.action == 'node.policy').order_by(AuditLog.id)).all()
        assert audit[-1].detail['downtime_policy'] == policy


def test_scheduled_reconnect_window_can_overlap_a_fixed_window():
    from acc.server.machine_presence import planned_downtime
    node = Node(last_seen=instant('2026-10-24T00:00:00'), downtime_policy={
        'timezone': 'UTC', 'windows': [
            {'days': [5], 'start': '01:30', 'until_reconnect': True},
            {'days': [5], 'start': '08:00', 'end': '09:00'}]})
    assert planned_downtime(node, instant('2026-10-24T08:30:00')) == {
        'active': True, 'until_reconnect': True, 'ends_at': None}
    node.last_reconnected_at = instant('2026-10-24T08:15:00')
    assert planned_downtime(node, instant('2026-10-24T08:30:00')) == {
        'active': True, 'until_reconnect': False, 'ends_at': instant('2026-10-24T09:00:00')}
    assert not planned_downtime(node, instant('2026-10-24T09:00:00'))['active']


def test_scheduled_reconnect_backup_has_no_invented_return_time(backup_jobs, database, monkeypatch):
    from acc.server import backup_jobs as module
    at = instant('2026-10-24T20:00:00').timestamp()
    monkeypatch.setattr(module.time, 'time', lambda: at)
    with database() as db:
        node = db.get(Node, 'n')
        node.last_seen = instant('2026-10-24T00:00:00')
        node.downtime_policy = {'timezone': 'UTC', 'windows': [
            {'days': [5], 'start': '01:30', 'until_reconnect': True}]}
        db.commit()
    target, = backup_jobs.state()['targets']
    assert target['planned_downtime'] == {'active': True, 'until_reconnect': True, 'ends_at': None}
    assert target['next_capture_expected_at'] is None
    backup_jobs.schedule()
    assert not backup_jobs.pending()


async def test_policy_with_default_empty_windows_retains_its_shape(api):
    await api.post('/api/nodes', json={'name': 'demo'})
    response = await api.post('/api/nodes/demo/policy', json={
        'role': 'always_on', 'downtime_policy': {'timezone': 'UTC'}})
    assert response.status_code == 200, response.text
    assert response.json()['downtime_policy'] == {'timezone': 'UTC', 'windows': []}


def test_heartbeat_after_start_is_not_a_reconnect():
    from acc.server.machine_presence import planned_downtime
    node = Node(last_reconnected_at=instant('2026-10-23T15:00:00'),
                last_seen=instant('2026-10-24T06:30:01'), downtime_policy={
        'timezone': 'America/Chicago', 'windows': [
            {'days': list(range(7)), 'start': '01:30', 'until_reconnect': True}]})
    assert planned_downtime(node, instant('2026-10-24T20:00:00')) == {
        'active': True, 'until_reconnect': True, 'ends_at': None}


async def test_break_it_6_old_enrollment_policy_cannot_modify_reused_machine_name(api):
    original = (await api.post('/api/nodes', json={'name': 'demo', 'role': 'always_on'})).json()
    assert (await api.post('/api/nodes/demo/remove')).status_code == 200
    replacement = (await api.post('/api/nodes', json={'name': 'demo', 'role': 'always_on'})).json()
    assert replacement['id'] != original['id']
    stale = await api.post('/api/nodes/demo/policy', json={
        'role': 'always_on', 'downtime_until_reconnect': True,
        'expected_revision': original['policy_revision']})
    assert stale.status_code == 409, stale.text
    current = next(n for n in (await api.get('/api/nodes')).json() if n['name'] == 'demo')
    assert current['downtime_until_reconnect'] is False
    assert current['policy_revision'] == replacement['policy_revision']
