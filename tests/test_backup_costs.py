"""Backup cadence upgrades and explicitly bounded cost scenarios."""
import json

import pytest
from test_backup_server import restic_binary, source  # noqa: F401

from acc import backup_policy as policy
from acc import backup_server as backup

# Fixtures are imported from the existing isolated backup harness.
# ruff: noqa: F811


def test_missing_old_defaults_upgrade_but_explicit_intervals_survive(source):
    legacy = {'id':'a'*32, 'kind':'local', 'repository':'/example', 'created_at':1, 'enabled':True}
    explicit = {**legacy, 'id':'b'*32, 'schedule_seconds':3600, 'verify_interval_seconds':0}
    backup.private_write(source.root/'targets.json', backup.canonical([legacy, explicit]))
    a, b = source.targets()
    assert a['schedule_seconds'] == 86400
    assert a['verify_interval_seconds'] == 604800
    assert b['schedule_seconds'] == 3600
    assert b['verify_interval_seconds'] == 0
    assert json.loads((source.root/'targets.json').read_bytes()) == [legacy, explicit]


def test_new_node_and_s3_targets_get_daily_weekly_defaults(source):
    for target in [source.add_node_target('a'*32,'archive-node'),
                   source.add_s3_target('https://s3.example.com','backups','auto','example')]:
        assert target['enabled'] is False
        assert target['schedule_seconds'] == 86400
        assert target['verify_interval_seconds'] == 604800


def test_verification_due_first_weekly_and_explicit_every_capture():
    target = {'verify_interval_seconds':604800}
    assert policy.full_due(target, {}, 100)
    assert not policy.full_due(target, {'restore_tested_at':100}, 100+604799)
    assert policy.full_due(target, {'restore_tested_at':100}, 100+604800)
    assert policy.full_due({'verify_interval_seconds':0}, {'restore_tested_at':100}, 101)


def test_monthly_estimate_labels_unknowns_and_scales_with_cadence():
    daily = policy.estimate({'kind':'s3','schedule_seconds':86400,'verify_interval_seconds':604800},
                            {'bytes':1_400_000_000,'uploaded_bytes':10_000_000,'retention_objects':100})
    assert daily['captures'] == 30
    assert daily['full_verifications'] == pytest.approx(30/7)
    assert daily['upload_bytes'] == 300_000_000
    assert daily['download_bytes'] == pytest.approx(12_000_000_000)
    assert daily['s3_policy_requests'] == 30*(400+2+7)
    hourly = policy.estimate({'kind':'node','schedule_seconds':3600,'verify_interval_seconds':0},
                             {'bytes':1_400_000_000,'uploaded_bytes':10_000_000})
    assert hourly['restic_requests'] == 34560
    assert daily['restic_requests'] == pytest.approx(30*30+(30/7)*17)
    assert hourly['captures'] == 720 and hourly['full_verifications'] == 720
    assert hourly['download_bytes'] == 2_016_000_000_000
    unknown = policy.estimate({'kind':'s3'}, {})
    assert unknown['upload_bytes'] is None and unknown['download_bytes'] is None
    assert unknown['s3_policy_requests'] is None


async def test_schedule_api_is_audited_and_preserves_settings_on_invalid_input(api):
    from sqlalchemy import select

    from acc.server.models import AuditLog
    jobs = api.app.state.ctx.backup_jobs
    jobs.service.initialize()
    target = {'id':'a'*32, 'kind':'local', 'repository':'/example', 'created_at':1, 'enabled':False}
    backup.private_write(jobs.service.root/'targets.json', backup.canonical([target]))
    path = '/api/host/backups/' + target['id'] + '/schedule'
    response = await api.post(path, json={'confirmed':True, 'schedule_seconds':3600,'verify_interval_seconds':604800})
    assert response.status_code == 200, response.text
    assert jobs.service.target(target['id'])['schedule_seconds'] == 3600
    assert not jobs.service.target(target['id'])['enabled']
    with jobs.db() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action=='backup.schedule')).detail == {
            'schedule_seconds':3600,'verify_interval_seconds':604800}
    assert (await api.post(path,json={'confirmed':True,'schedule_seconds':1,'verify_interval_seconds':604800})).status_code == 422
    assert jobs.service.target(target['id'])['schedule_seconds'] == 3600


