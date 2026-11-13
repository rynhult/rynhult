"""Production runtime factory and main grants over a fixture-owned authenticated Unix stream."""
import asyncio
import os
import pwd
from dataclasses import replace
from types import SimpleNamespace

import pytest
from relay_helpers import setup

from acc import providers
from acc.broker.config import BrokerConfig
from acc.broker.relay_runtime import RuntimeTransportFactory
from acc.broker.server import Broker
from acc.server.broker_client import Brokers
from acc.server.db import write_intent
from acc.server.models import RelayRun, RelaySession, Session, SessionRun
from acc.server.session_relay.hub import Hub
from acc.server.session_relay.runtime import RuntimeManager
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import RuntimeAttestation
from acc.session_relay.runtime_peer import RuntimePeer


@pytest.fixture
async def runtime_case(tmp_path, monkeypatch):
    import acc.broker.relay_runtime as module
    case = setup(tmp_path)
    account = pwd.getpwuid(os.getuid()).pw_name
    workspace = tmp_path / 'work'
    workspace.mkdir()
    exe = tmp_path / 'codex-fixture'
    exe.write_text('fixture executable pin; never executed')
    exe.chmod(0o700)
    monkeypatch.setattr(module, 'CLI_DIGEST', p.sha256_digest(exe.read_bytes()))
    monkeypatch.setattr(type(providers.get('codex')), 'bootstrap', lambda *_args, **_kwargs: None)
    owners = []
    class Owner:
        alive = True
        def __init__(self, spec):
            self.attestation = RuntimeAttestation(spec.binding.binding_id, spec.run_id, '', 'fixture-incarnation')
            self.calls = []
            self.exit = asyncio.Event()
            self.exit_code = 0
            async def wait():
                await self.exit.wait()
                return self.exit_code
            self.runtime = SimpleNamespace(process=SimpleNamespace(wait=wait), exit_proven=False)
        @classmethod
        async def start(cls, root, spec, executable, env, observe, **_options):
            owner = cls(spec)
            owners.append(owner)
            return owner
        async def request(self, method, params):
            self.calls.append((method, params))
            if method == 'thread/start':
                return {'thread': {'id': 'fixture-native-id'},'sandbox':{'type':'workspaceWrite',
                    'writableRoots':[],'excludeSlashTmp':True,'excludeTmpdirEnvVar':True}}
            if method == 'turn/start':
                return {'turn': {'id': 'fixture-turn-' + str(sum(call[0] == 'turn/start' for call in self.calls)), 'status': 'inProgress'}}
            return {}
        async def notify(self, *_):
            pass
        async def stop(self):
            self.runtime.exit_proven = True  # explicit fixture receipt, no native claim
            self.alive = False
            self.exit.set()
    monkeypatch.setattr(module, 'CodexRuntimeOwner', Owner)
    config = BrokerConfig(socket_path=tmp_path / 'b.sock', run_dir=tmp_path / 'runs',
                          allowed_roots=(workspace,), allowed_uids=frozenset({os.getuid()}),
                          providers={'codex': str(exe)})
    config.run_dir.mkdir(mode=0o700)
    factory = RuntimeTransportFactory(config, owned_view_factory=None)
    broker = Broker(config, relay_transport=factory)
    server = await asyncio.start_unix_server(broker.handle, path=str(config.socket_path), limit=1 << 20)
    brokers = Brokers(config.socket_path, None, account, 'fixture-services')
    case.ctx.broker = brokers
    delivery = SimpleNamespace(env={}, catalog=None, check_env=lambda _: None)
    case.ctx.supervisor = SimpleNamespace(catalog=None, vault=SimpleNamespace(deliver=lambda *_args, **_kwargs: delivery,
        redactor=lambda *_args: SimpleNamespace(text=lambda text: text)))
    @write_intent
    def reset():
        with case.db() as db:
            db.delete(db.get(RelayRun, 1))
            db.delete(db.get(RelaySession, 'relay'))
            db.delete(db.get(SessionRun, 1))
            db.flush()
            session = db.get(Session, 'relay')
            session.id = 'abc123def456'
            session.run_dir = str(config.run_dir / session.id)
            session.run_as, session.workdir = account, str(workspace)
            session.launch_spec.files = {'guardrails.json': '{}'}
            session.launch_spec.inputs = {'provider': 'codex'}
            db.commit()
    reset()
    manager = RuntimeManager(case.ctx)
    launch = manager.new_launch(case.resolve)
    @write_intent
    def reserve():
        with case.db() as db:
            launch.reserve(db, db.get(Session, 'abc123def456'))
            db.commit()
    reserve()
    case.manager, case.launch, case.owners, case.factory = manager, launch, owners, factory
    yield case
    await manager.close()
    server.close()
    await server.wait_closed()
    await brokers.close()
    # Broker EOF cleanup can finish after the client transport closes.
    for _ in range(100):
        if not factory.active:
            break
        await asyncio.sleep(.01)
    assert not factory.active
    case.engine.dispose()


