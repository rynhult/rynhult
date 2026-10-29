"""Server backup jobs on 2026-10-25: a target that failed every hour kept a staging folder each time, the retained
folders used up the capture budget, and then every capture failed, for every target, with an opaque error."""
import asyncio
import logging
import os
import shutil

import pytest
from sqlalchemy import select

from acc import backup_server as backup
from acc.server.backup_jobs import BackupJobs
from acc.server.db import MaintenanceBusy
from acc.server.models import AuditLog

FAILING, WORKING = 'a' * 32, 'b' * 32


@pytest.fixture
def jobs(api, env, monkeypatch):
    jobs = BackupJobs(env['settings'], api.app.state.db)
    jobs.service.initialize()
    backup.private_write(jobs.service.root / 'targets.json', backup.canonical([
        {'id': t, 'kind': 'local', 'repository': '/mounted/example', 'repository_id': t, 'key_epoch': 1,
         'enabled': True, 'created_at': 1} for t in (FAILING, WORKING)]))
    for target in (FAILING, WORKING):
        backup.private_write(jobs.service.root / 'keys' / target, b'example-repository-password')
    uploads = []

    def back_up(target_id, *, binary, stage):
        uploads.append((target_id, stage.name))
        if target_id == FAILING:
            raise backup.Refused('restic backup failed (exit 1)')  # what an unreachable or full receiver gives
        return {'backup_id': stage.name}
    monkeypatch.setattr(jobs.service, 'back_up', back_up)
    jobs.uploads = uploads
    return jobs


def hour(jobs):
    for target in (FAILING, WORKING):
        jobs.request(target, 'capture', 'system')
    jobs.work()
    return {j['target_id']: j for j in jobs.jobs() if j['at'] == max(k['at'] for k in jobs.jobs() if k['target_id'] == j['target_id'])}


def stages(jobs):
    return sorted(p.name for p in jobs.service.staging.iterdir())


def size(path):
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file())


async def test_a_failing_target_keeps_one_stage_and_the_other_target_keeps_working(jobs, api, monkeypatch):
    first = await asyncio.to_thread(hour, jobs)
    assert first[WORKING]['state'] == 'complete' and first[FAILING]['state'] == 'failed'
    assert first[FAILING]['error'] == 'Backup or its check failed: restic backup failed (exit 1)'
    assert stages(jobs) == [first[FAILING]['backup_id']]  # kept for review
    # Room for little more than two captures: before the fix, the third hour's capture failed for both targets.
    monkeypatch.setattr(backup, 'MAX_BYTES', int(size(jobs.service.staging) * 2.5))
    for _ in range(4):
        latest = await asyncio.to_thread(hour, jobs)
        assert latest[WORKING]['state'] == 'complete', latest[WORKING]
        assert latest[FAILING]['error'].endswith('restic backup failed (exit 1)')
        assert stages(jobs) == [latest[FAILING]['backup_id']]  # the newest failed stage only
    with api.app.state.db() as db:
        discarded = db.scalars(select(AuditLog).where(AuditLog.action == 'backup.staging_discarded')).all()
    assert len(discarded) == 4 and all(row.detail['stages'] == 1 and row.detail['bytes'] > 0 for row in discarded)


async def test_retained_stages_over_budget_no_longer_block_capture(jobs, monkeypatch):
    """The live state: many retained stages, more than MAX_BYTES together. The next run keeps only the newest."""
    monkeypatch.setattr(backup, 'MAX_BYTES', 8 << 20)
    names = [os.urandom(16).hex() for _ in range(3)]
    for age, name in enumerate(names):
        stage = jobs.service.staging / name
        (stage / 'git').mkdir(parents=True)
        (stage / 'git' / 'pack').write_bytes(b'\0' * (3 << 20))
        os.utime(stage, (1_000_000 + age, 1_000_000 + age))
    latest = await asyncio.to_thread(hour, jobs)
    assert latest[WORKING]['state'] == 'complete', latest[WORKING]
    assert names[-1] not in stages(jobs) and names[0] not in stages(jobs) and stages(jobs) == [latest[FAILING]['backup_id']]


