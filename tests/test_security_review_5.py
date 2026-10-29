"""Bounded defensive proofs for review five; no real external requests."""
import asyncio
import socket
import time

import httpx
import pytest
from test_member_views import member  # noqa: F401 (fixture)
from test_users import csrf

from acc.server import node_host, publish_addresses


async def test_detect_stops_reading_an_oversized_response():
    chunks = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(16):
                chunks.append(1)
                yield b'8.8.8.8' + b' ' * 1024

    result = await publish_addresses.detect(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Body())), interfaces={})
    assert result['suggestions'] == [] and len(result['failures']) == 2
    assert len(chunks) <= 2


async def test_detect_refuses_compression_before_reading():
    import gzip

    read = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            read.append(True)
            yield gzip.compress(b'8.8.8.8')

    result = await publish_addresses.detect(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, stream=Body(), headers={'content-encoding': 'gzip'})), interfaces={})
    assert result['suggestions'] == [] and len(result['failures']) == 2
    assert read == []


async def test_detect_has_a_total_deadline(monkeypatch):
    monkeypatch.setattr(publish_addresses, 'DETECT_TIMEOUT', 0.02, raising=False)

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(30):
                await asyncio.sleep(0.01)
                yield b' '

    try:
        result = await asyncio.wait_for(publish_addresses.detect(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Slow())), interfaces={}), 0.2)
    except TimeoutError:
        pytest.fail('Detect exceeded its total deadline')
    assert len(result['failures']) == 2 and not result['suggestions']


@pytest.mark.parametrize('ip', ['127.0.0.1', '169.254.169.254', '10.0.0.8', '::ffff:10.0.0.8'])
async def test_detect_refuses_private_dns_before_network(monkeypatch, ip):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]

    reached = []

    async def network(self, request):
        reached.append(request.url.host)
        return httpx.Response(200, text='8.8.8.8')

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', network)
    result = await publish_addresses.detect(interfaces={})
    assert reached == [], 'unsafe DNS destination reached the network transport'
    assert not result['suggestions'] and len(result['failures']) == 2


@pytest.mark.parametrize('at', [1e18, time.time() + 86400], ids=['out-of-range', 'tomorrow'])  # time IDs differ per xdist worker
def test_node_rejects_out_of_range_or_future_collection_times(at):
    assert node_host.clean_snapshot({'collected_at': at}) is None
    assert node_host.clean_light({'collected_at': at}) is None


async def test_detect_pins_public_dns_preserving_tls_identity(monkeypatch):
    calls = []
    received = []

    async def resolve(host, *args, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]

    async def network(self, request):
        received.append(request)
        return httpx.Response(200, text='1.1.1.1')

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', network)
    result = await publish_addresses.detect(interfaces={})
    assert result['agreement'] and len(result['suggestions']) == 2
    assert sorted(calls) == ['api.ipify.org', 'checkip.amazonaws.com']
    assert len(received) == 2
    for request in received:
        assert request.url.host == '8.8.8.8'
        assert request.headers['host'] == request.extensions['sni_hostname']
        assert request.headers['accept-encoding'] == 'identity'


@pytest.mark.parametrize('method,path,body', [
    ('GET', '/api/board', None),
    ('GET', '/api/host?machine=lab-box', None),
    ('GET', '/api/publish/addresses', None),
    ('POST', '/api/publish/addresses/detect', None),
    ('POST', '/api/publish/addresses', {'address': '8.8.8.8', 'label': 'Edge', 'access': 'public'}),
    ('POST', '/api/publish/addresses/unknown/default', None),
    ('POST', '/api/publish/addresses/unknown/remove', None),
    ('POST', '/api/issue-servers/resolve', {'base_url': 'https://git.example.lan'}),
    ('POST', '/api/issue-servers', {'base_url': 'https://git.example.lan', 'addresses': ['10.0.0.8']}),
    ('POST', '/api/issue-servers/unknown/confirm', {'base_url': 'https://git.example.lan', 'addresses': ['10.0.0.8']}),
    ('POST', '/api/issue-servers/unknown/remove', None),
])
async def test_new_owner_routes_refuse_member_and_anonymous(api, member, method, path, body):  # noqa: F811
    denied = await member.request(method, path, json=body, headers=csrf(member))
    assert denied.status_code == 403
    async with httpx.AsyncClient(transport=api._transport, base_url=api.base_url) as anonymous:
        denied = await anonymous.request(method, path, json=body)
        assert denied.status_code == 401


def test_node_old_collection_times_remain_visible_as_old():
    for at in (0, time.time() - 3600, time.time() + 60):
        assert node_host.clean_snapshot({'collected_at': at})['collected_at'] == at
        assert node_host.clean_light({'collected_at': at})['collected_at'] == at
    for at in (float('inf'), float('nan'), -1, True, 'today'):
        assert node_host.clean_snapshot({'collected_at': at}) is None
        assert node_host.clean_light({'collected_at': at}) is None


async def test_bad_node_time_cannot_refresh_a_cached_reading():
    from acc.server.nodes import NodeLink

    async def send(data):
        pass

    link = NodeLink('example-node', 'lab-box', send, {'capabilities': ['host-v1']}, [])
    try:
        at = time.time() - 3600
        link.host_state({'t': 'host-state', 'snapshot': {'collected_at': at, 'hostname': 'old-report'}})
        previous = link.host_snapshot
        received = link.host_snapshot_at
        light_received = link.host_light_at
        link.host_state({'t': 'host-state', 'snapshot': {'collected_at': 1e18, 'hostname': 'bad-report'}})
        link.host_state({'t': 'host-light', 'light': {'collected_at': 1e18, 'cpu_percent': 0}})
        assert link.host_snapshot is previous and previous['collected_at'] == at
        assert link.host_snapshot_at == received and link.host_light_at == light_received
    finally:
        link.close()
        await asyncio.gather(link._sender, link._watchdog, return_exceptions=True)


async def test_detect_never_reuses_verified_tls_between_service_names(tmp_path, monkeypatch):
    """Even equal pinned IPs must verify each original hostname, using real local TLS and keep-alive."""
    import ssl

    from test_installer import FakePrimary, pem

    from acc.server import issue_sources

    primary = FakePrimary(tmp_path)
    primary.server.RequestHandlerClass.protocol_version = 'HTTP/1.1'
    primary.routes['/'] = b'8.8.8.8'
    ca = tmp_path / 'test-ca.pem'
    ca.write_bytes(pem(primary.ca))
    port = primary.server.server_address[1]
    monkeypatch.setattr(publish_addresses, 'SERVICES', (primary.url, f'https://wrong.example.com:{port}'))

    async def resolve(base):
        if 'wrong.example.com' in base:
            await asyncio.sleep(0.1)  # the first service's socket is idle before the second uses the same IP
        return ['127.0.0.1']

    monkeypatch.setattr(issue_sources, 'resolve_addresses', resolve)
    # Confine this test to its own loopback HTTPS fixture; production still rejects loopback.
    monkeypatch.setattr(issue_sources, 'allowed_address', lambda *args: True)
    monkeypatch.setattr(issue_sources, 'server_context', lambda server: ssl.create_default_context(cafile=ca))
    try:
        result = await publish_addresses.detect(interfaces={})
        assert result['suggestions'] == [{'address': '8.8.8.8', 'source': primary.url, 'access': 'public'}]
        assert result['failures'] == [f'https://wrong.example.com:{port}']
        assert primary.hits == ['/']
    finally:
        primary.close()