async def test_person_launch_enrolls_one_native_owner_without_qualifying_text_or_terminal(runtime_case):
    case = runtime_case
    with case.db() as db:
        assert db.get(RelaySession, 'abc123def456').current_run_id == case.launch.run_id
        assert db.get(RelayRun, case.launch.run_id) is None  # pending enrollment fences legacy starts
    await case.launch.launch('abc123def456')
    with case.db() as db:
        row = db.get(RelaySession, 'abc123def456')
        run = db.get(RelayRun, row.current_run_id)
        assert run.identity['binding']['native_conversation'] == 'fixture-native-id'
        assert db.get(SessionRun, run.run_id).verified_at
        assert row.capabilities == {}
    assert len(case.owners) == 1
    assert [call[0] for call in case.owners[0].calls] == ['initialize', 'thread/start']
    assert 'abc123def456' in case.manager.runs


async def test_opaque_grant_stages_private_bytes_preserves_full_identity_and_settles(runtime_case):
    case = runtime_case
    await case.launch.launch('abc123def456')
    state = next(iter(case.factory.active.values()))
    state.adapter.record = replace(state.adapter.record, qualification_id='fixture-only', qualification_date='2026-11-10', operations={
        'text': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('test fake only',))})
    params = {'sessionId': 'abc123def456', 'expectedRunId': str(case.launch.run_id), 'text': 'private fixture prompt',
              'expectedConfigurationRevision': 0, 'expectedControlRevision': 1, 'ifBusy': 'queue'}
    registered = Hub(case.ctx).register(case.resolve, 'abc123def456', str(case.launch.run_id), 'grant',
        'turn.submit', p.operation_digest('turn.submit', params))
    receipt = await case.manager.submit(case.resolve, 'abc123def456', {**params, 'operationId': registered.operation_id})
    assert receipt.state is p.ReceiptState.ACCEPTED
    assert receipt.actor == case.resolve()[1] and receipt.payload_digest == registered.receipt.payload_digest
    assert state.journal.lookup(receipt.operation_id)[0] == 'accepted'
    assert not case.launch.grants
    assert list(state.staging.root.glob('*.payload')) == []
    assert case.owners[0].calls[-1][0] == 'turn/start'
    with case.db() as db:
        assert 'private fixture prompt' not in str(db.get(RelayRun, case.launch.run_id).identity)


