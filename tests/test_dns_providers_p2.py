"""Portable ownership refuses forged markers, drift and uncertain mutations."""
import httpx
import pytest

from acc.server.db import make_engine, make_sessionmaker
from acc.server.models import Base


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'dns.db'}")
    Base.metadata.create_all(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


async def test_cloudflare_ledger_lifecycle_and_drift(db):
    from test_publish import FakeCloudflareAPI

    from acc.server.dns_providers import CloudflareProvider, DNSError, OwnedDNS
    remote = FakeCloudflareAPI()
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(remote.handler))
    dns = OwnedDNS(provider, db)
    first = await dns.ensure_record('z1', 'site.example.com', 'A', '192.0.2.1', 'pub-1')
    assert remote.records[first.id]['content'] == '192.0.2.1'
    await dns.ensure_record('z1', 'site.example.com', 'A', '192.0.2.2', 'pub-2')
    assert remote.records[first.id]['content'] == '192.0.2.2'
    remote.records[first.id]['content'] = '192.0.2.99'
    with pytest.raises(DNSError, match='ownership'):
        await dns.delete_owned_record('z1', 'site.example.com', 'pub-3')
    assert remote.records[first.id]['content'] == '192.0.2.99'
    remote.records[first.id]['content'] = '192.0.2.2'
    await dns.delete_owned_record('z1', 'site.example.com', 'pub-4')
    assert not remote.records
    await provider.close()


async def test_marker_alone_never_adopts_a_record(db):
    from test_publish import FakeCloudflareAPI

    from acc.server.dns_providers import CloudflareProvider, DNSError, OwnedDNS
    remote = FakeCloudflareAPI()
    remote.records['foreign'] = dict(id='foreign', name='site.example.com', type='A', content='192.0.2.1',
                                     ttl=300, proxied=False, comment='managed by ACC')
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(remote.handler))
    with pytest.raises(DNSError, match='ownership'):
        await OwnedDNS(provider, db).ensure_record('z1', 'site.example.com', 'A', '192.0.2.2', 'pub-1')
    assert remote.records['foreign']['content'] == '192.0.2.1'
    await provider.close()


async def test_uncertain_create_is_not_retried(db):
    from test_publish import FakeCloudflareAPI

    from acc.server.dns_providers import CloudflareProvider, DNSError, OwnedDNS
    remote = FakeCloudflareAPI()
    writes = []
    def handler(request):
        result = remote.handler(request)
        if request.method == 'POST':
            writes.append(request)
            raise httpx.ReadTimeout('secret-canary', request=request)
        return result
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(handler))
    dns = OwnedDNS(provider, db)
    for _ in range(2):
        with pytest.raises(DNSError) as error:
            await dns.ensure_record('z1', 'site.example.com', 'A', '192.0.2.1', 'pub-1')
        assert 'secret-canary' not in str(error.value)
    assert len(writes) == 1 and len(remote.records) == 1
    await provider.close()

AZURE_FIELDS = dict(tenant_id='11111111-1111-1111-1111-111111111111',
    subscription_id='22222222-2222-2222-2222-222222222222', client_id='33333333-3333-3333-3333-333333333333',
    client_secret='example-secret', resource_group='dns-trial', zone_name='example.com')
AWS_FIELDS = dict(access_key_id='AKIAEXAMPLEEXAMPLE123', secret_access_key='example-secret',
                  hosted_zone_id='ZEXAMPLE', zone_name='example.com')


