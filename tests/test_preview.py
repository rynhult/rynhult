"""Preview boundaries: removing any gate here exposes another person's app or ACC itself."""
import asyncio
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
import pytest
from helpers import preview_listener, write_db
from test_auth import (
    cookie_client,
    do_setup,
    live,  # noqa: F401
)
from test_users import csrf

from acc.server import auth
from acc.server.models import AuthSession, PortReservation, Project, User
from acc.server.preview_remote import machine_for


@pytest.fixture(autouse=True)
def demo_socket():
    global PORT
    with preview_listener() as sock:
        PORT = sock.getsockname()[1]
        yield sock


@pytest.fixture(params=['local', 'remote'])
async def viewer(api, env, project, request):
    await do_setup(api, env)
    client = cookie_client(api)
    with write_db(api.app.state.db) as s:
        s.get(Project, project['id']).owner = 'casey'
        s.add(PortReservation(port=PORT, project_id=project['id'], owner='casey'))
        s.commit()
    link = node = None
    if request.param == 'remote':
        from acc.node.preview import PreviewRelay
        from acc.server.nodes import NodeLink
        async def to_node(data):
            node.receive(data)
        async def to_main(data):
            link.receive(data)
        node = PreviewRelay(to_main)
        link = NodeLink('demo-id', 'demo-machine', to_node, {'capabilities':['preview-v1']}, [])
        api.app.state.ctx.broker.hub.attach(link)
        with write_db(api.app.state.db) as s:
            s.get(Project, project['id']).host = 'demo-machine'
            s.commit()
    yield client, project['id']
    if link:
        api.app.state.ctx.broker.hub.detach(link)
        node.close()


@pytest.fixture
async def demo(demo_socket):
    async def reply(reader, writer):
        try:
            await reader.readuntil(b'\r\n\r\n')
        except asyncio.IncompleteReadError:
            writer.close()
            return
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\nSet-Cookie: acc_session=evil; Domain=example.com\r\nAccess-Control-Allow-Origin: *\r\n\r\ndemo')
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(reply, sock=demo_socket)
    yield
    server.close()
    await server.wait_closed()


async def minted(client, pid):
    r = await client.post(f'/api/projects/{pid}/preview', json={'port': PORT}, headers=csrf(client))
    assert r.status_code == 200, r.text
    return r.json()['url']


async def enter(api, url):
    parts = urlsplit(url)
    client = httpx.AsyncClient(transport=api._transport, base_url=f'{parts.scheme}://{parts.netloc}')
    r = await client.post('/__acc_preview/login', json={'token': parts.fragment}, headers={'Origin': f'{parts.scheme}://{parts.netloc}'})
    return client, r


async def test_preview_lists_only_listening_reserved_ports(viewer, demo):
    client, pid = viewer
    r = await client.get(f'/api/projects/{pid}/preview')
    assert r.status_code == 200
    assert r.json()['ports'] == [PORT]
    assert r.json()['available'] is True


