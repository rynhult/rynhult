"""Unsupported controls cannot reach callbacks; supported text still works."""
import json

import pytest
from test_session_relay_integration import case as http_case  # noqa: F401
from test_session_relay_native_input_host import host_case  # noqa: F401
from test_session_relay_native_socket import initialize, native_socket  # noqa: F401
from websockets.asyncio.client import unix_connect

from acc.session_relay import protocol as p
from acc.session_relay.native_turn import NativeTurnPolicy


@pytest.mark.parametrize('method,params', [
    ('config/value/write', {'keyPath': 'model', 'value': 'other'}),
    ('config/batchWrite', {'edits': []}),
    ('account/login/start', {'type': 'apiKey', 'apiKey': 'artificial'}),
    ('account/logout', {}), ('thread/start', {}), ('thread/fork', {'threadId': 'native'}),
    ('turn/steer', {'threadId': 'native', 'input': []}),
    ('command/exec', {'command': ['true']}),
    ('turn/start', {'threadId': 'native', 'effort': 'high', 'input': [{'type': 'text', 'text': 'hello'}]}),
    ('turn/start', {'threadId': 'native', 'model': 'other', 'input': [{'type': 'text', 'text': 'hello'}]}),
    ('turn/start', {'threadId': 'native', 'input': [{'type': 'localImage', 'path': '/fixture.png'}]}),
    ('turn/start', {'threadId': 'native', 'input': [{'type': 'text', 'text': 'hello', 'text_elements': [{}]}]}),
])
async def test_native_control_matrix_never_calls_owner_then_text_succeeds(native_socket, method, params):  # noqa: F811
    proxy, reads, _ = native_socket
    admitted = []
    async def route(method, params):
        admitted.append((method, params))
        return {'state': 'accepted'}
    proxy.writer_route = lambda _: route
    proxy.turn_policy = NativeTurnPolicy.from_start('native', {'thread': {'id': 'native'}, 'model': 'fixture'})
    async with unix_connect(str(proxy.path), compression=None) as client:
        await initialize(client)
        await client.send(json.dumps({'id': 1, 'method': method, 'params': params}))
        assert 'error' in json.loads(await client.recv())
        assert not reads and not admitted
        await client.send(json.dumps({'id': 2, 'method': 'turn/start', 'params': {
            'threadId': 'native', 'input': [{'type': 'text', 'text': 'safe continuation'}]}}))
        assert 'result' in json.loads(await client.recv())
    assert admitted == [('turn.submit', {'text': 'safe continuation'})]


@pytest.mark.parametrize('client_info', [None, [], 'codex-tui', 1])
async def test_malformed_native_initialize_refuses_without_handler_crash(native_socket, client_info, caplog):  # noqa: F811
    proxy, reads, _ = native_socket
    async with unix_connect(str(proxy.path), compression=None) as client:
        await client.send(json.dumps({'id': 1, 'method': 'initialize', 'params': {'clientInfo': client_info}}))
        assert 'error' in json.loads(await client.recv())
        await initialize(client)
    assert not reads
    assert not any(record.getMessage() == 'connection handler failed' for record in caplog.records)


@pytest.mark.parametrize('data', [b'/model other\r', b'/effort high\r', b'/config\r', b'/login\r',
    b'/attach fixture.png\r', b'/editor\r', b'/permissions\r', b'/resume\r', b'/logout\r',
    b'!true\r', b'\x1b[A', b'\x1b[200~paste\x1b[201~', b'\xc3\xa9', b'a\rb\r'])
async def test_host_control_matrix_does_not_consume_sequence_or_draft(host_case, data):  # noqa: F811
    c = host_case
    with pytest.raises(p.RelayError):
        await c.host.write_frames(c.grant, 1, data)
    assert c.host.draft == '' and c.host.ticket is None
    assert c.host.admitted == c.host.processed == 0
    assert not c.state['bytes'] and not c.state['admissions']
    await c.host.write_frames(c.grant, 1, b'safe continuation\r')
    assert (await c.host.native_request('turn.submit', {'text': 'safe continuation'}, 'message'))['state'] == 'accepted'
    assert c.host.processed == 1 and len(c.state['admissions']) == 1


@pytest.mark.parametrize('method', ['configuration.request', 'turn.steer', 'attachment.begin',
    'attachment.chunk', 'attachment.finish', 'attachment.remove', 'switch.commit'])
async def test_public_http_unsupported_controls_refuse_without_runtime_creation(http_case, method):  # noqa: F811
    from pathlib import Path

    import httpx
    from test_session_relay_integration import attach

    c = http_case
    cases = json.loads((Path(__file__).parent / 'fixtures/session_relay/commands.json').read_text())['cases']
    frame = next(item['frame'] for item in cases if item['name'] == 'valid ' + method)
    frame['params']['sessionId'] = 'relay'
    if 'expectedRunId' in frame['params']:
        frame['params']['expectedRunId'] = '1'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        await attach(client)
        reply = await client.post('/api/sessions/relay/relay', json=frame)
        assert reply.json()['error']['data']['code'] == 'UNSUPPORTED'
        assert not hasattr(c.ctx, 'relay_runtime')


@pytest.mark.parametrize('extra', [{'model':'other'}, {'effort':'high'},
    {'attachments':[{'path':'fixture.png'}]}, {'editor':'external'}, {'config':{'hooks':False}},
    {'nativeId':'other'}, {'fork':True}])
async def test_public_text_with_unsupported_controls_refuses_before_operation_or_runtime(http_case, extra):  # noqa: F811
    from pathlib import Path

    import httpx
    from sqlalchemy import select
    from test_session_relay_integration import attach

    from acc.server.models import RelayOperation

    c = http_case
    examples = json.loads((Path(__file__).parent/'fixtures/session_relay/commands.json').read_text())['cases']
    frame = next(item['frame'] for item in examples if item['name']=='valid turn.submit')
    frame['params'].update(sessionId='relay',expectedRunId='1',**extra)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app),base_url='http://fixture') as client:
        await attach(client)
        reply = (await client.post('/api/sessions/relay/relay',json=frame)).json()
        assert 'error' in reply
    with c.db() as db:
        assert not list(db.scalars(select(RelayOperation)))
    assert not hasattr(c.ctx,'relay_runtime')