async def test_authority_loss_during_native_probe_never_reaches_turn_start(runtime_case):
    case = runtime_case
    await case.launch.launch('abc123def456')
    state = next(iter(case.factory.active.values()))
    state.adapter.record = replace(state.adapter.record, qualification_id='fixture-only', qualification_date='2026-11-10', operations={
        'text': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('test fake only',))})
    probe = state.adapter.probe_native
    async def revoke(binding):
        result = await probe(binding)
        case.state['revoked'] = True
        return result
    state.adapter.probe_native = revoke
    params = {'sessionId': 'abc123def456', 'expectedRunId': str(case.launch.run_id), 'text': 'fixture',
              'expectedConfigurationRevision': 0, 'expectedControlRevision': 1, 'ifBusy': 'reject'}
    registered = Hub(case.ctx).register(case.resolve, 'abc123def456', str(case.launch.run_id), 'revocation',
        'turn.submit', p.operation_digest('turn.submit', params))
    receipt = await case.manager.submit(case.resolve, 'abc123def456', {**params, 'operationId': registered.operation_id})
    assert receipt.state is p.ReceiptState.FAILED
    assert not any(method == 'turn/start' for method, _ in case.owners[0].calls)


async def test_peer_eof_invalidates_pending_authority_without_retry(tmp_path):
    peers = []
    entered = asyncio.Event()
    release = asyncio.Event()
    async def handle(method, params):
        entered.set()
        await release.wait()
        return True
    async def accept(reader, writer):
        peer = RuntimePeer(reader, writer, handle)
        peers.append(peer)
    server = await asyncio.start_unix_server(accept, path=str(tmp_path / 'peer.sock'))
    reader, writer = await asyncio.open_unix_connection(str(tmp_path / 'peer.sock'))
    client = RuntimePeer(reader, writer, handle)
    task = asyncio.create_task(client.call('authority.verify', {}))
    await entered.wait()
    peers[0].invalidate()
    with pytest.raises(ConnectionError):
        await task
    assert client.closed
    release.set()
    await client.close()
    await peers[0].close()
    server.close()
    await server.wait_closed()


async def test_real_cookie_http_registration_and_send_use_current_transaction_auth(runtime_case, monkeypatch):
    import httpx
    from fastapi import FastAPI

    from acc.server import auth
    from acc.server.api.context import Context
    from acc.server.api.session_relay import router
    from acc.server.models import User

    case = runtime_case
    await case.launch.launch('abc123def456')
    state = next(iter(case.factory.active.values()))
    state.adapter.record = replace(state.adapter.record, qualification_id='fixture-only', qualification_date='2026-11-10',
        operations={'text': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('test fake only',))})
    from test_session_relay_writer_routes import retire
    state.writers.retire = retire  # artificial physical retirement, never placement qualification
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    settings = replace(case.ctx.settings, cookie_secure=False, allowed_origins=('http://fixture',))
    ctx = Context(settings, case.db, case.ctx.supervisor, case.ctx.broker, None, 'fixture-api-token')
    ctx.relay_runtime = case.manager
    case.manager.ctx = case.launch.ctx = ctx
    @write_intent
    def login():
        with case.db() as db:
            token, csrf = auth.create_session(db, db.get(User, 1), 'fixture')
            db.get(RelaySession, 'abc123def456').capabilities = {'text': 'qualified', 'queue': 'qualified', 'terminal.input': 'qualified'}
            db.commit()
            return token, csrf
    token, csrf = login()
    app = FastAPI()
    app.include_router(router(ctx))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
            headers={'Origin': 'http://fixture', 'X-ACC-CSRF': csrf}, cookies={ctx.session_cookie: token}) as client:
        async def call(method, params):
            return await client.post('/api/sessions/abc123def456/relay', json={
                'jsonrpc': '2.0', 'id': 'fixture', 'method': 'ry/' + method, 'params': params})
        initialized = (await call('initialize', {'major': 1, 'minor': 0, 'features': ['queue', 'writer'], 'clientId': 'browser'})).json()
        client.headers['X-ACC-Relay-Connection'] = initialized['result']['connectionToken']
        attached = await call('session.attach', {'sessionId': 'abc123def456', 'clientId': 'browser', 'connectionSeq': 1})
        assert 'result' in attached.json(), attached.text
        take = {'sessionId': 'abc123def456', 'expectedRunId': str(case.launch.run_id), 'view': 'chat',
            'reason': 'Person chooses initial Chat input', 'expectedRevision': 1,
            'attachmentId': attached.json()['result']['attachmentId']}
        initial = await call('operation.register', {'sessionId': 'abc123def456', 'expectedRunId': str(case.launch.run_id),
            'registrationKey': 'http-initial-chat', 'method': 'writer.take', 'payloadDigest': p.operation_digest('writer.take', take)})
        taken = await call('writer.take', {**take, 'operationId': initial.json()['result']['operationId']})
        assert taken.json()['result']['state'] == 'accepted', taken.text
        params = {'sessionId': 'abc123def456', 'expectedRunId': str(case.launch.run_id), 'text': 'private HTTP fixture',
                  'expectedConfigurationRevision': 0, 'expectedControlRevision': 2, 'ifBusy': 'queue'}
        registered = await call('operation.register', {'sessionId': 'abc123def456', 'expectedRunId': str(case.launch.run_id),
            'registrationKey': 'http', 'method': 'turn.submit', 'payloadDigest': p.operation_digest('turn.submit', params)})
        assert 'result' in registered.json(), registered.text
        submitted = await call('turn.submit', {**params, 'operationId': registered.json()['result']['operationId']})
        assert 'result' in submitted.json(), submitted.text
        assert submitted.json()['result']['state'] == 'accepted', submitted.text
        assert submitted.json()['result']['actor']['principal'] == 'alex'
        assert len([call for call in case.owners[0].calls if call[0] == 'turn/start']) == 1


