"""Main-side node work must finish its cancellation before lifecycle boundaries return."""
import asyncio

import pytest
from helpers import assert_no_task_leaks, task_snapshot
from test_auth import live  # noqa: F401

from acc.server.broker_client import Brokers
from acc.server.nodes import NodeHub, NodeLink


async def discard(data):
    pass


async def test_broker_shutdown_drains_attached_node_workers(tmp_path):
    with task_snapshot() as before:
        hub = NodeHub()
        link = NodeLink('id', 'lab-box', discard, {}, [])
        hub.attach(link)
        codes = []
        link.on_close = codes.append
        brokers = Brokers(tmp_path / 'unused.sock', None, 'agent', '', hub=hub)
        try:
            await brokers.close()
            assert link.closed, 'broker shutdown left its authenticated node connected'
            assert codes == [1001], 'shutdown must permit reconnect, not revoke enrollment'
        finally:
            link.close()
        await assert_no_task_leaks(before)


async def test_reconnect_storm_retires_old_work_and_removal_joins_finalizers():
    with task_snapshot() as before:
        hub = NodeHub()
        entered, cleaned = set(), set()
        async def work(index):
            entered.add(index)
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.add(index)
        try:
            for index in range(40):
                link = NodeLink(str(index), 'lab-box', discard, {}, [])
                hub.attach(link)
                owner = getattr(link, 'tasks', None)
                assert owner is not None, 'node background work has no lifecycle owner'
                owner.start(work(index), 'test-work')
                await asyncio.sleep(0)
            hub.disconnect('lab-box')
            await hub.drain()
            assert cleaned == entered == set(range(40))
        finally:
            for link in list(hub.links.values()):
                link.close()
        await assert_no_task_leaks(before)


async def test_leak_assertion_fails_and_cleans_up():
    with task_snapshot() as before:
        leaked = asyncio.create_task(asyncio.Event().wait(), name='deliberate-leak')
        with pytest.raises(AssertionError, match='deliberate-leak'):
            await assert_no_task_leaks(before)
        assert leaked.cancelled()


async def test_task_failure_is_logged_once_and_does_not_cancel_siblings(caplog):
    from acc.server.node_tasks import NodeTasks

    with task_snapshot() as before:
        owner = NodeTasks('lab-box')
        async def fail():
            raise RuntimeError('single-failure')
        sibling = owner.start(asyncio.Event().wait(), 'sibling')
        failed = owner.start(fail(), 'failure')
        await asyncio.gather(failed, return_exceptions=True)
        assert not sibling.done()
        owner.cancel()
        await owner.join()
        assert len([r for r in caplog.records if 'task node:lab-box:failure failed' in r.message]) == 1
        await assert_no_task_leaks(before)


async def test_repeated_cancellation_joins_a_running_identity_thread():
    import threading

    from acc.server.node_tasks import NodeTasks, joined_thread

    entered, release = threading.Event(), threading.Event()
    def work():
        entered.set()
        assert release.wait(5)
    with task_snapshot() as before:
        owner = NodeTasks('lab-box')
        task = owner.start(joined_thread(work), 'identity')
        try:
            await asyncio.to_thread(entered.wait, 2)
            assert entered.is_set()
            owner.cancel()
            await asyncio.sleep(0)
            task.cancel()  # cancellation can arrive from both handler and shutdown
            await asyncio.sleep(0)
            assert not task.done(), 'cancellation abandoned an in-flight mutation thread'
        finally:
            release.set()
            owner.cancel()
            await owner.join()
        assert task.cancelled()
        await assert_no_task_leaks(before)


async def test_removal_waits_for_inflight_identity_followup(api, env, live, monkeypatch):  # noqa: F811
    import json
    import threading

    from test_multihost import enroll, handshake

    from acc.server import edge

    entered, release = threading.Event(), threading.Event()
    def switched(*args):
        entered.set()
        assert release.wait(5)
        return False
    monkeypatch.setattr(edge, 'switched', switched)
    key, node_id = await enroll(api)
    ws = await handshake(live, key, node_id)
    assert json.loads(await ws.recv()) == {'t':'ready'}
    removal = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        removal = asyncio.create_task(api.post('/api/nodes/pc/remove'))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(removal), .1)
    finally:
        release.set()
        if removal:
            assert (await removal).status_code == 200
        await ws.close()