@pytest.mark.parametrize('name', ['cloudflare', 'azure', 'route53'])
async def test_provider_recorded_lifecycle_and_ownership(db, name):
    """Fixtures reflect documented wire formats; removing conditional writes must break these tests."""
    import json
    from pathlib import Path

    from acc.server.dns_providers import DNSError, OwnedDNS, make_provider
    fixtures = json.loads((Path(__file__).parent / 'fixtures/dns' / f'{name}.json').read_text())
    requests = []
    def handler(request):
        requests.append(request)
        expected = fixtures.pop(0)
        assert request.method == expected['method']
        assert expected['path'] in str(request.url)
        if 'if_match' in expected:
            assert request.headers.get('If-Match') == expected['if_match']
        if 'if_none_match' in expected:
            assert request.headers.get('If-None-Match') == expected['if_none_match']
        if name == 'route53':
            assert request.headers['Authorization'].startswith('AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE')
            if request.method == 'POST':
                assert '<Action>UPSERT</Action>' not in request.content.decode()
                if expected.get('replaces'):
                    assert '<Action>DELETE</Action>' in request.content.decode()
        return httpx.Response(200, json=expected['json']) if 'json' in expected else httpx.Response(
            200, text=expected['xml'], headers={'Content-Type': 'text/xml'})
    provider = make_provider(name, {'api_token': 'example-token'} if name == 'cloudflare' else AZURE_FIELDS if name == 'azure' else AWS_FIELDS,
                             transport=httpx.MockTransport(handler))
    zone = (await provider.zones())[0]['id']
    dns = OwnedDNS(provider, db)
    await dns.ensure_record(zone, 'site.example.com', 'A', '192.0.2.1', 'pub-1')
    await dns.ensure_record(zone, 'site.example.com', 'A', '192.0.2.2', 'pub-2')
    with pytest.raises(DNSError, match='ownership'):
        await dns.delete_owned_record(zone, 'site.example.com', 'pub-3')
    await dns.delete_owned_record(zone, 'site.example.com', 'pub-4')
    assert not fixtures
    await provider.close()


@pytest.mark.parametrize('name,fields', [('azure', AZURE_FIELDS), ('route53', AWS_FIELDS)])
async def test_multifield_vault_is_purpose_bound(api, env, name, fields):
    from test_auth import PASSWORD
    from test_dns_infrastructure import owner_client

    from acc.server import infrastructure as infra
    from acc.server.models import InfrastructureCredential
    from acc.server.vault import VaultError
    headers = await owner_client(api, env)
    response = await api.post('/api/dns/credentials', headers=headers, json=dict(name='Trial', provider=name,
        purpose='publishing', fields=fields, password=PASSWORD))
    assert response.status_code == 201, response.text
    assert fields['client_secret' if name == 'azure' else 'secret_access_key'] not in response.text
    with api.app.state.db() as s:
        key = s.get(InfrastructureCredential, response.json()['id'])
        store = api.app.state.supervisor.vault
        assert infra.value(store, key) == fields
        key.purpose = 'certificates'
        with pytest.raises(VaultError):
            infra.value(store, key)


async def test_publishing_vault_selection_never_falls_back_to_legacy(api, env, monkeypatch):
    from test_auth import PASSWORD
    from test_dns_infrastructure import owner_client
    from test_publish import FakeCloudflareAPI

    from acc.server.dns_providers import CloudflareProvider
    from acc.server.models import DNSBinding
    headers = await owner_client(api, env)
    ids = []
    for purpose in ('certificates', 'publishing'):
        r = await api.post('/api/dns/credentials', headers=headers, json=dict(name=purpose, provider='cloudflare',
            purpose=purpose, password=PASSWORD, fields={'api_token': 'selected-vault-token-1234'}))
        ids.append(r.json()['id'])
    with api.app.state.db() as s:
        s.add(DNSBinding(id=1, provider='cloudflare', zone='example.com', certificate_id=ids[0], publishing_id=ids[1]))
        s.commit()
    remote = FakeCloudflareAPI()
    seen = []
    def handler(request):
        seen.append(request.headers['Authorization'])
        return remote.handler(request)
    monkeypatch.setattr('acc.server.dns_providers.make_provider', lambda name, fields: CloudflareProvider(
        fields['api_token'], transport=httpx.MockTransport(handler)))
    publisher = api.app.state.publisher
    provider, zone_name = publisher.dns_selection()
    assert zone_name == 'example.com'
    assert (await provider.zones())[0]['name'] == 'example.com'
    assert seen == ['Bearer selected-vault-token-1234']
    await provider.close()
    # A broken vault must refuse instead of returning to a potentially overprivileged legacy credential.
    env['settings'].vault_key_path.unlink()
    with pytest.raises(Exception):
        publisher.dns_selection()


