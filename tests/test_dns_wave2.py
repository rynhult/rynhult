"""Synthetic provider transcripts: no live accounts or DNS mutations."""
import json
from pathlib import Path

import httpx
import pytest

FIELDS = {
    'googleclouddns': dict(project_id='example-project', managed_zone='example-zone', zone_name='example.com',
                          service_account_json=json.dumps({'type': 'service_account', 'project_id': 'example-project',
                          'client_email': 'dns@example-project.iam.gserviceaccount.com', 'private_key': '-----BEGIN PRIVATE KEY-----\nexample-key\n-----END PRIVATE KEY-----\n',
                          'token_uri': 'https://oauth2.googleapis.com/token'})),
    'digitalocean': dict(api_token='example-token', zone_name='example.com'),
    'hetzner': dict(api_token='example-token', zone_name='example.com'),
}


@pytest.fixture
def db(tmp_path):
    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.models import Base
    engine = make_engine(f"sqlite:///{tmp_path / 'dns.db'}")
    Base.metadata.create_all(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


@pytest.mark.parametrize('name', FIELDS)
async def test_transcript_lifecycle_ownership_and_pagination(db, name):
    from acc.server.dns_providers import DNSError, OwnedDNS, make_provider
    transcript = json.loads((Path(__file__).parent / 'fixtures/dns' / f'{name}.json').read_text())
    def handler(request):
        row = transcript.pop(0)
        assert request.method == row['method']
        assert request.url.raw_path.decode() == row['path']
        if 'body' in row:
            assert json.loads(request.content) == row['body']
        assert request.headers['Authorization'] == 'Bearer example-token'
        return httpx.Response(row.get('status', 200), json=row['response'])
    provider = make_provider(name, FIELDS[name], transport=httpx.MockTransport(handler))
    # OAuth signing has its own test; this transcript starts after exchange.
    if name == 'googleclouddns':
        provider.bearer = 'example-token'
    zone = (await provider.zones())[0]['id']
    owned = OwnedDNS(provider, db)
    first = await owned.ensure_record(zone, 'site.example.com', 'A', '192.0.2.1', 'one')
    assert first.values == ('192.0.2.1',)
    second = await owned.ensure_record(zone, 'site.example.com', 'A', '192.0.2.2', 'two')
    assert second.values == ('192.0.2.2',)
    with pytest.raises(DNSError, match='ownership_refused'):
        await owned.delete_owned_record(zone, 'site.example.com', 'refuse-drift')
    await owned.delete_owned_record(zone, 'site.example.com', 'delete')
    with pytest.raises(DNSError, match='ownership_refused'):
        await owned.ensure_record(zone, 'site.example.com', 'A', '192.0.2.3', 'refuse-foreign')
    assert not transcript
    await provider.close()


@pytest.mark.parametrize('name', FIELDS)
@pytest.mark.parametrize('status', [401, 403, 500, 302])
async def test_errors_are_redacted_and_redirects_refused(name, status):
    from acc.server.dns_providers import DNSError, make_provider
    def handler(request):
        return httpx.Response(status, json={'error': 'secret-canary'}, headers={'Location': 'https://example.com/steal'})
    provider = make_provider(name, FIELDS[name], transport=httpx.MockTransport(handler))
    if name == 'googleclouddns':
        provider.bearer = 'example-token'
    with pytest.raises(DNSError) as error:
        await provider.zones()
    assert 'secret-canary' not in str(error.value)
    await provider.close()


@pytest.mark.parametrize('name', FIELDS)
def test_credentials_roundtrip_and_caddy_configuration(name):
    from acc.dns_credentials import caddy_provider, credential_environment, parse_environment
    token = {'provider': name, 'fields': FIELDS[name]}
    assert parse_environment(credential_environment(token)) == token
    config = caddy_provider(name, FIELDS[name])
    assert config['name'] == name
    if name == 'digitalocean':
        # The module's JSON name differs from its Caddyfile subdirective.
        from acc.dns_credentials import caddy_directive
        assert config == {'name': 'digitalocean', 'auth_token': 'example-token'}
        assert caddy_directive(name, FIELDS[name]) == 'dns digitalocean {\n  api_token "example-token"\n}'
    elif name == 'hetzner':
        assert config == {'name': 'hetzner', 'api_token': 'example-token'}
    else:
        assert config['gcp_project'] == 'example-project'
        assert config['gcp_application_default'].startswith('/etc/caddy/acc-dns-google/')


@pytest.mark.parametrize('name', FIELDS)
async def test_wave2_vault_is_owner_only_and_purpose_bound(api, env, name):
    from test_auth import PASSWORD
    from test_dns_infrastructure import owner_client

    from acc.server import infrastructure as infra
    from acc.server.models import InfrastructureCredential
    from acc.server.vault import VaultError
    headers = await owner_client(api, env)
    response = await api.post('/api/dns/credentials', headers=headers, json=dict(
        name='Trial', provider=name, purpose='publishing', fields=FIELDS[name], password=PASSWORD))
    assert response.status_code == 201, response.text
    assert 'example-token' not in response.text and 'example-key' not in response.text
    with api.app.state.db() as s:
        key = s.get(InfrastructureCredential, response.json()['id'])
        store = api.app.state.supervisor.vault
        assert infra.value(store, key) == FIELDS[name]
        key.purpose = 'certificates'
        with pytest.raises(VaultError):
            infra.value(store, key)


@pytest.mark.parametrize('name', FIELDS)
async def test_page_limits_and_foreign_zone_refusal(name):
    from acc.server.dns_providers import DNSError, make_provider
    responses = {
        'googleclouddns': {'rrsets': [], 'nextPageToken': 'repeat'},
        'digitalocean': {'domain_records': [], 'links': {'pages': {'next': 'https://example.com/steal'}}},
        'hetzner': {'rrsets': [], 'meta': {'pagination': {'next_page': 2}}},
    }
    reads = []
    def handler(request):
        reads.append(str(request.url))
        return httpx.Response(200, json=responses[name])
    provider = make_provider(name, FIELDS[name], transport=httpx.MockTransport(handler))
    provider.zone = '/dns/v1/projects/example-project/managedZones/example-zone' if name == 'googleclouddns' else 'example.com' if name == 'digitalocean' else '123'
    provider.bearer, provider.verified, provider.ttl = 'example-token', True, 300
    with pytest.raises(DNSError, match='zone_refused'):
        await provider.list_records('foreign', 'site.example.com')
    assert not reads
    with pytest.raises(DNSError, match='provider_page_limit'):
        await provider.list_records(provider.zone, 'site.example.com')
    assert len(reads) == 100
    assert all('example.com/steal' not in url for url in reads)
    await provider.close()


async def test_google_oauth_assertion_is_signed_and_no_credential_chain():
    import base64
    import time
    from urllib.parse import parse_qs

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from acc.server.dns_providers import make_provider
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fields = dict(FIELDS['googleclouddns'])
    account = json.loads(fields['service_account_json'])
    account['private_key'] = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()).decode()
    fields['service_account_json'] = json.dumps(account)
    calls = []
    def decode(part):
        return base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))
    def handler(request):
        calls.append(str(request.url))
        if request.url.host == 'oauth2.googleapis.com':
            data = parse_qs(request.content.decode())
            assert data['grant_type'] == ['urn:ietf:params:oauth:grant-type:jwt-bearer']
            header, payload, signature = data['assertion'][0].split('.')
            assert json.loads(decode(header)) == {'alg': 'RS256', 'typ': 'JWT'}
            claims = json.loads(decode(payload))
            assert claims['iss'] == 'dns@example-project.iam.gserviceaccount.com'
            assert claims['aud'] == 'https://oauth2.googleapis.com/token'
            assert claims['scope'] == 'https://www.googleapis.com/auth/ndev.clouddns.readwrite'
            assert claims['iat'] <= time.time() < claims['exp'] <= time.time() + 3600
            key.public_key().verify(decode(signature), (header + '.' + payload).encode(), padding.PKCS1v15(), hashes.SHA256())
            return httpx.Response(200, json={'access_token': 'fresh-token', 'expires_in': 3600})
        assert request.headers['Authorization'] == 'Bearer fresh-token'
        return httpx.Response(200, json={'name': 'example-zone', 'dnsName': 'example.com.', 'visibility': 'public'})
    provider = make_provider('googleclouddns', fields, transport=httpx.MockTransport(handler))
    assert await provider.validate_credentials()
    assert await provider.validate_credentials()
    assert len(calls) == 3  # one exchange, two zone checks
    await provider.close()