async def test_disconnect_cancels_remote_tool_job_and_records_interruption(api, project, monkeypatch):
    from helpers import pause_supervisor, write_db
    from test_tools import seed

    from acc.server.models import Session, ToolRequest

    await pause_supervisor(api)
    sid = await seed(api, project)
    with write_db(api.app.state.db) as db:
        db.get(Session, sid).placement.host = 'lab-box'
        db.commit()
    service = api.app.state.supervisor.tools
    hub = api.app.state.ctx.broker.hub
    entered = asyncio.Event()
    async def carry(request):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(service, '_carry', carry)
    link = NodeLink('id', 'lab-box', discard, {}, [])
    hub.attach(link)
    rid = service.request_session(sid, manager='npm', name='prettier', reason='Format')
    service.decide(rid, '@api', True)
    tasks = list(service.tasks)
    try:
        await entered.wait()
        hub.disconnect('lab-box')
        await hub.drain()
        assert all(task.done() for task in tasks), 'tool executor escaped its node lifecycle'
        with api.app.state.db() as db:
            assert db.get(ToolRequest, rid).status == 'failed'
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_supervisor_cancellation_joins_parallel_spool_fetches(api, project, monkeypatch):
    from helpers import pause_supervisor, write_db

    from acc.server.models import Session

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    response = await api.post('/api/sessions', json={'project_id':project['id'], 'provider':'claude', 'name':'Spools'})
    assert response.status_code == 201, response.text
    with write_db(api.app.state.db) as db:
        db.get(Session, response.json()['id']).placement.host = 'lab-box'
        db.commit()
    hub = supervisor.broker.hub
    hub.attach(NodeLink('id', 'lab-box', discard, {}, []))
    entered, finished = asyncio.Event(), asyncio.Event()
    children = []
    async def fetch(*args):
        children.append(asyncio.current_task())
        entered.set()
        try: await asyncio.Event().wait()
        finally: finished.set()
    monkeypatch.setattr(supervisor, '_fetch_host_spools', fetch)
    parent = asyncio.create_task(supervisor.fetch_remote_spools())
    try:
        await entered.wait()
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
        assert finished.is_set(), 'supervisor cancellation abandoned a per-node spool fetch'
    finally:
        for task in children: task.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        await hub.close()


async def test_disconnect_cancels_only_that_nodes_backup_push(api, project, monkeypatch):
    from helpers import pause_supervisor, write_db

    from acc.server.models import Project

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    with write_db(api.app.state.db) as db:
        db.get(Project, project['id']).host = 'lab-box'
        db.commit()
    hub = supervisor.broker.hub
    hub.attach(NodeLink('id', 'lab-box', discard, {}, []))
    entered, finished = asyncio.Event(), asyncio.Event()
    async def backup(*args):
        entered.set()
        try: await asyncio.Event().wait()
        finally: finished.set()
    monkeypatch.setattr(supervisor, 'backup_project', backup)
    parent = asyncio.create_task(supervisor._backup_round({project['id']}))
    try:
        await entered.wait()
        hub.disconnect('lab-box')
        await hub.drain()
        assert finished.is_set(), 'backup push escaped its node lifecycle'
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)


