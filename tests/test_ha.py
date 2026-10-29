"""Manual authority changes must fence old SQL writers, including raw SQL."""
import pytest
from sqlalchemy import text

from acc.server.db import make_engine, migrate


def test_epoch_fences_raw_sql_and_orm_transactions(tmp_path):
    from acc.server.ha import Authority, Fenced

    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    first, second = make_engine(url), make_engine(url)
    a, b = Authority(first, 'main-a'), Authority(second, 'main-b')
    a.install_write_guard()
    b.install_write_guard()
    try:
        assert a.status()['role'] == 'standby'
        with pytest.raises(Fenced), first.begin() as connection:
            connection.execute(text("INSERT INTO setting (key, value, updated_at) VALUES ('blocked', '{}', CURRENT_TIMESTAMP)"))
        a.promote(expected_epoch=0)
        with first.begin() as connection:
            connection.execute(text("INSERT INTO setting (key, value, updated_at) VALUES ('allowed', '{}', CURRENT_TIMESTAMP)"))
        with pytest.raises(Fenced):
            b.promote(expected_epoch=0)
        a.demote(expected_epoch=1)
        b.promote(expected_epoch=2)
        assert b.status()['epoch'] == 3
        with pytest.raises(Fenced), first.begin() as connection:
            connection.execute(text("DELETE FROM setting WHERE key='allowed'"))
        with second.connect() as connection:
            assert connection.execute(text("SELECT key FROM setting WHERE key='allowed'")).scalar() == 'allowed'
    finally:
        first.dispose()
        second.dispose()


def test_ha_requires_postgres_and_identity(env):
    from dataclasses import replace

    from acc.server.app import create_app

    with pytest.raises(ValueError, match='PostgreSQL'):
        create_app(replace(env['settings'], ha_node_id='main-a'))


def test_stale_epoch_is_never_reacquired_on_restart(tmp_path):
    from acc.server.ha import Authority, Fenced

    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    a = Authority(engine, 'main-a')
    a.promote(expected_epoch=0)
    restarted = Authority(engine, 'main-a')
    assert restarted.status()['role'] == 'standby'
    with pytest.raises(Fenced):
        restarted.check()
    engine.dispose()


def test_transfer_still_accepts_a_fresh_schema(tmp_path):
    from acc.db_transfer import copy_database, open_source
    source = tmp_path / 'source.db'
    target = tmp_path / 'target.db'
    migrate(f'sqlite:///{source}')
    migrate(f'sqlite:///{target}')
    engine = make_engine(f'sqlite:///{target}')
    with open_source(source) as src, engine.begin() as dst:
        assert copy_database(src, dst)['ha_authority']['rows'] == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_standby_never_opens_a_broker_socket(tmp_path):
    from acc.server.broker_client import Brokers
    from acc.server.ha import Authority, Fenced

    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    authority = Authority(engine, 'main-a')
    brokers = Brokers(tmp_path / 'absent.sock', None, 'alex', '', authority=authority)
    with pytest.raises(Fenced):
        await brokers.call('ping', host='local')
    authority.promote(expected_epoch=0)
    with pytest.raises(Fenced, match='execution node'):
        await brokers.call('launch', host='local')
    authority.demote(expected_epoch=1)
    engine.dispose()


@pytest.mark.asyncio
async def test_gate_refuses_standby_effects_and_serves_health(tmp_path):
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from acc.server.ha import Authority
    from acc.server.ha_runtime import Gate

    effects = []
    async def effect(request):
        effects.append('acted')
        return JSONResponse({'ok': True})
    async def health(request):
        return JSONResponse({'ok': True})
    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    authority = Authority(engine, 'main-a')
    app = Gate(Starlette(routes=[Route('/api/effect', effect, methods=['GET', 'POST']),
                                Route('/api/health', health)]), authority=authority)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acc') as client:
        assert (await client.get('/api/health')).status_code == 200
        assert (await client.get('/api/effect')).status_code == 503
        assert (await client.post('/api/effect')).status_code == 503
        assert effects == []
        authority.promote(expected_epoch=0)
        assert (await client.post('/api/effect')).status_code == 200
        authority.demote(expected_epoch=1)
        assert (await client.post('/api/effect')).status_code == 503
        assert effects == ['acted']
    engine.dispose()


