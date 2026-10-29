"""Real paired endpoints: loss of credit, cancellation and node policy must fail closed."""
import asyncio
import json

import pytest


async def test_credit_blocks_until_consumed_and_cancel_wakes_sender():
    from acc.preview_stream import Mux
    left = right = None
    async def to_right(data):
        right.receive(data)
    async def to_left(data):
        left.receive(data)
    left, right = Mux(to_right), Mux(to_left)
    a = left.create(1)
    b = right.create(1)
    await a.send(b'D', b'first')
    pending = asyncio.create_task(a.send(b'D', b'second'))
    await asyncio.sleep(.01)
    assert not pending.done()
    assert await b.recv() == (b'D', b'first')
    await asyncio.wait_for(pending, .2)
    pending = asyncio.create_task(a.send(b'D', b'third'))
    await asyncio.sleep(.01)
    await b.close()
    with pytest.raises(ConnectionError):
        await pending
    left.close(); right.close()


async def test_limits_and_unsolicited_frames_cancel_only_stream():
    from acc.preview_stream import Mux, pack
    sent = []
    async def send(data):
        sent.append(data)
    mux = Mux(send)
    stream = mux.create(1)
    mux.receive(pack(b'D', 1, b'a'))
    mux.receive(pack(b'D', 1, b'b'))
    with pytest.raises(ConnectionError):
        await stream.recv()
    other = mux.create(3)
    with pytest.raises(ValueError):
        await other.send(b'D', b'x' * 65537)
    for i in range(5, 35, 2):
        mux.create(i)
    with pytest.raises(ConnectionError):
        mux.create(99)
    mux.close()


@pytest.mark.parametrize('host,port,granted', [('example.com',8000,True), ('localhost',8000,True),
    ('127.0.0.1',7999,True), ('127.0.0.1',8000,False)])
async def test_node_refuses_other_hosts_and_unreserved_ports(host, port, granted, monkeypatch):
    from acc.node.preview import PreviewRelay
    from acc.preview_stream import pack
    sent = []
    async def send(data):
        sent.append(data)
    attempts = []
    async def forbidden_connect(*args, **kwargs):
        attempts.append(args)
        raise OSError('test: connection must never be attempted')
    monkeypatch.setattr(asyncio, 'open_connection', forbidden_connect)
    relay = PreviewRelay(send)
    if granted:
        relay.receive(pack(b'R', 0, json.dumps({'project': 1, 'ports': [8000]}).encode()))
    relay.receive(pack(b'O', 1, json.dumps({'project':1,'host':host,'port':port,'mode':'probe'}).encode()))
    await asyncio.sleep(.02)
    assert sent and sent[-1][:1] == b'C'
    assert attempts == []
    relay.close()

async def test_http_response_stream_through_node(preview_socket):
    from acc.node.preview import PreviewRelay
    from acc.preview_stream import Mux, pack
    async def app(reader, writer):
        await reader.readuntil(b'\r\n\r\n')
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello')
        await writer.drain()
        writer.close()
    port = preview_socket.getsockname()[1]
    server = await asyncio.start_server(app, sock=preview_socket)
    main = node = None
    async def to_node(data):
        node.receive(data)
    async def to_main(data):
        main.receive(data)
    main, node = Mux(to_node), PreviewRelay(to_main)
    try:
        node.receive(pack(b'R', 0, json.dumps({'project':1,'ports':[port]}).encode()))
        stream = main.create(1)
        node.receive(pack(b'O', 1, json.dumps({'project':1,'host':'127.0.0.1','port':port,'mode':'http',
            'method':'GET','path':'/','headers':{},'protocols':[]}).encode()))
        await stream.send(b'F')
        kind, meta = await stream.recv()
        assert kind == b'H' and json.loads(meta)['status'] == 200
        assert await stream.recv() == (b'D', b'hello')
        assert await stream.recv() == (b'F', b'')
    finally:
        node.close(); main.close()
        server.close(); await server.wait_closed()