@pytest.mark.parametrize('change', [dict(token_uri='https://example.com/steal'), dict(type='external_account'),
                                    dict(project_id='foreign'), dict(client_email='bad\nemail'), dict(universe_domain='example.com')])
def test_google_rejects_untrusted_credential_routing(change):
    from acc.dns_credentials import check_fields
    fields = dict(FIELDS['googleclouddns'])
    fields['service_account_json'] = json.dumps({**json.loads(fields['service_account_json']), **change})
    with pytest.raises(ValueError):
        check_fields('googleclouddns', fields)


def test_live_harness_accepts_wave2_only_with_explicit_acknowledgement():
    from acc.dns_live_test import parser
    for name in FIELDS:
        args = parser().parse_args(['--provider', name, '--disposable-zone', 'example.com',
            '--publishing-credentials', '/tmp/example-publishing.json', '--certificate-credentials', '/tmp/example-cert.json',
            '--caddy', '/tmp/example-caddy', '--candidate-sha256', 'a' * 64, '--state-dir', '/tmp/example-state', '--ack-disposable'])
        assert args.provider == name and args.ack_disposable


async def test_google_ownership_zone_identity_includes_project():
    from acc.server.dns_providers import make_provider
    def handler(request):
        return httpx.Response(200, json={'name': 'example-zone', 'dnsName': 'example.com.', 'visibility': 'public'})
    zones = []
    for project in ['example-project', 'other-project']:
        fields = dict(FIELDS['googleclouddns'], project_id=project)
        fields['service_account_json'] = json.dumps({**json.loads(fields['service_account_json']), 'project_id': project})
        provider = make_provider('googleclouddns', fields, transport=httpx.MockTransport(handler))
        provider.bearer = 'example-token'
        zones.append((await provider.zones())[0]['id'])
        await provider.close()
    assert zones == ['/dns/v1/projects/example-project/managedZones/example-zone',
                     '/dns/v1/projects/other-project/managedZones/example-zone']


