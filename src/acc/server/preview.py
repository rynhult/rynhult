"""Isolated preview gateway. Never dispatch a preview hostname to Rynhult's application."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from ..preview_stream import MAX_DATA
from . import preview_remote
from .auth import now
from .models import AuthSession, PortReservation, Project

TTL = 300
COOKIE = '__Host-acc_preview'
# Never forward credentials, hop-by-hop headers, proxy routing hints, or upstream security policies.
DROP = {'cookie', 'authorization', 'proxy-authorization', 'host', 'connection', 'keep-alive',
        'transfer-encoding', 'te', 'trailer', 'upgrade', 'set-cookie', 'content-length',
        'content-encoding', 'content-security-policy', 'content-security-policy-report-only',
        'x-frame-options', 'clear-site-data', 'service-worker-allowed', 'www-authenticate',
        'strict-transport-security', 'report-to', 'nel', 'alt-svc', 'x-acc-csrf'}
BOOTSTRAP = """<!doctype html><meta charset="utf-8"><title>Opening preview</title>
<p id="status">Opening preview…</p><script>
const landing = new URL(location.href).searchParams.get('frame') === '1' ? '/__acc_preview/frame' : '/';
const token = location.hash.slice(1); history.replaceState(null, '', location.pathname);
fetch('/__acc_preview/login', {method:'POST', headers:{'Content-Type':'application/json'},
body:JSON.stringify({token})}).then(r => {if (!r.ok) throw Error(); location.replace(landing);})
.catch(() => document.getElementById('status').textContent = 'Preview access expired. Reload the Preview tab in Rynhult.');
</script>"""


PICKER_SCRIPT = Path(__file__).with_name('preview_picker.js').read_text()


def picker_frame(origins):
    # Configuration only: never insert any upstream HTML or page description here.
    config = json.dumps(origins).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return ('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project preview</title><style>html,body{margin:0;width:100%;height:100%;overflow:hidden}
iframe{width:100%;height:100%;border:0}#outline{position:fixed;pointer-events:none;outline:2px solid Highlight;z-index:2147483647}</style>
</head><body><iframe id="app" title="Preview app" src="/" sandbox="allow-scripts allow-forms allow-same-origin" referrerpolicy="no-referrer"></iframe>
<div id="outline" hidden aria-hidden="true"></div><script type="application/json" id="acc-origins">''' + config +
            '</script><script src="/__acc_preview/picker.js"></script></body></html>')


def domain(settings):
    return (settings.preview_domain or settings.wildcard_zone or urlsplit(settings.allowed_origins[0]).hostname).lower()


def host_for(settings, pid, port):
    return f'preview-{pid}-{port}.{domain(settings)}'


def cookie_name(settings):
    return COOKIE if settings.cookie_secure else 'acc_preview'


def sign(ctx, claims):
    data = base64.urlsafe_b64encode(json.dumps(claims, separators=(',', ':')).encode()).decode().rstrip('=')
    signature = hmac.new(ctx.token.encode(), b'acc-preview-v1:' + data.encode(), hashlib.sha256).hexdigest()
    return f'{data}.{signature}'


def validate(ctx, token, host):
    from .admin_features import require
    require(ctx.settings, 'live_preview')
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError()
        data, signature = token.split('.')
        expected = hmac.new(ctx.token.encode(), b'acc-preview-v1:' + data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))
        if claims['exp'] <= now().timestamp() or claims['host'] != host:
            raise ValueError()
        pid, port = claims['project'], claims['port']
        if host != host_for(ctx.settings, pid, port) or type(port) is not int or not 8000 <= port <= 8199:
            raise ValueError()
        with ctx.db() as s:
            login = s.get(AuthSession, claims['login'])
            project = s.get(Project, pid)
            reservation = s.get(PortReservation, port)
            if (login is None or login.expires_at <= now() or project is None or project.archived
                    or (project.host or 'local') != claims.get('machine', 'local') or reservation is None or reservation.project_id != pid
                    or (login.user.role != 'owner' and project.owner != login.user.username)):
                raise ValueError()
        return claims
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise HTTPException(401, 'Preview access expired. Reload the Preview tab in Rynhult.') from None


async def listening(port):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 0.25)
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, TimeoutError):
        return False


def headers(items, *, response=False):
    connection = next((v for k, v in items if k.lower() == 'connection'), '')
    excluded = (DROP - {'content-encoding'} if response else DROP) | {v.strip().lower() for v in connection.split(',')}
    return {k: v for k, v in items if k.lower() not in excluded
            and not k.lower().startswith(('x-forwarded-', 'forwarded', 'access-control-', 'sec-websocket-'))}


def target_url(request, port, scheme):
    raw = request.scope.get('raw_path', quote(request.scope['path'], safe='/').encode('ascii'))
    query = request.scope.get('query_string', b'')
    return httpx.URL(scheme=scheme, host='127.0.0.1', port=port,
                     raw_path=raw + (b'?' + query if query else b''))


SIGN_IN_NAVIGATIONS = {'/api/auth/oidc/start', '/api/auth/oidc/callback'}

class PreviewBoundary:
    """Outer ASGI boundary: applies to HTTP and WebSockets, before dashboard middleware/routes."""
    def __init__(self, app, ctx):
        self.app, self.ctx = app, ctx

    def policy(self):
        origins = ' '.join(self.ctx.settings.allowed_origins)
        return {'Content-Security-Policy': f"frame-ancestors {origins} 'self'; sandbox allow-scripts allow-forms allow-same-origin; worker-src 'none'",
                'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'}

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        request = Request(scope) if scope['type'] == 'http' else WebSocket(scope, receive, send)
        host = request.url.hostname or ''
        # Reserve the prefix on every host; malformed preview hosts must fail closed too.
        is_preview = host.startswith('preview-')
        if not is_preview:
            origin = request.headers.get('origin')
            foreign = origin and not self.ctx.origin_ok(origin)
            cross_fetch = request.headers.get('sec-fetch-site') in ('same-site', 'cross-site') and (
                request.headers.get('sec-fetch-mode') != 'navigate' or scope['path'].startswith('/api/'))
            # Company sign-in arrives by top-level navigation from elsewhere: the provider's redirect back, and a
            # sign-in link opened from mail. Those two routes check the rest themselves (organizations.start/callback).
            if (cross_fetch and self.ctx.settings.organizations_enabled and scope['path'] in SIGN_IN_NAVIGATIONS
                    and scope['type'] == 'http' and scope['method'] == 'GET'
                    and request.headers.get('sec-fetch-mode') == 'navigate'):
                cross_fetch = False
            if foreign or cross_fetch:
                if scope['type'] == 'websocket':
                    return await send({'type': 'websocket.close', 'code': 4403})
                return await JSONResponse({'detail': 'bad origin'}, 403)(scope, receive, send)
            return await self.app(scope, receive, send)
        if not re.fullmatch(r'preview-[1-9][0-9]*-8[01][0-9]{2}\.' + re.escape(domain(self.ctx.settings)), host):
            if scope['type'] == 'websocket':
                return await send({'type': 'websocket.close', 'code': 4401})
            return await Response(status_code=404)(scope, receive, send)
        if scope['type'] == 'websocket':
            return await self.websocket(request, host)
        try:
            response = await self.http(Request(scope, receive), host)
        except HTTPException as e:
            response = JSONResponse({'detail': e.detail}, e.status_code)
        response.headers.update(self.policy())
        await response(scope, receive, send)

    async def http(self, request, host):
        ctx = self.ctx
        origin = request.headers.get('origin')
        own_origin = f"{'https' if ctx.settings.cookie_secure else 'http'}://{request.url.netloc}"
        if origin is not None and origin != own_origin:
            raise HTTPException(403, 'Open this preview from the Preview tab in Rynhult.')
        if request.url.path == '/__acc_preview/start' and request.method == 'GET':
            return HTMLResponse(BOOTSTRAP)
        if request.url.path == '/__acc_preview/login' and request.method == 'POST':
            # The exchange is same-origin only; its token is never a Rynhult credential.
            if origin != own_origin:
                raise HTTPException(403, 'Open this preview from the Preview tab in Rynhult.')
            try:
                token = (await request.json())['token']
                validate(ctx, token, host)
            except (ValueError, KeyError, TypeError):
                raise HTTPException(401, 'Preview access expired. Reload the Preview tab in Rynhult.') from None
            response = JSONResponse({'ok': True})
            response.set_cookie(cookie_name(ctx.settings), token, max_age=TTL, httponly=True,
                                secure=ctx.settings.cookie_secure, samesite='none' if ctx.settings.cookie_secure else 'lax')
            return response
        claims = validate(ctx, request.cookies.get(cookie_name(ctx.settings), ''), host)
        if request.url.path in ('/__acc_preview/frame', '/__acc_preview/picker.js'):
            if request.method != 'GET':
                raise HTTPException(405, 'This preview page can only be opened, not sent to.')
            if request.url.path.endswith('.js'):
                return Response(PICKER_SCRIPT, media_type='application/javascript')
            return HTMLResponse(picker_frame(ctx.settings.allowed_origins))
        # No arbitrary URL input, no DNS resolution, no redirects followed; path stays on loopback.
        if preview_remote.machine_for(ctx, claims['project']) != 'local':
            return await self.remote_http(request, host, claims)
        target = target_url(request, claims['port'], 'http')
        client = httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=30)
        try:
            upstream = await client.send(client.build_request(request.method, target,
                headers=headers(list(request.headers.items())), content=request.stream()), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(502, 'App is unavailable. Check that its dev server is running.') from None

        async def body():
            try:
                async for chunk in upstream.aiter_raw():
                    validate(ctx, request.cookies.get(cookie_name(ctx.settings), ''), host)
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()
        return StreamingResponse(body(), status_code=upstream.status_code, headers=headers(list(upstream.headers.items()), response=True))

    async def remote_http(self, request, host, claims):
        context = preview_remote.opened(self.ctx, claims['project'], claims['port'], 'http',
            **preview_remote.metadata(request, headers(list(request.headers.items()))))
        upload = None
        try:
            stream = await context.__aenter__()
            async def send_body():
                try:
                    async for chunk in request.stream():
                        validate(self.ctx, request.cookies.get(cookie_name(self.ctx.settings), ''), host)
                        for offset in range(0, len(chunk), MAX_DATA):
                            await stream.send(b'D', chunk[offset:offset + MAX_DATA])
                    await stream.send(b'F')
                except BaseException:
                    await stream.close()
                    raise
            upload = stream.tasks.start(send_body(), "preview-upload", report=False)
            kind, value = await stream.recv()
            if kind != b'H':
                raise ConnectionError('missing HTTP response')
            meta = preview_remote.response_metadata(value)
        except BaseException as error:
            if upload:
                upload.cancel()
                await asyncio.gather(upload, return_exceptions=True)
            await context.__aexit__(None, None, None)
            if isinstance(error, (ConnectionError, TimeoutError, ValueError)):
                raise HTTPException(502, 'App is unavailable. Check that its dev server is running.') from None
            raise

        async def body():
            try:
                while True:
                    kind, chunk = await stream.recv()
                    validate(self.ctx, request.cookies.get(cookie_name(self.ctx.settings), ''), host)
                    if kind == b'F':
                        return
                    if kind != b'D':
                        raise ConnectionError('unexpected HTTP frame')
                    yield chunk
            finally:
                upload.cancel()
                await asyncio.gather(upload, return_exceptions=True)
                await context.__aexit__(None, None, None)
        return StreamingResponse(body(), status_code=meta['status'], headers=headers(meta['headers'], response=True))

    async def websocket(self, ws, host):
        ctx = self.ctx
        token = ws.cookies.get(cookie_name(ctx.settings), '')
        try:
            claims = validate(ctx, token, host)
            origin = ws.headers.get('origin')
            if origin != f"{'https' if ctx.settings.cookie_secure else 'http'}://{ws.url.netloc}":
                raise HTTPException(403)
        except HTTPException:
            return await ws.close(4401)
        target = str(target_url(ws, claims['port'], 'ws'))
        try:
            connection = (preview_remote.websocket(ctx, claims['project'], claims['port'], ws, headers(list(ws.headers.items())))
                          if preview_remote.machine_for(ctx, claims['project']) != 'local' else
                          connect(target, additional_headers=headers(list(ws.headers.items())),
                                  subprotocols=ws.scope.get('subprotocols') or None, proxy=None, max_size=4 << 20))
            async with connection as upstream:
                await ws.accept(subprotocol=upstream.subprotocol)

                async def inbound():
                    while True:
                        message = await ws.receive()
                        if message['type'] == 'websocket.disconnect':
                            return
                        validate(ctx, token, host)
                        await upstream.send(message.get('text') if message.get('text') is not None else message['bytes'])

                async def outbound():
                    async for message in upstream:
                        validate(ctx, token, host)
                        await (ws.send_text(message) if isinstance(message, str) else ws.send_bytes(message))

                async def recheck():
                    while True:
                        await asyncio.sleep(1)
                        validate(ctx, token, host)
                tasks = [asyncio.create_task(fn()) for fn in (inbound, outbound, recheck)]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (HTTPException, OSError, TimeoutError, ValueError, WebSocketException, WebSocketDisconnect):
            pass
        finally:
            try:
                await ws.close(4401)
            except RuntimeError:
                pass