@pytest.mark.parametrize('revoke', ['release', 'expiry'])
async def test_websocket_text_binary_and_reservation_revocation(revoke, preview_socket):
    from websockets.asyncio.server import serve

    from acc.node.preview import PreviewRelay
    from acc.preview_stream import Mux, pack
    async def echo(ws):
        async for message in ws:
            await ws.send(message)
    port = preview_socket.getsockname()[1]
    server = await serve(echo, sock=preview_socket)
    main = node = None
    async def to_node(data):
        node.receive(data)
    async def to_main(data):
        main.receive(data)
    main, node = Mux(to_node), PreviewRelay(to_main)
    try:
        node.receive(pack(b'R', 0, json.dumps({'project':7,'ports':[port]}).encode()))
        stream = main.create(1)
        node.receive(pack(b'O', 1, json.dumps({'project':7,'host':'127.0.0.1','port':port,'mode':'ws',
            'method':'GET','path':'/echo','headers':{},'protocols':[]}).encode()))
        assert (await stream.recv())[0] == b'H'
        await stream.send(b'T', b'hello')
        assert await stream.recv() == (b'T', b'hello')
        await stream.send(b'B', b'\x00\xff')
        assert await stream.recv() == (b'B', b'\x00\xff')
        if revoke == 'release':
            node.receive(pack(b'R', 0, json.dumps({'project':7,'ports':[]}).encode()))
        else:
            node.reservations[7] = ([port], asyncio.get_running_loop().time() - 1)
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(stream.recv(), 1)
    finally:
        node.close(); main.close()
        server.close(); await server.wait_closed()


async def test_priority_sender_control_overtakes_queued_preview():
    from acc.preview_sender import PrioritySender
    from acc.preview_stream import pack
    gate = asyncio.Event()
    started = asyncio.Event()
    sent = []
    async def wire(data):
        started.set()
        await gate.wait()
        sent.append(data)
    sender = PrioritySender(wire)
    first = asyncio.create_task(sender.send(pack(b'D', 1, b'first')))
    await started.wait()
    preview = asyncio.create_task(sender.send(pack(b'D', 3, b'second')))
    terminal = asyncio.create_task(sender.send(b'd\x00\x00\x00\x01terminal'))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, preview, terminal)
    assert [item[-6:] for item in sent] == [b'\x01first', b'rminal', b'second']
    sender.close()


async def test_expired_snapshot_refuses_connection():
    from acc.node.preview import PreviewRelay
    from acc.preview_stream import pack
    sent = []
    async def send(data):
        sent.append(data)
    node = PreviewRelay(send)
    node.reservations[1] = ([8000], asyncio.get_running_loop().time() - 1)
    node.receive(pack(b'O', 1, b'{"project":1,"host":"127.0.0.1","port":8000,"mode":"probe"}'))
    await asyncio.sleep(.01)
    assert sent[-1] == b'C\x00\x00\x00\x01'
    node.close()

async def test_cancel_before_open_task_runs_releases_slot():
    from acc.node.preview import PreviewRelay
    from acc.preview_stream import pack
    async def send(data):
        pass
    node = PreviewRelay(send)
    for sid in range(1, 65, 2):
        node.receive(pack(b'O', sid, b'{}'))
        node.receive(pack(b'C', sid))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert not node.tasks
    assert not node.mux.streams
    node.close()


@pytest.mark.parametrize('data', [b'D', b'Z\0\0\0\1', b'D\0\0\0\1' + b'x' * 65537,
    b'A\0\0\0\1x', b'F\0\0\0\1x'])
async def test_invalid_frames_fail_closed(data):
    from acc.preview_stream import Mux
    async def send(data):
        pass
    mux = Mux(send)
    with pytest.raises(ValueError):
        mux.receive(data)
    mux.close()

async def test_preview_budget_leaves_room_for_control(monkeypatch):
    from acc.preview_sender import PrioritySender
    from acc.preview_stream import pack
    gate = asyncio.Event()
    async def wire(data):
        await gate.wait()
    sender = PrioritySender(wire)
    jobs = [asyncio.create_task(sender.send(pack(b'D', sid, b'x' * 65536))) for sid in range(1, 32)]
    jobs.append(asyncio.create_task(sender.send(pack(b'D', 33, b'x' * 57184))))
    jobs.append(asyncio.create_task(sender.send(pack(b'D', 35, b'overflow'))))
    await asyncio.sleep(.01)
    control = asyncio.create_task(sender.send(b'ping'))
    await asyncio.sleep(.01)
    assert not control.done()  # queued despite exhausted preview allocation
    gate.set()
    results = await asyncio.gather(*jobs, return_exceptions=True)
    assert any(isinstance(result, ConnectionError) for result in results)
    await control
    sender.close()