@pytest.fixture(params=['sqlite-boundary', 'postgresql'])
def ha_apps(request, tmp_path, env, monkeypatch):
    """SQLite exercises app wiring without a driver; PG verifies actual lock semantics."""
    from dataclasses import replace

    from acc.server import app as module
    if request.param == 'postgresql':
        url = request.getfixturevalue('postgres_url')
        engine = make_engine(url)
        with engine.begin() as connection:
            connection.exec_driver_sql('DROP SCHEMA public CASCADE')
            connection.exec_driver_sql('CREATE SCHEMA public')
        engine.dispose()
    else:
        url = 'postgresql+psycopg://unused.example.lan/test'
        local_url = f'sqlite:///{tmp_path}/shared.db'
        monkeypatch.setattr(module, 'make_engine', lambda _, **options: make_engine(local_url))
        monkeypatch.setattr(module, 'migrate', lambda _: migrate(local_url))
    apps = []
    for name in ('main-a', 'main-b'):
        data = tmp_path / name
        (data / 'secrets').mkdir(parents=True)
        (data / 'secrets' / 'api_token').write_text('example-owner-token')
        settings = replace(env['settings'], data_dir=data, database_url=url, ha_node_id=name)
        apps.append(module.create_app(settings))
    yield apps
    for app in apps:
        authority = app.state.ha.authority
        authority.release()
        authority.control.dispose()
        authority.engine.dispose()


@pytest.mark.asyncio
async def test_two_apps_manual_handover_and_stale_sql(ha_apps):
    import httpx

    from acc.server.ha import Fenced
    a, b = ha_apps
    headers = {'Authorization': 'Bearer example-owner-token'}
    async with (a.router.lifespan_context(a), b.router.lifespan_context(b),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url='http://acc', headers=headers) as ca,
                httpx.AsyncClient(transport=httpx.ASGITransport(app=b), base_url='http://acc', headers=headers) as cb):
        assert (await ca.get('/api/ha/status')).json()['role'] == 'standby'
        assert (await cb.post('/api/projects', json={})).status_code == 503
        assert (await ca.post('/api/ha/promote', json={'expected_epoch': 0})).status_code == 200
        assert (await ca.get('/api/ha/status')).json()['role'] == 'active'
        assert (await cb.post('/api/ha/promote', json={'expected_epoch': 1})).status_code == 409
        assert (await ca.post('/api/ha/demote', json={'expected_epoch': 1})).status_code == 200
        assert (await cb.post('/api/ha/promote', json={'expected_epoch': 2})).status_code == 200
        assert (await ca.get('/api/health')).json()['ha']['role'] == 'standby'
        assert (await cb.get('/api/health')).json()['ha']['role'] == 'active'
        with pytest.raises(Fenced), a.state.db.kw['bind'].begin() as connection:
            connection.execute(text("DELETE FROM setting"))
        assert (await ca.post('/api/ha/promote', json={'expected_epoch': 0})).status_code == 409
        assert (await cb.post('/api/ha/demote', json={'expected_epoch': 3})).status_code == 200
        assert (await ca.post('/api/ha/promote', json={'expected_epoch': 4})).status_code == 200


