"""Security review of DNS providers phase 2 (SRD2-01..05). Fake transports and temporary roots only; no live DNS."""
import hashlib
import os
import stat
from pathlib import Path

import httpx
import pytest
from helpers import write_db
from sqlalchemy import select
from test_auth import PASSWORD
from test_dns_infrastructure import owner_client
from test_dns_providers_p2 import AWS_FIELDS
from test_publish import FakeCloudflareAPI

from acc.server.dns_providers import CloudflareProvider, DNSError
from acc.server.models import AuditLog, InfrastructureCredential


async def cloudflare_binding(api, env, *, imported=False):
    """A Cloudflare binding on example.com; optionally a separate, unbound imported legacy Publishing credential."""
    headers = await owner_client(api, env)
    ids = []
    for purpose in ('certificates', 'publishing'):
        r = await api.post('/api/dns/credentials', headers=headers, json=dict(name=purpose, provider='cloudflare',
            purpose=purpose, fields={'api_token': 'vault-selection-12345678'}, password=PASSWORD))
        ids.append(r.json()['id'])
    saved = await api.post('/api/dns/binding', headers=headers, json=dict(provider='cloudflare', zone='example.com',
        certificate_id=ids[0], publishing_id=ids[1], password=PASSWORD))
    assert saved.status_code == 200, saved.text
    legacy = env['settings'].cloudflare_token_path
    legacy.write_text('legacy-selection-87654321')
    legacy.chmod(0o600)
    imported_id = None
    if imported:
        r = await api.post('/api/dns/credentials', headers=headers, json=dict(name='Imported Cloudflare publishing',
            provider='cloudflare', purpose='publishing', fields={'api_token': 'legacy-selection-87654321'},
            password=PASSWORD))
        imported_id = r.json()['id']
        with write_db(api.app.state.db) as s:
            s.get(InfrastructureCredential, imported_id).migration_source = 'legacy-publishing'
            s.commit()
    return headers, imported_id


def counting_factory(seen, zones=None):
    remote = FakeCloudflareAPI()
    if zones is not None:
        remote.zones = zones
    def handler(request):
        seen.append((request.headers['Authorization'], request.url.path))
        return remote.handler(request)
    return lambda name, fields: CloudflareProvider(fields['api_token'], transport=httpx.MockTransport(handler))


# ---- SRD2-01: legacy Publishing rollback ignores retirement and the bound zone ------------------------------------

@pytest.mark.parametrize('retire', ['remove', 'disable', 'replace'])
async def test_srd2_01_retired_legacy_publishing_token_cannot_be_restored(api, env, monkeypatch, retire):
    headers, imported = await cloudflare_binding(api, env, imported=True)
    seen = []
    monkeypatch.setattr('acc.server.dns_providers.make_provider', counting_factory(seen))
    # The owner restores legacy once (allowed), then retires the imported copy of that same token in Keys.
    assert (await api.post('/api/dns/publishing-source', headers=headers,
                           json={'source': 'legacy', 'password': PASSWORD})).status_code == 200
    body = {'password': PASSWORD} | ({'fields': {'api_token': 'rotated-publishing-123456'}} if retire == 'replace' else {}) # gitleaks:allow -- synthetic fixture
    assert (await api.post(f'/api/dns/credentials/{imported}/{retire}', headers=headers, json=body)).status_code == 200
    # Already-selected legacy stops at the next job, with no fallback to the retired token.
    from acc.server.publish import PublishRefused
    with pytest.raises(PublishRefused, match='retired'):
        api.app.state.publisher.dns_selection()
    assert not any('legacy-selection' in auth for auth, _ in seen)
    # And it cannot be selected again.
    assert (await api.post('/api/dns/publishing-source', headers=headers,
                           json={'source': 'vault', 'password': PASSWORD})).status_code == 200
    again = await api.post('/api/dns/publishing-source', headers=headers, json={'source': 'legacy', 'password': PASSWORD})
    assert again.status_code == 409 and 'retired' in again.text
    assert (await api.get('/api/dns/binding')).json()['legacy_publishing'] == 'retired'


