"""Repository lock recovery uses disposable fake CLIs and repositories only."""
import asyncio
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone

import pytest
from test_backup_server import source  # noqa: F401

from acc import backup_server as backup

REPOSITORY = 'a' * 64
LOCK = 'b' * 64


@pytest.fixture
def cli(tmp_path):
    import sys
    script = tmp_path / 'restic'
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'calls': []}))
    (tmp_path / 'repository' / 'locks').mkdir(parents=True)
    # Locks are files in repository/locks/ as restic keeps them, so LocalTarget.delete_lock is exercised for real.
    script.write_text(f'''#!{sys.executable}
import json, os, signal, sys, time
from pathlib import Path
p = Path(__file__).with_name('state.json')
s = json.loads(p.read_text())
a = sys.argv[1:]
if a == ['version']:
    print('restic 0.19.1'); sys.exit(0)
folder = Path(a[a.index('--repo') + 1]) / 'locks'
locks = {{f.name: f for f in folder.iterdir()}}
command = next(x for x in a if x in ('check','backup','cat','list','unlock','snapshots'))
s['calls'].append(a); p.write_text(json.dumps(s))
if s.get('slow_metadata') and command == 'cat' and 'config' not in a:
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # inherited ignored SIGINT must not defeat the deadline
    time.sleep(s['slow_metadata'])  # a slow or trickling receiver
if command == 'list':
    print('\\n'.join(sorted(locks))); sys.exit(0)
if command == 'cat':
    if 'config' in a and '--no-lock' not in a and locks: sys.exit(11)
    if 'config' in a: print(json.dumps({{'id': '{REPOSITORY}'}}))
    elif a[a.index('lock')+1] in locks: print(locks[a[a.index('lock')+1]].read_text())
    else: sys.exit(1)
    sys.exit(0)
if command == 'unlock':
    for name, f in locks.items():
        if json.loads(f.read_text()).get('restic_stale'): f.unlink()
    sys.exit(0)
if s.get('slow'):
    marker = p.with_name('active-lock')
    marker.write_text(str(os.getpid()))
    def stop(*a):
        marker.unlink(missing_ok=True)
        sys.exit(130)
    signal.signal(signal.SIGINT, stop)
    while True: time.sleep(.02)
if locks:
    print('repository locked; token=DO_NOT_LEAK /private/customer.txt', file=sys.stderr)
    sys.exit(11)
print('[]' if command == 'snapshots' else 'checked')
''')
    script.chmod(0o700)
    backend = backup.LocalTarget(tmp_path / 'repository', tmp_path / 'key', script)
    backend.expected_repository_id = REPOSITORY
    audits = []
    backend.lock_audit = audits.append
    return backend, state, audits


def lock(*, host='archive.example.lan', age=60, pid=123, stale=False):
    return {'hostname': host, 'time': datetime.fromtimestamp(time.time()-age, timezone.utc).isoformat(),
            'pid': pid, 'exclusive': True, 'restic_stale': stale,
            'username': 'DO_NOT_LEAK', 'password': 'DO_NOT_LEAK'}


def put(state, *, locks=None, **values):
    if locks is not None:
        folder = state.with_name('repository') / 'locks'
        for old in folder.iterdir():
            old.unlink()
        for name, record in locks.items():
            (folder / name).write_text(json.dumps(record))
    state.write_text(json.dumps({**json.loads(state.read_text()), **values}))


def present(state):
    return {f.name for f in (state.with_name('repository') / 'locks').iterdir()}


def restic_unlocks(state):
    return sum('unlock' in call for call in json.loads(state.read_text())['calls'])


def ours(backend, pid, *, started, ended):
    """A child this main started and reaped, as _raw_command records it."""
    backend._record_child({'pid': pid, 'boot': backup.boot_id(), 'started': time.time() - started,
                           'ended': None if ended is None else time.time() - ended})