@pytest.mark.parametrize('name,fields', [('azure', AZURE_FIELDS), ('route53', AWS_FIELDS)])
async def test_new_provider_binding_saves_without_claiming_qualification(api, env, name, fields):
    from test_auth import PASSWORD
    from test_dns_infrastructure import owner_client
    headers = await owner_client(api, env)
    keys = []
    for purpose in ('certificates', 'publishing'):
        r = await api.post('/api/dns/credentials', headers=headers, json=dict(name='Trial ' + purpose, provider=name,
            purpose=purpose, fields=fields, password=PASSWORD))
        keys.append(r.json()['id'])
    response = await api.post('/api/dns/binding', headers=headers, json=dict(provider=name, zone='example.com',
        certificate_id=keys[0], publishing_id=keys[1], password=PASSWORD))
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'saved'
    with pytest.raises(Exception, match='qualified for publishing'):
        api.app.state.publisher.dns_selection()


def test_live_harness_requires_disposable_acknowledgement():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-m', 'acc.dns_live_test', '--provider', 'azure'],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert '--disposable-zone' in result.stderr and '--ack-disposable' in result.stderr


async def test_live_record_probe_leaves_no_owned_records(db):
    from test_publish import FakeCloudflareAPI

    from acc.dns_live_test import publishing_probe
    from acc.server.dns_providers import CloudflareProvider
    remote = FakeCloudflareAPI()
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(remote.handler))
    results = await publishing_probe(provider, db, 'z1', 'example.com')
    assert results == ['PASS publishing create', 'PASS publishing update', 'PASS publishing ownership refusal',
                       'PASS publishing delete']
    assert remote.records == {}
    await provider.close()


async def test_owner_can_explicitly_restore_legacy_publishing(api, env):
    from test_auth import PASSWORD
    from test_dns_infrastructure import owner_client
    headers = await owner_client(api, env)
    result = await api.post('/api/dns/publishing-source', headers=headers,
                            json={'source': 'legacy', 'password': PASSWORD})
    assert result.status_code == 409  # no Cloudflare binding, so no meaningful rollback
    assert 'Save Cloudflare as the DNS provider' in result.text


@pytest.mark.parametrize('provider', ['cloudflare', 'azure', 'route53'])
async def test_reads_refuse_rate_limit_without_leaking_response(provider):
    from acc.server.dns_providers import DNSError, make_provider
    fields = {'api_token': 'example-token'} if provider == 'cloudflare' else AZURE_FIELDS if provider == 'azure' else AWS_FIELDS
    def handler(request):
        return httpx.Response(429, text='secret-response-canary', headers={'Retry-After': '600'})
    client = make_provider(provider, fields, transport=httpx.MockTransport(handler))
    with pytest.raises(DNSError) as error:
        await client.list_records('z1' if provider == 'cloudflare' else client.zone if provider == 'azure' else 'ZEXAMPLE', 'site.example.com')
    assert 'secret-response-canary' not in str(error.value)
    await client.close()


async def test_azure_refuses_a_foreign_pagination_endpoint():
    from acc.server.dns_providers import AzureProvider, DNSError
    def handler(request):
        if request.method == 'POST':
            return httpx.Response(200, json={'access_token': 'synthetic-bearer'})
        return httpx.Response(200, json={'value': [], 'nextLink': 'https://attacker.example.com/steal'})
    client = AzureProvider(AZURE_FIELDS, transport=httpx.MockTransport(handler))
    with pytest.raises(DNSError, match='pagination'):
        await client.list_records(client.zone, 'site.example.com')
    await client.close()