async def test_small_http_chunks_arrive_before_upstream_closes(preview_socket):
    from acc.node.preview import PreviewRelay
    from acc.preview_stream import Mux, pack
    release = asyncio.Event()
    async def app(reader, writer):
        try:
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\ndata: ready\n\n')
            await writer.drain()
            await release.wait()
        finally:
            writer.close()
    port = preview_socket.getsockname()[1]
    server = await asyncio.start_server(app, sock=preview_socket)
    main = node = None
    async def to_node(data):
        node.receive(data)
    async def to_main(data):
        main.receive(data)
    main, node = Mux(to_node), PreviewRelay(to_main)
    try:
        node.receive(pack(b'R', 0, json.dumps({'project':1,'ports':[port]}).encode()))
        stream = main.create(1)
        node.receive(pack(b'O', 1, json.dumps({'project':1,'host':'127.0.0.1','port':port,'mode':'http',
            'method':'GET','path':'/','headers':{},'protocols':[]}).encode()))
        await stream.send(b'F')
        assert (await stream.recv())[0] == b'H'
        assert await asyncio.wait_for(stream.recv(), .5) == (b'D', b'data: ready\n\n')
    finally:
        release.set()
        node.close(); main.close()
        server.close(); await server.wait_closed()

async def test_cancelled_queued_open_is_never_transmitted():
    from acc.preview_sender import PrioritySender
    from acc.preview_stream import pack
    gate, started = asyncio.Event(), asyncio.Event()
    sent = []
    async def wire(data):
        started.set()
        await gate.wait()
        sent.append(data)
    sender = PrioritySender(wire)
    first = asyncio.create_task(sender.send(b'control'))
    await started.wait()
    opened = asyncio.create_task(sender.send(pack(b'O', 1, b'{}')))
    await asyncio.sleep(0)
    opened.cancel()
    await asyncio.gather(opened, return_exceptions=True)
    gate.set()
    await first
    await sender.send(b'barrier')
    assert sent == [b'control', b'barrier']
    sender.close()

async def test_stream_ids_cannot_be_replayed_or_use_control_namespace():
    from acc.preview_stream import Mux
    async def send(data):
        pass
    mux = Mux(send)
    for sid in (0, 2, -1):
        with pytest.raises(ConnectionError):
            mux.create(sid)
    stream = mux.create(1)
    await stream.close()
    with pytest.raises(ConnectionError):
        mux.create(1)
    mux.close()


async def test_stalled_credit_and_receive_have_deadlines(monkeypatch):
    import acc.preview_stream as protocol
    monkeypatch.setattr(protocol, 'TIMEOUT', .01)
    async def send(data):
        pass
    mux = protocol.Mux(send)
    stream = mux.create(1)
    await stream.send(b'D', b'first')
    with pytest.raises(TimeoutError):
        await stream.send(b'D', b'no-credit')
    with pytest.raises(TimeoutError):
        await stream.recv()
    mux.close()

async def test_node_does_not_borrow_another_projects_reservation(monkeypatch):
    from acc.node.preview import PreviewRelay
    from acc.preview_stream import pack
    attempts, sent = [], []
    async def forbidden_connect(*args, **kwargs):
        attempts.append(args)
        raise OSError('must not connect')
    async def send(data):
        sent.append(data)
    monkeypatch.setattr(asyncio, 'open_connection', forbidden_connect)
    node = PreviewRelay(send)
    node.receive(pack(b'R', 0, b'{"project":1,"ports":[8000]}'))
    node.receive(pack(b'O', 1, b'{"project":2,"host":"127.0.0.1","port":8000,"mode":"probe"}'))
    await asyncio.sleep(.01)
    assert sent == [b'C\x00\x00\x00\x01']
    assert attempts == []
    node.close()

async def test_cancelling_tasks_still_count_towards_node_stream_budget():
    from acc.node.preview import PreviewRelay
    from acc.preview_stream import pack
    async def send(data):
        pass
    node = PreviewRelay(send)
    try:
        for sid in range(1, 33, 2):
            node.receive(pack(b'O', sid, b'{}'))
            node.receive(pack(b'C', sid))
        with pytest.raises(ConnectionError):
            node.receive(pack(b'O', 33, b'{}'))
    finally:
        node.close()