def test_live_foreign_lock_is_explained_without_raw_output(cli):
    backend, state, audits = cli
    put(state, locks={LOCK: lock()})
    with pytest.raises(backup.RepositoryLocked) as caught:
        backend.command('check', '--read-data')
    assert 'Backup storage is locked' in str(caught.value)
    assert caught.value.locks[0]['hostname'] == 'archive.example.lan'
    assert caught.value.locks[0]['pid'] == 123
    assert caught.value.locks[0]['exclusive'] is True
    assert 50 <= caught.value.locks[0]['age_seconds'] <= 90
    assert 'DO_NOT_LEAK' not in str(caught.value) + json.dumps(caught.value.locks)
    assert not audits and LOCK in present(state)


@pytest.mark.parametrize('kind', ['old-foreign', 'ours-reaped', 'dead-local-not-ours', 'live-local', 'recent-foreign',
                                  'malformed'])
def test_automatic_unlock_is_conservative_audited_and_retries_once(cli, kind, monkeypatch):
    backend, state, audits = cli
    record = lock()
    if kind == 'old-foreign': record = lock(age=14401)
    if kind == 'ours-reaped':
        record = lock(host=socket.gethostname(), pid=4242)
        ours(backend, 4242, started=120, ended=30)
    if kind == 'dead-local-not-ours':
        record = lock(host=socket.gethostname(), pid=4242)
        def absent(pid, signal):
            raise ProcessLookupError()
        monkeypatch.setattr(backup.os, 'kill', absent)
    if kind == 'live-local': record = lock(host=socket.gethostname(), pid=os.getpid())
    if kind == 'malformed': record['time'] = 'not a date'
    put(state, locks={LOCK: record})
    if kind in ('old-foreign', 'ours-reaped'):
        assert backend.command('check') == b'checked\n'
        assert any(a.get('phase') == 'complete' and LOCK in a['removed'] for a in audits)
        assert LOCK not in present(state)
    else:
        with pytest.raises(backup.RepositoryLocked): backend.command('check')
        assert not audits and LOCK in present(state)
    calls = json.loads(state.read_text())['calls']
    assert sum('check' in c for c in calls) == (2 if audits else 1)
    assert restic_unlocks(state) == 0  # exactly the judged IDs, never restic's repository-wide unlock


def test_manual_unlock_only_removes_stale_locks_and_audits(cli):
    backend, state, audits = cli
    put(state, locks={LOCK: lock(age=1900), 'c'*64: lock()})
    result = backend.remove_stale_locks(automatic=False)
    assert result['removed'] == [LOCK]
    assert 'c'*64 in present(state)
    assert audits[0]['phase'] == 'requested' and audits[-1]['phase'] == 'complete'


def test_repository_mismatch_never_unlocks(cli):
    backend, state, audits = cli
    backend.expected_repository_id = 'd'*64
    put(state, locks={LOCK: lock(age=15000, stale=True)})
    with pytest.raises(backup.RepositoryLocked): backend.command('check')
    assert not audits and LOCK in present(state)


def test_timeout_terminates_gracefully_and_waits_for_lock_cleanup(cli):
    backend, state, _ = cli
    put(state, slow=True)
    backend.command_timeout = .2
    with pytest.raises(backup.Refused, match='timed out'): backend.command('check')
    assert not state.with_name('active-lock').exists()


async def test_cancellation_waits_for_restic_lock_cleanup(cli):
    from acc.restic_process import cancelled
    backend, state, _ = cli
    put(state, slow=True)
    event = threading.Event()
    token = cancelled.set(event)
    try:
        task = asyncio.create_task(asyncio.to_thread(backend.command, 'check'))
    finally:
        cancelled.reset(token)
    for _ in range(200):
        if state.with_name('active-lock').exists(): break
        await asyncio.sleep(.01)
    assert state.with_name('active-lock').exists()
    event.set()
    with pytest.raises(InterruptedError): await task
    assert not state.with_name('active-lock').exists()