async def test_person_new_session_opt_in_reaches_production_factory_and_stop(api, project, env, runtime_case, monkeypatch):
    from pathlib import Path

    from test_auth import cookie_client, do_setup

    import acc.broker.relay_runtime as module

    # Reuse the fixture owner with the real API's separately owned broker. Every
    # CLI, account home, database, hook and socket remains inside these fixtures.
    async def fixture_view_start(view, authorize):
        assert await authorize() is True
        # This API fixture has no native CLI. Placement has its isolated proof.
    monkeypatch.setattr(module.OwnedTUI, 'start', fixture_view_start)
    monkeypatch.setenv('ACC_FEATURE_SESSION_RELAY', 'true')
    monkeypatch.setattr(module, 'CLI_DIGEST', p.sha256_digest(Path(env['broker'].providers['codex']).read_bytes()))
    original = pwd.getpwuid(os.getuid())
    account = env['settings'].default_account
    monkeypatch.setattr(module.pwd, 'getpwuid', lambda _: pwd.struct_passwd((
        account, original.pw_passwd, original.pw_uid, original.pw_gid,
        original.pw_gecos, str(env['home']), original.pw_shell)))
    await do_setup(api, env)
    client = cookie_client(api)
    headers = {'Origin': 'http://acc', 'X-ACC-CSRF': client.cookies.get('acc_csrf')}
    body = {'project_id': project['id'], 'provider': 'codex', 'name': 'Personal relay',
            'session_relay': True, 'worktree': False}
    preview = await client.post('/api/sessions/preview', json=body, headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()['command'][1:4] == ['app-server', '--listen', 'stdio://']
    created = await client.post('/api/sessions', json=body, headers=headers)
    assert created.status_code == 201, created.text
    identifier = created.json()['id']
    with api.app.state.db() as db:
        row = db.get(RelaySession, identifier)
        assert row is not None and not row.sealed
        run = db.get(RelayRun, row.current_run_id)
        assert run.identity['binding']['native_conversation'] == 'fixture-native-id'
        assert db.get(SessionRun, run.run_id).verified_at is not None
        assert row.capabilities == {}  # enrollment grants no unqualified input
    assert len(runtime_case.owners) == 1
    stopped = await client.post(f'/api/sessions/{identifier}/stop', headers=headers)
    assert stopped.status_code == 200, stopped.text
    assert not runtime_case.owners[0].alive


async def test_queued_payload_is_broker_private_and_fifo_dispatches_after_native_completion(runtime_case):
    case = runtime_case
    await case.launch.launch('abc123def456')
    state = next(iter(case.factory.active.values()))
    state.adapter.record = replace(state.adapter.record, qualification_id='fixture-only', qualification_date='2026-11-10',
        operations={'text': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('test fake only',))})
    async def submit(key):
        params = {'sessionId': 'abc123def456', 'expectedRunId': str(case.launch.run_id), 'text': key,
                  'expectedConfigurationRevision': 0, 'expectedControlRevision': 1, 'ifBusy': 'queue'}
        registered = Hub(case.ctx).register(case.resolve, 'abc123def456', str(case.launch.run_id), key,
            'turn.submit', p.operation_digest('turn.submit', params))
        return await case.manager.submit(case.resolve, 'abc123def456', {**params, 'operationId': registered.operation_id})
    first = await submit('first-private-message')
    second = await submit('queued-private-message')
    assert first.state is p.ReceiptState.ACCEPTED and second.state is p.ReceiptState.QUEUED
    assert len(case.launch.grants) == 1
    assert next(iter(case.launch.grants.values())).operation.params == {}
    assert len(list(state.staging.root.glob('*.payload'))) == 1
    await state.events.put({'method': 'turn/completed', 'params': {
        'threadId': 'fixture-native-id', 'turn': {'id': state.adapter.turns[first.operation_id], 'status': 'completed'}}})
    for _ in range(200):
        if len([call for call in case.owners[0].calls if call[0] == 'turn/start']) == 2 and not case.launch.grants:
            break
        await asyncio.sleep(.01)
    assert len([call for call in case.owners[0].calls if call[0] == 'turn/start']) == 2
    assert not case.launch.grants
    assert list(state.staging.root.glob('*.payload')) == []


