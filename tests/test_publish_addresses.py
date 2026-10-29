"""Owner address inventory: validation, bootstrap, selection and detection."""
import httpx
import pytest

from acc.server import publish_addresses as addresses


@pytest.mark.parametrize('value', [
    '10.0.0.1', '127.0.0.1', '169.254.1.1', '100.64.0.1', '224.0.0.1',
    '192.0.2.1', '198.51.100.1', '203.0.113.1', '240.0.0.1', '0.0.0.0',
    '::', '::1', 'fe80::1', 'fc00::1', 'ff02::1', '2001:db8::1', '::ffff:8.8.8.8',
    '8.8.0.0/15', '2606:4700::/16',
])
def test_refuses_nonpublic_or_excessive_blocks(value):
    with pytest.raises(ValueError):
        addresses.validate(value, 'public')


def test_ipv6_and_block_picking():
    assert addresses.validate('2606:4700:1234::/48', 'public') == '2606:4700:1234::/48'
    rows = [{'address': '8.8.8.0/24', 'access': 'public', 'default': True}]
    assert addresses.choose(rows, 'public', '8.8.8.42') == '8.8.8.42'
    assert addresses.choose(rows, 'public', None) == '8.8.8.1'
    for ip in ['8.8.9.1', '8.8.8.0', '8.8.8.255']:
        with pytest.raises(ValueError):
            addresses.choose(rows, 'public', ip)


async def test_bootstrap_and_overlap_audit(api):
    rows = (await api.get('/api/publish/addresses')).json()
    assert rows[0]['address'] == '203.0.113.10' and rows[0]['source'] == 'environment'
    added = await api.post('/api/publish/addresses', json={'address': '8.8.8.0/24', 'label': 'Edge', 'access': 'public'})
    assert added.status_code == 201, added.text
    overlap = await api.post('/api/publish/addresses', json={'address': '8.8.8.8', 'label': 'Duplicate', 'access': 'public'})
    assert overlap.status_code == 409
    with api.app.state.db() as db:
        from acc.server.models import AuditLog
        assert db.query(AuditLog).filter_by(action='publish.address_add').one().detail['address'] == '8.8.8.0/24'


async def test_detect_compares_without_saving():
    def reply(request):
        return httpx.Response(200, text='8.8.8.8' if request.url.host == 'api.ipify.org' else '1.1.1.1')
    result = await addresses.detect(transport=httpx.MockTransport(reply), interfaces={})
    assert result['agreement'] is False
    assert {s['address'] for s in result['suggestions']} == {'8.8.8.8', '1.1.1.1'}
    def fail(request):
        raise httpx.ConnectError('offline')
    result = await addresses.detect(transport=httpx.MockTransport(fail), interfaces={})
    assert not result['suggestions'] and len(result['failures']) == 2


@pytest.mark.parametrize('value', ['192.0.0.9', '192.88.99.1', '2001:1::1', '2002:808:808::1', '3fff::1'])
def test_refuses_special_purpose_ranges(value):
    with pytest.raises(ValueError):
        addresses.validate(value, 'public')


async def test_bootstrap_is_persistent_and_pins_existing_publications(api, env):
    from acc.server.models import PublishRequest
    with api.app.state.db() as db:
        db.add(PublishRequest(id='pub-legacy', action='publish', host='legacy.example.com', port=8100,
                              access='public', status='applied', requested_by='owner'))
        db.commit()
    rows = (await api.get('/api/publish/addresses')).json()
    from dataclasses import replace
    with api.app.state.db() as db:
        assert addresses.inventory(db, replace(env['settings'], origin_public_ip='1.1.1.1')) == rows
    with api.app.state.db() as db:
        assert db.get(PublishRequest, 'pub-legacy').context['target_ip'] == '203.0.113.10'


async def test_default_changes_affect_only_future_requests_and_block_is_not_expanded(api):
    from acc.server.models import PublishRequest
    publisher = api.app.state.publisher
    old = publisher.request(action='publish', host='old.example.com', port=8100, access='public', requested_by='owner')
    rows = (await api.post('/api/publish/addresses', json={
        'address': '8.8.8.0/24', 'label': 'Edge', 'access': 'public'})).json()
    assert len(rows) == 3
    assert (await api.post(f"/api/publish/addresses/{rows[-1]['id']}/default")).status_code == 200
    new = publisher.request(action='publish', host='new.example.com', port=8100, access='public', requested_by='owner')
    with api.app.state.db() as db:
        assert db.get(PublishRequest, old).context['target_ip'] == '203.0.113.10'
        assert db.get(PublishRequest, new).context['target_ip'] == '8.8.8.1'


async def test_ipv6_overlap_and_internal_public_overlap_refused(api):
    body = {'address': '2606:4700:1234::/48', 'label': 'Edge', 'access': 'public'}
    assert (await api.post('/api/publish/addresses', json=body)).status_code == 201
    for value, access in [('2606:4700:1234::42', 'public'), ('2606:4700:1234::/64', 'internal')]:
        result = await api.post('/api/publish/addresses', json=dict(body, address=value, access=access))
        assert result.status_code == 409 and 'overlap' in result.text


async def test_detect_agreement_and_interface_suggestions():
    import socket
    from types import SimpleNamespace
    result = await addresses.detect(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='8.8.8.8')),
        interfaces={'demo': [SimpleNamespace(family=socket.AF_INET6, address='2606:4700:1234::42'),
                             SimpleNamespace(family=socket.AF_INET, address='10.0.0.42')]})
    assert result['agreement'] is True
    assert {'address': '10.0.0.42', 'source': 'Interface demo', 'access': 'internal'} in result['suggestions']
    assert {'address': '2606:4700:1234::42', 'source': 'Interface demo', 'access': 'public'} in result['suggestions']