async def test_srd2_01_legacy_rollback_keeps_the_bound_zone(api, env, monkeypatch):
    headers, _ = await cloudflare_binding(api, env)
    seen = []
    monkeypatch.setattr('acc.server.dns_providers.make_provider', counting_factory(seen))
    assert (await api.post('/api/dns/publishing-source', headers=headers,
                           json={'source': 'legacy', 'password': PASSWORD})).status_code == 200
    provider, selected = api.app.state.publisher.dns_selection()
    await provider.close()
    assert selected == 'example.com'  # baseline: None, so every zone the legacy token can edit
    assert [z['name'] for z in await api.app.state.publisher.zones()] == ['example.com']


# ---- SRD2-02: every zone listing decrypts the credential and calls the provider ----------------------------------

async def test_srd2_02_zone_listing_is_cached_per_selection(api, env, monkeypatch):
    headers, _ = await cloudflare_binding(api, env)
    seen = []
    monkeypatch.setattr('acc.server.dns_providers.make_provider', counting_factory(seen))
    publisher = api.app.state.publisher
    for _ in range(5):
        assert [z['name'] for z in await publisher.zones()] == ['example.com']
    assert len(seen) == 1  # baseline: one provider round trip per listing
    # A different selection is a different cache entry: never serve one credential's view for another.
    assert (await api.post('/api/dns/publishing-source', headers=headers,
                           json={'source': 'legacy', 'password': PASSWORD})).status_code == 200
    await publisher.zones()
    assert len(seen) == 2 and 'legacy-selection' in seen[-1][0]


async def test_srd2_02_concurrent_listings_share_one_provider_call(api, env, monkeypatch):
    import asyncio
    await cloudflare_binding(api, env)
    seen = []
    monkeypatch.setattr('acc.server.dns_providers.make_provider', counting_factory(seen))
    await asyncio.gather(*(api.app.state.publisher.zones() for _ in range(10)))
    assert len(seen) == 1


# ---- SRD2-03: the Route 53 hosted-zone ID is not bound to the zone name -----------------------------------------

def route53_handler(apex, calls):
    def handler(request):
        calls.append((request.method, request.url.path, dict(request.url.params)))
        rrset = (f'<ResourceRecordSet><Name>{apex}.</Name><Type>SOA</Type><TTL>900</TTL><ResourceRecords>'
                 '<ResourceRecord><Value>ns.example.net. admin.example.net. 1 7200 900 1209600 86400</Value>'
                 '</ResourceRecord></ResourceRecords></ResourceRecordSet>')
        return httpx.Response(200, text='<ListResourceRecordSetsResponse xmlns="https://route53.amazonaws.com/doc/2013-04-01/">'
                              f'<ResourceRecordSets>{rrset}</ResourceRecordSets><IsTruncated>false</IsTruncated>'
                              '</ListResourceRecordSetsResponse>')
    return handler


async def test_srd2_03_route53_refuses_a_hosted_zone_for_another_name():
    """The entered ID belongs to the parent zone; Route 53 would accept trial names below its apex."""
    from acc.server.dns_providers import Route53Provider
    calls = []
    fields = {**AWS_FIELDS, 'zone_name': 'trial.example.com'}
    provider = Route53Provider(fields, transport=httpx.MockTransport(route53_handler('example.com', calls)))
    with pytest.raises(DNSError, match='zone'):
        await provider.zones()  # baseline: returns the owner's pairing without asking Route 53
    with pytest.raises(DNSError, match='zone'):
        await provider.validate_credentials()
    assert all(method == 'GET' for method, _, _ in calls)
    await provider.close()


async def test_srd2_03_route53_accepts_its_own_apex():
    from acc.server.dns_providers import Route53Provider
    calls = []
    provider = Route53Provider(AWS_FIELDS, transport=httpx.MockTransport(route53_handler('example.com', calls)))
    assert await provider.zones() == [{'id': 'ZEXAMPLE', 'name': 'example.com'}]
    assert await provider.zones() == [{'id': 'ZEXAMPLE', 'name': 'example.com'}]
    assert len(calls) == 1 and calls[0][2] == {'name': 'example.com', 'type': 'SOA', 'maxitems': '1'}
    await provider.close()