def test_two_backend_instances_never_start_concurrent_commands(cli):
    from concurrent.futures import ThreadPoolExecutor
    backend, state, _ = cli
    put(state, slow=True)
    backend.command_timeout = .5
    other = backup.LocalTarget(backend.repository, backend.password_file, backend.binary)
    other.lease_timeout = .05
    with ThreadPoolExecutor() as pool:
        future = pool.submit(backend.command, 'check')
        for _ in range(200):
            if state.with_name('active-lock').exists(): break
            time.sleep(.01)
        with pytest.raises(backup.BackendBusy, match='in progress'): other.command('check')
        with pytest.raises(backup.Refused): future.result(timeout=5)
    assert sum('check' in c for c in json.loads(state.read_text())['calls']) == 1


async def test_owner_unlock_requires_confirmation_and_queues_audited_job(api, env):
    from helpers import pause_supervisor
    from test_auth import cookie_client, do_setup
    from test_users import csrf

    from acc.server.models import AuditLog
    await pause_supervisor(api)
    await do_setup(api, env)
    owner = cookie_client(api)
    jobs = api.app.state.ctx.backup_jobs
    jobs.service.initialize()
    target = {'id': 'f'*32, 'kind': 'local', 'repository': '/mounted/example',
              'repository_id': REPOSITORY, 'key_epoch': 1, 'enabled': False, 'created_at': 1}
    backup.private_write(jobs.service.root / 'targets.json', backup.canonical([target]))
    path = f"/api/host/backups/{target['id']}/unlock"
    assert (await owner.post(path, json={}, headers=csrf(owner))).status_code == 422
    result = await owner.post(path, json={'confirmed': True}, headers=csrf(owner))
    assert result.status_code == 202, result.text
    assert result.json()['action'] == 'unlock'
    assert result.json()['actor'] == 'casey'
    with api.app.state.db() as db:
        row = db.query(AuditLog).filter_by(action='backup.request', target=target['id']).one()
        assert row.detail['action'] == 'unlock'
    api.cookies.clear()
    assert (await api.post(path, json={'confirmed': True})).status_code == 401


async def test_worker_persists_safe_lock_details_for_host_status(api, env, cli, monkeypatch):
    from helpers import pause_supervisor
    await pause_supervisor(api)
    backend, state, _ = cli
    put(state, locks={LOCK: lock()})
    jobs = api.app.state.ctx.backup_jobs
    jobs.service.initialize()
    target = {'id': 'f'*32, 'kind': 'local', 'repository': '/mounted/example',
              'repository_id': REPOSITORY, 'key_epoch': 1, 'enabled': False, 'created_at': 1}
    backup.private_write(jobs.service.root / 'targets.json', backup.canonical([target]))
    def verify(*a, **kw): backend.command('check')
    monkeypatch.setattr(jobs.service, 'verify', verify)
    jobs.request(target['id'], 'verify', 'owner', 'e'*32)
    await asyncio.to_thread(jobs.work)
    last = jobs.state()['targets'][0]['last_job']
    assert last['state'] == 'failed' and last['repository_locked'] is True
    assert last['locks'][0]['hostname'] == 'archive.example.lan'
    assert 'Backup storage is locked' in last['error']
    assert 'DO_NOT_LEAK' not in json.dumps(last)


async def test_worker_shutdown_signals_restic_and_joins_cleanup(api, cli, monkeypatch):
    from helpers import pause_supervisor
    await pause_supervisor(api)
    backend, state, _ = cli
    put(state, slow=True)
    jobs = api.app.state.ctx.backup_jobs
    monkeypatch.setattr(jobs, 'tick', lambda: backend.command('check'))
    task = asyncio.create_task(jobs.run())
    try:
        for _ in range(200):
            if state.with_name('active-lock').exists(): break
            await asyncio.sleep(.01)
        assert state.with_name('active-lock').exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not state.with_name('active-lock').exists()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_mixed_lock_set_never_auto_unlocks(cli):
    backend, state, audits = cli
    put(state, locks={LOCK: lock(age=15000, stale=True), 'c'*64: lock()})
    with pytest.raises(backup.RepositoryLocked): backend.command('check')
    assert not audits


