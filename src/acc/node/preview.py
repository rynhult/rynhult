"""Node preview endpoint: only fresh project reservations on literal loopback."""
import asyncio
import json

import httpx
from websockets.asyncio.client import connect

from ..preview_stream import HEADER, MAX_DATA, MAX_META, MAX_STREAMS, Mux


class PreviewRelay:
    def __init__(self, send):
        self.send = send
        self.mux = Mux(send)
        self.reservations = {}
        self.tasks = {}
        self.watch = asyncio.create_task(self._watch())

    def allowed(self, project, port):
        ports, deadline = self.reservations.get(project, ([], 0))
        return port in ports and asyncio.get_running_loop().time() < deadline

    def receive(self, data):
        if len(data) < HEADER.size:
            raise ValueError('short preview frame')
        kind, sid = HEADER.unpack_from(data)
        payload = data[HEADER.size:]
        if kind == b'R':
            if sid != 0 or len(payload) > MAX_META:
                raise ValueError('invalid reservation frame')
            value = json.loads(payload)
            if (set(value) != {'project', 'ports'} or type(value['project']) is not int
                    or not isinstance(value['ports'], list) or len(value['ports']) > 200
                    or any(type(p) is not int or not 8000 <= p <= 8199 for p in value['ports'])):
                raise ValueError('invalid reservation snapshot')
            # Only retain snapshots while needed; idle snapshots have a short fixed lifetime.
            if value['project'] not in self.reservations and len(self.reservations) >= 32:
                raise ValueError('reservation cache full')
            self.reservations[value['project']] = (value['ports'], asyncio.get_running_loop().time() + 3)
            self._revoke()
        elif kind == b'O':
            if len(payload) > MAX_META:
                raise ValueError('oversized preview open')
            if len(self.tasks) >= MAX_STREAMS:
                raise ConnectionError('too many preview streams opening or closing')
            stream = self.mux.create(sid)
            task = asyncio.create_task(self._run(stream, payload))
            self.tasks[sid] = (task, None, None)
            def finished(_task):
                self.tasks.pop(sid, None)
                stream.abort()
            task.add_done_callback(finished)
        else:
            self.mux.receive(data)
            if kind == b'C' and sid in self.tasks:
                self.tasks[sid][0].cancel()

    def _revoke(self):
        for task, project, port in list(self.tasks.values()):
            if project is not None and not task.cancelling() and not self.allowed(project, port):
                task.cancel()

    async def _watch(self):
        while True:
            await asyncio.sleep(.5)
            self._revoke()
            current = asyncio.get_running_loop().time()
            self.reservations = {p: v for p, v in self.reservations.items() if v[1] > current}

    async def _run(self, stream, payload):
        try:
            value = json.loads(payload)
            project, port = value['project'], value['port']
            mode = value['mode']
            fields = {'project', 'host', 'port', 'mode'}
            if mode in ('http', 'ws'):
                fields |= {'path', 'headers', 'method', 'protocols'}
            if (set(value) != fields or type(project) is not int or type(port) is not int
                    or value['host'] != '127.0.0.1' or not 8000 <= port <= 8199
                    or not self.allowed(project, port) or mode not in ('probe', 'http', 'ws')):
                raise ValueError('preview target refused')
            self.tasks[stream.id] = (asyncio.current_task(), project, port)
            if mode == 'probe':
                _, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), .25)
                writer.close()
                await writer.wait_closed()
                await stream.send(b'H', b'{}')
                # Keep metadata alive until consumed.
                await asyncio.wait_for(stream.credit.wait(), 30)
                return
            path = value['path']
            if not isinstance(path, str) or not path.startswith('/') or '\r' in path or '\n' in path:
                raise ValueError('bad preview path')
            url = httpx.URL(scheme='http' if mode == 'http' else 'ws', host='127.0.0.1', port=port,
                            raw_path=path.encode('ascii'))
            if mode == 'http':
                async def body():
                    while True:
                        kind, chunk = await stream.recv()
                        if kind == b'F':
                            return
                        if kind != b'D':
                            raise ValueError('expected HTTP body')
                        yield chunk
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as client:
                    async with client.stream(value['method'], url, headers=value['headers'], content=body()) as response:
                        await stream.send(b'H', json.dumps({'status': response.status_code,
                            'headers': list(response.headers.multi_items())}).encode())
                        async for chunk in response.aiter_raw():
                            for offset in range(0, len(chunk), MAX_DATA):
                                await stream.send(b'D', chunk[offset:offset + MAX_DATA])
                        await stream.send(b'F')
                        await asyncio.wait_for(stream.credit.wait(), 30)
            else:
                async with connect(str(url), additional_headers=value['headers'], subprotocols=value['protocols'] or None,
                                   proxy=None, max_size=MAX_DATA, open_timeout=30) as ws:
                    await stream.send(b'H', json.dumps({'protocol': ws.subprotocol}).encode())
                    async def incoming():
                        while True:
                            kind, chunk = await stream.recv(idle_websocket=True)
                            if kind not in (b'T', b'B'):
                                raise ValueError('expected WebSocket message')
                            await ws.send(chunk.decode() if kind == b'T' else chunk)
                    async def outgoing():
                        async for chunk in ws:
                            await stream.send(b'T' if isinstance(chunk, str) else b'B',
                                              chunk.encode() if isinstance(chunk, str) else chunk)
                    tasks = [asyncio.create_task(fn()) for fn in (incoming, outgoing)]
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            try:
                await stream.close()
            except Exception:
                pass  # the owning machine link may already be disconnected

    def close(self):
        self.watch.cancel()
        for task, _, _ in self.tasks.values():
            task.cancel()
        self.reservations.clear()
        self.mux.close()

    async def aclose(self):
        tasks = [entry[0] for entry in self.tasks.values()] + [self.watch]
        self.close()
        await asyncio.gather(*tasks, return_exceptions=True)