@pytest.mark.asyncio
async def test_ha_controls_require_owner(ha_apps):
    import httpx
    app = ha_apps[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acc') as client:
        assert (await client.post('/api/ha/promote', json={'expected_epoch': 0})).status_code == 401
        assert (await client.post('/api/ha/demote', json={'expected_epoch': 0})).status_code == 401
        assert (await client.get('/api/ha/status')).status_code == 401


def test_postgres_promotion_refuses_inflight_work_and_stale_snapshot(postgres_url):
    from acc.server.ha import Authority, Fenced
    engine = make_engine(postgres_url)
    with engine.begin() as connection:
        connection.exec_driver_sql('DROP SCHEMA public CASCADE')
        connection.exec_driver_sql('CREATE SCHEMA public')
    migrate(postgres_url)
    a, b = Authority(engine, 'main-a'), Authority(engine, 'main-b')
    a.install_write_guard()
    try:
        a.promote(expected_epoch=0)
        # Keep a repeatable-read application snapshot from the old epoch.
        stale = engine.connect()
        stale.execute(text('SELECT epoch FROM ha_authority'))
        with a.activity():
            with pytest.raises(Fenced, match='draining'):
                a.demote(expected_epoch=1)
        a.demote(expected_epoch=1)
        b.promote(expected_epoch=2)
        # Spoof only process memory: the database still refuses its old incarnation.
        a.epoch, a.accepting, a.leader = 1, True, a.control.connect()
        with pytest.raises(Fenced):
            stale.execute(text("INSERT INTO setting (key,value,updated_at) VALUES ('stale','{}',CURRENT_TIMESTAMP)"))
        stale.close()
        with a.control.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM setting WHERE key='stale'")).scalar() == 0
    finally:
        a.release()
        b.release()
        a.control.dispose()
        b.control.dispose()
        engine.dispose()


def test_crash_recovery_requires_explicit_fence_confirmation(tmp_path):
    from acc.server.ha import Authority, Fenced
    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    a, b = Authority(engine, 'main-a'), Authority(engine, 'main-b')
    a.promote(expected_epoch=0)
    a.release()  # process stopped: durable holder stays, unlike clean demotion
    with pytest.raises(Fenced, match='independently fenced'):
        b.promote(expected_epoch=1)
    b.promote(expected_epoch=1, old_main_fenced=True)
    assert b.status()['epoch'] == 2
    b.release()
    a.control.dispose()
    b.control.dispose()
    engine.dispose()


def test_transitions_record_the_owner_and_epoch(tmp_path):
    from acc.server.ha import Authority
    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    a = Authority(engine, 'main-a')
    a.promote(expected_epoch=0, actor='alex')
    a.demote(expected_epoch=1, actor='alex')
    with engine.connect() as connection:
        assert connection.execute(text("SELECT actor, action FROM audit_log ORDER BY id")).all() == [
            ('alex', 'ha.promote'), ('alex', 'ha.demote')]
    a.control.dispose()
    engine.dispose()


def test_cluster_database_cannot_restart_without_ha_mode(ha_apps):
    from dataclasses import replace

    from acc.server.app import create_app
    with pytest.raises(ValueError, match='ACC_HA_NODE_ID'):
        create_app(replace(ha_apps[0].state.ctx.settings, ha_node_id=''))


def test_enrolled_node_reconnects_after_owner_handover(ha_apps):
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from starlette.websockets import WebSocketDisconnect

    from acc.server.models import Node

    a, b = ha_apps
    key = Ed25519PrivateKey.generate()
    with Session(a.state.ha.authority.control) as db:
        db.add(Node(id='abcdef', name='worker', status='active',
                    public_key=base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(),
                    info={}, role='always_on'))
        db.commit()
    headers = {'Authorization': 'Bearer example-owner-token'}
    def authenticate(ws):
        challenge = ws.receive_json()
        signature = key.sign(base64.b64decode(challenge['nonce']) + challenge['primary'].encode())
        ws.send_json({'t': 'hello', 'node_id': 'abcdef', 'signature': base64.b64encode(signature).decode(),
                      'accounts': ['alex'], 'info': {'os': 'linux'}})
        assert ws.receive_json() == {'t': 'ready'}
    with TestClient(a, headers=headers) as ca, TestClient(b, headers=headers) as cb:
        assert ca.post('/api/ha/promote', json={'expected_epoch': 0}).status_code == 200
        with ca.websocket_connect('/api/nodes/connect') as old:
            authenticate(old)
            assert ca.post('/api/ha/demote', json={'expected_epoch': 1}).status_code == 200
            # Any frame until the close: the link's follow-up may send a binary frame first, which receive_json can't
            # read (KeyError 'text' under load).
            while (message := old.receive())['type'] != 'websocket.close':
                assert message['type'] == 'websocket.send'
        assert cb.post('/api/ha/promote', json={'expected_epoch': 2}).status_code == 200
        with cb.websocket_connect('/api/nodes/connect') as replacement:
            authenticate(replacement)
            assert cb.get('/api/nodes').json()[0]['connected'] is True
        with pytest.raises(WebSocketDisconnect), ca.websocket_connect('/api/nodes/connect'):
            pass


@pytest.mark.asyncio
async def test_standby_also_fences_preview_gateway(ha_apps):
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ha_apps[0]),
                                 base_url='http://preview-1-8000.example.com') as client:
        assert (await client.get('/__acc_preview/start')).status_code == 503


@pytest.mark.asyncio
async def test_only_promoted_main_initializes_owner_setup(ha_apps):
    import httpx
    a, b = ha_apps
    assert not a.state.ctx.settings.setup_token_path.exists()
    assert not b.state.ctx.settings.setup_token_path.exists()
    headers = {'Authorization': 'Bearer example-owner-token'}
    async with (a.router.lifespan_context(a),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url='http://acc', headers=headers) as client):
        assert (await client.post('/api/ha/promote', json={'expected_epoch': 0})).status_code == 200
        assert a.state.ctx.settings.setup_token_path.is_file()
        assert not b.state.ctx.settings.setup_token_path.exists()


@pytest.mark.parametrize("loss", ["close", "invalidate"])
def test_lost_leadership_connection_reports_standby_and_cannot_write(tmp_path, loss):
    from acc.server.ha import Authority, Fenced
    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    authority = Authority(engine, 'main-a')
    authority.install_write_guard()
    authority.promote(expected_epoch=0)
    getattr(authority.leader, loss)()
    assert authority.status()['role'] == 'standby'
    with pytest.raises(Fenced), engine.begin() as connection:
        connection.execute(text('DELETE FROM setting'))
    authority.release()
    authority.control.dispose()
    engine.dispose()


@pytest.mark.asyncio
async def test_transition_database_failure_is_sanitized(ha_apps, monkeypatch):
    import httpx
    from sqlalchemy.exc import SQLAlchemyError
    app = ha_apps[0]
    def unavailable():
        raise SQLAlchemyError('private database diagnostics')
    monkeypatch.setattr(app.state.ha.authority.control, 'connect', unavailable)
    headers = {'Authorization': 'Bearer example-owner-token'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url='http://acc', headers=headers) as client:
        result = await client.post('/api/ha/promote', json={'expected_epoch': 0})
        assert result.status_code == 503
        assert 'private database' not in result.text


