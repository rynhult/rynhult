"""Round 3: preserve read positions, ordinary prompts and bounded shared-worker admission."""
import asyncio
import json

import pytest
from test_chat_api import SESSION, chat_world  # noqa: F401
from test_chat_reader import message, read, source


def event(response):
    assert response.status_code == 200, response.text
    return json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))


@pytest.mark.parametrize('failure', ['transport', 'offline', 'reader', 'discovery', 'error_with_position'])
async def test_cp11_failed_follow_preserves_exact_cursor_and_all_300_messages(api, chat_world, monkeypatch, failure):  # noqa: F811
    from acc.server.broker_client import BrokerError, MachineOffline
    headers, homes, call = chat_world
    address = f'/api/sessions/{SESSION}/chat'
    initial = (await api.get(address, headers=headers['alice'])).json()
    path = source(homes[('local', 'agent')])
    with path.open('a') as stream:
        for number in range(300):
            stream.write(json.dumps(message(f'n{number}', f'new-{number}')) + '\n')
    failed = False
    async def refuse(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            if failure == 'transport':
                raise BrokerError('fixture refusal')
            if failure == 'offline':
                raise MachineOffline('fixture offline')
            if failure == 'error_with_position':
                result = await call(*args, **kwargs)
                return {**result, 'state': 'error'}
            return {'state': 'error' if failure == 'reader' else 'unknown', 'source': None,
                    'items': [], 'omitted': 0, 'reset': False, 'before': None}
        return await call(*args, **kwargs)
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', refuse)
    result = event(await api.get(address + '/events', headers=headers['alice'], params={'after': initial['cursor'], 'wait': 0}))
    assert result['cursor'] == initial['cursor']
    assert result['observed_at'] is None and not result['reset']
    delivered = []
    for _ in range(4):
        result = event(await api.get(address + '/events', headers=headers['alice'], params={'after': result['cursor'], 'wait': 0}))
        assert not result['reset']
        delivered.extend(item['text'] for item in result['items'])
        if not result['more']:
            break
    assert delivered == [f'n{number}' for number in range(300)]


async def test_cp11_first_read_without_a_position_reports_reset(api, chat_world, monkeypatch):  # noqa: F811
    from acc.server.broker_client import BrokerUnavailable
    headers, _, _ = chat_world
    async def fail(*args, **kwargs):
        raise BrokerUnavailable('fixture refusal')
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', fail)
    result = (await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])).json()
    assert result['reset'] is True and result['state'] == 'error'
    again = event(await api.get(f'/api/sessions/{SESSION}/chat/events', headers=headers['alice'],
                                 params={'after': result['cursor'], 'wait': 0}))
    assert again['reset'] and again['cursor'] == result['cursor']


@pytest.mark.parametrize('prompt', ['if a < b then stop', 'use Vec<T> here', 'fix the <div> layout',
    'a < b and c > d', '<user_query>use Vec<T> here</user_query>',
    'Before <system-reminder>private</system-reminder>after <div>',
    'a < b <system-reminder>private</system-reminder> < T>'])
def test_cp12_only_known_context_is_removed(tmp_path, prompt):
    source(tmp_path, records=[{**message(), 'type': 'user', 'message': {'content': prompt}}])
    result = read(tmp_path)
    expected = prompt.replace('<user_query>', '').replace('</user_query>', '').replace('<system-reminder>private</system-reminder>', '')
    assert [row['text'] for row in result['items']] == [expected]
    assert result['omitted'] == (1 if 'system-reminder' in prompt else 0)


@pytest.mark.parametrize('flag', ['isMeta', 'isSidechain'])
def test_cp14_known_private_records_are_not_corrupt_history(tmp_path, flag):
    source(tmp_path, records=[{**message('private', 'hidden'), flag: True}, message('Visible', 'shown')])
    result = read(tmp_path)
    assert [row['text'] for row in result['items']] == ['Visible']
    assert result['state'] == 'ready' and result['omitted'] == 0