async def test_capture_busy_in_a_node_backup_job_requeues_the_round(api, project, monkeypatch, caplog):
    """Replay of capture-lock-busy onto #318: a node project's push runs as a node job (NodeHub.start), and the
    capture's MaintenanceBusy must still reach the round, which retries everything due instead of logging a failure."""
    import logging

    from helpers import pause_supervisor, write_db

    from acc.server.db import MaintenanceBusy
    from acc.server.models import Project

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    with write_db(api.app.state.db) as db:
        db.get(Project, project['id']).host = 'lab-box'
        db.commit()
    hub = supervisor.broker.hub
    hub.attach(NodeLink('id', 'lab-box', discard, {}, []))
    monkeypatch.setattr('acc.server.backups.connected', lambda broker, host: True)
    ran = []

    async def backup(project_id, force=False):
        ran.append(asyncio.current_task().get_name())
        raise MaintenanceBusy('busy')
    monkeypatch.setattr(supervisor, 'backup_project', backup)
    caplog.set_level(logging.INFO, 'acc.supervisor')
    try:
        await supervisor._backup_round({project['id']})
        assert ran == [f"node:lab-box:backup:{project['id']}"]  # it ran as that machine's job
        assert project['id'] in supervisor._backup_due and supervisor._backup_scan == -1e9
        assert any('git backup pass skipped' in r.getMessage() for r in caplog.records)
        assert not any(r.exc_info or r.levelno >= logging.WARNING for r in caplog.records)
    finally:
        hub.disconnect('lab-box')
        await hub.drain()


async def test_lifespan_exception_still_drains_node_work(env, broker):
    from acc.server.app import create_app

    app = create_app(env['settings'])
    link = None
    with task_snapshot() as before:
        try:
            with pytest.raises(RuntimeError, match='lifecycle-error'):
                async with app.router.lifespan_context(app):
                    link = NodeLink('id', 'lab-box', discard, {}, [])
                    app.state.ctx.broker.hub.attach(link)
                    raise RuntimeError('lifecycle-error')
            assert link.closed, 'exceptional lifespan exit skipped node cleanup'
        finally:
            # Keep the regression's failure isolated from later tests.
            await app.state.ctx.broker.close()
        await assert_no_task_leaks(before)


async def test_remote_identity_sweeper_is_joined_on_shutdown(monkeypatch):
    from types import SimpleNamespace

    from acc.server.remote_access import RemoteAccess

    service = RemoteAccess(SimpleNamespace())
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def sweep():
        entered.set()
        try: await asyncio.Event().wait()
        finally:
            await release.wait()
            finished.set()
    async def reconcile(): pass
    monkeypatch.setattr(service, 'sweeping', sweep)
    monkeypatch.setattr(service, 'reconcile', reconcile)
    task = asyncio.create_task(service.run())
    await entered.wait()
    task.cancel()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), .05)
    except asyncio.CancelledError:
        pytest.fail('remote-access parent exited before its sweeper cleanup')
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert finished.is_set()


async def test_authenticated_reconnect_storm_drains_all_generations(api, live):  # noqa: F811
    import json

    from helpers import wait_for
    from test_multihost import enroll, handshake

    key, node_id = await enroll(api)
    hub = api.app.state.ctx.broker.hub
    links, sockets = [], []
    try:
        for _ in range(12):
            ws = await handshake(live, key, node_id)
            sockets.append(ws)
            assert json.loads(await ws.recv()) == {'t':'ready'}
            links.append(hub.get('pc'))
        await sockets[-1].close()
        await wait_for(lambda: not hub.links and not hub.retiring)
        assert all(link.closed and not link.tasks.tasks for link in links)
        assert not [task for task in asyncio.all_tasks() if task.get_name().startswith('node:pc:')]
    finally:
        for ws in sockets:
            await ws.close()
        await hub.close()


async def test_stalled_socket_close_does_not_leave_a_receive_handler(api, live, monkeypatch):  # noqa: F811
    import json

    from helpers import wait_for
    from starlette.websockets import WebSocket
    from test_multihost import enroll, handshake

    from acc.server.api import nodes

    class ObservedLink(NodeLink):
        def __init__(self, *args, **kwargs):
            self.handler = asyncio.current_task()
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(nodes, 'NodeLink', ObservedLink)
    original = WebSocket.close
    async def stalled(socket, code=1000, reason=None):
        if code == 4401:
            await asyncio.Event().wait()
        await original(socket, code=code, reason=reason)
    monkeypatch.setattr(WebSocket, 'close', stalled)
    key, node_id = await enroll(api)
    ws = await handshake(live, key, node_id)
    assert json.loads(await ws.recv()) == {'t':'ready'}
    link = api.app.state.ctx.broker.hub.get('pc')
    try:
        assert (await api.post('/api/nodes/pc/remove')).status_code == 200
        await wait_for(link.handler.done, timeout=1)
    finally:
        await ws.close()