async def test_owner_adoption_requires_matching_historical_publication(api, env):
    from test_auth import PASSWORD
    from test_dns_infrastructure import owner_client
    headers = await owner_client(api, env)
    result = await api.post('/api/dns/adopt', headers=headers,
        json={'request_id': 'pub-missing', 'password': PASSWORD})
    assert result.status_code == 409
    assert 'publication' in result.text


async def test_uncertain_intent_keeps_desired_snapshot(db):
    from sqlalchemy import select
    from test_publish import FakeCloudflareAPI

    from acc.server.dns_providers import CloudflareProvider, DNSError, OwnedDNS
    from acc.server.models import DNSOwnedRecord
    remote = FakeCloudflareAPI()
    def handler(request):
        response = remote.handler(request)
        if request.method == 'POST':
            raise httpx.ReadTimeout('timeout', request=request)
        return response
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(handler))
    with pytest.raises(DNSError):
        await OwnedDNS(provider, db).ensure_record('z1', 'site.example.com', 'A', '192.0.2.1', 'pub-1')
    with db() as s:
        row = s.scalar(select(DNSOwnedRecord))
        assert row.desired['values'] == ['192.0.2.1']
        assert row.desired['marker'] == 'managed by ACC (pub-1)'
    await provider.close()


async def test_trial_txt_sentinel_cleanup_refuses_drift(db, tmp_path):
    from test_publish import FakeCloudflareAPI

    from acc.dns_live_test import cleanup_challenge_sentinel, seed_challenge_sentinel
    from acc.server.dns_providers import CloudflareProvider, DNSError
    remote = FakeCloudflareAPI()
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(remote.handler))
    record, intent = await seed_challenge_sentinel(provider, 'z1', '_acme-challenge.site.example.com', tmp_path)
    assert remote.records[record.id]['type'] == 'TXT'
    remote.records[record.id]['content'] = 'external-change'
    with pytest.raises(DNSError, match='sentinel'):
        await cleanup_challenge_sentinel(provider, 'z1', record, intent)
    assert remote.records[record.id]['content'] == 'external-change'
    remote.records[record.id]['content'] = record.values[0]
    await cleanup_challenge_sentinel(provider, 'z1', record, intent)
    assert remote.records == {} and not intent.exists()
    await provider.close()


async def test_explicit_legacy_source_switch_uses_only_the_requested_credential(api, env, monkeypatch):
    from test_auth import PASSWORD
    from test_dns_infrastructure import owner_client

    from acc.server.dns_providers import CloudflareProvider
    headers = await owner_client(api, env)
    ids = []
    for purpose in ('certificates', 'publishing'):
        r = await api.post('/api/dns/credentials', headers=headers, json=dict(name=purpose, provider='cloudflare',
            purpose=purpose, fields={'api_token': 'vault-selection-12345678'}, password=PASSWORD))
        ids.append(r.json()['id'])
    saved = await api.post('/api/dns/binding', headers=headers, json=dict(provider='cloudflare', zone='example.com',
        certificate_id=ids[0], publishing_id=ids[1], password=PASSWORD))
    assert saved.status_code == 200
    legacy = env['settings'].cloudflare_token_path
    legacy.write_text('legacy-selection-87654321')
    legacy.chmod(0o600)
    seen = []
    def factory(name, fields):
        seen.append(fields['api_token'])
        return CloudflareProvider(fields['api_token'], transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={'success': True, 'result': []})))
    monkeypatch.setattr('acc.server.dns_providers.make_provider', factory)
    for source, token in [('vault', 'vault-selection-12345678'), ('legacy', 'legacy-selection-87654321'),
                           ('vault', 'vault-selection-12345678')]:
        response = await api.post('/api/dns/publishing-source', headers=headers,
                                   json={'source': source, 'password': PASSWORD})
        assert response.status_code == 200, response.text
        provider, _ = api.app.state.publisher.dns_selection()
        await provider.close()
        assert seen[-1] == token
    assert legacy.read_text() == 'legacy-selection-87654321'