async def test_a_failed_capture_says_why(jobs, monkeypatch):
    monkeypatch.setattr(backup, 'MAX_BYTES', 100)
    latest = await asyncio.to_thread(hour, jobs)
    assert {j['error'] for j in latest.values()} == {'Backup could not be prepared: ' + backup.STAGING_FULL}
    assert jobs.uploads == []


async def test_other_errors_record_only_their_class(jobs, monkeypatch, caplog):
    def leaky(target_id, *, binary, stage):
        raise RuntimeError('/srv/example/secret-path token=hunter2')
    monkeypatch.setattr(jobs.service, 'back_up', leaky)
    caplog.set_level(logging.INFO, 'acc.supervisor')
    latest = await asyncio.to_thread(hour, jobs)
    assert latest[WORKING]['error'] == 'Backup or its check failed (RuntimeError). Try again, and check the Rynhult log if it keeps failing.'
    assert 'hunter2' not in caplog.text and 'failed (RuntimeError)' in caplog.text


async def test_capture_barrier_in_a_supervisor_stage_is_logged_without_a_traceback(api, monkeypatch, caplog):
    supervisor = api.app.state.supervisor
    calls = []

    async def busy():
        calls.append(1)
        raise MaintenanceBusy('Backup capture or maintenance is busy; retry shortly')
    monkeypatch.setattr(supervisor, 'evaluate_machines', busy)
    caplog.set_level(logging.INFO, 'acc.supervisor')
    supervisor._wake.set()
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(.05)
    await asyncio.sleep(.1)
    assert calls
    records = [r for r in caplog.records if 'maintenance is busy' in r.message]
    assert len(records) == len(calls) and all(r.levelno == logging.INFO for r in records)
    assert 'supervisor stage machines failed' not in caplog.text and 'Traceback' not in caplog.text


async def test_a_kept_stage_never_costs_the_next_capture(jobs, monkeypatch):
    """Review 2026-10-25: one kept stage halves the budget, so above MAX_BYTES / 2 every capture failed again."""
    first = await asyncio.to_thread(hour, jobs)
    kept = first[FAILING]['backup_id']
    monkeypatch.setattr(backup, 'MAX_BYTES', int(size(jobs.service.staging) * 1.5))  # room for one capture, not two
    latest = await asyncio.to_thread(hour, jobs)
    assert latest[WORKING]['state'] == 'complete', latest[WORKING]
    assert latest[FAILING]['error'].endswith('restic backup failed (exit 1)')
    assert stages(jobs) == [latest[FAILING]['backup_id']] and kept not in stages(jobs)


async def test_a_cleanup_failure_is_logged_and_never_stops_the_capture(jobs, monkeypatch, caplog):
    def denied(keep=()):
        raise PermissionError(13, 'Permission denied', '/srv/example/secret-path')
    monkeypatch.setattr(jobs.service, 'discard_stages', denied)
    caplog.set_level(logging.INFO, 'acc.supervisor')
    latest = await asyncio.to_thread(hour, jobs)
    assert latest[WORKING]['state'] == 'complete', latest[WORKING]
    assert 'could not remove superseded staging: staging cleanup failed (PermissionError)' in caplog.text
    assert 'secret-path' not in caplog.text


def test_discarding_never_follows_links_out_of_staging(jobs, tmp_path):
    outside = tmp_path / 'outside'
    (outside / 'keep').mkdir(parents=True)
    (outside / 'keep' / 'vault.key').write_bytes(b'live')
    staging = jobs.service.staging
    (staging / ('c' * 32)).symlink_to(outside, target_is_directory=True)  # a stage name that is a link
    stage = staging / ('d' * 32)
    stage.mkdir()
    (stage / 'secrets').symlink_to(outside / 'keep', target_is_directory=True)  # a link inside a stage
    (stage / 'acc.db').write_bytes(b'x' * 10)
    (staging / 'not-a-stage').mkdir()
    assert jobs.service.discard_stages() == (1, 10)
    assert (outside / 'keep' / 'vault.key').read_bytes() == b'live'
    assert sorted(p.name for p in staging.iterdir()) == ['c' * 32, 'not-a-stage']
    assert shutil.rmtree.avoids_symlink_attacks  # fd-based removal: no link swapped in mid-walk is followed