async def test_edge_receive_children_do_not_escape_request_cancellation():
    from types import SimpleNamespace

    from acc.server.edge import HEADER, EdgeGuard

    entered = asyncio.Event()
    async def receive():
        entered.set()
        await asyncio.Event().wait()
    async def send(message): pass
    async def app(scope, receive, send): await receive()
    ctx = SimpleNamespace(edge=SimpleNamespace(open=set(), check=lambda *args: {'kind': 'node', 'public_hostname': ''}))
    with task_snapshot() as before:
        request = asyncio.create_task(EdgeGuard(app, ctx)(
            {'type':'websocket', 'path':'/api/nodes/connect', 'headers':[(HEADER,b'a' * 64)]}, receive, send))
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await assert_no_task_leaks(before)


async def test_main_shutdown_joins_pending_publication(env, broker):
    from acc.server.app import create_app

    app = create_app(env['settings'])
    pending = None
    with task_snapshot() as before:
        try:
            async with app.router.lifespan_context(app):
                pending = asyncio.create_task(asyncio.Event().wait(), name='test-publish')
                app.state.ctx.publisher.tasks.add(pending)
            assert pending.done(), 'non-HA shutdown abandoned publication work'
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            app.state.ctx.publisher.tasks.discard(pending)
        await assert_no_task_leaks(before)


async def test_node_disconnect_drains_preview_renewal_before_request_exit(api, project):
    from helpers import pause_supervisor, write_db

    from acc.server.models import PortReservation, Project
    from acc.server.preview_remote import opened

    await pause_supervisor(api)
    ctx = api.app.state.ctx
    hub = ctx.broker.hub
    link = NodeLink('id', 'lab-box', discard, {'capabilities':['preview-v1']}, [])
    hub.attach(link)
    with write_db(ctx.db) as db:
        db.get(Project, project['id']).host = 'lab-box'
        db.add(PortReservation(port=8000, project_id=project['id'], owner='@api'))
        db.commit()
    before = set(asyncio.all_tasks())
    async with opened(ctx, project['id'], 8000, 'http'):
        await asyncio.sleep(0)
        children = set(asyncio.all_tasks()) - before
        assert children
        hub.disconnect('lab-box')
        await hub.drain()
        assert all(task.done() for task in children), 'preview renewal survived its disconnected node'


async def test_supervisor_shutdown_joins_host_snapshot_thread(api, monkeypatch):
    import threading

    from helpers import pause_supervisor

    await pause_supervisor(api)
    entered, release = threading.Event(), threading.Event()
    def snapshot():
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(api.app.state.supervisor, 'refresh_host', snapshot)
    task = asyncio.create_task(api.app.state.supervisor.run())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), .05)
        except asyncio.CancelledError:
            pytest.fail('supervisor shutdown abandoned its snapshot mutation thread')
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_root_worker_failure_is_logged_once_at_its_lifecycle_owner(env, broker, monkeypatch, caplog):
    from acc.server.app import create_app

    app = create_app(env['settings'])
    entered = asyncio.Event()
    async def fail():
        entered.set()
        raise RuntimeError('root-worker-failure')
    monkeypatch.setattr(app.state.supervisor, 'run', fail)
    async with app.router.lifespan_context(app):
        await entered.wait()
        await asyncio.sleep(0)
    records = [r for r in caplog.records if r.exc_info and str(r.exc_info[1]) == 'root-worker-failure']
    assert len(records) == 1, 'root background failure was lost or logged more than once'
