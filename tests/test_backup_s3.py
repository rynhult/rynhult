"""Recorded S3 protocol responses; no cloud accounts or credentials."""

import httpx
import pytest
from isolated_settings import isolated_settings

from acc.backup_server import Refused


def test_s3_location_rejects_credential_urls_and_non_tls():
    from acc.backup_s3 import location
    for endpoint in ('http://example.com', 'https://user:secret@example.com', 'https://example.com/path',
                     'https://example.com?token=secret', 'https://example.com/#fragment'):
        with pytest.raises(Refused):
            location(endpoint, 'backups', 'us-east-1')
    assert location('https://s3.example.com/', 'backups', 'auto') == ('https://s3.example.com', 'backups', 'auto')


def test_s3_fields_are_purpose_bound_infrastructure_credentials():
    from acc.server.infrastructure import check_fields
    assert check_fields('s3', {'access_key_id': 'EXAMPLE', 'secret_access_key': 'example-secret'})
    with pytest.raises(Exception):
        check_fields('s3', {'access_key_id': 'x', 'secret_access_key': 'x\nmalicious'})


def recorded(request):
    assert request.headers['authorization'].startswith('AWS4-HMAC-SHA256 Credential=EXAMPLE/')
    query = dict(request.url.params)
    if 'versioning' in query:
        return httpx.Response(200, text='<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Status>Enabled</Status></VersioningConfiguration>')
    if 'object-lock' in query:
        return httpx.Response(200, text='<ObjectLockConfiguration><ObjectLockEnabled>Enabled</ObjectLockEnabled><Rule><DefaultRetention><Mode>COMPLIANCE</Mode><Days>60</Days></DefaultRetention></Rule></ObjectLockConfiguration>')
    if 'retention' in query:
        return httpx.Response(200, text='<Retention><Mode>COMPLIANCE</Mode><RetainUntilDate>2099-01-01T00:00:00Z</RetainUntilDate></Retention>')
    if request.method == 'DELETE':
        return httpx.Response(403, text='<Error><Code>AccessDenied</Code></Error>')
    if request.method == 'PUT':
        assert b'plaintext' not in request.content
        return httpx.Response(200, headers={'x-amz-version-id': 'recorded-v1'})
    if 'list-type' in query:
        return httpx.Response(200, text='<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>acc/test/data/ab/cd</Key></Contents></ListBucketResult>')
    return httpx.Response(200, content=b'opaque-encrypted-probe', headers={'x-amz-version-id':'recorded-v1'})


def client(handler=recorded):
    from acc.backup_s3 import S3
    return S3({'endpoint':'https://s3.example.com','bucket':'backups','region':'us-east-1','prefix':'acc/test'},
              {'access_key_id':'EXAMPLE','secret_access_key':'example-secret'},
              transport=httpx.MockTransport(handler))


def test_recorded_immutability_and_repository_retention():
    with client() as s3:
        evidence = s3.probe('a'*64)
        assert evidence['mode'] == 'COMPLIANCE'
        assert evidence['default_days'] == 60
        assert s3.retained_until() > 4_000_000_000


@pytest.mark.parametrize('defect', ['versioning', 'object-lock', 'delete', 'retention', 'redirect'])
def test_recorded_failures_never_claim_immutability(defect):
    def response(request):
        if defect == 'redirect':
            return httpx.Response(307, headers={'location':'https://attacker.example.com/'})
        if defect in request.url.params:
            return httpx.Response(200, text='<Configuration/>')
        if defect == 'delete' and request.method == 'DELETE':
            return httpx.Response(204)
        return recorded(request)
    with client(response) as s3, pytest.raises(Refused):
        s3.probe('a'*64)


def test_versioning_without_object_lock_is_observed_but_unprotected():
    def response(request):
        if 'object-lock' in request.url.params:
            return httpx.Response(501, text='<Error><Code>NotImplemented</Code></Error>')
        return recorded(request)
    with client(response) as s3:
        assert s3.capabilities() == {'versioning': True, 'mode': None, 'default_days': 0}
        with pytest.raises(Refused):
            s3.probe('a'*64)


