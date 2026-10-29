"""Bounded, credit-controlled preview messages; independent of browser authorization."""
from __future__ import annotations

import asyncio
import struct

HEADER = struct.Struct('>cI')
KINDS = frozenset([b'R', b'O', b'H', b'D', b'T', b'B', b'F', b'A', b'C'])
CONTENT = frozenset([b'H', b'D', b'T', b'B', b'F'])
MAX_DATA = 65536
MAX_META = 16384
MAX_STREAMS = 16
TIMEOUT = 30


def pack(kind, sid, payload=b''):
    return HEADER.pack(kind, sid) + payload


class Stream:
    def __init__(self, mux, sid):
        self.mux, self.id = mux, sid
        self.queue = asyncio.Queue(maxsize=1)
        self.credit = asyncio.Event()
        self.credit.set()
        self.closed = False

    async def send(self, kind, payload=b''):
        if kind not in CONTENT or len(payload) > (MAX_META if kind == b'H' else MAX_DATA):
            raise ValueError('invalid preview content')
        await asyncio.wait_for(self.credit.wait(), TIMEOUT)
        if self.closed:
            raise ConnectionError('preview stream closed')
        self.credit.clear()
        await self.mux.send(pack(kind, self.id, payload))

    async def recv(self, *, idle_websocket=False):
        item = await asyncio.wait_for(self.queue.get(), None if idle_websocket else TIMEOUT)
        if item is None or self.closed:
            raise ConnectionError('preview stream closed')
        await self.mux.send(pack(b'A', self.id))
        return item

    def abort(self):
        self.closed = True
        self.credit.set()
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)
        self.mux.streams.pop(self.id, None)

    async def close(self):
        if not self.closed:
            self.abort()
            await self.mux.send(pack(b'C', self.id))


class Mux:
    def __init__(self, send):
        self.send = send
        self.streams = {}
        self.last_id = 0

    def create(self, sid):
        if (type(sid) is not int or sid <= self.last_id or sid > 0xffffffff or sid % 2 != 1
                or len(self.streams) >= MAX_STREAMS):
            raise ConnectionError('preview stream limit or duplicate id')
        self.last_id = sid
        stream = self.streams[sid] = Stream(self, sid)
        return stream

    def receive(self, data):
        if len(data) < HEADER.size:
            raise ValueError('short preview frame')
        kind, sid = HEADER.unpack_from(data)
        payload = data[HEADER.size:]
        limit = MAX_DATA if kind in (b'D', b'T', b'B') else MAX_META
        if kind not in KINDS or len(payload) > limit or (kind in (b'A', b'C', b'F') and payload):
            raise ValueError('invalid preview frame')
        stream = self.streams.get(sid)
        if stream is None:
            return
        if kind == b'C':
            stream.abort()
        elif kind == b'A':
            stream.credit.set()
        elif kind in CONTENT:
            if stream.queue.full():
                stream.abort()
            else:
                stream.queue.put_nowait((kind, payload))
        else:
            raise ValueError('unexpected preview frame')

    def close(self):
        for stream in list(self.streams.values()):
            stream.abort()