async def test_cloudflare_read_retries_a_short_rate_limit_but_never_retries_writes(monkeypatch):
    import asyncio

    from acc.server.dns_providers import CloudflareProvider, DNSError, Record
    attempts = []
    async def immediate(delay):
        assert 0 <= delay <= 2
    monkeypatch.setattr(asyncio, 'sleep', immediate)
    def handler(request):
        attempts.append(request.method)
        if request.method == 'GET' and attempts.count('GET') == 2:
            return httpx.Response(200, json={'success': True, 'result': []})
        return httpx.Response(429, json={'success': False}, headers={'Retry-After': '0'})
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(handler))
    assert await provider.zones() == []
    with pytest.raises(DNSError):
        await provider.write_record('z1', Record('', 'site.example.com', 'A', ('192.0.2.1',)), None)
    assert attempts == ['GET', 'GET', 'POST']
    await provider.close()


async def test_azure_family_change_refusal_keeps_owned_record_removable(db):
    from acc.server.dns_providers import AzureProvider, DNSError, OwnedDNS
    live = []
    writes = []
    def handler(request):
        if 'login.microsoftonline.com' in str(request.url):
            return httpx.Response(200, json={'access_token': 'synthetic-bearer'})
        if request.method == 'GET':
            return httpx.Response(200, json={'value': live})
        writes.append(request.method)
        if request.method == 'DELETE':
            live.clear()
            return httpx.Response(200, json={})
        record = dict(id='/example/A/site', name='site', type='Microsoft.Network/dnsZones/A', etag='v1',
                      properties={'TTL': 300, 'ARecords': [{'ipv4Address': '192.0.2.1'}]})
        live[:] = [record]
        return httpx.Response(200, json=record)
    provider = AzureProvider(AZURE_FIELDS, transport=httpx.MockTransport(handler))
    owned = OwnedDNS(provider, db)
    await owned.ensure_record(provider.zone, 'site.example.com', 'A', '192.0.2.1', 'pub-1')
    with pytest.raises(DNSError, match='family'):
        await owned.ensure_record(provider.zone, 'site.example.com', 'AAAA', '2001:db8::1', 'pub-2')
    await owned.delete_owned_record(provider.zone, 'site.example.com', 'pub-3')
    assert writes == ['PUT', 'DELETE'] and live == []
    await provider.close()


async def test_competing_jobs_cannot_create_twice(db):
    import asyncio

    from test_publish import FakeCloudflareAPI

    from acc.server.dns_providers import CloudflareProvider, DNSError, OwnedDNS
    entered, release = asyncio.Event(), asyncio.Event()
    remote = FakeCloudflareAPI()
    async def handler(request):
        if request.method == 'GET':
            entered.set()
            await release.wait()
        return remote.handler(request)
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(handler))
    first = asyncio.create_task(OwnedDNS(provider, db).ensure_record('z1', 'site.example.com', 'A', '192.0.2.1', 'pub-1'))
    await entered.wait()
    try:
        with pytest.raises(DNSError, match='reconciliation'):
            await OwnedDNS(provider, db).ensure_record('z1', 'site.example.com', 'A', '192.0.2.2', 'pub-2')
    finally:
        release.set()
        await first
    assert remote.counter == 1
    assert next(iter(remote.records.values()))['content'] == '192.0.2.1'
    await provider.close()


async def test_compressed_provider_responses_are_decoded_once():
    """Cloudflare gzipped a 22 KB zone list and Publishing showed provider_unavailable (Oct 26). Requests ask for
    identity; a server that compresses anyway is still decoded once."""
    import gzip
    import json

    from acc.server.dns_providers import CloudflareProvider
    zones = [{'id': f'z{i}', 'name': f'zone{i}.example', 'status': 'active', 'permissions': ['#dns_records:edit']}
             for i in range(20)]
    payload = gzip.compress(json.dumps({'success': True, 'result': zones,
                                        'result_info': {'total_pages': 1}}).encode())
    def handler(request):
        assert request.headers['Accept-Encoding'] == 'identity'
        return httpx.Response(200, content=payload, headers={'Content-Encoding': 'gzip',
                                                             'Content-Type': 'application/json'})
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(handler))
    assert [z['name'] for z in await provider.zones()] == [f'zone{i}.example' for i in range(20)]
    await provider.close()