async def test_native_resume_projects_frozen_settings_and_fresh_history_without_resume(runtime_case):
    case = runtime_case
    await case.launch.launch('abc123def456')
    state = next(iter(case.factory.active.values()))
    state.native_start = {'thread': {'id': 'fixture-native-id', 'turns': []}, 'model': 'frozen-model',
                          'approvalPolicy': 'untrusted'}
    async def read(method, params):
        assert method == 'thread/read'
        assert params == {'threadId': 'fixture-native-id', 'includeTurns': True}
        return {'thread': {'id': 'fixture-native-id', 'turns': [{'id': 'current-history'}]}}
    state.owner.request = read
    result = await state.native_read('thread/resume', {'threadId': 'fixture-native-id'})
    assert result['model'] == 'frozen-model'
    assert result['approvalPolicy'] == 'untrusted'
    assert result['thread']['turns'] == [{'id': 'current-history'}]
    assert state.native_start['thread']['turns'] == []


async def test_reviewed_launch_composes_owned_view_and_host_before_start(runtime_case):
    case = runtime_case
    views = []
    class View:
        @staticmethod
        def available():
            return True
        def __init__(self, root, config, spec, executable, native_socket, *, digest):
            self.native_socket = native_socket
            self.closed = False
            views.append(self)
        async def open(self, run):
            raise AssertionError('Launch must not attach or claim input')
        def accepts(self, websocket):
            return False
        def bootstrapped(self):
            return not self.closed  # artificial process boundary only
        async def start(self, authorize):
            assert self.native_socket.writer_route is not None
            assert await authorize() is True
        async def close(self):
            self.closed = True
            return True
    case.factory.owned_view_factory = View
    await case.launch.launch('abc123def456')
    state = next(iter(case.factory.active.values()))
    assert state.owned_view is views[0]
    assert state.terminal_host is not None
    assert state.terminal_host.attested is False
    with case.db() as db:
        assert db.get(RelaySession, 'abc123def456').capabilities == {}
    await case.manager.close()
    for _ in range(100):
        if views[0].closed:
            break
        await asyncio.sleep(.01)
    assert views[0].closed


