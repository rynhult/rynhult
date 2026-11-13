"""Real SDK with an artificial CLI stdio replay. No live accounts or provider traffic."""
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acc.server import claude_subscription as consent
from acc.server.admin_features import Features
from acc.server.config import Settings
from acc.server.models import AuditLog, Base, User
from acc.session_relay.adapters.base import AdapterOperation, CloseMode, Outcome, ReviewedSpec
from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter
from acc.session_relay.protocol import Actor, ActorKind, Binding, Channel, Fence, RelayError

CLI = Path(__file__).parent / 'fixtures/session_relay/claude_agent/fake-claude'


def operation(method='turn.submit', **params):
    return AdapterOperation('op-' + method, 'session', 'run', method, 'a' * 64,
                            Actor(ActorKind.PERSON, 'fixture', Channel.CHAT), Fence(1, 'broker', 'hub'), 999999,
                            params or {'text': 'fixture'})


def spec(tmp_path, **options):
    (tmp_path / 'settings.json').write_text('{"disableAllHooks": false, "hooks": {}}')
    return ReviewedSpec(Binding('binding', 'installation', 'node', 'fixture', str(tmp_path), 'claude', 'fixture-conversation', 'a' * 64),
                        'run', dict(session_id='session', cli_path=str(CLI.resolve()), settings_path=str(tmp_path / 'settings.json'), guard_intact=True, prior_owner_exit_verified=True, **options))


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_real_sdk_replay_same_session_usage_idle_and_no_resend(tmp_path):
    pytest.importorskip('claude_agent_sdk')
    events = []
    async def publish(value):
        events.append(value)
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=publish, clock=lambda: 1000)
    await adapter.start(spec(tmp_path))
    try:
        assert (await adapter.submit(operation())).outcome is Outcome.UNKNOWN
        await until(lambda: not adapter.active)
        assert not any('conversation-checkpoint' in str(e) for e in events)
        assert adapter.client.options.resume == 'fixture-conversation'
        assert not adapter.client.options.fork_session
        assert adapter.client.options.setting_sources == []
        assert adapter.client.options.permission_mode == 'default'
        assert not adapter.client.options.allowed_tools
        assert adapter.client.options.tools is None
        assert not adapter.client.options.env  # adapter never loads/forwards credentials
        assert adapter.client.options.settings == str(tmp_path / 'settings.json')
        assert not adapter.client.options.hooks  # installed Rynhult hooks remain in settings
        count = len(events)
        assert (await adapter.submit(operation())).outcome is Outcome.UNKNOWN
        assert len(events) == count
        assert adapter.usage == {'input_tokens': 10, 'output_tokens': 4}
        await adapter.close(CloseMode.DETACH)
        assert not adapter.owner.done()
        adapter.last_activity = 0
        assert await adapter.idle_stop()
        assert adapter.owner.done()
    finally:
        await adapter.close(CloseMode.STOP)


@pytest.mark.asyncio
async def test_real_sdk_person_only_approval_and_interrupt(tmp_path):
    pytest.importorskip('claude_agent_sdk')
    events = []
    async def publish(value):
        events.append(value)
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=publish)
    await adapter.start(spec(tmp_path))
    try:
        await adapter.submit(operation(text='approve-fixture'))
        await until(lambda: adapter.pending)
        assert not await adapter.idle_stop()
        decision = next(iter(adapter.pending))
        reply = operation('decision.reply', decision_id=decision, choice='allow')
        with pytest.raises(RelayError):
            await adapter.reply_decision(replace(reply, actor=Actor(ActorKind.SERVICE, 'lead', Channel.TEAM)))
        await adapter.reply_decision(reply)
        with pytest.raises(RelayError):
            await adapter.reply_decision(reply)
        await until(lambda: not adapter.active)
        await adapter.submit(replace(operation(text='wait-fixture'), operation_id='second'))
        await adapter.interrupt(operation('turn.interrupt', turnId='second'))
        await until(lambda: not adapter.active)
    finally:
        await adapter.close(CloseMode.STOP)