# ---- SRD2-04: the harness executes the candidate by path after hashing it ---------------------------------------

async def test_srd2_04_harness_executes_only_the_verified_bytes(tmp_path, monkeypatch):
    import argparse
    import json

    from acc import dns_live_test as harness
    candidate = tmp_path / 'bin' / 'caddy'
    candidate.parent.mkdir()
    candidate.write_bytes(b'#!/bin/sh\necho reviewed\n')
    candidate.chmod(0o755)
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    credentials = []
    for name, secret in (('pub', 'publishing-secret'), ('cert', 'certificate-secret')):
        path = tmp_path / f'{name}.json'
        path.write_text(json.dumps({**AWS_FIELDS, 'secret_access_key': secret}))
        path.chmod(0o600)
        credentials.append(path)
    state = tmp_path / 'state'

    class FakeProvider:
        name = 'route53'
        async def zones(self):
            return [{'id': 'ZEXAMPLE', 'name': 'example.com'}]
        async def close(self):
            pass

    async def publishing_probe(provider, db, zone, zone_name):
        # Whoever can write the candidate's directory swaps it after the digest check.
        candidate.write_bytes(b'#!/bin/sh\necho unreviewed\n')
        return []
    executed = {}
    async def certificate_probe(provider, fields, zone, zone_name, binary, state_dir):
        path = Path(binary)
        executed.update(path=path, data=path.read_bytes(), mode=stat.S_IMODE(path.stat().st_mode))
        return []
    monkeypatch.setattr(harness, 'make_provider', lambda name, fields: FakeProvider())
    monkeypatch.setattr(harness, 'publishing_probe', publishing_probe)
    monkeypatch.setattr(harness, 'certificate_probe', certificate_probe)
    args = argparse.Namespace(provider='route53', disposable_zone='example.com', ack_disposable=True,
        publishing_credentials=credentials[0], certificate_credentials=credentials[1], state_dir=state,
        caddy=candidate, candidate_sha256=digest)
    umask = os.umask(0o077)
    try:
        await harness.run(args)  # sets the harness process's umask to 077, which must not outlive this test
    finally:
        os.umask(umask)
    assert executed['data'] == b'#!/bin/sh\necho reviewed\n'  # baseline: the swapped, unreviewed bytes
    assert executed['path'].parent == state and executed['mode'] == 0o700
    assert not executed['path'].exists()  # the private copy is removed afterwards


# ---- SRD2-05: post-authentication refusals on the new routes are not audited ------------------------------------

async def test_srd2_05_refused_adoption_and_source_switch_are_audited(api, env):
    headers = await owner_client(api, env)
    assert (await api.post('/api/dns/adopt', headers=headers,
                           json={'request_id': 'pub-missing', 'password': PASSWORD})).status_code == 409
    assert (await api.post('/api/dns/publishing-source', headers=headers,
                           json={'source': 'legacy', 'password': PASSWORD})).status_code == 409
    with api.app.state.db() as s:
        rows = {(r.action, r.detail.get('result')) for r in s.scalars(select(AuditLog))}
    assert ('dns.record.adopt', 'refused') in rows
    assert ('dns.publishing.source', 'refused') in rows
    assert not any(PASSWORD in str(r) for r in rows)


# ---- Notes: unexpected vault errors escape as uncaught exceptions ------------------------------------------------

async def test_srd2_note_malformed_legacy_token_refuses_instead_of_escaping(api, env):
    from acc.server.publish import PublishRefused
    legacy = env['settings'].cloudflare_token_path
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('not a token!')
    legacy.chmod(0o600)
    with pytest.raises(PublishRefused):  # baseline: VaultError, which execute() does not catch (stuck "applying")
        api.app.state.publisher.dns_selection()