async def test_http_upload_and_response_stream_beyond_one_frame(preview_socket):
    from acc.node.preview import PreviewRelay
    from acc.preview_stream import Mux, pack
    payload = b'body' * 40000
    observed = []
    async def app(reader, writer):
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            body = bytearray()
            while size := int(await reader.readline(), 16):
                body.extend(await reader.readexactly(size))
                await reader.readexactly(2)
            await reader.readline()
            observed.append((head.split(b'\r\n')[0], bytes(body)))
            result = b'echo:' + body
            writer.write(f'HTTP/1.1 201 Created\r\nContent-Length: {len(result)}\r\n\r\n'.encode() + result)
            await writer.drain()
        finally:
            writer.close()
    port = preview_socket.getsockname()[1]
    server = await asyncio.start_server(app, sock=preview_socket)
    main = node = None
    async def to_node(data):
        node.receive(data)
    async def to_main(data):
        main.receive(data)
    main, node = Mux(to_node), PreviewRelay(to_main)
    try:
        node.receive(pack(b'R', 0, json.dumps({'project':1,'ports':[port]}).encode()))
        stream = main.create(1)
        node.receive(pack(b'O', 1, json.dumps({'project':1,'host':'127.0.0.1','port':port,'mode':'http',
            'method':'POST','path':'/echo?q=%2F','headers':{},'protocols':[]}).encode()))
        async def upload():
            for offset in range(0, len(payload), 65536):
                await stream.send(b'D', payload[offset:offset + 65536])
            await stream.send(b'F')
        sender = asyncio.create_task(upload())
        kind, value = await stream.recv()
        assert kind == b'H' and json.loads(value)['status'] == 201
        result = bytearray()
        while True:
            kind, value = await stream.recv()
            if kind == b'F':
                break
            assert kind == b'D' and len(value) <= 65536
            result.extend(value)
        await sender
        assert result == b'echo:' + payload
        assert observed == [(b'POST /echo?q=%2F HTTP/1.1', payload)]
    finally:
        await node.aclose(); main.close()
        server.close(); await server.wait_closed()


async def test_reservation_snapshot_cache_is_bounded():
    from acc.node.preview import PreviewRelay
    from acc.preview_stream import pack
    async def send(data):
        pass
    node = PreviewRelay(send)
    try:
        for pid in range(1, 33):
            node.receive(pack(b'R', 0, json.dumps({'project':pid,'ports':[8000]}).encode()))
        with pytest.raises(ValueError):
            node.receive(pack(b'R', 0, b'{"project":33,"ports":[8000]}'))
        node.receive(pack(b'R', 0, b'{"project":1,"ports":[]}'))
        assert not node.allowed(1, 8000)
    finally:
        await node.aclose()

async def test_idle_websocket_waits_for_link_or_authorization_cancellation(monkeypatch):
    import acc.preview_stream as protocol
    from acc.server.preview_remote import RemoteSocket
    monkeypatch.setattr(protocol, 'TIMEOUT', .01)
    async def send(data):
        pass
    mux = protocol.Mux(send)
    socket = RemoteSocket(mux.create(1), None)
    pending = asyncio.create_task(socket.__anext__())
    await asyncio.sleep(.03)
    assert not pending.done()
    mux.close()
    with pytest.raises(ConnectionError):
        await pending

@pytest.mark.parametrize('value', [[], {}, {'status':True,'headers':[]}, {'status':700,'headers':[]},
    {'status':200,'headers':[['X-Test','bad\r\nCookie: injected']]},
    {'status':200,'headers':[['bad\nname','value']]}, {'status':200,'headers':[['X-Test',4]]}])
def test_invalid_response_metadata_is_refused(value):
    from acc.server.preview_remote import response_metadata
    with pytest.raises(ValueError):
        response_metadata(json.dumps(value).encode())


def test_response_metadata_preserves_safe_headers_and_status():
    from acc.server.preview_remote import response_metadata
    assert response_metadata(b'{"status":201,"headers":[["Content-Type","text/plain"]]}') == {
        'status':201, 'headers':[['Content-Type','text/plain']]}

@pytest.mark.parametrize('protocol', ['unoffered', 'bad\r\nHeader: injected', 7])
def test_node_cannot_choose_an_unoffered_websocket_protocol(protocol):
    from acc.server.preview_remote import response_metadata
    with pytest.raises(ValueError):
        response_metadata(json.dumps({'protocol':protocol}).encode(), protocols=['hmr'])
