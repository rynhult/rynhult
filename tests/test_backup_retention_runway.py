"""Runway is computed from a version-bound receipt, never a maintenance promise."""
import pytest
from isolated_settings import isolated_settings

from acc.backup_server import canonical, private_write
from acc.server.models import AttentionItem

NOW = 1793518200.0
DAY = 86400


@pytest.fixture
def jobs(tmp_path):
    from acc.server.backup_jobs import BackupJobs
    from acc.server.db import make_engine, make_sessionmaker, migrate
    settings = isolated_settings(tmp_path)
    settings.data_dir.mkdir()
    migrate(settings.db_url)
    engine = make_engine(settings.db_url)
    value = BackupJobs(settings, make_sessionmaker(engine))
    value.service.initialize()
    target = value.service.add_s3_target('https://s3.example.com', 'demo-backups', 'us-east-1', 'example')
    target.update(repository_id='a' * 64, enabled=True)
    private_write(value.service.root / 'targets.json', canonical([target]))
    private_write(value.service.root / 'kit-exported', canonical({'repositories': {target['id']: target['repository_id']}}))
    yield value, target
    engine.dispose()


def receipt(jobs, remaining, **changes):
    worker, target = jobs
    value = {'captured_at': NOW, 'verified_at': NOW, 'immutability_checked_at': NOW,
             'version_bound_retention': True, 'immutable_until': NOW + remaining, **changes}
    private_write(worker.service.root / f"result-{target['id']}.json", canonical(value))


@pytest.mark.parametrize('remaining, runway, warning, protected', [
    (60 * DAY, 30, False, True), (37 * DAY + 1, 7 + 1 / DAY, False, True),
    (37 * DAY, 7, True, True), (30 * DAY, 0, True, True), (30 * DAY - 1, 0, True, False),
])
def test_runway_and_warning_boundaries(jobs, remaining, runway, warning, protected):
    receipt(jobs, remaining)
    state = jobs[0].state(now=NOW)
    target = state['targets'][0]
    assert target.get('retention_runway_days') == pytest.approx(runway)
    assert target['retention_warning'] is warning
    assert state['protected'] is protected


@pytest.mark.parametrize('changes', [
    {'version_bound_retention': False}, {'immutable_until': None}, {'immutable_until': float('inf')},
    {'immutability_checked_at': NOW + 1},
])
def test_unverified_or_future_receipt_has_no_invented_runway(jobs, changes):
    receipt(jobs, 60 * DAY, **changes)
    state = jobs[0].state(now=NOW)
    assert state['targets'][0].get('retention_runway_days') is None
    assert not state['protected']


def test_future_capture_cannot_qualify_after_clock_moves_back(jobs):
    receipt(jobs, 60 * DAY, captured_at=NOW + 1)
    assert not jobs[0].state(now=NOW)['protected']


def test_runway_warning_is_not_suppressed_by_maintenance_and_resolves(jobs):
    from sqlalchemy import select
    worker, target = jobs
    receipt(jobs, 36 * DAY)
    worker.remind(now=NOW)
    with worker.db() as db:
        alert = db.scalar(select(AttentionItem).where(AttentionItem.dedupe_key == 'backup:retention:' + target['id']))
        assert alert is not None and alert.resolved_at is None
    receipt(jobs, 60 * DAY)
    worker.remind(now=NOW)
    with worker.db() as db:
        assert db.scalar(select(AttentionItem).where(AttentionItem.dedupe_key == 'backup:retention:' + target['id'])).resolved_at is not None