async def test_owner_s3_setup_seals_credentials_and_refuses_remote_prune(api, env):
    from sqlalchemy import select

    from acc.server.models import AuditLog, InfrastructureCredential
    payload = {'endpoint':'https://s3.example.com','bucket':'demo-backups','region':'auto',
               'credentials':{'access_key_id':'EXAMPLE','secret_access_key':'canary-never-reveal'}}
    response = await api.post('/api/host/backups/s3-targets', json=payload)
    assert response.status_code == 201, response.text
    target = response.json()
    assert target['kind'] == 's3' and target['enabled'] is False
    assert target['prefix'].endswith(target['id'])
    assert 'canary-never-reveal' not in response.text
    with api.app.state.db() as db:
        key = db.get(InfrastructureCredential, target['credential_id'])
        assert key.provider == 's3' and key.purpose == 'backup-writer'
        assert 'canary-never-reveal' not in key.ciphertext
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'backup.target_add'))
    state = await api.get('/api/host/backups')
    assert not state.json()['protected']
    assert 'canary-never-reveal' not in state.text
    for action, body in [('prune', {'confirmed':True}), ('run', {})]:
        refused = await api.post(f"/api/host/backups/{target['id']}/{action}", json=body)
        assert refused.status_code == 409
    kit = await api.post('/api/host/backups/kit', json={'confirmed':True})
    assert 'canary-never-reveal' not in kit.text
    assert 'independent' in kit.json()['targets'][0]['recovery_instructions']


def test_s3_protection_expires_and_requires_fresh_success(tmp_path):
    import time

    from acc.backup_server import canonical, private_write
    from acc.server.backup_jobs import BackupJobs
    from acc.server.db import make_engine, make_sessionmaker, migrate
    settings = isolated_settings(tmp_path)
    settings.data_dir.mkdir()
    migrate(settings.db_url)
    jobs = BackupJobs(settings, make_sessionmaker(make_engine(settings.db_url)))
    service = jobs.service
    service.initialize()
    target = service.add_s3_target('https://s3.example.com', 'demo-backups', 'auto', 'credential-id')
    result = {'captured_at':time.time(), 'verified_at':time.time(), 'snapshot_id':'c'*64,
              'immutability_checked_at':time.time(), 'immutable_until':time.time()+31*86400}
    private_write(service.root / f"result-{target['id']}.json", canonical(result))
    assert not jobs.state()['protected']
    target['repository_id'] = 'r' * 64
    private_write(service.root / 'targets.json', canonical([target]))
    private_write(service.root / 'kit-exported', canonical({'repositories':{target['id']:target['repository_id']}}))
    # Receipts from before version-bound verification must not survive an upgrade as protected.
    assert not jobs.state()['protected']
    result['version_bound_retention'] = True
    private_write(service.root / f"result-{target['id']}.json", canonical(result))
    assert jobs.state()['protected']
    result['immutable_until'] = time.time() + 29 * 86400
    private_write(service.root / f"result-{target['id']}.json", canonical(result))
    assert not jobs.state()['protected']