@pytest.mark.parametrize('payload,encoding', [
    (b'not gzip', 'gzip'),
    (b'[]', None),
    (b'{"success":true,"result":null}', None),
    (b'{"success":true,"result":[],"result_info":[]}', None),
])
async def test_hostile_zone_replies_fail_closed_without_credentials(payload, encoding):
    from acc.server.dns_providers import CloudflareProvider, DNSError
    def handler(request):
        assert request.headers['Accept-Encoding'] == 'identity'
        return httpx.Response(200, content=payload, headers={'Content-Encoding': encoding} if encoding else {})
    provider = CloudflareProvider('break8-secret-canary', transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(DNSError) as refused:
            await provider.zones()
        assert 'break8-secret-canary' not in str(refused.value)
    finally:
        await provider.close()


async def test_gzip_zone_reply_cannot_bypass_decoded_response_limit():
    import gzip

    from acc.server.dns_providers import CloudflareProvider, DNSError
    payload = gzip.compress(b' ' * (3 * 1024 * 1024))
    provider = CloudflareProvider('break8-secret-canary', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload, headers={'Content-Encoding': 'gzip'})))
    try:
        with pytest.raises(DNSError, match='provider_response_limit'):
            await provider.zones()
    finally:
        await provider.close()


async def test_streamed_gzip_limit_also_bounds_peak_decoding_memory():
    import gzip
    import tracemalloc

    from acc.server.dns_providers import CloudflareProvider, DNSError
    payload = gzip.compress(b' ' * (12 * 1024 * 1024))
    class Wire(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Wire(), headers={'Content-Encoding': 'gzip'})))
    tracemalloc.start()
    try:
        with pytest.raises(DNSError, match='provider_response_limit'):
            await provider.zones()
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 8 * 1024 * 1024, f'allocated {peak} bytes before rejecting a 2 MiB response'
    finally:
        tracemalloc.stop()
        await provider.close()


@pytest.mark.parametrize('encoding', ['identity', 'gzip', 'deflate', 'raw-deflate'])
async def test_streamed_zone_replies_keep_supported_encodings(encoding):
    import gzip
    import json
    import zlib

    from acc.server.dns_providers import CloudflareProvider
    body = json.dumps({'success': True, 'result': []}).encode()
    payload = (gzip.compress(body) if encoding == 'gzip' else zlib.compress(body) if encoding == 'deflate'
               else zlib.compress(body, wbits=-zlib.MAX_WBITS) if encoding == 'raw-deflate' else body)
    class Wire(httpx.AsyncByteStream):
        async def __aiter__(self):
            for offset in range(0, len(payload), 7):
                yield payload[offset:offset + 7]
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(lambda request:
        httpx.Response(200, stream=Wire(), headers={'Content-Encoding': 'deflate' if encoding == 'raw-deflate' else encoding})))
    try:
        assert await provider.zones() == []
    finally:
        await provider.close()


@pytest.mark.parametrize('damage', ['truncated', 'trailing', 'stacked'])
async def test_uncertain_compressed_replies_are_refused(damage):
    import gzip

    from acc.server.dns_providers import CloudflareProvider, DNSError
    payload = gzip.compress(b'{"success":true,"result":[]}')
    payload = payload[:-8] if damage == 'truncated' else payload + b'junk' if damage == 'trailing' else payload
    class Wire(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(lambda request:
        httpx.Response(200, stream=Wire(), headers={'Content-Encoding': 'gzip, gzip' if damage == 'stacked' else 'gzip'})))
    try:
        with pytest.raises(DNSError, match='provider_response_invalid'):
            await provider.zones()
    finally:
        await provider.close()
