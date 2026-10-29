"""Release labels are server-owned metadata; preview selection remains available."""
import pytest
from test_auth import PASSWORD
from test_dns_infrastructure import owner_client
from test_dns_providers_p2 import AWS_FIELDS, AZURE_FIELDS
from test_dns_wave2 import FIELDS

from acc.dns_credentials import PROVIDERS
from acc.server.models import DNSBinding

ALL_FIELDS = {'cloudflare': {'api_token': 'example-token-0123456789'},
              'azure': AZURE_FIELDS, 'route53': AWS_FIELDS, **FIELDS}


async def test_provider_qualification_list_is_owner_only_and_defaults_cloudflare(api, env):
    assert (await api.get('/api/dns/providers')).status_code in (401, 403)
    await owner_client(api, env)
    result = await api.get('/api/dns/providers')
    assert result.status_code == 200
    rows = result.json()
    assert [row['id'] for row in rows] == list(PROVIDERS)
    assert rows[0] == {'id': 'cloudflare', 'qualification': 'qualified'}
    assert all(row['qualification'] == 'preview' for row in rows[1:])


@pytest.mark.parametrize('provider', ALL_FIELDS)
async def test_preview_credentials_and_saved_bindings_remain_available(api, env, provider, monkeypatch):
    headers = await owner_client(api, env)
    status = 'qualified' if provider == 'cloudflare' else 'preview'
    ids = []
    for purpose in ('certificates', 'publishing'):
        added = await api.post('/api/dns/credentials', headers=headers, json={
            'name': 'Example ' + purpose, 'provider': provider, 'purpose': purpose,
            'fields': ALL_FIELDS[provider], 'password': PASSWORD})
        assert added.status_code == 201, added.text
        assert added.json()['qualification'] == status
        ids.append(added.json()['id'])
    saved = await api.post('/api/dns/binding', headers=headers, json={
        'provider': provider, 'zone': 'example.com', 'certificate_id': ids[0],
        'publishing_id': ids[1], 'password': PASSWORD})
    assert saved.status_code == 200, saved.text
    assert saved.json()['qualification'] == status
    old = saved.json()
    # Changing just the provider's status must update all API views, including existing rows.
    monkeypatch.setitem(PROVIDERS[provider], 'qualification', 'qualified' if status == 'preview' else 'preview')
    changed = 'qualified' if status == 'preview' else 'preview'
    binding = (await api.get('/api/dns/binding')).json()
    assert binding['qualification'] == changed
    for key in ('provider', 'zone', 'revision', 'certificate_id', 'publishing_id', 'status'):
        assert binding[key] == old[key]
    assert all(key['qualification'] == changed for key in (await api.get('/api/dns/credentials')).json())
    listed = (await api.get('/api/dns/providers')).json()
    assert next(row for row in listed if row['id'] == provider)['qualification'] == changed
    # The same single status replaces the old independent Publishing availability flag.
    from acc.server.publish import PublishRefused
    publisher = api.app.state.publisher
    sentinel = object()
    monkeypatch.setattr(publisher, '_provider', lambda name, fields: sentinel)
    if changed == 'qualified':
        assert publisher.dns_selection() == (sentinel, 'example.com')
    else:
        with pytest.raises(PublishRefused, match='qualified for publishing'):
            publisher.dns_selection()
    # Replacement remains possible for a saved preview provider, and doesn't rewrite its binding.
    replaced = await api.post(f'/api/dns/credentials/{ids[0]}/replace', headers=headers,
                             json={'password': PASSWORD, 'fields': ALL_FIELDS[provider]})
    assert replaced.status_code == 200, replaced.text
    with api.app.state.db() as s:
        assert s.get(DNSBinding, 1).provider == provider