def test_cp15_middle_rewrite_plus_append_resets_immediately(tmp_path):
    path = source(tmp_path, records=[message('x' * 6000, str(number)) for number in range(4)])
    first = read(tmp_path)
    data = path.read_bytes()
    at = data.find(b'x' * 20, len(data) // 3)
    with path.open('r+b') as stream:
        stream.seek(at)
        stream.write(b'y' * 20)
        stream.seek(0, 2)
        stream.write((json.dumps(message('Appended', 'new')) + '\n').encode())
    result = read(tmp_path, after=first['source']['end'], checkpoint=first['source'])
    assert result['reset'] and not result['items']


async def test_cp13_two_followers_and_file_search_queue_on_one_account(tmp_path, monkeypatch):
    from acc.broker import search_worker
    from acc.broker.config import BrokerConfig
    worker = search_worker.Searches()
    entered, release = asyncio.Event(), asyncio.Event()
    operations = []
    class Process:
        returncode = None
        def kill(self):
            self.returncode = -9
        async def communicate(self, payload=None):
            request = json.loads(payload)
            operations.append(request['operation'])
            if len(operations) == 1:
                entered.set()
                await release.wait()
            self.returncode = 0
            # File search is not present on this branch; project only its worker
            # operation envelope, exercising the same shared admission slot.
            return json.dumps({'result': {'state': 'ready'}, 'cache': {}} if request['operation'] == 'chat_read'
                              else {'matches': ['example.txt']}).encode(), b''
    async def spawn(*args, **kwargs):
        return Process()
    monkeypatch.setattr(search_worker.asyncio, 'create_subprocess_exec', spawn)
    config = BrokerConfig(run_dir=tmp_path / 'run')
    first = asyncio.create_task(worker.run(config, {}, operation='chat_read'))
    await entered.wait()
    second = asyncio.create_task(worker.run(config, {}, operation='chat_read'))
    search = asyncio.create_task(worker.run(config, {}, operation='files_search'))
    await asyncio.sleep(0)
    waiting = not second.done() and not search.done()
    release.set()
    results = await asyncio.gather(first, second, search, return_exceptions=True)
    assert waiting
    assert all(isinstance(value, dict) for value in results), results
    assert [value.get('state') for value in results[:2]] == ['ready', 'ready']
    assert results[2] == {'matches': ['example.txt']}
    assert operations == ['chat_read', 'chat_read', 'files_search'] and not worker.active


async def test_cp13_api_busy_is_distinct_and_keeps_cursor(api, chat_world, monkeypatch):  # noqa: F811
    from acc.server.broker_client import BrokerError
    headers, _, _ = chat_world
    address = f'/api/sessions/{SESSION}/chat'
    initial = (await api.get(address, headers=headers['alice'])).json()
    async def busy(*args, **kwargs):
        from acc.server import broker_client
        raise getattr(broker_client, 'BrokerBusy', BrokerError)('fixture busy')
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', busy)
    result = event(await api.get(address + '/events', headers=headers['alice'], params={'after': initial['cursor'], 'wait': 0}))
    assert result['state'] == 'busy' and result['reason'] == 'account_busy'
    assert result['cursor'] == initial['cursor'] and not result['capabilities']['read_history']


@pytest.mark.parametrize('pending_prefix', [False, True])
async def test_cp11_failed_older_page_keeps_both_cursor_offsets(api, chat_world, monkeypatch, pending_prefix):  # noqa: F811
    from acc.server.broker_client import BrokerError
    headers, homes, _ = chat_world
    source(homes[('local', 'agent')], records=[message(str(n), str(n)) for n in range(300)])
    address = f'/api/sessions/{SESSION}/chat'
    page = (await api.get(address, headers=headers['alice'])).json()
    assert page['before_cursor']
    async def fail(*args, **kwargs):
        if pending_prefix:
            return {'state': 'unknown', 'source': kwargs['checkpoint'], 'items': [], 'omitted': 0,
                    'reset': False, 'reason': 'prefix_verification', 'more': True, 'before': None}
        raise BrokerError('fixture deadline')
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', fail)
    failed = (await api.get(address, headers=headers['alice'], params={'before': page['before_cursor']})).json()
    assert failed['cursor'] == page['before_cursor'] == failed['before_cursor']
    assert not failed['reset'] and failed['observed_at'] is None


async def test_cp13_queue_is_bounded_and_cancelled_waiters_do_not_leak(tmp_path, monkeypatch):
    from acc.broker import search_worker
    from acc.broker.config import BrokerConfig
    monkeypatch.setattr(search_worker, 'QUEUE_LIMIT', 2)
    monkeypatch.setattr(search_worker, 'QUEUE_WAIT', .02)
    worker = search_worker.Searches()
    worker.active = True  # Simulate a still-owned, unreapable child.
    config = BrokerConfig(run_dir=tmp_path / 'run')
    first = asyncio.create_task(worker.run(config, {}, operation='chat_read'))
    second = asyncio.create_task(worker.run(config, {}, operation='files_search'))
    await asyncio.sleep(0)
    assert len(worker.waiters) == 2
    with pytest.raises(search_worker.ReadBusy, match='queue is full'):
        await worker.run(config, {}, operation='chat_read')
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(search_worker.ReadBusy, match='busy'):
        await second
    assert not worker.waiters and worker.active  # No second child until reap.
    worker._release()
    assert not worker.active


@pytest.mark.parametrize('release_first', [False, True])
async def test_cp13_cancel_at_queue_handoff_releases_exactly_once(release_first):
    from acc.broker.search_worker import Searches
    worker = Searches()
    worker.active = True
    waiting = asyncio.create_task(worker._acquire())
    await asyncio.sleep(0)
    if release_first:
        worker._release()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    if not release_first:
        worker._release()
    assert not worker.active and not worker.waiters


async def test_cp13_busy_code_survives_broker_dispatch_and_transport(tmp_path, monkeypatch):
    from acc.broker.config import BrokerConfig
    from acc.broker.search_worker import ReadBusy
    from acc.broker.server import Broker
    from acc.server.broker_client import BrokerBusy, BrokerClient
    broker = Broker(BrokerConfig(run_dir=tmp_path / 'run'))
    async def busy(*args, **kwargs):
        raise ReadBusy('do not expose this detail')
    monkeypatch.setattr(broker._searches, 'run', busy)
    async def handle(reader, writer):
        try:
            result = await broker.dispatch((await reader.readline()).strip())
            assert result['code'] == 'read_busy' and 'do not expose' not in result['error']
            writer.write(json.dumps(result).encode() + b'\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    async def connect():
        return await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
    client = BrokerClient(tmp_path / 'unused.sock', opener=connect)
    try:
        with pytest.raises(BrokerBusy, match='busy'):
            await client.call('chat_read', provider='claude', native_id='fixture')
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


def test_cp15_prefix_verification_resumes_on_original_fragment_boundaries(tmp_path, monkeypatch):
    from acc.broker import chat
    monkeypatch.setattr(chat, 'CHUNK', 64)
    path = source(tmp_path, records=[message('x' * 100, str(n)) for n in range(10)])
    first = read(tmp_path)
    monkeypatch.setattr(chat, 'MAX_BYTES', 512)
    with path.open('a') as stream:
        stream.write(json.dumps(message('Next', 'new')) + '\n')
    pending = 0
    for _ in range(30):
        result = read(tmp_path, after=first['source']['end'], checkpoint=first['source'])
        assert not result['reset']
        if result['reason'] == 'prefix_verification':
            assert result['source'] == first['source'] and not result['items']
            pending += 1
        else:
            assert [row['text'] for row in result['items']] == ['Next']
            break
    else:
        pytest.fail('prefix verification did not finish within bounded fixture rounds')
    assert pending > 1


@pytest.mark.parametrize('tag', [
    'command-args', 'command-new-private-field', 'local-command-stderr', 'local-command-caveat',
    'bash-stdout', 'bash-stderr', 'bash-input', 'ide_opened_file', 'task-notification', 'user-prompt-submit-hook', 'ide_selection',
    'environment_details', 'INSTRUCTIONS', 'turn_aborted', 'collaboration_mode', 'system',
])
def test_cp16_cli_context_is_private_without_swallowing_prose(tag):
    import time

    from acc.broker.chat import _user_text
    text, omitted = _user_text(f'Explain Vec<T>. <{tag}>PRIVATE</{tag}> Keep this.', time.monotonic() + 1)
    assert text == 'Explain Vec<T>.  Keep this.'
    assert omitted == 1


@pytest.mark.parametrize('opening', [
    '<system-reminder ' + 'x' * 300 + '>',
    '<system-reminder attr="<injected>">',
    '<system-reminder',
    '<command-args ' + 'x' * 300 + '>',
])
def test_cp16_malformed_known_opening_fails_private(opening):
    import time

    from acc.broker.chat import _user_text
    text, omitted = _user_text('Keep Vec<T>. ' + opening + '\nPRIVATE</system-reminder>', time.monotonic() + 1)
    assert text == 'Keep Vec<T>.'
    assert omitted >= 1