@pytest.mark.parametrize('method', ['thread/read', 'thread/resume', 'thread/turns/list'])
async def test_cold_native_history_uses_actual_start_only_before_any_turn(runtime_case, method):
    case = runtime_case
    await case.launch.launch('abc123def456')
    state = next(iter(case.factory.active.values()))
    state.native_start = {'thread': {'id': 'fixture-native-id', 'turns': [], 'historyMode': 'legacy'}, 'model': 'frozen'}
    state.owner.turn_attempted = False
    async def unavailable(*_):
        from acc.session_relay.adapters.codex import NativeRejection
        raise NativeRejection()
    state.owner.request = unavailable
    result = await state.native_read(method, {'threadId': 'fixture-native-id', 'includeTurns': True})
    assert result == ({'data': [], 'nextCursor': None, 'backwardsCursor': None} if method == 'thread/turns/list'
                      else state.native_start if method == 'thread/resume' else {'thread': state.native_start['thread']})
    state.owner.turn_attempted = True
    from acc.session_relay.adapters.codex import NativeRejection
    with pytest.raises(NativeRejection):
        await state.native_read(method, {'threadId': 'fixture-native-id', 'includeTurns': True})


async def test_unknown_first_native_turn_permanently_disables_empty_history_snapshot():
    from acc.broker.session_relay import CodexRuntimeOwner
    spec = SimpleNamespace(binding=SimpleNamespace(binding_id='binding', native_conversation='native'), run_id='run')
    runtime = SimpleNamespace(spec=spec, token='owner', validate=lambda: None)
    async def lost(*_, before_write):
        assert await before_write() is True
        raise ConnectionError('receipt lost')
    owner = CodexRuntimeOwner(runtime, SimpleNamespace(request=lost))
    with pytest.raises(ConnectionError):
        await owner.request('turn/start', {'threadId': 'native'})
    assert owner.turn_attempted is True


async def test_native_message_projection_keeps_user_role_and_scopes_reused_item_ids_to_turn(runtime_case):
    from acc.server.models import RelayEvent
    c = runtime_case
    await c.launch.launch('abc123def456')
    state = next(iter(c.factory.active.values()))
    for turn, text in [('first-turn', 'first native text'), ('second-turn', 'second native text')]:
        await state.events.put({'method': 'item/completed', 'params': {
            'threadId': 'fixture-native-id', 'turnId': turn,
            'item': {'id': 'item-1', 'type': 'userMessage', 'content': [{'type': 'text', 'text': text}]}}})
    for _ in range(100):
        with c.db() as db:
            messages = [row.event['payload'] for row in db.query(RelayEvent).all()
                        if row.event['kind'] == 'message.upsert']
        if len(messages) == 2:
            break
        await asyncio.sleep(.01)
    assert [value['text'] for value in messages] == ['first native text', 'second native text']
    assert all(value['role'] == 'user' for value in messages)
    assert len({value['itemId'] for value in messages}) == 2