def test_failed_audit_prevents_unlock(cli):
    backend, state, _ = cli
    put(state, locks={LOCK: lock(age=15000, stale=True)})
    def denied(detail): raise RuntimeError('audit unavailable')
    backend.lock_audit = denied
    with pytest.raises(RuntimeError, match='audit unavailable'): backend.command('check')
    assert LOCK in present(state) and restic_unlocks(state) == 0


def test_force_kill_is_bounded_and_reaps_only_owned_child(cli, monkeypatch):
    from acc import restic_process
    backend, state, _ = cli
    # This intentionally uncooperative fixture emulates a child whose network cleanup hangs.
    script = backend.binary.read_text().replace('signal.signal(signal.SIGINT, stop)',
                                               'signal.signal(signal.SIGINT, signal.SIG_IGN)')
    backend.binary.write_text(script)
    put(state, slow=True)
    backend.command_timeout = .2
    monkeypatch.setattr(restic_process, 'CLEANUP_SECONDS', .1)
    with pytest.raises(backup.Refused, match='timed out'): backend.command('check')
    pid = int(state.with_name('active-lock').read_text())
    with pytest.raises(ProcessLookupError): os.kill(pid, 0)


def test_one_retry_reports_a_new_lock_without_unlocking_again(cli, monkeypatch):
    backend, state, audits = cli
    put(state, locks={LOCK: lock(age=15000)})
    deleted, delete = [], backend.delete_lock

    def delete_then_another_appears(identity):
        delete(identity)
        deleted.append(identity)
        (state.with_name('repository') / 'locks' / ('c' * 64)).write_text(json.dumps(lock(host='another.example.lan')))

    monkeypatch.setattr(backend, 'delete_lock', delete_then_another_appears)
    with pytest.raises(backup.RepositoryLocked) as caught: backend.command('check')
    assert caught.value.locks[0]['hostname'] == 'another.example.lan'
    calls = json.loads(state.read_text())['calls']
    assert deleted == [LOCK] and restic_unlocks(state) == 0
    assert sum('check' in c for c in calls) == 2


async def test_target_check_keeps_safe_lock_reason(api, monkeypatch):
    jobs = api.app.state.ctx.backup_jobs
    def locked(*a, **kw):
        raise backup.RepositoryLocked('check', [{'id':LOCK, 'hostname':'archive.example.lan', 'pid':123,
                                                'age_seconds':60, 'exclusive':True, 'created_at':1}])
    monkeypatch.setattr(jobs.service, 'check_target', locked)
    response = await api.post(f"/api/host/backups/{'f'*32}/check")
    assert response.status_code == 409
    assert 'Backup storage is locked' in response.json()['detail']
    assert 'archive.example.lan' in response.json()['detail']


async def test_manual_job_reports_remaining_active_lock_and_records_owner_audit(api, env, cli, monkeypatch):
    from contextlib import contextmanager

    from helpers import pause_supervisor

    from acc.server.models import AuditLog
    await pause_supervisor(api)
    backend, state, _ = cli
    put(state, locks={LOCK: lock(age=1900, stale=True), 'c'*64: lock()})
    jobs = api.app.state.ctx.backup_jobs
    jobs.service.initialize()
    target = {'id':'f'*32,'kind':'local','repository':'/mounted/example','repository_id':REPOSITORY,
              'key_epoch':1,'enabled':False,'created_at':1}
    backup.private_write(jobs.service.root / 'targets.json', backup.canonical([target]))
    @contextmanager
    def configured(target, binary):
        yield jobs.service.configure_backend(backend, target)
    monkeypatch.setattr(jobs.service, 'backend', configured)
    jobs.request(target['id'], 'unlock', 'casey')
    await asyncio.to_thread(jobs.work, binary=backend.binary)
    last = jobs.state()['targets'][0]['last_job']
    assert last['state'] == 'complete'
    assert last['result']['removed'] == [LOCK] and last['result']['remaining'] == 1
    assert last['repository_locked'] and last['locks'][0]['id'] == 'c'*64
    with api.app.state.db() as db:
        rows = db.query(AuditLog).filter_by(action='backup.unlock', actor='casey').order_by(AuditLog.id).all()
        assert [row.detail['phase'] for row in rows] == ['requested', 'complete']
        assert rows[-1].detail['removed'] == [LOCK]
        assert 'DO_NOT_LEAK' not in json.dumps([r.detail for r in rows])


