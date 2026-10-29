"""Gateway adapter for one live machine link. No retries or local fallback."""
import asyncio
import json
import re
from contextlib import asynccontextmanager

from sqlalchemy import select

from ..preview_stream import MAX_META, pack
from .models import PortReservation, Project

LEGACY = 'Preview is available for projects on this machine. Update this machine to enable preview.'


def link_for(ctx, machine):
    hub = ctx.broker.hub
    if hub is None:
        raise ConnectionError(LEGACY)
    link = hub.get(machine)
    if 'preview-v1' not in link.info.get('capabilities', []):
        raise ConnectionError(LEGACY)
    return link


def machine_for(ctx, pid):
    with ctx.db() as session:
        return session.get(Project, pid).host or 'local'


@asynccontextmanager
async def opened(ctx, pid, port, mode, **metadata):
    machine = machine_for(ctx, pid)
    link = link_for(ctx, machine)
    sid = link.preview_next
    link.preview_next += 2
    stream = link.preview.create(sid)
    stream.tasks = link.tasks  # request children must retain this exact connection across reconnects

    async def refresh():
        with ctx.db() as session:
            project = session.get(Project, pid)
            ports = list(session.scalars(select(PortReservation.port).where(PortReservation.project_id == pid))) \
                if project and not project.archived and project.host == machine else []
        await link.preview_sender.send(pack(b'R', 0, json.dumps({'project':pid, 'ports':ports}).encode()))
        if port not in ports:
            raise ConnectionError('preview reservation revoked')

    async def renew():
        try:
            while True:
                await asyncio.sleep(1)
                await refresh()
        finally:
            await stream.close()

    renewal = None
    try:
        await refresh()
        value = json.dumps({'project':pid, 'host':'127.0.0.1', 'port':port, 'mode':mode, **metadata}).encode()
        if len(value) > MAX_META:
            raise ConnectionError('preview request metadata too large')
        await link.preview_sender.send(pack(b'O', sid, value))
        renewal = link.tasks.start(renew(), "preview-renewal", report=False)
        yield stream
    finally:
        if renewal:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
        await stream.close()


async def listening(ctx, pid, port):
    try:
        async with opened(ctx, pid, port, 'probe') as stream:
            kind, _ = await stream.recv()
            return kind == b'H'
    except (ConnectionError, TimeoutError):
        return False


def metadata(request, clean_headers):
    raw = request.scope.get('raw_path', request.url.path.encode('ascii'))
    query = request.scope.get('query_string', b'')
    return {'method': request.scope.get('method', 'GET'),
            'path': (raw + (b'?' + query if query else b'')).decode('ascii'),
            'headers': clean_headers, 'protocols': request.scope.get('subprotocols', [])}


def response_metadata(payload, *, protocols=None):
    """A node's response remains untrusted, including HTTP header syntax."""
    if len(payload) > MAX_META:
        raise ValueError('oversized preview response metadata')
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError('invalid preview response metadata')
    if protocols is not None:
        if set(value) != {'protocol'} or (value['protocol'] is not None and (
                not isinstance(value['protocol'], str) or value['protocol'] not in protocols)):
            raise ValueError('invalid preview WebSocket protocol')
        return value
    if (set(value) != {'status', 'headers'} or type(value['status']) is not int
            or not 200 <= value['status'] <= 599 or not isinstance(value['headers'], list)):
        raise ValueError('invalid preview HTTP response')
    for item in value['headers']:
        if (not isinstance(item, list) or len(item) != 2 or any(not isinstance(v, str) for v in item)
                or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", item[0])
                or any(ord(c) < 32 and c != '\t' or ord(c) == 127 for c in item[1])):
            raise ValueError('invalid preview HTTP header')
        item[1].encode('latin1')
    return value


class RemoteSocket:
    def __init__(self, stream, protocol):
        self.stream, self.subprotocol = stream, protocol

    async def send(self, message):
        await self.stream.send(b'T' if isinstance(message, str) else b'B',
                               message.encode() if isinstance(message, str) else message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        kind, value = await self.stream.recv(idle_websocket=True)
        if kind not in (b'T', b'B'):
            raise ConnectionError('unexpected WebSocket frame')
        return value.decode() if kind == b'T' else value


@asynccontextmanager
async def websocket(ctx, pid, port, request, clean_headers):
    async with opened(ctx, pid, port, 'ws', **metadata(request, clean_headers)) as stream:
        kind, value = await stream.recv()
        if kind != b'H':
            raise ConnectionError('missing WebSocket response')
        yield RemoteSocket(stream, response_metadata(value, protocols=request.scope.get('subprotocols', []))['protocol'])