@pytest.mark.parametrize('code,outcome,status,signal,state', [
    (0, 'normal', 0, None, 'stopped'),
    (7, 'abnormal', 7, None, 'recovery_needed'),
    (-15, 'abnormal', None, 15, 'recovery_needed'),
    (None, 'unknown', None, None, 'recovery_needed'),
])
async def test_owned_exit_preserves_status_and_truthful_replay(runtime_case, code, outcome, status, signal, state):
    from sqlalchemy import select

    from acc.server.models import RelayEvent
    from acc.server.session_relay import history

    c = runtime_case
    await c.launch.launch('abc123def456')
    c.owners[0].exit_code = code
    c.owners[0].exit.set()
    for _ in range(500):
        with c.db() as db:
            native = db.get(SessionRun, c.launch.run_id)
            ended = native.ended_at is not None
        if ended:
            break
        await asyncio.sleep(.01)
    assert ended
    with c.db() as db:
        row = db.get(RelaySession, 'abc123def456')
        native = db.get(SessionRun, c.launch.run_id)
        assert row.sealed
        assert (native.exit_status, native.exit_signal) == (status, signal)
        assert native.end_reason == 'native_exit_' + outcome
        assert db.get(Session, row.session_id).observed_state == state
        events = db.scalars(select(RelayEvent).where(RelayEvent.session_id == row.session_id)).all()
        assert any(event.event['payload'].get('exitOutcome') == outcome for event in events)
        _, actor = c.resolve()
        replay = history.History(b'x' * 32).page(db, row, 'fixture', actor=actor, mask=lambda text: text)
        assert any(event['payload'].get('exitOutcome') == outcome for event in replay['events'])
    assert len(c.owners) == 1


@pytest.mark.parametrize('params', [
    {'kind': 'exited', 'returnCode': True},
    {'kind': 'exited', 'returnCode': '0'},
    {'kind': 'exited', 'returnCode': 1 << 31},
    {'kind': 'exited', 'returnCode': 0, 'untrusted': True},
])
async def test_malformed_exit_evidence_cannot_seal_or_claim_success(runtime_case, params):
    c = runtime_case
    await c.launch.launch('abc123def456')
    with pytest.raises(p.RelayError):
        c.launch.publication(params)
    with c.db() as db:
        assert not db.get(RelaySession, 'abc123def456').sealed
        assert db.get(SessionRun, c.launch.run_id).ended_at is None


async def test_late_unknown_settlement_observes_existing_uncertain_receipt(runtime_case):
    c = runtime_case
    await c.launch.launch('abc123def456')
    params = {'sessionId': c.launch.session_id, 'expectedRunId': str(c.launch.run_id),
        'turnId': 'synthetic-target', 'expectedControlRevision': 1}
    registration = Hub(c.ctx).register(c.resolve, c.launch.session_id, str(c.launch.run_id), 'uncertain-observation',
        'turn.interrupt', p.operation_digest('turn.interrupt', params))
    token = c.launch.issue(c.resolve, registration.operation_id, params)
    @write_intent
    def fenced():
        from acc.server.session_relay import store
        with c.db() as db:
            row = store.lock(db, c.launch.session_id)
            op = store.operation(db, row.session_id, registration.operation_id, c.resolve()[1])
            store.transition(db, row, op, p.ReceiptState.SUBMITTING)
            receipt = store.transition(db, row, op, p.ReceiptState.UNCERTAIN)
            db.commit()
            return receipt
    previous = fenced()
    result = c.launch.settle(token, {'outcome': 'unknown'})
    assert result == previous and result.state is p.ReceiptState.UNCERTAIN
    assert not c.launch.grants
    assert not any(method == 'turn/interrupt' for method, _ in c.owners[0].calls)


@pytest.mark.parametrize('invalid', ['confirmation', 'deadline'])
async def test_registration_must_still_be_admissible_before_any_broker_staging(runtime_case, invalid):
    from datetime import timedelta

    from acc.server.models import RelayOperation, utcnow
    c = runtime_case
    await c.launch.launch('abc123def456')
    params = {'sessionId': 'abc123def456', 'expectedRunId': str(c.launch.run_id), 'text': 'Never stage this registration',
              'expectedConfigurationRevision': 0, 'expectedControlRevision': 1, 'ifBusy': 'reject'}
    registered = Hub(c.ctx).register(c.resolve, 'abc123def456', str(c.launch.run_id), 'pre-stage-invalid',
        'turn.submit', p.operation_digest('turn.submit', params))

    @write_intent
    def revoke():
        with c.db() as db:
            operation = db.get(RelayOperation, registered.operation_id)
            if invalid == 'confirmation':
                operation.confirmed = False
            else:
                operation.deadline = utcnow() - timedelta(seconds=1)
            db.commit()
    revoke()
    with pytest.raises(p.RelayError) as refused:
        await c.manager.submit(c.resolve, 'abc123def456', {**params, 'operationId': registered.operation_id})
    assert refused.value.code is p.ErrorCode.STALE_AUTHORITY
    state = next(iter(c.factory.active.values()))
    assert state.journal.lookup(registered.operation_id) is None
    assert list(state.staging.root.glob('*.payload')) == []
    assert not c.launch.grants
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)


