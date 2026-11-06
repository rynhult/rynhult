"""Read-only Chat routes: real account fixtures behind a simulated broker boundary."""
import asyncio
import json

import pytest
from helpers import pause_supervisor, write_db
from sqlalchemy import select
from test_chat_reader import NATIVE, message, source

from acc.server import auth
from acc.server.models import AuditLog, LaunchSpec, Session, SessionRun, User

SESSION = 'c' * 32


@pytest.fixture
async def chat_world(api, project, tmp_path, monkeypatch):
    from acc.broker.chat import read_chat
    monkeypatch.setitem(api.app.state.ctx.settings.features.choices, 'chat', True)
    await pause_supervisor(api)
    homes = {('local', 'agent'): tmp_path / 'one', ('second-machine', 'other'): tmp_path / 'two'}
    for location, home in homes.items():
        text = 'Hello é 🌲' if location[0] == 'local' else 'Second-machine private message'
        source(home, records=[message(text)])
    with write_db(api.app.state.db) as db:
        for username, account in [('alice', 'agent'), ('bob', 'other')]:
            db.add(User(username=username, role='member', account=account,
                        password_hash='fixture', totp_secret=auth.new_totp_secret()))
        db.add(Session(id=SESSION, project_id=project['id'], provider='claude', name='Chat',
                       owner='alice', run_as='agent', workdir='/work', tmux_name='chat-fixture', native_id=NATIVE,
                       launch_spec=LaunchSpec(provider='claude', inputs={}, files={}, digest='fixture')))
        db.flush()
        db.add(SessionRun(session_id=SESSION, reason='launch', verified_at=auth.now()))
        headers = {}
        for user in db.scalars(select(User)).all():
            token, _ = auth.create_session(db, user, 'fixture')
            headers[user.username] = {'Authorization': '', 'Cookie': f'{api.app.state.ctx.session_cookie}={token}'}
        db.commit()

    async def call(method, *, account, host, **params):
        assert method == 'chat_read'
        return await asyncio.to_thread(read_chat, homes[(host, account)], **params)
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', call)
    class Client:
        def __init__(self, account, host):
            self.account, self.host = account, host
        async def call(self, method, **params):
            return await api.app.state.ctx.broker.call(method, account=self.account, host=self.host, **params)
        async def close(self):
            pass
    def fresh(account, host=None, timeout=60):
        assert timeout == 3
        return Client(account, host)
    monkeypatch.setattr(api.app.state.ctx.broker, 'fresh', fresh)
    return headers, homes, call


async def test_page_bound_to_person_and_run_and_never_starts_work(api, chat_world):
    headers, _, _ = chat_world
    first = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])
    assert first.status_code == 200, first.text
    data = first.json()
    assert [m['text'] for m in data['items']] == ['Hello é 🌲']
    assert data['run_id'] and data['capabilities']['send_text'] is False
    assert (await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['bob'])).status_code == 404
    assert (await api.post(f'/api/sessions/{SESSION}/chat', headers=headers['alice'], json={'text': 'no'})).status_code == 404
    bad = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'], params={'before': data['cursor'] + 'x'})
    assert bad.status_code == 400
    with write_db(api.app.state.db) as db:
        run = db.scalar(select(SessionRun).where(SessionRun.session_id == SESSION))
        run.ended_at = auth.now()
        db.add(SessionRun(session_id=SESSION, reason='resume', verified_at=auth.now()))
        db.commit()
    changed = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'], params={'before': data['cursor']})
    assert changed.status_code == 409
    resumed = (await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])).json()
    assert resumed['run_id'] != data['run_id']
    assert resumed['items'][0]['id'] == data['items'][0]['id']
    assert resumed['historical_run_attribution'] == 'unknown'


async def test_revocation_during_awaited_read_drops_every_byte(api, chat_world, monkeypatch):
    headers, _, call = chat_world
    async def revoked(*args, **kw):
        result = await call(*args, **kw)
        with write_db(api.app.state.db) as db:
            db.get(Session, SESSION).owner = 'bob'
            db.commit()
        return result
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', revoked)
    response = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])
    assert response.status_code == 404 and 'Hello' not in response.text