@pytest.mark.parametrize('name', FIELDS)
@pytest.mark.parametrize('operation', ['create', 'update', 'delete'])
async def test_mutation_errors_leave_a_sticky_uncertain_intent(db, name, operation):
    from acc.server.dns_providers import DNSError, OwnedDNS, make_provider
    transcript = json.loads((Path(__file__).parent / 'fixtures/dns' / f'{name}.json').read_text())
    mutations = 0
    target = {'create': 1, 'update': 2, 'delete': 3}[operation]
    def handler(request):
        nonlocal mutations
        row = transcript.pop(0)
        assert request.method == row['method'] and request.url.raw_path.decode() == row['path']
        if request.method != 'GET':
            mutations += 1
            if mutations == target:
                return httpx.Response(403, json={'error': 'secret-canary'})
        return httpx.Response(200, json=row['response'])
    provider = make_provider(name, FIELDS[name], transport=httpx.MockTransport(handler))
    if name == 'googleclouddns':
        provider.bearer = 'example-token'
    zone = (await provider.zones())[0]['id']
    owned = OwnedDNS(provider, db)
    async def attempt():
        if operation == 'delete':
            await owned.delete_owned_record(zone, 'site.example.com', 'delete')
        else:
            await owned.ensure_record(zone, 'site.example.com', 'A', '192.0.2.2' if operation == 'update' else '192.0.2.1', 'write')
    if operation != 'create':
        await owned.ensure_record(zone, 'site.example.com', 'A', '192.0.2.1', 'first')
    if operation == 'delete':
        await owned.ensure_record(zone, 'site.example.com', 'A', '192.0.2.2', 'second')
        with pytest.raises(DNSError, match='ownership_refused'):
            await owned.delete_owned_record(zone, 'site.example.com', 'drift')
    with pytest.raises(DNSError, match='mutation_uncertain_reconcile'):
        await attempt()
    with pytest.raises(DNSError, match='reconciliation_required'):
        await attempt()
    assert mutations == target
    await provider.close()