@pytest.mark.asyncio
async def test_authority_is_rechecked_after_waiting_for_request_input(tmp_path):
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from acc.server.ha import Authority
    from acc.server.ha_runtime import Gate
    effects = []
    async def effect(request):
        await request.body()
        effects.append('acted')
        return JSONResponse({'ok': True})
    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    authority = Authority(engine, 'main-a')
    authority.promote(expected_epoch=0)
    async def body():
        authority.leader.close()
        yield b'command'
    app = Gate(Starlette(routes=[Route('/api/effect', effect, methods=['POST'])]), authority=authority)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://acc') as client:
        assert (await client.post('/api/effect', content=body())).status_code == 503
        assert effects == []
    authority.release()
    authority.control.dispose()
    engine.dispose()


@pytest.mark.asyncio
async def test_active_ha_reconciles_remote_work_without_local_broker(env, broker, monkeypatch):
    from types import SimpleNamespace

    from acc.server.broker_client import BrokerClient, Brokers
    from acc.server.db import make_sessionmaker
    from acc.server.ha import Authority
    from acc.server.models import LaunchSpec, Project, Session
    from acc.server.nodes import NodeHub
    from acc.server.supervisor import Supervisor
    url = env['settings'].db_url
    migrate(url)
    engine = make_engine(url)
    db = make_sessionmaker(engine)
    authority = Authority(engine, 'main-a')
    authority.install_write_guard()
    authority.promote(expected_epoch=0)
    hub = NodeHub()
    hub.links['worker'] = SimpleNamespace(closed=False)
    brokers = Brokers(env['settings'].broker_socket, None, 'agent', '', hub=hub, authority=authority)
    # Replace only node-channel transport with the real isolated broker's Unix
    # socket; keep authority, dispatch and reconciliation behavior intact.
    client = BrokerClient(env['settings'].broker_socket, authority=authority)
    monkeypatch.setattr(brokers, '_remote_client', lambda account, host: client)
    supervisor = Supervisor(env['settings'], db, brokers)
    assert brokers.accounts() == []
    assert supervisor._models_stale() is False
    with db() as connection:
        project = Project(slug='remote', name='Remote', host='worker', account='agent', workspace_path=str(env['workspaces'] / 'demo'))
        spec = LaunchSpec(provider='claude', inputs={}, files={}, digest='example')
        connection.add(Session(id='remote-stop', name='Remote', project=project, launch_spec=spec,
                               provider='claude', tmux_name='acc-remote', run_as='agent',
                               desired_state='stopped', team_stop_pending=True))
        connection.commit()
    try:
        await supervisor.reconcile()
        with db() as connection:
            assert connection.get(Session, 'remote-stop').team_stop_pending is False
    finally:
        await client.close()
        authority.release()
        authority.control.dispose()
        engine.dispose()


@pytest.mark.asyncio
async def test_connection_monitor_serializes_with_owner_handover(ha_apps):
    import asyncio

    import httpx
    app = ha_apps[0]
    runtime = app.state.ha
    headers = {'Authorization': 'Bearer example-owner-token'}
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acc', headers=headers) as client):
        assert (await client.post('/api/ha/promote', json={'expected_epoch': 0})).status_code == 200
        async with runtime.transition:
            workers = list(runtime.tasks)
            # Assert before SQLAlchemy physically closes the DBAPI handle. An
            # unlocked fault injection could close SQLite during a worker check
            # and crash Python instead of producing a useful test failure.
            from sqlalchemy import event
            @event.listens_for(runtime.authority.control, 'invalidate', once=True)
            def invalidation_is_serialized(dbapi_connection, connection_record, exception):
                assert runtime.authority.lock._is_owned(), 'Leader invalidation must hold Authority.lock'
            # transition serializes async handover/monitor tasks, not worker
            # threads using the retained leader in Authority.check().
            with runtime.authority.lock:
                runtime.authority.leader.invalidate()
            await asyncio.sleep(1.2)
            # The monitor must not race an owner's drain/epoch transaction.
            assert runtime.tasks == workers
        for _ in range(30):
            if not runtime.tasks:
                break
            await asyncio.sleep(0.1)
        assert not runtime.tasks