async def test_srd2_note_binding_with_unreadable_credential_is_a_clean_refusal(api, env):
    from test_dns_providers_p2 import AZURE_FIELDS
    headers = await owner_client(api, env)
    ids = []
    for purpose in ('certificates', 'publishing'):
        r = await api.post('/api/dns/credentials', headers=headers, json=dict(name=purpose, provider='azure',
            purpose=purpose, fields=AZURE_FIELDS, password=PASSWORD))
        ids.append(r.json()['id'])
    with api.app.state.db() as s:  # a value the current vault key cannot open (restored from elsewhere)
        key = s.get(InfrastructureCredential, ids[1])
        key.ciphertext = 'v1:' + 'A' * 64
        s.commit()
    response = await api.post('/api/dns/binding', headers=headers, json=dict(provider='azure', zone='example.com',
        certificate_id=ids[0], publishing_id=ids[1], password=PASSWORD))
    assert response.status_code == 409  # baseline: unhandled VaultError (500)


# ---- SRD2-06: the root helper ignores the release's per-provider qualification -----------------------------------

def qualified(**providers):
    return {'status': 'qualified', 'provider_qualification': {
        name: {'certificates': providers.get(name, False), 'publishing': False, 'evidence': None}
        for name in ('cloudflare', 'azure', 'route53')}}


@pytest.fixture
def private_root(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    yield root, fd
    os.close(fd)


@pytest.mark.parametrize('release', [
    qualified(cloudflare=True),                                   # Azure explicitly unqualified (the TXT hold)
    {**qualified(cloudflare=True, azure=True), 'status': 'pending-ci-and-qualification'},  # bundle not qualified
    {'status': 'qualified'},                                      # phase 1 manifest: a Cloudflare-only bundle
    {},                                                           # no installed manifest
])
def test_srd2_06_policy_alone_cannot_enable_an_unqualified_provider(private_root, release):
    from test_dns_providers_p2 import AZURE_FIELDS

    from acc.dns_helper import DNSHelper, Refused
    root, fd = private_root
    calls = []
    helper = DNSHelper(fd, {'zones': ['example.com'], 'providers': ['cloudflare', 'azure']},
                       lambda action, token: calls.append(action) or True, owner=os.getuid(), release=lambda: release)
    with pytest.raises(Refused, match='provider_unqualified'):  # baseline: applied (policy was the only gate)
        helper.handle(dict(action='apply', provider='azure', zone='example.com', revision=1, fields=AZURE_FIELDS))
    assert calls == [] and not (root / 'current.env').exists()


def test_srd2_06_provider_needs_both_release_and_policy(private_root):
    from test_dns_helper import TOKEN

    from acc.dns_helper import DNSHelper, Refused
    _, fd = private_root
    release = qualified(cloudflare=True, azure=True)
    helper = DNSHelper(fd, {'zones': ['example.com']}, lambda action, token: True, owner=os.getuid(),
                       release=lambda: release)
    from test_dns_providers_p2 import AZURE_FIELDS
    with pytest.raises(Refused, match='invalid_binding'):  # qualified, but the local policy doesn't allow it
        helper.handle(dict(action='apply', provider='azure', zone='example.com', revision=1, fields=AZURE_FIELDS))
    applied = helper.handle(dict(action='apply', provider='cloudflare', zone='example.com', revision=2,
                                 fields={'api_token': TOKEN}))
    assert applied['ok']
    # A phase 1 manifest (no per-provider table) still qualifies Cloudflare, and only Cloudflare.
    release.clear()
    release['status'] = 'qualified'
    assert helper.handle(dict(action='apply', provider='cloudflare', zone='example.com', revision=3,
                              fields={'api_token': TOKEN}))['ok']


def test_srd2_06_helper_without_a_release_refuses_and_reads_the_installed_manifest(private_root):
    import json

    from test_dns_helper import TOKEN

    from acc.dns_helper import DNSHelper, Refused, installed_release
    root, fd = private_root
    helper = DNSHelper(fd, {'zones': ['example.com']}, lambda action, token: True, owner=os.getuid())
    with pytest.raises(Refused, match='provider_unqualified'):  # fail closed
        helper.handle(dict(action='apply', provider='cloudflare', zone='example.com', revision=1,
                           fields={'api_token': TOKEN}))
    assert installed_release(fd, owner=os.getuid()) == {}
    (root / 'release.json').write_text(json.dumps(qualified(cloudflare=True)))
    (root / 'release.json').chmod(0o600)
    assert installed_release(fd, owner=os.getuid())['provider_qualification']['cloudflare']['certificates'] is True