async def test_unlock_request_never_returns_an_unrelated_pending_job(api):
    jobs = api.app.state.ctx.backup_jobs
    jobs.service.initialize()
    target = {'id':'f'*32,'kind':'local','repository':'/mounted/example','repository_id':REPOSITORY,
              'key_epoch':1,'enabled':False,'created_at':1}
    backup.private_write(jobs.service.root / 'targets.json', backup.canonical([target]))
    jobs.request(target['id'], 'verify', 'casey', 'e'*32)
    with pytest.raises(backup.Refused, match='active'):
        jobs.request(target['id'], 'unlock', 'casey')


def test_setup_preserves_lock_reason_instead_of_attempting_init(source, cli, monkeypatch):  # noqa: F811
    from contextlib import contextmanager
    backend, state, _ = cli
    put(state, locks={LOCK: lock()})
    target = source.add_node_target('d'*32, 'archive-node')
    @contextmanager
    def configured(*a): yield backend
    monkeypatch.setattr(source, 'backend', configured)
    with pytest.raises(backup.RepositoryLocked):
        source.check_target(target['id'], binary=backend.binary)


@pytest.mark.parametrize('field,value', [('time', 123), ('hostname', 'token=DO_NOT_LEAK\n/private/path'),
                                         ('pid', -1), ('pid', True), ('exclusive', 'yes')])
def test_untrusted_lock_metadata_is_unknown_not_an_unsafe_recovery(cli, field, value):
    backend, state, audits = cli
    record = lock(age=15000, stale=True)
    record[field] = value
    put(state, locks={LOCK: record})
    with pytest.raises(backup.RepositoryLocked) as caught: backend.command('check')
    assert caught.value.locks == [] and 'Lock details unavailable' in str(caught.value)
    assert 'DO_NOT_LEAK' not in str(caught.value)
    assert not audits


def test_cancel_racing_child_exit_closes_owned_pipes(cli, monkeypatch):
    from acc import restic_process
    backend, _, _ = cli
    started = []
    popen = restic_process.subprocess.Popen
    def spawn(*a, **kw):
        process = popen(*a, **kw)
        started.append(process)
        return process
    def cancel_after_exit():
        if started:
            started[0].wait(timeout=5)
            raise InterruptedError('cancel coincided with child exit')
    monkeypatch.setattr(restic_process.subprocess, 'Popen', spawn)
    monkeypatch.setattr(restic_process, 'check_cancelled', cancel_after_exit)
    with pytest.raises(InterruptedError): backend.command('check')
    assert started[0].stdout.closed and started[0].stderr.closed


@pytest.mark.parametrize('owner', ['other-user', 'same-user-live', 'unknown', 'dead-not-ours'])
def test_owner_unlock_never_removes_a_recent_same_host_lock_it_cannot_prove_is_its_own(cli, owner, monkeypatch):
    """BR-1/BR-3: restic 0.19.1 calls a same-host lock under 30 minutes stale whenever it cannot signal the PID (EPERM
    included; accd runs as `acc`, so an owner's live `sudo restic check` qualifies). And a matching hostname is not
    proof: an HA standby or restored clone shares it, with PIDs that look dead here. Only this main's own reaped
    children, or the age threshold, make a recent lock removable."""
    backend, state, audits = cli
    errors = {'other-user': PermissionError(1, 'not permitted'), 'unknown': OSError(22, 'invalid'),
              'dead-not-ours': ProcessLookupError()}

    def kill(pid, signal):
        if owner in errors:
            raise errors[owner]

    monkeypatch.setattr(backup.os, 'kill', kill)
    ours(backend, 4242, started=3000, ended=2000)  # our child with that PID, but long before this lock existed
    put(state, locks={LOCK: lock(host=socket.gethostname(), age=600, pid=4242, stale=True)})
    result = backend.remove_stale_locks(automatic=False, actor='owner')
    assert result['removed'] == [] and result['remaining'] == 1
    assert LOCK in present(state) and not audits and restic_unlocks(state) == 0