def test_consent_default_accept_revoke_owner_disable_and_record():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    features = Features(Settings(), {'session_relay': True})
    who = SimpleNamespace(user_id=7, username='fixture')
    with Session(engine) as db:
        db.add(User(id=7, username='fixture', password_hash='fixture', totp_secret='fixture'))
        db.commit()
        assert not consent.allowed(db, 7, features)
        with pytest.raises(ValueError):
            consent.update(db, who, True, 'stale')
        accepted = consent.update(db, who, True, consent.VERSION)
        db.commit()
        assert consent.allowed(db, 7, features)
        assert not consent.allowed(db, 8, features)
        assert accepted['accepted_by'] == 7 and accepted['accepted_at']
        assert accepted['notice'] == consent.NOTICE
        assert db.scalar(select(AuditLog)).action == 'claude_subscription.accept'
        features.choices['claude_subscription_chat'] = False
        assert not consent.allowed(db, 7, features)
        features.choices['claude_subscription_chat'] = True
        consent.update(db, who, False, None)
        db.commit()
        assert not consent.allowed(db, 7, features)
        assert consent.read(db, 7)['accepted_at'] == accepted['accepted_at']
        consent.update(db, who, True, consent.VERSION)
        db.commit()
        db.get(User, 7).username = 'replacement-person'
        db.commit()
        assert not consent.allowed(db, 7, features)  # reused numeric ID never inherits consent


def test_policy_only_subscription_person_and_flag(tmp_path):
    class DB:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def get(self, model, *_):
            if model is User:
                return SimpleNamespace(username='fixture')
            from acc.server.models import RelayDecision
            if model is RelayDecision:
                return None
            return SimpleNamespace(value={'enabled': True, 'accepted_by': 7, 'version': consent.VERSION,
                                          'accepted_username': 'fixture'})
    features = Features(Settings(), {})
    policy = consent.relay_policy(DB, features)
    reviewed = spec(tmp_path, auth_mode='subscription', person_id=7)
    assert not policy(reviewed)
    assert policy(reviewed, operation('turn.interrupt', turnId='second'))
    assert not policy(reviewed, operation('decision.reply', decision_id='decision', choice='deny'))
    features.choices['session_relay'] = True
    assert policy(reviewed, operation())
    assert not policy(reviewed, replace(operation(), actor=Actor(ActorKind.PERSON, '7', Channel.CHAT)))
    assert not policy(reviewed, replace(operation(), actor=Actor(ActorKind.SERVICE, 'lead', Channel.TEAM)))
    features.choices['claude_subscription_chat'] = False
    assert not policy(reviewed, operation())
    assert policy(spec(tmp_path, auth_mode='api_key'), operation())
    assert not policy(spec(tmp_path, auth_mode='unknown'), operation())


@pytest.mark.asyncio
async def test_start_refuses_missing_exit_proof_and_native_id(tmp_path):
    async def publish(_):
        pass
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=publish)
    reviewed = spec(tmp_path)
    for invalid in (replace(reviewed, spec={**reviewed.spec, 'prior_owner_exit_verified': False}),
                    replace(reviewed, binding=replace(reviewed.binding, native_conversation=''))):
        with pytest.raises(RelayError):
            await adapter.start(invalid)
        assert adapter.owner is None


@pytest.mark.asyncio
async def test_real_sdk_busy_switch_and_unknown_process_exit(tmp_path):
    pytest.importorskip('claude_agent_sdk')
    async def publish(_):
        pass
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=publish)
    await adapter.start(spec(tmp_path))
    try:
        await adapter.submit(operation(text='wait-fixture'))
        with pytest.raises(RelayError):
            await adapter.stop_owner()
        assert not (await adapter.prepare_switch()).idle
        await adapter.interrupt(operation('turn.interrupt', turnId='op-turn.submit'))
        await until(lambda: not adapter.active)
        evidence = await adapter.stop_owner()
        assert evidence.exit.value == 'exited'
        assert evidence.native_conversation == 'fixture-conversation'
        resumed = await adapter.resume_owner(replace(spec(tmp_path), run_id='next-run'))
        assert resumed.native_conversation == 'fixture-conversation'
        assert resumed.run_id == 'next-run'
        assert adapter.client.options.resume == 'fixture-conversation'
    finally:
        await adapter.close(CloseMode.STOP)


