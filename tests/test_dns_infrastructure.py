"""Infrastructure credentials never enter the project vault namespace."""
import json

import pytest
from sqlalchemy import select
from test_auth import PASSWORD, cookie_client, do_setup
from test_users import browser, join

from acc.server import vault
from acc.server.models import VaultKey

CANARY = 'dns-canary-0123456789abcdef'


async def owner_client(api, env):
    await do_setup(api, env)
    cookie_client(api)
    return {'Origin': 'http://acc', 'X-ACC-CSRF': api.cookies.get('acc_csrf')}


async def test_owner_only_write_only_namespace(api, env, project, member_accounts):
    headers = await owner_client(api, env)
    body = dict(name='Cloudflare certificates', provider='cloudflare', purpose='certificates',
                fields={'api_token': CANARY}, password=PASSWORD)
    added = await api.post('/api/dns/credentials', json=body, headers=headers)
    assert added.status_code == 201, added.text
    key = added.json()
    assert CANARY not in added.text and 'ciphertext' not in key
    assert key['provider'] == 'cloudflare'
    assert (await api.get('/api/vault/keys')).json() == []
    for suffix, payload in [('give', {'project_id': project['id']}), ('', {'shared': True, 'remote': True}),
                            ('replace', {'value': CANARY}), ('remove', {})]:
        response = await api.post(f"/api/vault/keys/{key['id']}/{suffix}".rstrip('/'), json=payload, headers=headers)
        assert response.status_code == 404
    listing = await api.get('/api/dns/credentials')
    assert CANARY not in listing.text
    assert (await api.post('/api/dns/credentials', json={**body, 'password': 'wrong'}, headers=headers)).status_code == 403
    assert (await api.post('/api/dns/credentials', json={**body, 'shared': True}, headers=headers)).status_code == 422
    assert (await api.post('/api/dns/credentials', json={**body, 'provider': 'azure'}, headers=headers)).status_code == 422
    invite = await api.post('/api/users/invites', headers=headers, json={'role': 'member', 'account': 'dana-agent'})
    async with browser(api) as client:
        assert (await join(client, invite.json()['link'], 'riley')).status_code == 200
        assert (await client.get('/api/dns/credentials')).status_code == 403
    with api.app.state.db() as s:
        assert s.scalars(select(VaultKey)).all() == []
        assert vault.plan(s, project['id'], 'casey', True).ids == []