async def test_daily_scheduler_and_stale_age_use_each_targets_interval(api, monkeypatch):
    from acc.server import backup_jobs
    jobs = api.app.state.ctx.backup_jobs
    jobs.service.initialize()
    target = {'id':'a'*32,'kind':'local','repository':'/example','created_at':1,'enabled':True}
    backup.private_write(jobs.service.root/'targets.json',backup.canonical([target]))
    backup.private_write(jobs.service.root/f"result-{target['id']}.json",backup.canonical({'captured_at':1000}))
    jobs.store({'id':'b'*32,'target_id':target['id'],'action':'capture','actor':'owner','at':1000,'state':'complete'})
    monkeypatch.setattr(backup_jobs.time,'time',lambda:1000+3600)
    jobs.schedule()
    assert not jobs.pending()
    assert not jobs.state()['targets'][0]['stale']
    monkeypatch.setattr(backup_jobs.time,'time',lambda:1000+86400)
    jobs.schedule()
    assert len(jobs.pending()) == 1


def test_real_capture_checks_metadata_between_full_restores(source, restic_binary, tmp_path, monkeypatch):
    target = source.add_local_target(tmp_path/'encrypted', binary=restic_binary)
    first = source.back_up(target['id'], binary=restic_binary)
    assert first['check_kind'] == 'full'
    commands = []
    original = backup.LocalTarget.command
    def command(self, *args, **kwargs):
        commands.append(args)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(backup.LocalTarget, 'command', command)
    second = source.back_up(target['id'], binary=restic_binary)
    assert second['check_kind'] == 'metadata'
    assert second['full_backup_id'] == first['backup_id']
    assert second['full_snapshot_id'] == first['snapshot_id']
    assert second['restore_tested_at'] == first['restore_tested_at']
    assert second['verified_at'] == first['verified_at']
    assert ('check',) in commands
    assert not any(args[0]=='dump' or '--read-data' in args for args in commands)
    assert second['uploaded_bytes'] >= 0
    previous = source.root/f"result-{target['id']}.json"
    second['restore_tested_at'] -= 604800
    backup.private_write(previous, backup.canonical(second))
    commands.clear()
    third = source.back_up(target['id'], binary=restic_binary)
    assert third['check_kind'] == 'full'
    assert ('check','--read-data') in commands
    assert any(args[0]=='dump' for args in commands)
    assert third['restore_tested_at'] > first['restore_tested_at']


def test_metadata_failure_keeps_previous_success_and_plaintext_for_retry(source, restic_binary, tmp_path, monkeypatch):
    target = source.add_local_target(tmp_path/'encrypted',binary=restic_binary)
    first = source.back_up(target['id'],binary=restic_binary)
    original = backup.LocalTarget.command
    def command(self,*args,**kwargs):
        if args == ('check',):
            raise backup.Refused('fixture metadata failure')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(backup.LocalTarget,'command',command)
    with pytest.raises(backup.Refused,match='fixture metadata'):
        source.back_up(target['id'],binary=restic_binary)
    assert json.loads((source.root/f"result-{target['id']}.json").read_bytes()) == first


@pytest.mark.parametrize('protocol', ['node','s3'])
def test_real_restic_http_meter_reads_data_only_for_full_checks(restic_binary, tmp_path, protocol):
    from backup_cost_meter import measure
    result = measure(restic_binary, tmp_path, protocol)
    assert result['initial_capture']['upload_bytes'] > 8*1024*1024
    assert result['incremental_capture']['upload_bytes'] > 0
    assert result['metadata_check']['download_bytes'] < result['full_check']['download_bytes'] / 100
    assert result['snapshot_readback']['download_bytes'] > 7*1024*1024
    assert result['full_check']['requests'] > result['metadata_check']['requests']


@pytest.mark.parametrize('objects,requests', [(1,13),(100,409)])
def test_s3_protection_request_count_from_recorded_responses(objects, requests):
    import httpx
    from test_backup_s3 import client, recorded
    count = 0
    def respond(request):
        nonlocal count
        count += 1
        if 'list-type' in request.url.params:
            return httpx.Response(200,text='<ListBucketResult><IsTruncated>false</IsTruncated>'+''.join(
                f'<Contents><Key>acc/test/data/ab/{i:064x}</Key></Contents>' for i in range(objects))+'</ListBucketResult>')
        return recorded(request)
    with client(respond) as s3:
        s3.probe('a'*64)
        before=s3.retained_versions()
        after=s3.retained_versions()
    assert before == after
    assert count == requests