@pytest.mark.asyncio
async def test_promoted_workers_do_not_inherit_api_write_transactions(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from acc.server.db import WRITE_INTENT
    from acc.server.ha import Authority
    from acc.server.ha_runtime import Runtime
    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    authority = Authority(engine, 'main-a')
    authority.promote(expected_epoch=0)
    intents = []
    async def work():
        intents.append(WRITE_INTENT.get())
        with engine.connect() as connection:
            assert connection.execute(text('SELECT 1')).scalar() == 1
    from acc.server.config import Settings
    from acc.server.nodes import NodeHub
    ctx = SimpleNamespace(settings=Settings(), supervisor=SimpleNamespace(run=work), updates=SimpleNamespace(run=work),
                          backup_jobs=SimpleNamespace(run=work),
                          broker=SimpleNamespace(hub=NodeHub()))
    runtime = Runtime(authority, ctx)
    token = WRITE_INTENT.set(True)  # promote is a mutating API request
    try:
        runtime.start()
    finally:
        WRITE_INTENT.reset(token)
    await asyncio.gather(*runtime.tasks)
    assert intents == [False, False, False]
    authority.release()
    authority.control.dispose()
    engine.dispose()


def test_login_last_seen_is_read_only_on_standby_and_refreshes_on_active(tmp_path):
    from datetime import timedelta

    from sqlalchemy.orm import Session as DbSession

    from acc.server import auth
    from acc.server.db import make_sessionmaker
    from acc.server.ha import Authority
    from acc.server.models import AuthSession, User
    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    authority = Authority(engine, 'main-a')
    authority.install_write_guard()
    db = make_sessionmaker(engine)
    db.configure(info={'ha_authority': authority})
    previous = auth.now() - timedelta(hours=1)
    with DbSession(authority.control) as connection:
        user = User(username='alex', password_hash='example', totp_secret='example', role='owner')
        connection.add(AuthSession(token_hash=auth.digest('example-cookie'), csrf='example', user=user,
                                   last_seen_at=previous, expires_at=auth.now() + timedelta(days=1)))
        connection.commit()
    try:
        with db() as connection:
            auth.lookup_session(connection, 'example-cookie')
            connection.commit()
            assert connection.get(AuthSession, auth.digest('example-cookie')).last_seen_at == previous
        authority.promote(expected_epoch=0)
        with db() as connection:
            auth.lookup_session(connection, 'example-cookie')
            connection.commit()
            assert connection.get(AuthSession, auth.digest('example-cookie')).last_seen_at > previous
    finally:
        authority.release()
        authority.control.dispose()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('method, outcome, blocked', [('launch', 'cancel', True), ('launch', 'timeout', True),
                                                       ('ping', 'cancel', False)])
async def test_uncertain_command_delivery_refuses_clean_handover(tmp_path, method, outcome, blocked):
    import asyncio
    import json

    from acc.server.broker_client import BrokerClient, BrokerUnavailable
    from acc.server.ha import Authority, Fenced
    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    a = Authority(engine, 'main-a')
    a.promote(expected_epoch=0)
    delivered, finish = asyncio.Event(), asyncio.Event()
    async def handle(reader, writer):
        message = json.loads(await reader.readline())
        assert message['method'] == method
        delivered.set()
        await finish.wait()
        writer.close()
        await writer.wait_closed()
    path = tmp_path / 'broker.sock'
    server = await asyncio.start_unix_server(handle, path=str(path))
    client = BrokerClient(path, authority=a, timeout=0.05 if outcome == 'timeout' else 60)
    task = asyncio.create_task(client.call(method))
    try:
        await asyncio.wait_for(delivered.wait(), 2)
        if outcome == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(BrokerUnavailable):
                await task
        if blocked:
            with pytest.raises(Fenced, match='uncertain'):
                a.demote(expected_epoch=1)
            assert a.status()['uncertain'] is True
            a.release()  # independently fenced/stopped before recovery, never a clean handover
            b = Authority(engine, 'main-b')
            assert b.status()['uncertain'] is True
            with pytest.raises(Fenced):
                b.promote(expected_epoch=1)
            b.promote(expected_epoch=1, old_main_fenced=True)
            assert b.status()['uncertain'] is False
            b.release()
            b.control.dispose()
        else:
            a.demote(expected_epoch=1)
    finally:
        finish.set()
        await client.close()
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0)
        a.release()
        a.control.dispose()
        engine.dispose()


@pytest.mark.asyncio
async def test_inflight_publish_execution_requires_fenced_recovery(ha_apps):
    import asyncio

    import httpx
    app = ha_apps[0]
    headers = {'Authorization': 'Bearer example-owner-token'}
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acc', headers=headers) as client):
        assert (await client.post('/api/ha/promote', json={'expected_epoch': 0})).status_code == 200
        # A helper invocation whose outcome has not returned. Only the external
        # wait is substituted; demotion and its authority transaction are real.
        pending = asyncio.create_task(asyncio.Event().wait())
        app.state.publisher.tasks.add(pending)
        try:
            result = await client.post('/api/ha/demote', json={'expected_epoch': 1})
            assert result.status_code == 409
            assert 'uncertain' in result.json()['detail']
            assert (await client.get('/api/ha/status')).json()['uncertain'] is True
            assert (await client.get('/api/health')).json()['ha']['uncertain'] is True
        finally:
            app.state.publisher.tasks.discard(pending)
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    # The old process has now shut down. Only explicit independently-fenced
    # recovery on the other main can clear the durable uncertainty flag.
    standby = ha_apps[1]
    async with (standby.router.lifespan_context(standby),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=standby), base_url='http://acc', headers=headers) as client):
        assert (await client.post('/api/ha/promote', json={'expected_epoch': 1})).status_code == 409
        result = await client.post('/api/ha/promote', json={'expected_epoch': 1, 'old_main_fenced': True})
        assert result.status_code == 200
        assert result.json()['uncertain'] is False
        assert result.json()['epoch'] == 2


