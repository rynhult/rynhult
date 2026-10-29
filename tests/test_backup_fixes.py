"""Regression checks for capture admission, removed targets and shutdown."""
import asyncio
import json
import threading

import pytest
from helpers import pause_supervisor, write_db

from acc import backup_server as backup
from acc.maintenance import LeaseBusy
from acc.server.db import MaintenanceBusy
from acc.server.models import AuditLog, Node


@pytest.mark.parametrize('error_type', [BlockingIOError, LeaseBusy, MaintenanceBusy])
@pytest.mark.parametrize('action', ['disable', 'enable', 'run', 'check'])
async def test_backup_routes_report_lease_contention(api, monkeypatch, action, error_type):
    jobs = api.app.state.ctx.backup_jobs
    def busy(*a, **kw):
        raise error_type('fixture: busy flock')
    method = {'run': 'request', 'check': 'check_target'}.get(action, action)
    monkeypatch.setattr(jobs.service if action == 'check' else jobs, method, busy)
    response = await api.post('/api/host/backups/' + 'a'*32 + '/' + action, json={'confirmed': True})
    assert response.status_code == 409
    assert response.json()['detail'] == 'A backup is running; try again shortly.'


async def test_removed_machine_target_cannot_run_and_can_be_removed_without_erasing_history(api):
    await pause_supervisor(api)
    jobs = api.app.state.ctx.backup_jobs
    target = jobs.service.add_node_target('a'*32, 'archive-box')
    with write_db(jobs.db) as db:
        db.add(Node(id='a'*32, name='archive-box', status='revoked'))
        db.commit()
    result = {'captured_at': 1, 'backup_id': 'b'*32}
    backup.private_write(jobs.service.root / f"result-{target['id']}.json", backup.canonical(result))
    row = (await api.get('/api/host/backups')).json()['targets'][0]
    assert row['machine_removed'] is True and row['stale'] is False
    assert row['status'] == 'machine removed'
    for action in ['run', 'enable']:
        response = await api.post(f"/api/host/backups/{target['id']}/{action}", json={'confirmed': True})
        assert response.status_code == 409 and 'removed' in response.json()['detail']
    url = f"/api/host/backups/{target['id']}/remove"
    assert (await api.post(url, json={'confirmed': False})).status_code == 422
    assert (await api.post(url, json={'confirmed': True})).status_code == 200
    assert jobs.service.targets() == []
    assert json.loads((jobs.service.root / f"result-{target['id']}.json").read_text()) == result
    with jobs.db() as db:
        assert db.query(AuditLog).filter_by(action='backup.target_remove', target=target['id']).count() == 1


async def test_repeated_shutdown_waits_for_backup_thread(api, monkeypatch):
    await pause_supervisor(api)
    jobs = api.app.state.ctx.backup_jobs
    entered, released, finished = threading.Event(), threading.Event(), threading.Event()
    def work():
        from acc.restic_process import cancelled
        entered.set()
        assert cancelled.get().wait(5)
        assert released.wait(5)
        finished.set()
    monkeypatch.setattr(jobs, 'tick', work)
    task = asyncio.create_task(jobs.run())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(.03)
        task.cancel()
        await asyncio.sleep(.03)
        assert not task.done(), 'shutdown abandoned the running backup thread'
    finally:
        released.set()
        await asyncio.gather(task, return_exceptions=True)
    assert finished.is_set()


def test_restic_cleanup_uses_interrupt_before_waiting():
    import signal

    from acc.restic_process import CLEANUP_SECONDS, stop
    class Child:
        events = []
        def poll(self): return None
        def send_signal(self, sig): self.events.append(sig)
        def terminate(self): self.events.append('terminate')
        def communicate(self, **kwargs): self.events.append(('wait', kwargs['timeout']))
    child = Child()
    stop(child)
    assert child.events == [signal.SIGINT, ('wait', CLEANUP_SECONDS)]