async def test_preview_origin_never_routes_acc_and_strips_credentials(viewer, demo):
    client, pid = viewer
    preview, login = await enter(client, await minted(client, pid))
    try:
        assert login.status_code == 200
        cookie = login.headers['set-cookie']
        assert 'HttpOnly' in cookie and 'Domain=' not in cookie
        r = await preview.get('/')
        assert r.text == 'demo'
        assert 'set-cookie' not in r.headers
        assert 'access-control-allow-origin' not in r.headers
        assert "sandbox allow-scripts allow-forms allow-same-origin" in r.headers['content-security-policy']
        # Even ACC cookies or its bearer token sent to a preview host cannot reach ACC routes.
        preview.headers['Authorization'] = f'Bearer {client.app.state.ctx.token}'
        preview.cookies.update(client.cookies)
        r = await preview.get('/api/projects')
        assert r.text == 'demo'  # app path, never ACC's JSON
        r = await client.get('/api/projects', headers={'Origin': 'http://preview-1-8001.example.com'})
        assert r.status_code == 403
        r = await client.get('/api/projects', headers={'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'cors'})
        assert r.status_code == 403
        assert "frame-ancestors 'none'" in (await client.get('/')).headers['content-security-policy']

    finally:
        await preview.aclose()

async def test_token_scope_tamper_expiry_and_logout(viewer, demo, monkeypatch):
    client, pid = viewer
    url = await minted(client, pid)
    other, r = await enter(client, url.replace(f'preview-{pid}-{PORT}', f'preview-{pid + 1}-{PORT}'))
    await other.aclose()
    assert r.status_code == 401
    bad, r = await enter(client, url + 'x')
    await bad.aclose()
    assert r.status_code == 401
    preview, r = await enter(client, url)
    assert r.status_code == 200
    with write_db(client.app.state.db) as s:
        for record in s.query(AuthSession):
            s.delete(record)
        s.commit()
    assert (await preview.get('/')).status_code == 401
    await preview.aclose()


async def test_token_expires(viewer, demo, monkeypatch):
    from acc.server import preview as module
    client, pid = viewer
    url = await minted(client, pid)
    monkeypatch.setattr(module, 'now', lambda: auth.now() + timedelta(minutes=6))
    preview, r = await enter(client, url)
    await preview.aclose()
    assert r.status_code == 401


@pytest.mark.parametrize('change', ['port', 'host', 'permission', 'remote'])
async def test_access_rechecked_on_every_request(viewer, demo, change):
    client, pid = viewer
    preview, r = await enter(client, await minted(client, pid))
    assert r.status_code == 200
    with write_db(client.app.state.db) as s:
        if change == 'port':
            s.delete(s.get(PortReservation, PORT))
        elif change == 'host':
            s.get(Project, pid).host = 'other-machine'
        elif change == 'permission':
            s.scalar(__import__('sqlalchemy').select(User).where(User.username == 'casey')).role = 'member'
            s.get(Project, pid).owner = 'other'
        else:
            s.get(Project, pid).archived = True
        s.commit()
    assert (await preview.get('/')).status_code == 401
    await preview.aclose()


async def test_no_arbitrary_hosts_ports_or_cli_mint(viewer, demo):
    client, pid = viewer
    unreserved = 8000 if PORT != 8000 else 8001
    for body in ({'port': unreserved}, {'port': PORT, 'host': '169.254.169.254'}, {'port': 8740}):
        r = await client.post(f'/api/projects/{pid}/preview', json=body, headers=csrf(client))
        assert r.status_code in (404, 422)
    client.headers['Authorization'] = f'Bearer {client.app.state.ctx.token}'
    assert (await client.post(f'/api/projects/{pid}/preview', json={'port': PORT})).status_code == 403


async def test_remote_project_has_clear_unavailable_state(viewer):
    client, pid = viewer
    with write_db(client.app.state.db) as s:
        s.get(Project, pid).host = 'lab-box'
        s.commit()
    r = await client.get(f'/api/projects/{pid}/preview')
    assert r.json()['available'] is False
    assert 'not connected' in r.json()['reason']


async def test_project_member_and_other_member(viewer, demo):
    client, pid = viewer
    with write_db(client.app.state.db) as s:
        user = s.query(User).filter_by(username='casey').one()
        user.role = 'member'
        s.commit()
    assert (await client.get(f'/api/projects/{pid}/preview')).status_code == 200
    await minted(client, pid)
    with write_db(client.app.state.db) as s:
        s.get(Project, pid).owner = 'another-member'
        s.commit()
    assert (await client.get(f'/api/projects/{pid}/preview')).status_code == 404
    assert (await client.post(f'/api/projects/{pid}/preview', json={'port': PORT}, headers=csrf(client))).status_code == 404


async def test_no_credential_forwarding_and_no_redirect_ssrf(viewer, demo_socket):
    client, pid = viewer
    requests = []

    async def app(reader, writer):
        try:
            data = await reader.readuntil(b'\r\n\r\n')
            requests.append(data.lower())
            writer.write(b'HTTP/1.1 302 Found\r\nLocation: http://169.254.169.254/latest/\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
    server = await asyncio.start_server(app, sock=demo_socket)
    try:
        url = await minted(client, pid)
        preview, r = await enter(client, url)
        preview.headers.update({'Authorization': 'secret', 'X-Forwarded-Host': 'acc', 'Forwarded': 'host=acc', 'X-ACC-CSRF': 'secret-csrf'})
        r = await preview.get('/redirect')
        assert r.status_code == 302
        assert r.headers['location'] == 'http://169.254.169.254/latest/'
        assert len(requests) == 1  # no redirect followed, discovery opens TCP only
        assert b'cookie:' not in requests[0]
        assert b'authorization:' not in requests[0]
        assert b'forwarded' not in requests[0]
        assert b'secret-csrf' not in requests[0]
        await preview.aclose()
    finally:
        server.close()
        await server.wait_closed()


async def test_websocket_preview_rechecks_revocation(viewer, demo_socket, live):  # noqa: F811
    import websockets
    from websockets.asyncio.server import serve
    client, pid = viewer

    async def echo(ws):
        async for message in ws:
            await ws.send(message)
    async with serve(echo, sock=demo_socket):
        preview, r = await enter(client, await minted(client, pid))
        assert r.status_code == 200
        hostname = f'preview-{pid}-{PORT}.example.com'
        ingress_port = urlsplit(live).port
        url = f'ws://{hostname}:{ingress_port}/hmr'
        # Explicit TCP destination avoids any DNS requirement in this isolated test.
        kwargs = {'host': '127.0.0.1', 'port': ingress_port, 'proxy': None,
                  'origin': f'http://{hostname}:{ingress_port}'}
        with pytest.raises(websockets.InvalidStatus):
            async with websockets.connect(url, **kwargs):
                pass
        async with websockets.connect(url, additional_headers={'Cookie': f"acc_preview={preview.cookies.get('acc_preview')}"}, **kwargs) as ws:
            await ws.send('hot reload')
            assert await ws.recv() == 'hot reload'
            with write_db(client.app.state.db) as s:
                s.delete(s.get(PortReservation, PORT))
                s.commit()
            with pytest.raises(websockets.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 3)
        await preview.aclose()


@pytest.mark.parametrize('token', [None, 1, {}, [], 'a.b', 'not-a-token'])
async def test_malformed_bootstrap_credentials_are_refused(viewer, token):
    client, pid = viewer
    preview = httpx.AsyncClient(transport=client._transport, base_url=f'http://preview-{pid}-{PORT}.example.com')
    try:
        r = await preview.post('/__acc_preview/login', json={'token': token}, headers={'Origin': str(preview.base_url).rstrip('/')})
        assert r.status_code == 401
    finally:
        await preview.aclose()


async def test_secure_preview_cookie_is_host_only_and_not_an_acc_credential(viewer, demo):
    from dataclasses import replace

    from acc.server.preview import host_for, sign
    client, pid = viewer
    ctx = client.app.state.ctx
    ctx.settings = replace(ctx.settings, cookie_secure=True)
    login_hash = auth.digest(client.cookies.get('acc_session'))
    host = host_for(ctx.settings, pid, PORT)
    token = sign(ctx, {'project':pid, 'port':PORT, 'host':host, 'machine': machine_for(ctx, pid), 'login':login_hash, 'exp':auth.now().timestamp() + 60})
    async with httpx.AsyncClient(transport=client._transport, base_url=f'https://{host}') as preview:
        r = await preview.post('/__acc_preview/login', json={'token':token}, headers={'Origin':f'https://{host}'})
        assert r.status_code == 200
        cookie = r.headers['set-cookie']
        assert cookie.startswith('__Host-acc_preview=')
        assert 'Secure' in cookie and 'HttpOnly' in cookie and 'Path=/' in cookie and 'Domain=' not in cookie
        assert 'x-frame-options' not in (await preview.get('/')).headers
        async with httpx.AsyncClient(transport=client._transport, base_url='https://acc') as acc:
            acc.cookies.set('__Host-acc_preview', token)
            assert (await acc.get('/api/projects')).status_code == 401
            acc.cookies.set('__Host-acc_session', token)
            assert (await acc.get('/api/projects')).status_code == 401


async def test_cross_origin_navigation_cannot_call_acc_api_as_viewer(viewer):
    client, _ = viewer
    r = await client.get('/api/projects', headers={'Sec-Fetch-Site':'same-site', 'Sec-Fetch-Mode':'navigate'})
    assert r.status_code == 403


async def test_preview_mutations_require_preview_origin(viewer, demo):
    client, pid = viewer
    preview, r = await enter(client, await minted(client, pid))
    try:
        assert r.status_code == 200
        r = await preview.post('/action', headers={'Origin':'https://other.example.com'}, content=b'change')
        assert r.status_code == 403
    finally:
        await preview.aclose()


async def test_proxy_preserves_encoded_paths_and_query(viewer, demo_socket):
    client, pid = viewer
    requests = []

    async def app(reader, writer):
        try:
            request = await reader.readuntil(b'\r\n\r\n')
            requests.append(request.split(b'\r\n')[0])
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
            await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
    server = await asyncio.start_server(app, sock=demo_socket)
    try:
        preview, r = await enter(client, await minted(client, pid))
        assert r.status_code == 200
        r = await preview.get('/file/a%2Fb%3Fc?value=%23keep')
        assert r.text == 'ok'
        assert requests == [b'GET /file/a%2Fb%3Fc?value=%23keep HTTP/1.1']
        await preview.aclose()
    finally:
        server.close()
        await server.wait_closed()


async def test_https_bootstrap_uses_external_scheme_behind_caddys_unix_proxy(viewer, demo):
    from dataclasses import replace

    from acc.server.preview import host_for, sign
    client, pid = viewer
    ctx = client.app.state.ctx
    ctx.settings = replace(ctx.settings, cookie_secure=True)
    host = host_for(ctx.settings, pid, PORT)
    token = sign(ctx, {'project':pid, 'port':PORT, 'host':host, 'machine': machine_for(ctx, pid),
                      'login':auth.digest(client.cookies.get('acc_session')), 'exp':auth.now().timestamp() + 60})
    # Caddy terminates TLS; accd has proxy_headers=False, so its ASGI scheme is HTTP.
    async with httpx.AsyncClient(transport=client._transport, base_url=f'http://{host}') as ingress:
        r = await ingress.post('/__acc_preview/login', json={'token':token}, headers={'Origin':f'https://{host}'})
        assert r.status_code == 200
        assert 'Secure' in r.headers['set-cookie']


async def test_old_node_offers_update(viewer):
    client, pid = viewer
    from acc.server.nodes import NodeLink
    async def discard(data):
        pass
    link = NodeLink('legacy', 'legacy-machine', discard, {}, [])
    client.app.state.ctx.broker.hub.attach(link)
    try:
        with write_db(client.app.state.db) as s:
            s.get(Project, pid).host = 'legacy-machine'
            s.commit()
        result = (await client.get(f'/api/projects/{pid}/preview')).json()
        assert result['available'] is False
        assert 'on this machine' in result['reason']
        assert 'update this machine' in result['reason'].lower()
    finally:
        client.app.state.ctx.broker.hub.detach(link)

@pytest.mark.parametrize('viewer', ['remote'], indirect=True)
async def test_remote_upload_disconnect_cancels_without_waiting_for_timeout(viewer, demo_socket):
    client, pid = viewer
    async def app(reader, writer):
        try:
            while await reader.read(65536):
                pass
        finally:
            writer.close()
    server = await asyncio.start_server(app, sock=demo_socket)
    preview = None
    try:
        preview, result = await enter(client, await minted(client, pid))
        assert result.status_code == 200
        async def interrupted():
            yield b'partial upload'
            raise RuntimeError('test browser disconnected')
        result = await asyncio.wait_for(preview.post('/upload', content=interrupted()), 1)
        assert result.status_code == 502
    finally:
        if preview:
            await preview.aclose()
        server.close()
        await server.wait_closed()


async def test_preview_keeps_compressed_body_opaque_to_main_and_node(viewer, demo_socket, monkeypatch):
    import gzip
    payload = b'demo compressed response' * 10000
    compressed = gzip.compress(payload)
    decoded_by_acc = []
    original = httpx.Response.aiter_bytes

    async def observed_decode(response, *args, **kwargs):
        if response.request.url.host == '127.0.0.1' and response.headers.get('content-encoding') == 'gzip':
            decoded_by_acc.append(True)
        async for chunk in original(response, *args, **kwargs):
            yield chunk
    monkeypatch.setattr(httpx.Response, 'aiter_bytes', observed_decode)

    async def reply(reader, writer):
        try:
            await reader.readuntil(b'\r\n\r\n')
            writer.write(f'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: {len(compressed)}\r\n\r\n'.encode() + compressed)
            await writer.drain()
        except asyncio.IncompleteReadError:
            pass  # the listening probe intentionally sends no HTTP request
        finally:
            writer.close()
    server = await asyncio.start_server(reply, sock=demo_socket)
    preview = None
    try:
        client, pid = viewer
        preview, login = await enter(client, await minted(client, pid))
        assert login.status_code == 200
        response = await preview.get('/')
        assert response.content == payload
        assert not decoded_by_acc
    finally:
        if preview:
            await preview.aclose()
        server.close()
        await server.wait_closed()


async def test_preview_listener_can_restart_after_closing_an_active_connection(demo_socket):
    """Repeated suite runs must not strand the preview range in TCP TIME_WAIT."""
    async def reply(reader, writer):
        await reader.readexactly(1)
        writer.write(b'ok')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    port = demo_socket.getsockname()[1]
    server = await asyncio.start_server(reply, sock=demo_socket)
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    writer.write(b'x')
    await writer.drain()
    assert await reader.read() == b'ok'
    writer.close()
    await writer.wait_closed()
    server.close()
    await server.wait_closed()
    restarted = await asyncio.start_server(reply, '127.0.0.1', port)
    restarted.close()
    await restarted.wait_closed()