# ---- Security review (HA phase 2): regressions for docs/reviews/2026-10-23-security-review-ha-phase-2.md


@pytest.mark.asyncio
async def test_lost_node_reply_blocks_clean_handover_but_keeps_the_active_main(tmp_path):
    import asyncio

    from acc.server.broker_client import BrokerClient, BrokerUnavailable
    from acc.server.ha import Authority, Fenced
    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    a = Authority(engine, 'main-a')
    a.install_write_guard()
    a.promote(expected_epoch=0)
    async def drop(reader, writer):
        # A stalled, restarted or hostile broker/node: it takes the command and never answers.
        await reader.readline()
        writer.close()
    path = tmp_path / 'broker.sock'
    server = await asyncio.start_unix_server(drop, path=str(path))
    client = BrokerClient(path, authority=a)
    try:
        with pytest.raises(BrokerUnavailable):
            await client.call('launch')
        # The epoch is unchanged and still held: other machines and people keep working.
        a.check()
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO setting (key, value, updated_at) VALUES ('after', '{}', CURRENT_TIMESTAMP)"))
        assert a.status()['role'] == 'active'
        assert a.status()['uncertain'] is True
        with pytest.raises(Fenced, match='uncertain'):
            a.demote(expected_epoch=1)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
        a.release()
        a.control.dispose()
        engine.dispose()


@pytest.mark.asyncio
async def test_one_request_database_error_does_not_fence_the_main(ha_apps):
    import httpx
    from sqlalchemy.exc import IntegrityError
    app = ha_apps[0]
    def conflict():
        raise IntegrityError('INSERT', {}, Exception('duplicate key'))
    app.add_api_route('/api/example-conflict', conflict, methods=['POST'])
    headers = {'Authorization': 'Bearer example-owner-token'}
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                  base_url='http://acc', headers=headers) as client):
        assert (await client.post('/api/ha/promote', json={'expected_epoch': 0})).status_code == 200
        assert (await client.post('/api/example-conflict')).status_code >= 400
        assert app.state.ha.authority.accepting is True
        assert (await client.get('/api/ha/status')).json()['role'] == 'active'


def test_startup_never_migrates_a_schema_another_main_is_using(ha_apps):
    from dataclasses import replace

    from alembic import command

    from acc.server.app import create_app
    from acc.server.db import alembic_config
    a = ha_apps[0]
    if a.state.ha.authority.postgres:
        pytest.skip('the SQLite boundary simulates an older schema; PostgreSQL covers the lock separately')
    url = str(a.state.db.kw['bind'].url)
    a.state.ha.authority.promote(expected_epoch=0)
    command.downgrade(alembic_config(url), '0045')  # this build's head is newer than the live schema
    def revision():
        with a.state.ha.authority.control.connect() as connection:
            return connection.execute(text('SELECT version_num FROM alembic_version')).scalar()
    settings = a.state.ctx.settings
    with pytest.raises(ValueError, match='holds authority'):
        create_app(replace(settings, ha_node_id='main-c', data_dir=settings.data_dir.parent / 'main-c'))
    with pytest.raises(ValueError, match='ACC_HA_NODE_ID'):
        create_app(replace(settings, ha_node_id='', data_dir=settings.data_dir.parent / 'single'))
    assert revision() == '0045'


@pytest.mark.asyncio
async def test_unauthenticated_traffic_cannot_hold_the_handover_barrier(ha_apps):
    import httpx
    app = ha_apps[0]
    authority = app.state.ha.authority
    real = authority.control
    opened = []
    class Counting:
        def __getattr__(self, name):
            return getattr(real, name)
        def connect(self):
            opened.append(1)
            return real.connect()
        def begin(self):
            return real.begin()
    authority.control = Counting()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url='http://acc') as client:
            postgres, authority.postgres = authority.postgres, True  # take the PostgreSQL barrier branch
            try:
                for _ in range(5):
                    assert (await client.post('/api/auth/login', json={})).status_code == 503
            finally:
                authority.postgres = postgres
            assert opened == []  # a standby refuses locally, never joining the cluster-wide drain lock
            for _ in range(20):
                assert (await client.get('/api/health')).status_code == 200
            assert len(opened) <= 2
    finally:
        authority.control = real