@pytest.mark.parametrize('lock', ['.capture-update.lock'])
def test_installer_dry_run_reports_active_capture(tmp_path, lock):
    import subprocess
    from pathlib import Path

    from acc.maintenance import lease
    data = tmp_path / 'srv/acc/data'
    with lease(data / lock):
        result = subprocess.run(['bash', str(Path(__file__).resolve().parents[1] / 'deploy/install.sh'),
                                 '--dry-run', '--alias-root', str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 75
    assert 'a backup capture is running' in (result.stdout + result.stderr).lower()


async def test_real_lease_contention_is_retryable_without_target_mutation(api, monkeypatch):
    from acc.maintenance import lease
    from acc.server import backup_jobs
    jobs = api.app.state.ctx.backup_jobs
    target = jobs.service.add_node_target('c'*32, 'archive-box')
    before = (jobs.service.root / 'targets.json').read_bytes()
    monkeypatch.setattr(backup_jobs, 'lease', lambda path, **kw: lease(path, timeout=0))
    with lease(jobs.service.root / '.intent'):
        response = await api.post(f"/api/host/backups/{target['id']}/disable", json={'confirmed': True})
    assert response.status_code == 409
    assert (jobs.service.root / 'targets.json').read_bytes() == before


def test_service_allows_restic_owned_cleanup():
    from pathlib import Path
    unit = (Path(__file__).resolve().parents[1] / 'deploy/accd.service').read_text()
    assert 'KillMode=mixed' in unit
    assert 'TimeoutStopSec=120' in unit


@pytest.mark.parametrize('pending_state', ['queued', 'running'])
async def test_cloud_target_removal_keeps_history_and_refuses_pending_work(api, monkeypatch, pending_state):
    await pause_supervisor(api)
    jobs = api.app.state.ctx.backup_jobs
    target = jobs.service.add_s3_target('https://s3.example.com', 'backups', 'auto', 'example')
    def no_cloud(*args, **kwargs):
        pytest.fail('Removing a target must not contact or delete bucket data')
    monkeypatch.setattr(jobs.service, 's3', no_cloud)
    result_path = jobs.service.root / f"result-{target['id']}.json"
    backup.private_write(result_path, backup.canonical({'captured_at': 1, 'backup_id': 'b'*32}))
    jobs.remind()
    job = {'id': 'c'*32, 'target_id': target['id'], 'action': 'capture', 'at': 1}
    jobs.store({**job, 'state': pending_state})
    url = f"/api/host/backups/{target['id']}/remove"
    response = await api.post(url, json={'confirmed': True})
    assert response.status_code == 409
    assert 'still running' in response.json()['detail']
    assert len(jobs.service.targets()) == 1
    jobs.store({**job, 'state': 'done'})
    assert (await api.post(url, json={'confirmed': False})).status_code == 422
    assert (await api.post(url, json={'confirmed': True})).status_code == 200
    assert jobs.service.targets() == []
    assert result_path.exists()
    assert (jobs.service.root / f"removed-target-{target['id']}.json").exists()
    assert jobs.jobs()[0]['id'] == job['id']
    from acc.server.models import AttentionItem
    with jobs.db() as db:
        assert db.query(AuditLog).filter_by(action='backup.target_remove', target=target['id']).count() == 1
        assert all(row.resolved_at for row in db.query(AttentionItem).all()
                   if row.dedupe_key.endswith(':' + target['id']))


async def test_active_machine_target_still_cannot_be_removed(api):
    await pause_supervisor(api)
    jobs = api.app.state.ctx.backup_jobs
    target = jobs.service.add_node_target('d'*32, 'archive-box')
    with write_db(jobs.db) as db:
        db.add(Node(id='d'*32, name='archive-box', status='online'))
        db.commit()
    response = await api.post(f"/api/host/backups/{target['id']}/remove", json={'confirmed': True})
    assert response.status_code == 409
    assert len(jobs.service.targets()) == 1