@pytest.fixture
def minio(tmp_path_factory):
    """Throwaway rootless Podman, including its image/store/config, all under pytest scratch.

    A runtime/image failure skips with a reason, never substitutes a real bucket.
    """
    import json
    import os
    import shutil
    import subprocess
    import time
    import uuid
    if not shutil.which('podman'):
        pytest.skip('MinIO: Podman is not installed')
    root = tmp_path_factory.mktemp('minio')
    podman = ['podman', '--root', str(root / 'store'), '--runroot', str(root / 'run'),
              '--tmpdir', str(root / 'tmp'), '--storage-driver', 'vfs', '--events-backend', 'file',
              '--cgroup-manager', 'cgroupfs']
    (root / 'runtime').mkdir(mode=0o700)
    storage = root / 'storage.conf'
    storage.write_text('[storage]\ndriver = "vfs"\ngraphroot = ' + json.dumps(str(root / 'store'))
                       + '\nrunroot = ' + json.dumps(str(root / 'run')) + '\n')
    environment = {**os.environ, 'HOME':str(root), 'XDG_CONFIG_HOME':str(root / 'config'),
                   'XDG_DATA_HOME':str(root / 'data'), 'TMPDIR':str(root),
                   'CONTAINERS_CONF':'/dev/null', 'CONTAINERS_CONF_OVERRIDE':'/dev/null',
                   'CONTAINERS_STORAGE_CONF':str(storage), 'XDG_RUNTIME_DIR':str(root / 'runtime')}
    name = 'acc-test-s3-' + uuid.uuid4().hex[:12]
    image = 'quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z'
    def run(*args, timeout=90):
        return subprocess.run([*podman, *args], env=environment, capture_output=True, timeout=timeout)
    try:
        try:
            result = run('run', '-d', '--name', name, '--network', 'slirp4netns',
                         '-p', '127.0.0.1::9000', '-e', 'MINIO_ROOT_USER=EXAMPLE',
                         '-e', 'MINIO_ROOT_PASSWORD=example-test-secret', image, 'server', '/data')
        except (OSError, subprocess.TimeoutExpired) as error:
            pytest.skip('MinIO: disposable Podman startup unavailable: ' + type(error).__name__)
        if result.returncode:
            # Fixed diagnostic plus bounded runtime output; only artificial test credentials exist here.
            pytest.skip('MinIO: disposable Podman startup failed: ' + result.stderr.decode(errors='replace')[-800:])
        port = run('port', name, '9000/tcp').stdout.decode().strip().rsplit(':', 1)[-1]
        endpoint = 'http://127.0.0.1:' + port
        for _ in range(100):
            try:
                if httpx.get(endpoint + '/minio/health/ready', timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        else:
            pytest.skip('MinIO: disposable server never became ready')
        yield endpoint
    finally:
        run('rm', '-f', name, timeout=20)
        run('system', 'reset', '--force', timeout=30)


import test_backup_server

source = test_backup_server.source
restic_binary = test_backup_server.restic_binary


def test_real_minio_ciphertext_capture_history_verify_restore_and_no_false_protection(minio, source, restic_binary, tmp_path, monkeypatch):
    """Native restic with a deliberately delete-capable disposable identity.

    Only the fixture's HTTP loopback endpoint bypasses production HTTPS validation.
    Positive Object Lock and restricted-writer behavior use recorded responses.
    """
    from acc import backup_s3
    from acc import backup_server as backup
    original = backup_s3.location
    monkeypatch.setattr(backup_s3, 'location', lambda endpoint, bucket, region:
                        (endpoint, bucket, region) if endpoint == minio else original(endpoint, bucket, region))
    credentials = {'access_key_id':'EXAMPLE','secret_access_key':'example-test-secret'}
    source.s3_credentials = lambda target: credentials
    target = source.add_s3_target(minio, 'demo-backups', 'us-east-1', 'test-only')
    with source.s3(target) as client:
        assert client.request('PUT').status_code == 200
        body = b'<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Status>Enabled</Status></VersioningConfiguration>'
        assert client.request('PUT', query={'versioning':''}, body=body).status_code == 200
    checked = source.check_target(target['id'], binary=restic_binary)
    assert checked['setup_verified_at'] and checked['immutability'] is None
    first = source.back_up(target['id'], binary=restic_binary)
    second = source.back_up(target['id'], binary=restic_binary)
    assert not first.get('immutable_until') and not second.get('immutable_until')
    assert len(source.history(target['id'], binary=restic_binary)['items']) == 2
    result = source.verify(target['id'], first['backup_id'], snapshot_id=first['snapshot_id'], binary=restic_binary)
    assert result['backup_id'] == first['backup_id']
    with source.backend(target, restic_binary) as backend:
        backup.restore(source.kit(), target['id'], first['backup_id'], tmp_path / 'recovered',
                       backend=backend, snapshot_id=first['snapshot_id'])
    assert (tmp_path / 'recovered/data/RESTORE-HOLD').is_file()
    with source.s3(target) as client:
        root = client.get_xml(query={'list-type':'2', 'prefix':target['prefix'] + '/'})
        for item in root.findall('Contents'):
            response = client.request('GET', item.findtext('Key'))
            assert response.status_code == 200
            assert source.settings.vault_key_path.read_bytes() not in response.content
            assert b'backup.capture_checkpoint' not in response.content
    assert not list(source.root.glob('s3-job-*'))


async def test_s3_writer_rotation_invalidates_setup_and_never_reveals_values(api):
    payload = {'endpoint':'https://s3.example.com','bucket':'demo-backups','region':'auto',
               'credentials':{'access_key_id':'EXAMPLE','secret_access_key':'example-secret'}}
    target = (await api.post('/api/host/backups/s3-targets', json=payload)).json()
    response = await api.post(f"/api/host/backups/{target['id']}/credentials", json={
        'credentials':{'access_key_id':'REPLACED','secret_access_key':'new-canary-secret'}})
    assert response.status_code == 200, response.text
    assert response.json()['setup_verified_at'] is None
    assert 'new-canary-secret' not in response.text
    from acc.server.infrastructure import value
    from acc.server.models import InfrastructureCredential
    from acc.server.vault import Vault
    with api.app.state.db() as db:
        key = db.get(InfrastructureCredential, target['credential_id'])
        assert key.revision == 2
        assert value(Vault(api.app.state.ctx.settings.vault_key_path), key)['access_key_id'] == 'REPLACED'


def test_s3_backend_materializes_private_credentials_then_cleans_on_failure(source, monkeypatch):
    import os
    import stat

    target = source.add_s3_target('https://s3.example.com', 'demo-backups', 'auto', 'test-key')
    source.s3_credentials = lambda t: {'access_key_id':'EXAMPLE','secret_access_key':'secret-canary'}
    # Pin/version checks are independently exercised by the existing restic fixture.
    monkeypatch.setattr('acc.restic_process.run', lambda *a, **kw: type('Result', (), {'stdout':b'restic 0.19.1'})())
    with pytest.raises(RuntimeError):
        with source.backend(target, '/restic') as backend:
            file = backend.credential_file
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
            assert stat.S_IMODE(file.parent.stat().st_mode) == 0o700
            assert backend.env()['AWS_SECRET_ACCESS_KEY'] == 'secret-canary'
            assert 'secret-canary' not in str(backend.repo_args())
            assert 'HTTPS_PROXY' not in backend.env()
            assert not any('AWS' in k for k in os.environ if k.startswith('ACC_'))
            raise RuntimeError('interrupted')
    assert not file.exists()
    assert not list(source.root.glob('s3-job-*'))


def test_repository_retention_inspects_all_pages_and_fails_on_old_deduplicated_chunks():
    import time
    def response(request):
        if 'list-type' in request.url.params:
            if 'continuation-token' not in request.url.params:
                return httpx.Response(200,text='<ListBucketResult><IsTruncated>true</IsTruncated><NextContinuationToken>second</NextContinuationToken><Contents><Key>acc/test/snapshots/new</Key></Contents></ListBucketResult>')
            return httpx.Response(200,text='<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>acc/test/data/ab/old</Key></Contents></ListBucketResult>')
        if request.url.path.endswith('/old') and 'retention' in request.url.params:
            return httpx.Response(200,text='<Retention><Mode>COMPLIANCE</Mode><RetainUntilDate>2020-01-01T00:00:00Z</RetainUntilDate></Retention>')
        return recorded(request)
    with client(response) as s3:
        assert s3.retained_until() < time.time()


@pytest.mark.parametrize('version', [None, 'null'])
def test_repository_retention_requires_explicit_object_versions(version):
    def response(request):
        if request.method == 'HEAD':
            return httpx.Response(200, headers={'x-amz-version-id': version} if version else {})
        return recorded(request)
    with client(response) as s3, pytest.raises(Refused, match='versioned'):
        s3.retained_until()


def test_repository_retention_reads_the_version_observed_before_a_concurrent_overwrite():
    import time

    def response(request):
        if request.method == 'HEAD':
            return httpx.Response(200, headers={'x-amz-version-id': 'original-short-retention'})
        if 'retention' in request.url.params and request.url.params.get('versionId') == 'original-short-retention':
            return httpx.Response(200, text='<Retention><Mode>COMPLIANCE</Mode>'
                                  '<RetainUntilDate>2020-01-01T00:00:00Z</RetainUntilDate></Retention>')
        # A concurrent replacement has a longer deadline; unversioned retention would observe it.
        return recorded(request)
    with client(response) as s3:
        assert s3.retained_until() < time.time()


def test_native_restic_s3_refuses_redirects(tmp_path, restic_binary):
    import subprocess
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from acc.backup_s3 import S3Target
    from acc.backup_server import canonical, private_write
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.send_header('Content-Length', '128')
            self.send_header('Last-Modified', 'Thu, 01 Jan 2026 00:00:00 GMT')
            self.send_header('ETag', '"' + 'a' * 32 + '"')
            self.end_headers()
        def do_GET(self):
            calls.append(self.path)
            self.send_response(307)
            self.send_header('Location', endpoint + '/redirect-must-not-be-followed')
            self.send_header('Content-Length', '0')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    endpoint = 'http://127.0.0.1:' + str(server.server_port)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    password, credentials = tmp_path / 'password', tmp_path / 'credentials'
    private_write(password, b'test-password')
    private_write(credentials, canonical({'access_key_id':'EXAMPLE','secret_access_key':'example-secret'}))
    backend = S3Target({'endpoint':endpoint,'bucket':'demo-backups','prefix':'acc/test','region':'us-east-1'},
                       password, credentials, restic_binary)
    try:
        try:
            result = subprocess.run([str(restic_binary), *backend.repo_args(), 'cat', 'config'], env=backend.env(),
                                    capture_output=True, timeout=5)
            assert result.returncode != 0
        except subprocess.TimeoutExpired as error:
            result = error
            # restic retries the ORIGINAL request; terminate the disposable child after observing that behavior.
            pass
        assert calls and not any('redirect-must-not-be-followed' in path for path in calls), result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_s3_jobs_capture_schedule_history_and_selected_restore_with_recorded_provider(source, restic_binary, tmp_path, monkeypatch):
    """Real capture/restic/restore plus recorded S3 conformance at the provider boundary.

    Repository IO uses a local encrypted fixture; native S3 IO is the optional MinIO test.
    """
    from contextlib import contextmanager

    from acc.backup_s3 import S3
    from acc.backup_server import LocalTarget, canonical, private_write
    from acc.server.backup_jobs import BackupJobs
    from acc.server.db import make_engine, make_sessionmaker
    credentials = {'access_key_id':'EXAMPLE','secret_access_key':'example-secret'}
    target = source.add_s3_target('https://s3.example.com','demo-backups','auto','fake-key-id')
    repository = tmp_path / 'encrypted-repository'
    @contextmanager
    def backend(target, binary):
        yield LocalTarget(repository, source.root / 'keys' / target['id'], binary)
    supported = [True]
    def response(request):
        if not supported[0] and 'retention' in request.url.params:
            return httpx.Response(403, text='<Error><Code>AccessDenied</Code></Error>')
        if 'list-type' in request.url.params:
            return httpx.Response(200,text=f'<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>{target["prefix"]}/data/ab/old</Key></Contents></ListBucketResult>')
        return recorded(request)
    monkeypatch.setattr(source, 'backend', backend)
    monkeypatch.setattr(source, 's3', lambda target: S3(target, credentials, transport=httpx.MockTransport(response)))
    checked = source.check_target(target['id'], binary=restic_binary)
    assert checked['immutability']['mode'] == 'COMPLIANCE'
    sessions = make_sessionmaker(make_engine(source.settings.db_url))
    jobs = BackupJobs(source.settings, sessions)
    jobs.service = source
    jobs.request(target['id'], 'capture', 'owner')
    jobs.work(binary=restic_binary)
    state = jobs.state()
    assert state['targets'][0]['last_job']['state'] == 'complete'
    assert state['protected'] is False
    capture = state['targets'][0]
    private_write(source.root / 'kit-exported', canonical({'targets':[target['id']],
                  'repositories':{target['id']:checked['repository_id']}}))
    assert jobs.enable(target['id'], 'owner')['targets'][0]['enabled']
    assert jobs.state()['protected'] is True
    history = source.history(target['id'], binary=restic_binary)
    assert len(history['items']) == 1
    jobs.request(target['id'], 'verify', 'owner', capture['backup_id'], capture['snapshot_id'])
    jobs.work(binary=restic_binary)
    assert jobs.state()['targets'][0]['last_job']['state'] == 'complete'
    assert jobs.state()['targets'][0]['captured_at'] == capture['captured_at']
    supported[0] = False
    jobs.request(target['id'], 'capture', 'owner')
    jobs.work(binary=restic_binary)
    assert jobs.state()['targets'][0]['last_job']['state'] == 'complete'
    assert not jobs.state()['protected']
    with pytest.raises(Refused, match='maintenance'):
        jobs.request(target['id'], 'prune', 'owner')


@pytest.mark.parametrize('bad_credentials', [{'secret_access_key':['never-echo-canary']}, {'extra':{'secret':'never-echo-canary'}}])
async def test_invalid_s3_request_never_echoes_credential_inputs(api, bad_credentials):
    response = await api.post('/api/host/backups/s3-targets', json={'endpoint':'https://s3.example.com',
        'bucket':'demo-backups','region':'auto','credentials':bad_credentials})
    assert response.status_code in (409, 422)
    assert 'never-echo-canary' not in response.text


async def test_owner_can_pause_hourly_capture_for_independent_s3_maintenance(api):
    from acc.backup_server import canonical, private_write
    payload = {'endpoint':'https://s3.example.com','bucket':'demo-backups','region':'auto',
               'credentials':{'access_key_id':'EXAMPLE','secret_access_key':'example-secret'}}
    target = (await api.post('/api/host/backups/s3-targets', json=payload)).json()
    service = api.app.state.ctx.backup_jobs.service
    target['enabled'] = True
    private_write(service.root / 'targets.json', canonical([target]))
    response = await api.post(f"/api/host/backups/{target['id']}/disable", json={'confirmed':True})
    assert response.status_code == 200
    assert not response.json()['targets'][0]['enabled']


async def test_backup_writer_is_absent_from_general_and_dns_credential_routes(api, env, project):
    from test_auth import PASSWORD
    from test_dns_infrastructure import owner_client
    headers = await owner_client(api, env)
    response = await api.post('/api/host/backups/s3-targets', headers=headers, json={
        'endpoint':'https://s3.example.com','bucket':'demo-backups','region':'auto',
        'credentials':{'access_key_id':'EXAMPLE','secret_access_key':'example-secret'}})
    assert response.status_code == 201
    credential_id = response.json()['credential_id']
    assert (await api.get('/api/vault/keys')).json() == []
    assert (await api.get('/api/dns/credentials')).json() == []
    denied = await api.post(f'/api/vault/keys/{credential_id}/give', json={'project_id':project['id']}, headers=headers)
    assert denied.status_code == 404
    denied = await api.post(f'/api/dns/credentials/{credential_id}/replace', json={
        'password':PASSWORD,'fields':{'access_key_id':'EXAMPLE','secret_access_key':'replacement-secret'}}, headers=headers)
    assert denied.status_code == 404


def test_s3_rotation_between_capture_and_upload_cannot_reuse_old_setup_receipt(source, tmp_path, monkeypatch):
    from contextlib import contextmanager

    from acc.backup_server import canonical, private_write
    target = source.add_s3_target('https://s3.example.com','demo-backups','auto','fake-key')
    target.update(repository_id='a'*64, setup_verified_at=1)
    private_write(source.root / 'targets.json', canonical([target]))
    def capture():
        target.update(setup_verified_at=None)
        private_write(source.root / 'targets.json', canonical([target]))
        return tmp_path / 'unuploaded'
    class Backend:
        def repository_id(self):
            pytest.fail('S3 IO reached using a setup receipt invalidated during capture')
    @contextmanager
    def backend(*args):
        yield Backend()
    monkeypatch.setattr(source, 'capture', capture)
    monkeypatch.setattr(source, 'backend', backend)
    with pytest.raises(Refused, match='setup changed'):
        source.back_up(target['id'])


def test_s3_refuses_oversized_or_compressed_capability_responses():
    with client(lambda request: httpx.Response(200, content=b'x' * ((2 << 20) + 1))) as s3, pytest.raises(Refused, match='too large'):
        s3.capabilities()
    class Unreadable(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail('compressed provider body was read before refusal')
            yield b''
    with client(lambda request: httpx.Response(200, headers={'content-encoding':'gzip'}, stream=Unreadable())) as s3:
        with pytest.raises(Refused, match='Compressed'):
            s3.capabilities()


def test_s3_retention_scan_bounds_empty_paginated_responses(monkeypatch):
    from acc import backup_s3
    monkeypatch.setattr(backup_s3, 'MAX_PAGES', 3, raising=False)
    pages = []
    def response(request):
        pages.append(len(pages))
        if len(pages) > 4:
            pytest.fail('retention scan did not enforce its page limit')
        return httpx.Response(200,text=f'<ListBucketResult><IsTruncated>true</IsTruncated><NextContinuationToken>{len(pages)}</NextContinuationToken></ListBucketResult>')
    with client(response) as s3, pytest.raises(Refused):
        s3.retained_until()


def test_s3_setup_requires_snapshot_write_even_if_retention_is_unsupported(source, monkeypatch):
    from contextlib import contextmanager

    from acc.backup_s3 import S3
    from acc.backup_server import canonical, private_write
    target = source.add_s3_target('https://s3.example.com','demo-backups','auto','fake-key')
    target['enabled'] = True
    private_write(source.root / 'targets.json', canonical([target]))
    class Backend:
        def repository_id(self):
            return 'b' * 64
        def command(self, action, *args, **kwargs):
            if action == 'backup':
                raise Refused('snapshot write refused')
            return b'{}'
    @contextmanager
    def backend(*args):
        yield Backend()
    monkeypatch.setattr(source, 'backend', backend)
    monkeypatch.setattr(source, 's3', lambda t: S3(t, {'access_key_id':'EXAMPLE','secret_access_key':'example-secret'},
                                               transport=httpx.MockTransport(recorded)))
    with pytest.raises(Refused, match='snapshot write refused'):
        source.check_target(target['id'])
    assert source.target(target['id'])['setup_verified_at'] is None
    assert not source.target(target['id'])['enabled']


@pytest.mark.parametrize(('query', 'signature'), [
    ({'lifecycle':''}, 'fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543'),
    ({'prefix':'J','max-keys':'2'}, '34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7'),
])
def test_s3_signer_matches_published_aws_vectors(monkeypatch, query, signature):
    """Public AWS example credentials, never an account. Reference:
    https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sig-v4-header-based-auth.html
    """
    from datetime import datetime, timezone

    from acc import backup_s3
    class FixedDate:
        @classmethod
        def now(cls, tz):
            return datetime(2013, 5, 24, tzinfo=timezone.utc)
    monkeypatch.setattr(backup_s3, 'datetime', FixedDate)
    def response(request):
        assert request.headers['authorization'].endswith('Signature=' + signature)
        return httpx.Response(200, content=b'')
    # Empty bucket reproduces AWS's virtual-hosted test vector at '/'; production location() always
    # requires a bucket and native restic always uses explicit path lookup.
    with backup_s3.S3({'endpoint':'https://examplebucket.s3.amazonaws.com','bucket':'','region':'us-east-1'},
                     {'access_key_id':'AKIAIOSFODNN7EXAMPLE','secret_access_key':'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'},
                     transport=httpx.MockTransport(response)) as s3:
        assert s3.request('GET', query=query).status_code == 200


def test_s3_worker_revalidates_https_before_materializing_credentials(source, monkeypatch):
    target = source.add_s3_target('https://s3.example.com','demo-backups','auto','fake-key')
    target['endpoint'] = 'http://s3.example.com'
    source.s3_credentials = lambda t: {'access_key_id':'EXAMPLE','secret_access_key':'example-secret'}
    monkeypatch.setattr('acc.restic_process.run', lambda *a, **kw: type('Result', (), {'stdout':b'restic 0.19.1'})())
    with pytest.raises(Refused, match='HTTPS'):
        with source.backend(target, '/restic'):
            pass
    assert not list(source.root.glob('s3-job-*'))


def test_reconciliation_keeps_credentials_of_an_active_owner_operation(source, monkeypatch):
    from acc.server.backup_jobs import BackupJobs
    from acc.server.db import make_engine, make_sessionmaker
    jobs = BackupJobs(source.settings, make_sessionmaker(make_engine(source.settings.db_url)))
    jobs.service = source
    target = source.add_s3_target('https://s3.example.com','demo-backups','auto','fake-key')
    source.s3_credentials = lambda t: {'access_key_id':'EXAMPLE','secret_access_key':'example-secret'}
    monkeypatch.setattr('acc.restic_process.run', lambda *a, **kw: type('Result', (), {'stdout':b'restic 0.19.1'})())
    abandoned = source.root / 's3-job-abandoned'
    abandoned.mkdir(mode=0o700)
    (abandoned / 'credentials.json').write_text('synthetic abandoned credential')
    with source.backend(target, '/restic') as backend:
        credential_file = backend.credential_file
        jobs.reconcile()
        assert not abandoned.exists()
        assert credential_file.exists(), 'reconciliation removed an active owner check/list credential file'
    assert not credential_file.exists()


@pytest.mark.parametrize('original_days', [1, 60])
def test_s3_shadowing_after_authenticated_readback_never_qualifies_replacement_retention(
        source, restic_binary, tmp_path, monkeypatch, original_days):
    """A stolen writer can shadow a verified pack without deleting its original version."""
    import time
    from contextlib import contextmanager
    from datetime import datetime, timezone

    from acc.backup_s3 import S3
    from acc.backup_server import LocalTarget, canonical, private_write
    from acc.server.backup_jobs import BackupJobs
    from acc.server.db import make_engine, make_sessionmaker

    target = source.add_s3_target('https://s3.example.com', 'demo-backups', 'auto', 'fake-key')
    version = ['original']
    overwrite = [False]

    class Repository(LocalTarget):
        def command(self, *args, **kwargs):
            result = super().command(*args, **kwargs)
            if overwrite[0] and args == ('check', '--read-data'):
                version[0] = 'junk-replacement'
            return result

    @contextmanager
    def backend(target, binary):
        yield Repository(tmp_path / 'encrypted', source.root / 'keys' / target['id'], binary)

    def response(request):
        if 'list-type' in request.url.params:
            return httpx.Response(200, text=f'<ListBucketResult><IsTruncated>false</IsTruncated>'
                                  f'<Contents><Key>{target["prefix"]}/data/ab/old</Key></Contents></ListBucketResult>')
        if request.url.path.endswith('/old'):
            if request.method == 'HEAD':
                return httpx.Response(200, headers={'x-amz-version-id': version[0]})
            if 'retention' in request.url.params:
                selected = request.url.params.get('versionId', version[0])
                days = original_days if selected == 'original' else 60
                until = datetime.fromtimestamp(time.time() + days * 86400, timezone.utc).isoformat()
                return httpx.Response(200, text=f'<Retention><Mode>COMPLIANCE</Mode>'
                                      f'<RetainUntilDate>{until}</RetainUntilDate></Retention>')
        return recorded(request)

    monkeypatch.setattr(source, 'backend', backend)
    monkeypatch.setattr(source, 's3', lambda t: S3(t, {'access_key_id': 'EXAMPLE',
                        'secret_access_key': 'example-secret'}, transport=httpx.MockTransport(response)))
    checked = source.check_target(target['id'], binary=restic_binary)
    private_write(source.root / 'kit-exported', canonical({
        'repositories': {target['id']: checked['repository_id']}}))
    overwrite[0] = True
    result = source.back_up(target['id'], binary=restic_binary)
    assert version[0] == 'junk-replacement'
    assert not result.get('immutable_until'), 'retention belongs to a shadow, not the authenticated ciphertext'
    jobs = BackupJobs(source.settings, make_sessionmaker(make_engine(source.settings.db_url)))
    jobs.service = source
    assert jobs.state()['protected'] is False


def test_break_it_6_offsetless_retention_cannot_claim_protection():
    def response(request):
        if 'retention' in request.url.params:
            return httpx.Response(200, text='<Retention><Mode>COMPLIANCE</Mode>'
                '<RetainUntilDate>2026-11-01T01:30:00</RetainUntilDate></Retention>')
        return recorded(request)
    with client(response) as s3, pytest.raises(Refused, match='deadline'):
        s3.retained_until()