@pytest.mark.asyncio
async def test_helper_requests_recheck_authority_at_the_effect(ha_apps):
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy.orm import Session as DbSession

    from acc.server.ha import Fenced
    from acc.server.models import PublishRequest
    app = ha_apps[0]
    settings, authority = app.state.ctx.settings, app.state.ha.authority
    with DbSession(authority.control) as db:
        db.add(PublishRequest(id='pub-example', action='unpublish', host='demo.example.com',
                              requested_by='alex', decided_by='alex', status='applying'))
        db.commit()
    for folder in (settings.publish_dir, settings.packages_dir):
        (folder / 'requests').mkdir(parents=True, exist_ok=True)
    authority.promote(expected_epoch=0)
    authority.demote(expected_epoch=1)  # e.g. lost after the request's last database write
    with pytest.raises(Fenced):
        await asyncio.wait_for(app.state.publisher._run_helper('pub-example', {'action': 'remove', 'host': 'demo.example.com'}), 0.5)
    request = SimpleNamespace(id='tool-0123456789ab', manager='apt', name='jq', version=None,
                              operation='install', host='local', account='alex')
    with pytest.raises(Fenced):
        await asyncio.wait_for(app.state.supervisor.tools._carry(request), 0.5)
    assert not list((settings.publish_dir / 'requests').iterdir())
    assert not list((settings.packages_dir / 'requests').iterdir())


def test_work_from_an_earlier_epoch_of_this_process_stays_fenced(tmp_path):
    import contextvars

    from acc.server.ha import Authority, Fenced
    url = f'sqlite:///{tmp_path}/ha.db'
    migrate(url)
    engine = make_engine(url)
    a = Authority(engine, 'main-a')
    a.install_write_guard()
    try:
        a.promote(expected_epoch=0)
        with a.activity():
            earlier = contextvars.copy_context()  # e.g. a worker thread that outlived cancellation
        a.demote(expected_epoch=1)
        a.promote(expected_epoch=2)
        def stale_write():
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO setting (key, value, updated_at) VALUES ('stale', '{}', CURRENT_TIMESTAMP)"))
        with pytest.raises(Fenced):
            earlier.run(stale_write)
        stale_write()  # current-epoch work is unaffected
    finally:
        a.release()
        a.control.dispose()
        engine.dispose()


def test_authority_connections_detect_a_partition(monkeypatch):
    from types import SimpleNamespace

    from acc.server import db
    from acc.server.ha import Authority
    captured = []
    class Boundary(Exception):
        pass
    def capture(url, **kwargs):
        captured.append(kwargs.get('connect_args', {}))
        raise Boundary
    monkeypatch.setattr(db, 'create_engine', capture)
    url = 'postgresql+psycopg://db.example.lan/acc?options=-csearch_path%3Dacc'
    with pytest.raises(Boundary):
        Authority(SimpleNamespace(url=url, dialect=SimpleNamespace(name='postgresql')), 'main-a')
    with pytest.raises(Boundary):
        db.make_engine(url, keepalive=True)
    for arguments in captured:
        assert arguments['keepalives'] == 1 and arguments['tcp_user_timeout'] > 0
        assert 'tcp_keepalives_idle' in arguments['options'] and 'tcp_user_timeout' in arguments['options']
        assert '-csearch_path=acc' in arguments['options']  # an owner's own options survive
    assert 'statement_timeout' in captured[0]['options']


@pytest.mark.asyncio
async def test_half_failed_promotion_hands_authority_back(ha_apps, monkeypatch):
    import httpx

    from acc.server import auth
    a, b = ha_apps
    def broken(*args, **kwargs):
        raise OSError('example: setup token folder unavailable')
    monkeypatch.setattr(auth, 'ensure_setup_token', broken)
    headers = {'Authorization': 'Bearer example-owner-token'}
    async with (a.router.lifespan_context(a),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=a, raise_app_exceptions=False),
                                  base_url='http://acc', headers=headers) as client):
        assert (await client.post('/api/ha/promote', json={'expected_epoch': 0})).status_code == 503
        status = (await client.get('/api/ha/status')).json()
        assert status['role'] == 'standby' and status['active_node'] is None and status['epoch'] == 2
        assert not a.state.ha.tasks
    # A clean hand-back: the standby needs no crash-fencing attestation.
    b.state.ha.authority.promote(expected_epoch=2)