async def test_follow_reconnect_and_rotation_emit_honest_reset(api, chat_world):
    headers, homes, _ = chat_world
    data = (await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])).json()
    path = source(homes[('local', 'agent')])
    with path.open('a') as f:
        f.write(json.dumps(message('Second', 'two')) + '\n')
    response = await api.get(f'/api/sessions/{SESSION}/chat/events', headers=headers['alice'],
                             params={'after': data['cursor'], 'wait': 0})
    assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream')
    event = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))
    assert [m['text'] for m in event['items']] == ['Second']
    again = await api.get(f'/api/sessions/{SESSION}/chat/events', headers=headers['alice'],
                          params={'after': event['cursor'], 'wait': 0})
    assert 'Second' not in again.text
    path.unlink()
    source(homes[('local', 'agent')], records=[message('Replacement')])
    reset = await api.get(f'/api/sessions/{SESSION}/chat/events', headers=headers['alice'],
                          params={'after': event['cursor'], 'wait': 0})
    assert '"reset": true' in reset.text
    with api.app.state.db() as db:
        actions = list(db.scalars(select(AuditLog.action).where(AuditLog.target == SESSION)))
    assert 'chat.open' in actions and 'chat.close' in actions


async def test_offline_and_unverified_run_are_not_empty(api, chat_world, monkeypatch):
    from acc.server.broker_client import MachineOffline
    headers, _, _ = chat_world
    async def offline(*args, **kw):
        raise MachineOffline('sensitive broker details')
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', offline)
    result = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])
    assert result.json()['state'] == 'offline' and 'sensitive' not in result.text
    with write_db(api.app.state.db) as db:
        db.scalar(select(SessionRun).where(SessionRun.session_id == SESSION)).verified_at = None
        db.commit()
    result = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])
    assert result.json()['state'] == 'unknown'


async def test_wrong_caller_cursor_and_second_machine_are_separate(api, chat_world):
    headers, _, _ = chat_world
    cursor = (await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])).json()['cursor']
    with write_db(api.app.state.db) as db:
        row = db.get(Session, SESSION)
        row.owner = 'bob'
        row.run_as = 'other'
        row.team_host = 'second-machine'
        db.commit()
    response = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['bob'], params={'before': cursor})
    assert response.status_code == 400
    fresh = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['bob'])
    assert fresh.status_code == 200 and fresh.json()['items'][0]['text'] == 'Second-machine private message'


async def test_known_vault_secret_is_redacted_before_pages_and_stream(api, chat_world):
    from acc.server.vault import create_key
    headers, homes, _ = chat_world
    secret = 'fixture-private-secret-never-leaves'
    with write_db(api.app.state.db) as db:
        key = create_key(db, api.app.state.supervisor.vault, name='Fixture key', variable='FIXTURE_KEY',
                         value=secret, owner='alice')
        db.get(Session, SESSION).vault_keys = [key.id]
        db.commit()
    source(homes[('local', 'agent')], records=[message('Token: ' + secret)])
    for suffix in ('/chat', '/chat/events?wait=0'):
        response = await api.get(f'/api/sessions/{SESSION}' + suffix, headers=headers['alice'])
        assert response.status_code == 200
        assert secret not in response.text and 'Fixture key' in response.text


async def test_stream_revocation_between_chunks_does_not_emit_awaited_result(api, chat_world, monkeypatch):
    headers, homes, call = chat_world
    count = 0
    async def revoke(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            source(homes[('local', 'agent')], records=[message('Not authorized anymore')])
        result = await call(*args, **kwargs)
        if count == 2:
            with write_db(api.app.state.db) as db:
                db.get(Session, SESSION).owner = 'bob'
                db.commit()
        return result
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', revoke)
    response = await api.get(f'/api/sessions/{SESSION}/chat/events?wait=2', headers=headers['alice'])
    assert 'Hello' in response.text and 'Not authorized anymore' not in response.text
    assert response.text.count('event: chat') == 1


async def test_untrusted_node_cannot_make_oversized_or_unstructured_page(api, chat_world, monkeypatch):
    headers, _, call = chat_world
    async def malformed(*args, **kw):
        result = await call(*args, **kw)
        result['source'] = {'path': '/private/no-disclosure', 'digest': 'bogus'}
        return result
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', malformed)
    response = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])
    assert response.status_code == 502 and '/private/' not in response.text


async def test_broker_run_verification_lost_during_read_refuses(api, chat_world, monkeypatch):
    headers, _, call = chat_world
    async def lose(*args, **kw):
        result = await call(*args, **kw)
        with write_db(api.app.state.db) as db:
            db.scalar(select(SessionRun).where(SessionRun.session_id == SESSION)).verified_at = None
            db.commit()
        return result
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', lose)
    response = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])
    assert response.status_code == 409 and 'Hello' not in response.text


async def test_shared_reads_release_canceled_waiter_without_canceling_other_view():
    from acc.server.api.chat import Reads
    reads = Reads()
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        entered.set()
        await release.wait()
        return {'text': 'read finished'}
    first = asyncio.create_task(reads.call(('binding', 'read'), 0, fetch))
    await entered.wait()
    ready = asyncio.Event()
    async def other_view():
        ready.set()
        return await reads.call(('binding', 'read'), 0, fetch)
    second = asyncio.create_task(other_view())
    await ready.wait()  # other_view entered the shared read before cancellation
    first.cancel()
    release.set()
    assert await second == {'text': 'read finished'}
    await asyncio.gather(first, return_exceptions=True)
    assert not reads.active and not reads.locks