@pytest.mark.parametrize('ended', [30, None])
def test_owner_unlock_removes_its_own_reaped_or_crashed_child_lock_and_old_locks(cli, ended, monkeypatch):
    backend, state, audits = cli

    def absent(pid, signal):
        raise ProcessLookupError()

    monkeypatch.setattr(backup.os, 'kill', absent)  # a crashed run's child (ended None) must be gone
    ours(backend, 4242, started=900, ended=ended)
    put(state, locks={LOCK: lock(host=socket.gethostname(), age=600, pid=4242),
                      'c' * 64: lock(host='standby.example.lan', age=1900)})
    assert backend.remove_stale_locks(automatic=False, actor='owner')['removed'] == sorted([LOCK, 'c' * 64])
    assert [a['phase'] for a in audits] == ['requested', 'complete'] and not present(state)


@pytest.mark.parametrize('race', ['new-live-lock', 'candidate-rewritten'])
def test_removal_rechecks_the_exact_ids_and_removes_only_those(cli, race):
    """BR-2: restic's repository-wide unlock would also remove a lock created after the check (for example an owner's
    `sudo restic` on this host, which restic calls stale on EPERM). Only the judged IDs are removed, each re-read just
    before removal and removed only if unchanged and still removable."""
    backend, state, audits = cli
    folder = state.with_name('repository') / 'locks'
    put(state, locks={LOCK: lock(age=1900)})
    audit = backend.lock_audit

    def meanwhile(detail):
        audit(detail)
        if detail['phase'] == 'requested':
            if race == 'new-live-lock':
                (folder / ('c' * 64)).write_text(json.dumps(lock(host=socket.gethostname(), age=0, pid=1, stale=True)))
            else:
                (folder / LOCK).write_text(json.dumps(lock(age=5)))

    backend.lock_audit = meanwhile
    result = backend.remove_stale_locks(automatic=False, actor='owner')
    assert restic_unlocks(state) == 0
    if race == 'new-live-lock':
        assert result['removed'] == [LOCK] and present(state) == {'c' * 64}
    else:
        assert result['removed'] == [] and present(state) == {LOCK}


def test_remote_backends_delete_exactly_one_lock_object(tmp_path, monkeypatch):
    """BR-2 on the node receiver (REST DELETE of locks/<id>) and S3 (DELETE of <prefix>/locks/<id>)."""
    from acc import backup_s3
    monkeypatch.setattr('acc.restic_process.run', lambda *a, **kw: type('Result', (), {'stdout': b'restic 0.19.1'})())
    rest = backup.RestTarget('http://receiver.example.lan:8000/main/', tmp_path / 'key', tmp_path / 'cred', 'main',
                             '/restic')
    sent = []
    monkeypatch.setattr(rest, 'receiver_request', lambda method, path, body=None: sent.append((method, path)) or (200, b''))
    rest.delete_lock(LOCK)
    assert sent == [('DELETE', 'locks/' + LOCK)]
    monkeypatch.setattr(rest, 'receiver_request', lambda *a, **kw: (403, b''))
    with pytest.raises(backup.Refused, match='receiver refused to remove the lock'):
        rest.delete_lock(LOCK)

    class Client:
        def __init__(self, target, credentials):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def request(self, method, key):
            sent.append((method, key))
            return type('Response', (), {'status_code': 204})()

    (tmp_path / 'cred').write_text(json.dumps({'access_key_id': 'EXAMPLE', 'secret_access_key': 'example'}))
    monkeypatch.setattr(backup_s3, 'S3', Client)
    s3 = backup_s3.S3Target({'endpoint': 'https://s3.example.com', 'bucket': 'demo', 'prefix': 'main/', 'region': 'auto'},
                            tmp_path / 'key', tmp_path / 'cred', '/restic')
    s3.delete_lock(LOCK)
    assert sent[-1] == ('DELETE', 'main/locks/' + LOCK)