@pytest.mark.asyncio
@pytest.mark.parametrize('lose_at', ['entry', 'zones', 'records'])
async def test_publish_stale_epoch_refuses_before_provider_write(tmp_path, monkeypatch, lose_at):
    from dataclasses import replace

    import httpx
    from sqlalchemy.orm import Session as DbSession
    from test_publish import FakeCloudflareAPI

    from acc.server.config import Settings
    from acc.server.db import make_sessionmaker
    from acc.server.dns_providers import CloudflareProvider
    from acc.server.ha import Authority, Fenced
    from acc.server.models import PublishRequest
    from acc.server.publish import Publisher

    url = f'sqlite:///{tmp_path}/publish.db'
    migrate(url)
    engine = make_engine(url)
    authority = Authority(engine, 'main-a')
    authority.promote(expected_epoch=0)
    db = make_sessionmaker(engine)
    db.configure(info={'ha_authority': authority})
    with DbSession(engine) as s:
        s.add(PublishRequest(id='pub-stale', action='publish', host='demo.example.com',
                             port=8000, access='public', requested_by='alex',
                             context={'target_ip': '192.0.2.1'}))
        s.commit()
    remote = FakeCloudflareAPI()
    writes = []
    def lose():
        authority.demote(expected_epoch=1)
    def handler(request):
        if request.method in ('POST', 'PUT', 'DELETE'):
            writes.append(request.method)
        result = remote.handler(request)
        if (lose_at == 'zones' and request.url.path.endswith('/zones') or
                lose_at == 'records' and request.url.path.endswith('/dns_records')):
            lose()
        return result
    provider = CloudflareProvider('example-token', transport=httpx.MockTransport(handler))
    settings = replace(Settings(), data_dir=tmp_path, origin_public_ip='192.0.2.1')
    publisher = Publisher(settings, db)
    monkeypatch.setattr(publisher, 'dns_selection', lambda: (provider, None))
    if lose_at == 'entry':
        lose()
    try:
        with pytest.raises(Fenced):
            await publisher._publish('pub-stale', 'demo.example.com', 8000, 'public')
        assert writes == []
        assert remote.records == {}
    finally:
        await provider.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_evaluation_status_is_owner_audited_without_public_poll_writes(ha_apps):
    import httpx
    from sqlalchemy import select

    from acc.server.models import AuditLog

    app = ha_apps[0]
    authority = app.state.ha.authority
    def evaluations():
        with authority.control.connect() as db:
            return db.execute(select(AuditLog.detail).where(
                AuditLog.action == 'ha.automatic_evaluation')).scalars().all()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acc') as client:
        public = (await client.get('/api/health')).json()['ha']
        assert public['automatic_evaluation']['reason'] == 'disabled'
        assert evaluations() == []
        assert (await client.get('/api/ha/status')).status_code == 401
        assert evaluations() == []
        for _ in range(2):
            response = await client.get('/api/ha/status', headers={'Authorization': 'Bearer example-owner-token'})
            assert response.status_code == 200
            assert response.json()['automatic_evaluation']['would_promote'] is False
            assert response.json()['automatic_failover'] is False
        assert len(evaluations()) == 1
        assert evaluations()[0]['promotion_wired'] is False
        assert authority.status()['epoch'] == 0


@pytest.mark.asyncio
async def test_ha_routes_are_inventoried_and_refuse_members(ha_apps, monkeypatch):
    import json
    from pathlib import Path

    import httpx
    from test_ownership import all_routes

    from acc.server.api.context import Principal

    app = ha_apps[0]
    records = json.loads((Path(__file__).parent / 'ownership_inventory.json').read_text())
    classified = {(r['method'], r['path']): r for r in records if r.get('configuration') == 'ha'}
    normal = {(r['method'], r['path']) for r in records if not r.get('configuration')}
    registered = {(method, prefix + route.path) for prefix, route in all_routes(app.routes)
                  if (prefix + getattr(route, 'path', '')).startswith('/api/')
                  for method in getattr(route, 'methods', {'WS'})}
    actual = registered - normal
    assert actual == set(classified)
    monkeypatch.setattr(app.state.ctx, 'principal', lambda request: Principal('blair', 'member', 'blair-agent'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acc') as client:
        for method, path in sorted(actual):
            response = await client.request(method, path, json={'expected_epoch': 0, 'old_main_fenced': True})
            assert response.status_code == classified[(method, path)]['other_member_status'] == 403
    assert app.state.ha.authority.status()['epoch'] == 0


def test_uncertainty_status_preserves_local_latch_after_database_write_failure(tmp_path, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from acc.server.ha import Authority, Fenced

    url = f"sqlite:///{tmp_path}/uncertain.db"
    migrate(url)
    engine = make_engine(url)
    authority = Authority(engine, 'main-a')
    try:
        authority.promote(expected_epoch=0)
        assert authority.public_status()['uncertain'] is False

        def unavailable():
            raise OperationalError('UPDATE', {}, Exception('database is locked'))

        with monkeypatch.context() as patch:
            patch.setattr(authority.control, 'begin', unavailable)
            authority.mark_uncertain('inflight publish')
        with pytest.raises(Fenced, match='uncertain'):
            authority.demote(expected_epoch=1)
        assert authority.status()['uncertain'] is True
        assert authority.public_status()['uncertain'] is True
        authority.release()
        authority.promote(expected_epoch=1, old_main_fenced=True)
        assert authority.status()['uncertain'] is False
    finally:
        authority.release()
        authority.control.dispose()
        engine.dispose()