@pytest.mark.asyncio
async def test_settings_http_person_acceptance_and_revocation(api):
    from test_admin_features import owner_browser
    headers = await owner_browser(api)
    before = await api.get('/api/preferences/claude-subscription', headers=headers)
    assert before.status_code == 200
    assert not before.json()['enabled']
    assert before.json()['notice'] == consent.NOTICE
    accepted = await api.put('/api/preferences/claude-subscription', headers=headers,
                             json={'enabled': True, 'version': consent.VERSION})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['accepted_at'] and accepted.json()['accepted_by']
    revoked = await api.put('/api/preferences/claude-subscription', headers=headers, json={'enabled': False})
    assert revoked.status_code == 200 and not revoked.json()['enabled']


@pytest.mark.asyncio
async def test_history_delegates_to_same_native_transcript_without_start(tmp_path):
    from acc.session_relay.adapters.base import HistoryPage, HistoryState
    page = HistoryPage(HistoryState.OK, ({'role': 'user', 'text': 'Terminal turn'},))
    observed = []
    async def history(binding):
        observed.append(binding.native_conversation)
        return page
    async def follow(binding, cursor):
        observed.append((binding.native_conversation, cursor))
        yield page
    async def publish(_):
        pass
    def never_launch(_):
        pytest.fail('History must never launch the SDK')
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=publish, client_factory=never_launch,
                                 history=history, history_follow=follow)
    adapter.spec = spec(tmp_path)  # the hub binds its read context, without starting execution
    assert await adapter.read_snapshot() == page
    assert [item async for item in adapter.follow('cursor')] == [page]
    assert observed == ['fixture-conversation', ('fixture-conversation', 'cursor')]
    assert adapter.owner is None


@pytest.mark.parametrize('boundary', ['connect', 'queued_query', 'queued_interrupt'])
async def test_consent_loss_across_sdk_await_refuses_native_effect(tmp_path, boundary, monkeypatch):
    """SDK-shaped in-memory client; requires no optional dependency or CLI."""
    import os
    allowed = True
    calls = []
    process = SimpleNamespace(pid=os.getpid(), returncode=None)
    class Client:
        _transport = SimpleNamespace(_process=process)
        async def connect(self):
            nonlocal allowed
            await asyncio.sleep(0)
            if boundary == 'connect':
                allowed = False
        async def disconnect(self):
            process.returncode = 0  # fake process object only
        async def query(self, text):
            calls.append(('query', text))
        async def interrupt(self):
            calls.append(('interrupt',))
    async def publish(_):
        pytest.fail('A revoked operation must not produce provider events')
    adapter = ClaudeAgentAdapter(policy=lambda *_: allowed, publish=publish, client_factory=lambda _: Client())
    if boundary == 'connect':
        with pytest.raises(RelayError):
            await adapter.start(spec(tmp_path))
        assert calls == [] and process.returncode == 0
        return
    await adapter.start(spec(tmp_path))
    put = adapter.commands.put
    async def revoke_after_enqueue(item):
        nonlocal allowed
        await put(item)
        if item[0] != 'stop':
            allowed = False
    monkeypatch.setattr(adapter.commands, 'put', revoke_after_enqueue)
    try:
        if boundary == 'queued_query':
            assert (await adapter.submit(operation())).outcome is Outcome.UNKNOWN
        else:
            with pytest.raises(RelayError):
                await adapter.interrupt(operation('turn.interrupt', turnId='second'))
        assert calls == []
    finally:
        await adapter.close(CloseMode.STOP)


@pytest.mark.parametrize('text', ['/help', ' \t/model', 'é' * 10001])
async def test_security_a2_chat_refuses_slash_commands_and_byte_overflow(text, monkeypatch):
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=None)
    monkeypatch.setattr(adapter, '_check', lambda *_: None)
    calls = []
    async def command(*args):
        calls.append(args)
    monkeypatch.setattr(adapter, '_command', command)
    with pytest.raises(RelayError):
        await adapter.submit(operation(text=text))
    assert not calls and not adapter.seen and not adapter.active