def test_children_are_recorded_and_bounded(cli):
    """BR-3: every locking restic child is recorded (PID, boot, start, reaped); metadata reads are not."""
    backend, state, _ = cli
    for _ in range(3):
        backend.command('check')
    children = backend.children()
    assert len(children) == 3 and all(c['ended'] is not None and c['ended'] >= c['started'] for c in children)
    assert all(c['boot'] == backup.boot_id() for c in children)
    for pid in range(backup.MAX_CHILDREN + 10):
        backend._record_child({'pid': 10 + pid, 'boot': 'b', 'started': pid, 'ended': pid + 1})
    assert len(backend.children()) == backup.MAX_CHILDREN


def test_lock_metadata_is_bounded_in_count_and_time(cli, monkeypatch):
    """BR-4: a slow or hostile receiver cannot hold the target's lease for hours through lock metadata reads."""
    backend, state, _ = cli
    put(state, locks={f'{n:064x}': lock() for n in range(backup.MAX_LOCKS + 1)})
    with pytest.raises(backup.Refused, match='metadata unavailable'):
        backend.locks()
    assert sum('cat' in c for c in json.loads(state.read_text())['calls']) == 0  # too many: none read
    monkeypatch.setattr(backup, 'METADATA_CALL_SECONDS', .6)
    monkeypatch.setattr(backup, 'METADATA_BUDGET_SECONDS', 1.0)
    for slow, most in ((.35, 4), (30, 2)):  # every read slow but answered; one read that stalls
        put(state, locks={f'{n:064x}': lock() for n in range(backup.MAX_LOCKS)}, slow_metadata=slow, calls=[])
        began = time.monotonic()
        with pytest.raises(backup.Refused, match='metadata unavailable'):
            backend.locks()
        assert time.monotonic() - began < 5
        assert sum('cat' in c for c in json.loads(state.read_text())['calls']) <= most


def test_history_reads_report_locks_and_never_remove_them(source, cli, monkeypatch):  # noqa: F811
    """BR-5: a GET of history never runs recovery, and reports a busy target clearly."""
    from contextlib import contextmanager
    backend, state, audits = cli
    put(state, locks={LOCK: lock(age=15000)})  # removable by the automatic rule
    monkeypatch.setattr(source, 'target', lambda target_id: {'id': 'f' * 32, 'repository_id': REPOSITORY})

    @contextmanager
    def configured(*a):
        yield backend

    monkeypatch.setattr(source, 'backend', configured)
    with pytest.raises(backup.RepositoryLocked):
        source.history('f' * 32, binary=backend.binary)
    assert LOCK in present(state) and not audits
    put(state, locks={})
    other = backup.LocalTarget(backend.repository, backend.password_file, backend.binary)
    with other.held(), pytest.raises(backup.BackendBusy, match='in progress'):
        source.history('f' * 32, binary=backend.binary)


async def test_app_lifespan_shutdown_interrupts_and_reaps_restic(env, broker, cli, monkeypatch):
    from helpers import wait_for

    from acc.server.app import create_app
    backend, state, _ = cli
    put(state, slow=True)
    app = create_app(env['settings'])
    jobs = app.state.ctx.backup_jobs
    monkeypatch.setattr(jobs, 'tick', lambda: backend.command('check'))
    async with app.router.lifespan_context(app):
        await wait_for(lambda: state.with_name('active-lock').exists())
    assert not state.with_name('active-lock').exists()