async def test_page_limits_and_cursor_do_not_expose_native_path(api, chat_world):
    headers, _, _ = chat_world
    for limit in (0, 201):
        assert (await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'],
                              params={'limit': limit})).status_code == 422
    response = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])
    assert response.headers['cache-control'] == 'no-store'
    assert NATIVE not in response.text and '/tmp/' not in response.text


async def test_cross_origin_chat_reads_refused_even_with_a_valid_login(api, chat_world):
    headers, _, _ = chat_world
    for suffix in ('/chat', '/chat/events?wait=0'):
        result = await api.get(f'/api/sessions/{SESSION}' + suffix,
                               headers={**headers['alice'], 'Origin': 'https://untrusted.example.com'})
        assert result.status_code == 403 and 'Hello' not in result.text


async def test_sse_last_event_id_resumes_after_latest_acknowledged_cursor(api, chat_world):
    headers, homes, _ = chat_world
    first = (await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])).json()
    path = source(homes[('local', 'agent')])
    with path.open('a') as f:
        f.write(json.dumps(message('Second', 'two')) + '\n')
    response = await api.get(f'/api/sessions/{SESSION}/chat/events?wait=0',
                             headers=headers['alice'], params={'after': first['cursor']})
    last = next(line[4:] for line in response.text.splitlines() if line.startswith('id: '))
    with path.open('a') as f:
        f.write(json.dumps(message('Third', 'three')) + '\n')
    resumed = await api.get(f'/api/sessions/{SESSION}/chat/events?wait=0',
                            headers={**headers['alice'], 'Last-Event-ID': last}, params={'after': first['cursor']})
    assert 'Third' in resumed.text and 'Second' not in resumed.text


async def test_unpaired_unicode_from_node_is_a_safe_refusal(api, chat_world, monkeypatch):
    headers, _, call = chat_world
    async def malformed(*args, **kw):
        result = await call(*args, **kw)
        result['items'][0]['text'] = '\ud800'
        return result
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', malformed)
    result = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])
    assert result.status_code == 502


@pytest.mark.parametrize('extra', [
    {'notices': {'tools': 'private text'}}, {'notices': {'tools': -1}},
    {'notices': {'tools': 0, 'path': '/private'}}, {'reason': '/private'}, {'more': 'yes'},
])
async def test_new_notice_fields_are_strictly_projected(api, chat_world, monkeypatch, extra):
    headers, _, call = chat_world
    async def malformed(*args, **kw):
        result = await call(*args, **kw)
        return {**result, **extra}
    monkeypatch.setattr(api.app.state.ctx.broker, 'call', malformed)
    result = await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])
    assert result.status_code == 502 and '/private' not in result.text


async def test_follow_burst_returns_all_messages_without_skipping_a_cursor(api, chat_world):
    headers, homes, _ = chat_world
    first = (await api.get(f'/api/sessions/{SESSION}/chat', headers=headers['alice'])).json()
    path = source(homes[('local', 'agent')])
    with path.open('a') as f:
        for i in range(3):
            f.write(json.dumps(message(f'M{i} ' + 'x' * 60000, f'burst{i}')) + '\n')
    delivered = []
    cursor = first['cursor']
    for _ in range(3):
        response = await api.get(f'/api/sessions/{SESSION}/chat/events', headers=headers['alice'],
                                 params={'after': cursor, 'wait': 0})
        assert response.status_code == 200
        envelope = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))
        delivered.extend(item['text'][:2] for item in envelope['items'])
        cursor = envelope['cursor']
        if not envelope['more']:
            break
    assert delivered == ['M0', 'M1', 'M2']


async def test_chat_switch_off_refuses_reads_and_follow_without_broker_io(api, chat_world, monkeypatch):
    headers, _, _ = chat_world
    monkeypatch.setitem(api.app.state.ctx.settings.features.choices, 'chat', False)
    def forbidden(*args, **kwargs):
        pytest.fail('disabled Chat reached the broker')
    monkeypatch.setattr(api.app.state.ctx.broker, 'fresh', forbidden)
    for suffix in ('chat', 'chat/events?wait=0'):
        response = await api.get(f'/api/sessions/{SESSION}/{suffix}', headers=headers['alice'])
        assert response.status_code == 409
        assert response.json()['detail'] == 'Chat is turned off in Admin features.'