@pytest.mark.parametrize('outcome', ['unknown', 'accepted'])
async def test_disconnect_seals_receipt_before_dispatch_settlement(runtime_case, outcome):
    """EOF and late dispatch completion share one original receipt/revision."""
    from acc.server.db import write_in_thread
    from acc.server.models import RelayOperation
    case = runtime_case
    await case.launch.launch('abc123def456')
    broker = next(iter(case.factory.active.values()))
    broker.adapter.record = replace(broker.adapter.record, qualification_id='fixture-only',
        qualification_date='2026-11-12', operations={
            'text': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('test fake only',))})
    params = {'sessionId': 'abc123def456', 'expectedRunId': str(case.launch.run_id),
        'text': 'one original EOF settlement', 'expectedConfigurationRevision': 0,
        'expectedControlRevision': 1, 'ifBusy': 'reject'}
    registered = Hub(case.ctx).register(case.resolve, 'abc123def456', str(case.launch.run_id),
        'disconnect-settlement', 'turn.submit', p.operation_digest('turn.submit', params))
    original = case.launch.peer.call
    sealed = []
    async def disconnect(method, payload):
        if method != 'dispatch':
            return await original(method, payload)
        result = await original(method, payload) if outcome == 'accepted' else None
        await write_in_thread(case.launch.fence_lost)
        with case.db() as db:
            sealed.append(dict(db.get(RelayOperation, registered.operation_id).receipt))
        if outcome == 'unknown':
            raise ConnectionError('exact barrier: disconnected before original dispatch returns')
        return result
    case.launch.peer.call = disconnect
    receipt = await case.manager.submit(case.resolve, 'abc123def456',
        {**params, 'operationId': registered.operation_id})
    assert sealed[0]['state'] == 'uncertain'
    with case.db() as db:
        stored = db.get(RelayOperation, registered.operation_id).receipt
        assert db.get(RelaySession, 'abc123def456').sealed
        if outcome == 'unknown':
            assert stored == sealed[0] and receipt.state is p.ReceiptState.UNCERTAIN
        else:
            assert stored['state'] == 'accepted' and stored['revision'] == sealed[0]['revision'] + 1
            assert receipt.state is p.ReceiptState.ACCEPTED
    assert not case.launch.grants
    assert sum(method == 'turn/start' for method, _ in case.owners[0].calls) == (outcome == 'accepted')


def test_security_a4_feature_off_refuses_allow_before_grant_resolution(tmp_path, monkeypatch):
    import time

    from acc.server.config import Settings
    from acc.server.session_relay.runtime import Launch
    monkeypatch.setenv('ACC_FEATURE_SESSION_RELAY', 'false')
    launch = Launch.__new__(Launch)
    launch.ctx = SimpleNamespace(settings=Settings(data_dir=tmp_path))
    launch.grants = {'allow': SimpleNamespace(operation=SimpleNamespace(
        method='decision.reply', params={'choiceId': 'allow'}, deadline_ms=int(time.time()*1000)+10000))}
    def forbidden(*args, **kwargs):
        raise AssertionError('feature-off allow reached authority/database work')
    launch.ctx.db = forbidden
    with pytest.raises(p.RelayError) as error:
        launch.resolve_grant('allow')
    assert error.value.code is p.ErrorCode.UNSUPPORTED