async def test_target_check_failure_in_restic_keeps_the_owner_guidance(api, monkeypatch):
    """A restic step that fails (or times out) is not a reason the owner can act on by itself: the check names the step
    and keeps the guidance. A specific refusal, such as the receiver's disk check, is shown as it is."""
    jobs = api.app.state.ctx.backup_jobs
    for error, wanted, unwanted in [
            (backup.ResticFailed('restic init failed (exit 1)'), 'Check connection, writer permissions', None),
            (backup.ResticFailed('restic check timed out; child terminated and reaped'), 'restic check timed out', None),
            (backup.Refused('repository identity mismatch'), 'repository identity mismatch', 'Check connection')]:
        def fail(*a, error=error, **kw):
            raise error
        monkeypatch.setattr(jobs.service, 'check_target', fail)
        response = await api.post(f"/api/host/backups/{'f'*32}/check")
        assert response.status_code == 409
        assert wanted in response.json()['detail']
        if unwanted:
            assert unwanted not in response.json()['detail']
    assert 'restic init failed (exit 1)' in str(backup.ResticFailed('restic init failed (exit 1)'))
    assert issubclass(backup.ResticFailed, backup.Refused)  # callers that catch Refused (e.g. cat config, then init) still do


def test_a_failed_restic_command_raises_restic_failed(tmp_path, monkeypatch):
    import subprocess as sp

    from acc import restic_process
    target = object.__new__(backup.LocalTarget)  # no restic binary here; __init__ checks its version
    target.repository, target.password_file, target.binary = tmp_path / 'repo', tmp_path / 'password', tmp_path / 'restic'
    target.command_timeout, target.env = 60, dict
    monkeypatch.setattr(restic_process, 'run', lambda *a, **kw: sp.CompletedProcess(a, 1, b'', b''))
    with pytest.raises(backup.ResticFailed, match='snapshots step failed'):
        target._raw_command('snapshots', record=False)
    monkeypatch.setattr(restic_process, 'run', lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(backup.ResticFailed, match='timed out'):
        target._raw_command('snapshots', record=False)


@pytest.mark.parametrize('stderr,classification', [
    ('unable to refresh lock: token=SECRET /private/repo\ncontext canceled', 'repository lock refresh failed'),
    ('Fatal: failed to refresh lock in time\nSECRET', 'repository lock refresh failed'),
    ('context canceled: SECRET /private/repo', 'operation cancelled inside restic'),
    ('SECRET /private/repo', 'cancellation cause unavailable'),
])
def test_self_cancelled_restic_has_safe_classified_diagnostics(cli, stderr, classification):
    backend, state, _ = cli
    # Exits 130 itself, without receiving any signal from the parent. Pinned
    # restic also uses 130 when its own operation context is cancelled.
    text = backend.binary.read_text()
    text = text.replace("if s.get('slow'):",
                        f"if command == 'check':\n    print({stderr!r}, file=sys.stderr)\n    sys.exit(130)\nif s.get('slow'):")
    backend.binary.write_text(text)
    with pytest.raises(backup.ResticFailed) as caught:
        backend.command('check', '--read-data')
    assert str(caught.value).startswith("The backup tool's check step failed (exit 130)")
    assert classification in str(caught.value)
    assert 'SECRET' not in str(caught.value) and '/private/repo' not in str(caught.value)
    assert '--read-data' in json.loads(state.read_text())['calls'][-1]


def test_full_data_check_keeps_long_deadline_without_stream_idle_budget(cli, monkeypatch):
    import subprocess

    from acc import restic_process
    backend, _, _ = cli
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b'checked', b'')
    monkeypatch.setattr(restic_process, 'run', run)
    assert backend.command('check', '--read-data') == b'checked'
    argv, options = calls[-1]
    assert argv[-2:] == ['check', '--read-data']
    assert '--read-data-subset' not in argv
    assert options['timeout'] == 7200
    assert options['idle_timeout'] is None and options['output_limit'] is None
    assert options['cleanup_timeout'] is None  # metadata's zero cleanup never leaks to check
