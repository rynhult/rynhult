"""Independent retention worker against an in-memory S3 wire protocol, never AWS."""
import base64
import hashlib
import json
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import httpx
import pytest

from acc.backup_s3 import S3
from acc.backup_server import Refused

DAY = 86400
NOW = datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc).timestamp()
PREFIX = 'acc/example/repository'
CONFIG = {'endpoint': 'https://s3.example.com', 'bucket': 'demo-backups', 'region': 'us-east-1',
          'prefix': PREFIX, 'writer_access_key_ids': ['EXAMPLE_WRITER']}
CREDS = {'access_key_id': 'EXAMPLE_MAINTENANCE', 'secret_access_key': 'example-maintenance-secret'}


def stamp(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat().replace('+00:00', 'Z')


class Cloud:
    def __init__(self):
        self.objects = {(PREFIX + '/config', 'old'): [NOW + 34 * DAY, 1024],
                        (PREFIX + '/data/ab/' + 'a' * 64, 'historical+/='): [NOW + 31 * DAY, 1 << 30],
                        (PREFIX + '/data/ab/' + 'a' * 64, 'current'): [NOW + 60 * DAY, 1 << 30],
                        (PREFIX + '/locks/' + 'b' * 64, 'lock'): [NOW + DAY, 20]}
        self.calls, self.puts = [], []
        self.fail, self.lost_response, self.bad_listing = None, None, False

    def handle(self, request):
        query = dict(request.url.params)
        self.calls.append((request.method, str(request.url)))
        if 'versions' in query:
            assert query['prefix'] == PREFIX + '/'
            root = ET.Element('ListVersionsResult')
            ET.SubElement(root, 'IsTruncated').text = 'true' if self.bad_listing else 'false'
            for (key, version), (_, size) in self.objects.items():
                row = ET.SubElement(root, 'Version')
                for tag, value in [('Key', key), ('VersionId', version), ('Size', str(size))]:
                    ET.SubElement(row, tag).text = value
            return httpx.Response(200, content=ET.tostring(root))
        key = request.url.path.removeprefix('/demo-backups/')
        pair = key, query.get('versionId')
        assert 'retention' in query and pair in self.objects
        if request.method == 'PUT':
            assert request.headers['content-md5'] == base64.b64encode(hashlib.md5(request.content).digest()).decode()
            root = ET.fromstring(request.content)
            assert root.find('{*}Mode').text == 'COMPLIANCE'
            until = datetime.fromisoformat(root.find('{*}RetainUntilDate').text.replace('Z', '+00:00')).timestamp()
            assert until >= self.objects[pair][0]  # Fake AWS also refuses shortening.
            assert 'bypass' not in str(request.headers)
            if pair == self.fail:
                return httpx.Response(503)
            self.objects[pair][0] = until
            self.puts.append(pair)
            if pair == self.lost_response:
                self.lost_response = None
                return httpx.Response(503)  # applied, but acknowledgement lost
            return httpx.Response(200)
        assert request.method == 'GET'
        return httpx.Response(200, text='<Retention><Mode>COMPLIANCE</Mode><RetainUntilDate>' +
                              stamp(self.objects[pair][0]) + '</RetainUntilDate></Retention>')


def worker(tmp_path, cloud, **kw):
    from acc.backup_s3_maintenance import Maintenance
    s3 = S3(CONFIG, CREDS, transport=httpx.MockTransport(cloud.handle))
    return Maintenance(s3, CONFIG, tmp_path / 'job', now=lambda: NOW, sleep=lambda _: None,
                       requests_per_second=10, **kw)


def test_dry_run_then_apply_keeps_historical_deduplicated_versions(tmp_path):
    cloud = Cloud()
    job = worker(tmp_path, cloud)
    planned = job.run()
    assert planned['status'] == 'planned' and planned['would_extend'] == 2
    assert planned['versions'] == 3 and planned['extra_gib_days'] > 28
    assert cloud.puts == []
    completed = job.run(apply=True)
    assert completed['status'] == 'complete' and completed['remaining'] == 0
    assert len(cloud.puts) == 2
    assert all(until >= NOW + 60 * DAY for pair, (until, _) in cloud.objects.items() if '/locks/' not in pair[0])
    audit = (tmp_path / 'job/audit.jsonl').read_text()
    assert 'extend_intent' in audit and 'extended' in audit
    assert CREDS['secret_access_key'] not in audit


def test_partial_failure_and_lost_ack_resume_without_repeating_a_put(tmp_path):
    cloud = Cloud()
    pair = (PREFIX + '/config', 'old')
    cloud.lost_response = pair
    job = worker(tmp_path, cloud)
    job.run()
    first = job.run(apply=True, max_updates=1)
    assert first['status'] == 'partial' and first['remaining'] > 0
    second = worker(tmp_path, cloud).run(apply=True)
    assert second['status'] == 'complete'
    assert cloud.puts.count(pair) == 1


@pytest.mark.parametrize('remaining, count', [(35 * DAY, 0), (35 * DAY - 1, 1), (30 * DAY, 1), (-1, 1)])
def test_clock_thresholds_are_utc_and_exact(tmp_path, remaining, count):
    cloud = Cloud()
    cloud.objects = {(PREFIX + '/config', 'old'): [NOW + remaining, 10]}
    assert worker(tmp_path, cloud).run()['would_extend'] == count


def test_writer_is_refused_even_if_provider_would_allow_retention(tmp_path):
    from acc.backup_s3_maintenance import Maintenance
    cloud = Cloud()
    s3 = S3(CONFIG, {**CREDS, 'access_key_id': 'EXAMPLE_WRITER'}, transport=httpx.MockTransport(cloud.handle))
    with pytest.raises(Refused, match='writer'):
        Maintenance(s3, CONFIG, tmp_path / 'job')
    assert cloud.calls == []


def test_partial_listing_never_writes_retention_or_replaces_plan(tmp_path):
    cloud = Cloud()
    cloud.bad_listing = True
    with pytest.raises(Refused, match='listing'):
        worker(tmp_path, cloud).run()
    assert cloud.puts == []
    assert not (tmp_path / 'job/job.json').exists()


def test_changed_binding_and_expired_plan_cannot_apply(tmp_path):
    cloud = Cloud()
    job = worker(tmp_path, cloud)
    job.run()
    job.now = lambda: NOW + DAY + 1
    with pytest.raises(Refused, match='expired'):
        job.run(apply=True)
    job.now = lambda: NOW - 61
    with pytest.raises(Refused, match='clock'):
        job.run(apply=True)
    job.now = lambda: NOW
    job.config = {**CONFIG, 'prefix': 'acc/other/repository'}
    with pytest.raises(Refused, match='binding'):
        job.run(apply=True)
    assert cloud.puts == []


def test_dry_run_cost_ceiling_and_rate_limit(tmp_path):
    cloud = Cloud()
    waits = []
    job = worker(tmp_path, cloud, storage_price=0.03, request_price=0.01)
    job.sleep = waits.append
    plan = job.run()
    assert plan['estimated_extra_storage_usd'] > 0
    assert plan['estimated_request_usd'] > 0
    assert waits and all(delay >= 0 for delay in waits)
    job.max_extra_gib_days = 1
    with pytest.raises(Refused, match='budget'):
        job.run(apply=True)
    assert cloud.puts == []


def test_private_credentials_and_explicit_purpose_required(tmp_path):
    from acc.backup_s3_maintenance import credentials
    path = tmp_path / 'identity.json'
    path.write_text(json.dumps(CREDS))
    path.chmod(0o600)
    with pytest.raises(Refused, match='purpose'):
        credentials(path)
    path.write_text(json.dumps({'purpose': 's3-retention-maintenance', **CREDS}))
    assert credentials(path) == CREDS
    path.chmod(0o644)
    with pytest.raises(Refused, match='private'):
        credentials(path)


def test_valid_pagination_uses_both_markers_and_inventories_historical_versions(tmp_path):
    cloud = Cloud()
    original = cloud.handle
    seen = []

    def paged(request):
        if 'versions' not in request.url.params:
            return original(request)
        marker = request.url.params.get('key-marker')
        seen.append(dict(request.url.params))
        entries = list(cloud.objects.items())
        subset = entries[:2] if marker is None else entries[2:]
        root = ET.Element('ListVersionsResult')
        ET.SubElement(root, 'IsTruncated').text = 'true' if marker is None else 'false'
        for (key, version), (_, size) in subset:
            row = ET.SubElement(root, 'Version')
            for tag, value in [('Key', key), ('VersionId', version), ('Size', str(size))]:
                ET.SubElement(row, tag).text = value
        if marker is None:
            ET.SubElement(root, 'NextKeyMarker').text = entries[1][0][0]
            ET.SubElement(root, 'NextVersionIdMarker').text = entries[1][0][1]
        return httpx.Response(200, content=ET.tostring(root))

    from acc.backup_s3_maintenance import Maintenance
    s3 = S3(CONFIG, CREDS, transport=httpx.MockTransport(paged))
    report = Maintenance(s3, CONFIG, tmp_path / 'job', now=lambda: NOW, sleep=lambda _: None).run()
    assert report['versions'] == 3
    assert report['would_extend'] == 2
    assert seen[1]['version-id-marker'] == 'historical+/='
    assert report['estimated_requests'] == 11  # 2 list pages + 3 retention reads + 6 apply calls


def test_failed_extension_keeps_checkpoint_and_can_resume(tmp_path):
    cloud = Cloud()
    cloud.fail = (PREFIX + '/config', 'old')
    job = worker(tmp_path, cloud)
    job.run()
    partial = job.run(apply=True)
    assert partial['status'] == 'partial' and partial['remaining'] == 1
    assert partial['errors'] == 1
    cloud.fail = None
    assert worker(tmp_path, cloud).run(apply=True)['status'] == 'complete'
    assert len(cloud.puts) == 2


def test_an_independent_extension_can_never_be_shortened(tmp_path):
    cloud = Cloud()
    job = worker(tmp_path, cloud)
    job.run()
    pair = (PREFIX + '/config', 'old')
    cloud.objects[pair][0] = NOW + 90 * DAY
    assert job.run(apply=True)['status'] == 'complete'
    assert pair not in cloud.puts
    assert cloud.objects[pair][0] == NOW + 90 * DAY


def test_long_inventory_uses_its_completion_clock_for_threshold_and_deadline(tmp_path):
    cloud = Cloud()
    cloud.objects = {(PREFIX + '/config', 'old'): [NOW + 35 * DAY + 1, 100]}
    job = worker(tmp_path, cloud)
    calls = [NOW, NOW + 2]
    job.now = lambda: calls.pop(0) if calls else NOW + 2
    plan = job.run()
    assert plan['would_extend'] == 1
    assert plan['until'] == NOW + 2 + 60 * DAY


def test_clock_jump_during_last_extension_cannot_report_complete(tmp_path):
    cloud = Cloud()
    cloud.objects = {(PREFIX + '/config', 'old'): [NOW + 34 * DAY, 100]}
    job = worker(tmp_path, cloud)
    job.run()
    original = cloud.handle
    shifted = [False]

    def shift(request):
        response = original(request)
        if request.method == 'PUT':
            shifted[0] = True
        return response

    job.client.client.close()
    job.client.client = httpx.Client(transport=httpx.MockTransport(shift))
    job.now = lambda: NOW + (31 * DAY if shifted[0] else 0)
    with pytest.raises(Refused, match='expired|clock'):
        job.run(apply=True)


@pytest.mark.parametrize('endpoint', ['https://s3.example.com', 'https://s3.example.com/'])
def test_cli_defaults_to_read_only_plan_and_sanitizes_failures(tmp_path, monkeypatch, capsys, endpoint):
    from acc import backup_s3_maintenance as module
    cloud = Cloud()
    # Freeze only this worker's clock; the signer retains its normal UTC clock.
    original = module.Maintenance
    monkeypatch.setattr(module, 'Maintenance', lambda *a, **kw: original(*a, **kw, now=lambda: NOW, sleep=lambda _: None))
    monkeypatch.setattr(module, 'S3', lambda target, creds: S3(target, creds, transport=httpx.MockTransport(cloud.handle)))
    target, identity = tmp_path / 'target.json', tmp_path / 'identity.json'
    for path, value in [(target, {**CONFIG, 'endpoint': endpoint}), (identity, {'purpose': 's3-retention-maintenance', **CREDS})]:
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    args = ['--target', str(target), '--credentials', str(identity), '--state', str(tmp_path / 'job')]
    assert module.main(args) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'planned'
    assert cloud.puts == []
    assert module.main([*args, '--apply']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    identity.write_text(json.dumps({'purpose': 's3-retention-maintenance', **CREDS, 'access_key_id': 'EXAMPLE_WRITER'}))
    assert module.main(args) == 1
    output = capsys.readouterr()
    assert CREDS['secret_access_key'] not in output.out + output.err


async def test_main_accepts_only_writers_and_rotation_creates_fresh_repository_and_kit(api):
    request = {'endpoint': 'https://s3.example.com', 'bucket': 'demo-backups', 'region': 'us-east-1',
               'credentials': {'access_key_id': 'EXAMPLE_WRITER', 'secret_access_key': 'example-secret'}}
    refused = await api.post('/api/host/backups/s3-targets', json={**request,
        'credentials': {**request['credentials'], 'purpose': 's3-retention-maintenance'}})
    assert refused.status_code == 422
    first = (await api.post('/api/host/backups/s3-targets', json=request)).json()
    second = (await api.post('/api/host/backups/s3-targets', json=request)).json()
    assert first['prefix'] != second['prefix']
    assert not first['enabled'] and not second['enabled']
    kit = (await api.post('/api/host/backups/kit', json={'confirmed': True})).json()
    keys = {t['id']: t['password'] for t in kit['targets']}
    assert keys[first['id']] != keys[second['id']]
    assert set(keys) == {first['id'], second['id']}


def test_unknown_prices_never_report_zero_dollar_cost(tmp_path):
    report = worker(tmp_path, Cloud()).run()
    assert report['extra_gib_days'] > 0
    assert report['estimated_extra_storage_usd'] is None
    assert report['estimated_request_usd'] is None
    assert report['pricing_supplied'] is False


def test_edited_plan_cannot_make_an_unbounded_compliance_commitment(tmp_path):
    cloud = Cloud()
    cloud.objects = {(PREFIX + '/config', 'old'): [NOW + 34 * DAY, 0]}
    job = worker(tmp_path, cloud)
    job.run()
    path = tmp_path / 'job/job.json'
    plan = json.loads(path.read_text())
    plan['until'] = NOW + 3650 * DAY
    path.write_text(json.dumps(plan))
    with pytest.raises(Refused, match='plan'):
        job.run(apply=True)
    assert cloud.puts == []


@pytest.mark.parametrize('mode,deadline', [('GOVERNANCE', stamp(NOW + 60 * DAY)),
                                         ('COMPLIANCE', '2026-12-31T00:00:00')])
def test_unverified_retention_never_produces_an_apply_plan(tmp_path, mode, deadline):
    cloud = Cloud()
    original = cloud.handle

    def unverified(request):
        if 'retention' in request.url.params:
            return httpx.Response(200, text=f'<Retention><Mode>{mode}</Mode><RetainUntilDate>{deadline}</RetainUntilDate></Retention>')
        return original(request)

    job = worker(tmp_path, cloud)
    job.client.client.close()
    job.client.client = httpx.Client(transport=httpx.MockTransport(unverified))
    with pytest.raises(Refused):
        job.run()
    assert not (tmp_path / 'job/job.json').exists()
    assert cloud.puts == []


def test_repository_boundary_violation_refuses_before_any_update(tmp_path):
    cloud = Cloud()
    cloud.objects[('another/repository/config', 'outside')] = [NOW + DAY, 100]
    with pytest.raises(Refused, match='boundary'):
        worker(tmp_path, cloud).run()
    assert cloud.puts == []


def test_over_budget_dry_run_still_reports_reviewable_cost_without_saving_plan(tmp_path):
    cloud = Cloud()
    report = worker(tmp_path, cloud, max_extra_gib_days=1).run()
    assert report['status'] == 'budget_exceeded'
    assert report['extra_gib_days'] > 1
    assert report['would_extend'] == 2 and len(report['objects']) == 2
    assert not (tmp_path / 'job/job.json').exists()
    assert cloud.puts == []


def test_writer_rotation_history_does_not_disable_maintenance_after_twenty_keys(tmp_path):
    from acc.backup_s3_maintenance import Maintenance
    cloud = Cloud()
    config = {**CONFIG, 'writer_access_key_ids': [f'EXAMPLE_WRITER_{i}' for i in range(30)]}
    with S3(config, CREDS, transport=httpx.MockTransport(cloud.handle)) as s3:
        assert Maintenance(s3, config, tmp_path / 'job', now=lambda: NOW, sleep=lambda _: None).run()['status'] == 'planned'
    before = len(cloud.calls)
    with S3(config, {**CREDS, 'access_key_id': 'EXAMPLE_WRITER_29'}, transport=httpx.MockTransport(cloud.handle)) as s3:
        with pytest.raises(Refused, match='writer must never'):
            Maintenance(s3, config, tmp_path / 'job')
    assert len(cloud.calls) == before


def test_audit_identifies_the_maintenance_key_without_logging_its_secret(tmp_path):
    job = worker(tmp_path, Cloud())
    job.run()
    job.run(apply=True)
    audit = (tmp_path / 'job/audit.jsonl').read_text()
    rows = [json.loads(line) for line in audit.splitlines()]
    assert {row.get('maintenance_key_id') for row in rows} == {CREDS['access_key_id']}
    assert CREDS['secret_access_key'] not in audit


def test_daily_job_keeps_thirty_day_floor_on_reused_chunks_for_three_months(tmp_path):
    cloud = Cloud()
    for pair in cloud.objects:
        if '/locks/' not in pair[0]:
            cloud.objects[pair][0] = NOW + 60 * DAY
    initial_deadline = cloud.objects[(PREFIX + '/config', 'old')][0]
    job = worker(tmp_path, cloud)
    for day in range(91):
        at = NOW + day * DAY
        job.now = lambda: at
        # Captures add new snapshots while deduplication keeps the original chunks/config.
        cloud.objects[(PREFIX + '/snapshots/' + f'{day:064x}', 'new')] = [at + 60 * DAY, 128]
        plan = job.run()
        assert plan['status'] == 'planned'
        if plan['would_extend']:
            assert job.run(apply=True)['status'] == 'complete'
        assert all(until >= at + 30 * DAY for (key, _), (until, _) in cloud.objects.items() if '/locks/' not in key)
    assert initial_deadline < at + 30 * DAY  # Hourly/new captures alone would have lost the floor.
    assert cloud.puts and cloud.objects[(PREFIX + '/config', 'old')][0] > initial_deadline


def test_fractional_clock_deadline_survives_provider_millisecond_precision(tmp_path):
    cloud = Cloud()
    original = cloud.handle

    def milliseconds(request):
        response = original(request)
        if request.method == 'PUT':
            pair = cloud.puts[-1]
            cloud.objects[pair][0] = int(cloud.objects[pair][0] * 1000) / 1000
        return response

    job = worker(tmp_path, cloud)
    job.client.client.close()
    job.client.client = httpx.Client(transport=httpx.MockTransport(milliseconds))
    job.now = lambda: NOW + .1234562
    job.run()
    assert job.run(apply=True)['status'] == 'complete'