def test_the_kept_stage_is_the_newest_complete_one_not_the_newest_folder(jobs):
    """Review 2026-10-25: a capture killed midway leaves a folder without a manifest; mtimes say nothing."""
    staging = jobs.service.staging

    def stage(finished_at, mtime, manifest=True, backup_id=None):
        name = os.urandom(16).hex()
        (staging / name).mkdir()
        if manifest is True:
            backup.private_write(staging / name / 'manifest.json', backup.canonical(
                {'manifest': {'backup_id': backup_id or name, 'finished_at': finished_at}, 'signature': ''}))
        elif manifest:
            (staging / name / 'manifest.json').write_bytes(manifest)
        os.utime(staging / name, (mtime, mtime))
        return name

    newest = stage(2_000, 1_000)
    stage(1_000, 2_000)  # finished earlier, touched later
    stage(None, 3_000, manifest=False)  # a capture killed midway: no manifest
    stage(None, 3_000, manifest=b'{"manifest": ')  # torn
    stage(3_000, 3_000, backup_id='e' * 32)  # a manifest that belongs to another stage
    assert jobs.service.newest_stage().name == newest
    jobs.discard_stages(keep={newest})
    assert stages(jobs) == [newest]


def test_interrupted_jobs_say_what_happens_next(jobs):
    capture = jobs.request(FAILING, 'capture', 'system')
    verify = jobs.request(WORKING, 'verify', 'owner', backup_id='f' * 32)
    for job in (capture, verify):
        jobs.store({**job, 'state': 'running'})
    jobs.reconcile()
    errors = {j['action']: j['error'] for j in jobs.jobs()}
    assert errors == {'capture': 'Interrupted: Rynhult stopped during the job. The next backup runs it again.',
                      'verify': 'Interrupted: Rynhult stopped during the job. Run it again.'}


async def test_a_kept_stage_goes_once_no_target_is_enabled(jobs):
    """Review 2026-10-25: cleanup ran only before captures, so disabling the failing target kept its plaintext stage."""
    first = await asyncio.to_thread(hour, jobs)
    await asyncio.to_thread(jobs.tick)  # enabled targets, captured this hour: the failed stage stays for review
    assert stages(jobs) == [first[FAILING]['backup_id']]
    targets = jobs.service.targets()
    backup.private_write(jobs.service.root / 'targets.json', backup.canonical([{**t, 'enabled': False} for t in targets]))
    await asyncio.to_thread(jobs.tick)
    assert stages(jobs) == []


async def test_next_target_and_stage_cleanup_do_not_cancel_an_active_capture(jobs, monkeypatch):
    import threading

    from acc.restic_process import cancelled, check_cancelled
    event = threading.Event()
    active = []
    order = []
    discard = jobs.service.discard_stages

    def back_up(target_id, *, binary, stage):
        check_cancelled()
        assert not active and stage.exists()
        active.append(target_id)
        order.append(target_id)
        # The same per-tick event covers serial targets; no target start sets it.
        assert cancelled.get() is event and not event.is_set()
        active.clear()
        return {'backup_id': stage.name}

    def cleanup(keep):
        assert not active
        check_cancelled()
        return discard(keep)

    monkeypatch.setattr(jobs.service, 'back_up', back_up)
    monkeypatch.setattr(jobs.service, 'discard_stages', cleanup)
    token = cancelled.set(event)
    try:
        result = await asyncio.to_thread(hour, jobs)
    finally:
        cancelled.reset(token)
    assert order == [FAILING, WORKING]
    assert all(job['state'] == 'complete' for job in result.values())
    assert not event.is_set() and not stages(jobs)