async def test_bindings_require_separate_purpose_and_revision(api, env):
    headers = await owner_client(api, env)
    ids = []
    for purpose in ('certificates', 'publishing'):
        response = await api.post('/api/dns/credentials', headers=headers, json=dict(
            name=purpose, provider='cloudflare', purpose=purpose, fields={'api_token': CANARY}, password=PASSWORD))
        assert response.status_code == 201, response.text
        ids.append(response.json()['id'])
    body = dict(provider='cloudflare', zone='example.com', certificate_id=ids[0], publishing_id=ids[1], password=PASSWORD)
    bad = await api.post('/api/dns/binding', headers=headers, json={**body, 'publishing_id': ids[0]})
    assert bad.status_code == 422
    saved = await api.post('/api/dns/binding', headers=headers, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()['status'] == 'saved'
    assert saved.json()['applied_revision'] is None
    assert (await api.post(f'/api/dns/credentials/{ids[0]}/remove', headers=headers,
                           json={'password': PASSWORD})).status_code == 409
    assert CANARY not in json.dumps((await api.get('/api/dns/binding')).json())


async def test_replaced_canaries_and_purpose_aad(api, env):
    from acc.server import infrastructure as infra
    from acc.server.models import InfrastructureCredential
    headers = await owner_client(api, env)
    response = await api.post('/api/dns/credentials', headers=headers, json=dict(
        name='Certificates', provider='cloudflare', purpose='certificates', fields={'api_token': CANARY}, password=PASSWORD))
    key_id = response.json()['id']
    replacement = 'new-dns-canary-9876543210'
    changed = await api.post(f'/api/dns/credentials/{key_id}/replace', headers=headers,
                             json={'fields': {'api_token': replacement}, 'password': PASSWORD})
    assert changed.status_code == 200 and changed.json()['revision'] == 2
    with api.app.state.db() as s:
        store = api.app.state.supervisor.vault
        redact = infra.redactor(s, store)
        assert CANARY not in redact.text(CANARY + replacement)
        assert replacement not in redact.text(CANARY + replacement)
        key = s.get(InfrastructureCredential, key_id)
        key.purpose = 'publishing'
        with pytest.raises(vault.VaultError):
            infra.value(store, key)


async def test_migration_is_explicit_idempotent_and_preserves_different_values(api, env, monkeypatch):
    from acc.server import dns_helper_client
    headers = await owner_client(api, env)
    async def root_request(message):
        assert message == {'action': 'import'}
        return {'ok': True, 'fields': {'api_token': CANARY}}
    monkeypatch.setattr(dns_helper_client, 'request', root_request)
    legacy = env['settings'].cloudflare_token_path
    legacy.write_text('publishing-separate-canary-12345678')
    legacy.chmod(0o600)
    for _ in range(2):
        response = await api.post('/api/dns/migrate', headers=headers, json={'password': PASSWORD})
        assert response.status_code == 200, response.text
        assert CANARY not in response.text
        assert response.json()['status'] == 'saved'
    keys = (await api.get('/api/dns/credentials')).json()
    assert len(keys) == 2
    assert {key['purpose'] for key in keys} == {'certificates', 'publishing'}
    assert legacy.exists()  # never delete before successful apply + 24 hours


async def test_validation_errors_never_echo_secret_input(api, env):
    headers = await owner_client(api, env)
    response = await api.post('/api/dns/credentials', headers=headers, json={
        'name': 'test', 'provider': 'cloudflare', 'purpose': 'certificates', 'fields': {'api_token': CANARY},
        'password': PASSWORD, 'unexpected': CANARY})
    assert response.status_code == 422
    assert CANARY not in response.text and PASSWORD not in response.text


async def test_validate_provider_failure_is_sanitized(api, env, monkeypatch):
    from acc.server.dns_providers import CloudflareProvider, DNSError
    headers = await owner_client(api, env)
    response = await api.post('/api/dns/credentials', headers=headers, json=dict(
        name='Certificates', provider='cloudflare', purpose='certificates', fields={'api_token': CANARY}, password=PASSWORD))
    key_id = response.json()['id']
    async def fail(self, refresh=False):
        raise DNSError('provider dumped ' + CANARY)
    monkeypatch.setattr(CloudflareProvider, 'validate_credentials', fail)
    result = await api.post(f'/api/dns/credentials/{key_id}/validate', headers=headers, json={'password': PASSWORD})
    assert result.status_code == 409
    assert CANARY not in result.text
    assert 'DNS validation failed' in result.text


async def test_retired_infrastructure_values_expire_without_sessions(api, env):
    from acc.server import infrastructure as infra
    from acc.server.models import InfrastructureCredential
    headers = await owner_client(api, env)
    response = await api.post('/api/dns/credentials', headers=headers, json=dict(name='Cert', provider='cloudflare',
        purpose='certificates', fields={'api_token': CANARY}, password=PASSWORD))
    key_id = response.json()['id']
    await api.post(f'/api/dns/credentials/{key_id}/replace', headers=headers,
        json={'fields': {'api_token': 'replacement-canary-12345678'}, 'password': PASSWORD}) # gitleaks:allow -- synthetic fixture
    with api.app.state.db() as s:
        key = s.get(InfrastructureCredential, key_id)
        assert len(key.retired) == 1
        infra.purge_retired(s, now=10**12)
        assert key.retired == []


async def test_apply_only_delivers_certificate_fields_and_rollback_is_audited(api, env, monkeypatch):
    from acc.server import dns_helper_client
    from acc.server.models import AuditLog
    headers = await owner_client(api, env)
    ids = []
    publishing = 'publishing-only-canary-987654321'
    for purpose, token in [('certificates', CANARY), ('publishing', publishing)]:
        created = await api.post('/api/dns/credentials', headers=headers, json=dict(name=purpose, provider='cloudflare',
            purpose=purpose, fields={'api_token': token}, password=PASSWORD))
        ids.append(created.json()['id'])
    saved = await api.post('/api/dns/binding', headers=headers, json=dict(provider='cloudflare', zone='example.com',
        certificate_id=ids[0], publishing_id=ids[1], password=PASSWORD))
    revision = saved.json()['revision']
    seen = []
    async def request(message):
        seen.append(message)
        return {'ok': True, 'generation': 'test-generation', 'issuance': 'unverified'}
    monkeypatch.setattr(dns_helper_client, 'request', request)
    applied = await api.post('/api/dns/apply', headers=headers, json={'password': PASSWORD, 'revision': revision})
    assert applied.status_code == 200 and applied.json()['applied_revision'] == revision
    assert seen[0]['fields'] == {'api_token': CANARY}
    assert publishing not in json.dumps(seen)
    assert CANARY not in applied.text
    stale = await api.post('/api/dns/apply', headers=headers, json={'password': PASSWORD, 'revision': revision - 1})
    assert stale.status_code == 409 and len(seen) == 1
    restored = await api.post('/api/dns/rollback', headers=headers, json={'password': PASSWORD})
    assert restored.status_code == 200 and restored.json()['status'] == 'saved'
    with api.app.state.db() as s:
        audits = s.scalars(select(AuditLog).where(AuditLog.action.like('dns.%'))).all()
        serialized = json.dumps([a.detail for a in audits])
        assert CANARY not in serialized and publishing not in serialized
        assert {'dns.apply', 'dns.rollback'} <= {a.action for a in audits}


async def test_missing_vault_key_blocks_infrastructure_add_without_recreation(api, env):
    from acc.server.models import InfrastructureCredential
    headers = await owner_client(api, env)
    body = dict(name='Cert', provider='cloudflare', purpose='certificates', fields={'api_token': CANARY}, password=PASSWORD)
    assert (await api.post('/api/dns/credentials', headers=headers, json=body)).status_code == 201
    store = api.app.state.supervisor.vault
    path = env['settings'].vault_key_path
    path.unlink()
    store.cipher = vault.Cipher(path)  # simulate a restart after restoring DB without secrets
    result = await api.post('/api/dns/credentials', headers=headers, json=body)
    assert result.status_code == 409
    assert 'restore' in result.text.lower() and CANARY not in result.text
    assert not path.exists()
    with api.app.state.db() as s:
        assert len(s.scalars(select(InfrastructureCredential)).all()) == 1
