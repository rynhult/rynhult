"""P2 observer references bind to the current process, never a restored writer."""
import asyncio
import json

import pytest
import websockets
from helpers import wait_for, write_db
from test_auth import ORIGIN, _idle, do_setup, live  # noqa: F401

from acc.server.models import Session, SessionRun
from acc.server.models import utcnow as now


async def running(api, env, project):
    await do_setup(api, env)
    api.headers.pop('Authorization', None)
    response = await api.post('/api/sessions', headers={**ORIGIN, 'X-ACC-CSRF': api.cookies.get('acc_csrf')},
                              json={'project_id': project['id'], 'provider': 'claude', 'name': 'observer'})
    assert response.status_code == 201, response.text
    sid = response.json()['id']
    await wait_for(lambda: _idle(api, sid))
    return sid


async def test_reference_is_authorized_uncached_and_has_a_real_current_run(api, env, project):
    sid = await running(api, env, project)
    response = await api.get(f'/api/sessions/{sid}/terminal-reference')
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    ref = response.json()
    with api.app.state.db() as db:
        target = db.get(Session, sid)
        assert ref == {'protocol': 'run-observer-v1', 'installation': api.app.state.ctx.terminal_instance, 'sessionId': sid, 'runId': str(target.runs[-1].id),
                       'person': 'casey', 'node': 'local', 'account': target.run_as}
    # Logout invalidates the same reference; no bearer fallback in this request.
    await api.post('/api/auth/logout', headers={**ORIGIN, 'X-ACC-CSRF': api.cookies.get('acc_csrf')})
    denied = await api.get(f'/api/sessions/{sid}/terminal-reference', headers={'Authorization': ''})
    assert denied.status_code == 401


@pytest.mark.parametrize('invalid', ['stale', 'write', 'take'])
async def test_bound_attach_refuses_stale_or_writable_requests(api, env, project, live, invalid):  # noqa: F811
    sid = await running(api, env, project)
    ref = (await api.get(f'/api/sessions/{sid}/terminal-reference')).json()
    run = ref['runId'] if invalid != 'stale' else '99999999'
    query = f'instance={ref["installation"]}&run={run}&mode={"write" if invalid == "write" else "read"}&take={1 if invalid == "take" else 0}'
    headers = {'Cookie': f'acc_session={api.cookies.get("acc_session")}', **ORIGIN}
    async with websockets.connect(f'{live}/api/sessions/{sid}/tty?{query}', additional_headers=headers) as ws:
        with pytest.raises(websockets.ConnectionClosed) as error:
            await ws.recv()
        assert error.value.rcvd.code == 4404
    assert sid not in api.app.state.ctx.writers.by_session


async def test_bound_observer_cannot_type_or_resize_and_seals_when_run_changes(api, env, project, live):  # noqa: F811
    sid = await running(api, env, project)
    ref = (await api.get(f'/api/sessions/{sid}/terminal-reference')).json()
    headers = {'Cookie': f'acc_session={api.cookies.get("acc_session")}', **ORIGIN}
    ctx = api.app.state.ctx
    ctx.terminal_recheck = .02
    async with websockets.connect(f'{live}/api/sessions/{sid}/tty?mode=read&run={ref["runId"]}&instance={ref["installation"]}', additional_headers=headers) as ws:
        mode = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert mode == {'t': 'mode', 'write': False, 'run': ref['runId'], 'instance': ref['installation'], 'protocol': 'run-observer-v1', 'rows': mode['rows'], 'cols': mode['cols']}
        await ws.send(json.dumps({'t': 'i', 'd': 'SHOULD-NOT-TYPE\r'}))
        await ws.send(json.dumps({'t': 'r', 'rows': 999, 'cols': 999}))
        assert sid not in ctx.writers.by_session
        with write_db(api.app.state.db) as db:
            old = db.get(SessionRun, int(ref['runId']))
            old.ended_at = now()
            db.add(SessionRun(session_id=sid, reason='resume', pid=old.pid))
            db.commit()
        with pytest.raises(websockets.ConnectionClosed):
            while True:
                await asyncio.wait_for(ws.recv(), 5)
    output = (await api.get(f'/api/sessions/{sid}/terminal-output')).json()['text']
    assert 'SHOULD-NOT-TYPE' not in output


async def test_run_change_while_broker_connects_refuses_before_attach(api, env, project, live, monkeypatch):  # noqa: F811
    sid = await running(api, env, project)
    ref = (await api.get(f'/api/sessions/{sid}/terminal-reference')).json()
    original = api.app.state.ctx.broker.open_stream
    captured = []

    async def connect(*args, **kwargs):
        reader, writer = await original(*args, **kwargs)
        with write_db(api.app.state.db) as db:
            old = db.get(SessionRun, int(ref['runId']))
            old.ended_at = now()
            db.add(SessionRun(session_id=sid, reason='resume', pid=old.pid))
            db.commit()
        real_write = writer.write
        def write(data):
            captured.append(data)
            real_write(data)
        monkeypatch.setattr(writer, 'write', write)
        return reader, writer

    monkeypatch.setattr(api.app.state.ctx.broker, 'open_stream', connect)
    headers = {'Cookie': f'acc_session={api.cookies.get("acc_session")}', **ORIGIN}
    async with websockets.connect(f'{live}/api/sessions/{sid}/tty?mode=read&run={ref["runId"]}&instance={ref["installation"]}', additional_headers=headers) as ws:
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 5)
    assert captured == []  # no binary detach frame on a still-JSON handshake channel


async def test_bound_observer_drops_forged_resize_and_input_frames():
    from fastapi import WebSocketDisconnect

    from acc.server.tty import Writers, bridge

    reader = asyncio.StreamReader()
    reader.feed_data(b'{"id":1,"ok":true,"readonly":true}\n')
    written = []
    class Writer:
        def write(self, data):
            written.append(data)
        async def drain(self):
            pass
        def close(self):
            pass
    class Browser:
        count = 0
        async def send_text(self, value):
            pass
        async def receive_text(self):
            self.count += 1
            if self.count == 1:
                return '{"t":"r","rows":999,"cols":999}'
            if self.count == 2:
                return '{"t":"i","d":"PRIVATE INPUT"}'
            raise WebSocketDisconnect()
    async def connect():
        return reader, Writer()
    async def authorized():
        return True
    await bridge(Browser(), connect, 'sid', 'acc-test', Writers(), False, False, 40, 120,
                 signed_in=authorized, bound_run='1')
    assert json.loads(written[0])['params']['readonly'] is True
    assert written[1:] == [b'x\x00\x00\x00\x00']


async def test_reference_cannot_be_reused_by_a_restarted_server(api, env, project, live, monkeypatch):  # noqa: F811
    sid = await running(api, env, project)
    ref = (await api.get(f'/api/sessions/{sid}/terminal-reference')).json()
    monkeypatch.setattr(api.app.state.ctx, 'terminal_instance', 'new-server-instance', raising=False)
    headers = {'Cookie': f'acc_session={api.cookies.get("acc_session")}', **ORIGIN}
    # A restarted/restored server could have the same session/run numbers.
    async with websockets.connect(f'{live}/api/sessions/{sid}/tty?mode=read&run={ref["runId"]}&instance=old-server-instance', additional_headers=headers) as ws:
        with pytest.raises(websockets.ConnectionClosed) as error:
            await ws.recv()
        assert error.value.rcvd.code == 4404


@pytest.mark.parametrize('role', ['owner', 'member'])
async def test_standalone_current_run_uses_the_same_observer_guards(api, env, project, live, role):  # noqa: F811
    from sqlalchemy import select

    from acc.server.models import User

    sid = await running(api, env, project)
    with write_db(api.app.state.db) as db:
        target = db.get(Session, sid)
        target.workdir = target.cwd
        target.project = None
        db.scalar(select(User).where(User.username == 'casey')).role = role
        db.commit()
    ref = (await api.get(f'/api/sessions/{sid}/terminal-reference')).json()
    headers = {'Cookie': f'acc_session={api.cookies.get("acc_session")}', **ORIGIN}
    async with websockets.connect(f'{live}/api/sessions/{sid}/tty?mode=read&run={ref["runId"]}&instance={ref["installation"]}', additional_headers=headers) as ws:
        mode = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert mode['write'] is False and mode['run'] == ref['runId']


async def test_observer_handshake_never_resizes_the_detached_provider(api, env, project, live, monkeypatch):  # noqa: F811
    import subprocess
    sid = await running(api, env, project)
    ref = (await api.get(f'/api/sessions/{sid}/terminal-reference')).json()
    with api.app.state.db() as db:
        name = db.get(Session, sid).tmux_name
    def tmux(*args):
        return subprocess.run(['tmux', '-L', env['broker'].tmux_socket, *args], check=True, text=True, capture_output=True).stdout.strip()
    tmux('resize-window', '-t', f'={name}:', '-x', '200', '-y', '50')
    tmux('set-window-option', '-t', f'={name}:', 'window-size', 'latest')
    before = tmux('display-message', '-p', '-t', f'={name}:', '#{pane_width}x#{pane_height}')
    import threading

    from acc.server import auth
    loop_thread = threading.get_ident()
    checked_on = []
    lookup = auth.lookup_session
    def checked(*args, **kwargs):
        checked_on.append(threading.get_ident())
        return lookup(*args, **kwargs)
    monkeypatch.setattr(auth, 'lookup_session', checked)
    cookie = '; '.join(f'{k}={v}' for k, v in api.cookies.items())
    url = live.replace('http', 'ws') + f'/api/sessions/{sid}/tty?mode=read&run={ref["runId"]}&instance={ref["installation"]}&rows=8&cols=30'
    async with websockets.connect(url, origin=ORIGIN['Origin'], additional_headers={'Cookie': cookie}) as ws:
        while isinstance(await ws.recv(), str):
            pass  # initial tmux redraw proves the native observer has attached
        assert tmux('display-message', '-p', '-t', f'={name}:', '#{pane_width}x#{pane_height}') == before
        assert any(thread != loop_thread for thread in checked_on)
        async with websockets.connect(live.replace('http', 'ws') + f'/api/sessions/{sid}/tty?mode=write',
                origin=ORIGIN['Origin'], additional_headers={'Cookie': cookie}) as controller:
            while True:
                message = await asyncio.wait_for(controller.recv(), 5)
                if isinstance(message, str) and json.loads(message).get('t') == 'mode':
                    break
            await controller.send(json.dumps({'t': 'r', 'rows': 36, 'cols': 110}))
            await wait_for(lambda: tmux('display-message', '-p', '-t', f'={name}:', '#{pane_width}x#{pane_height}') == '110x36')
        # The remaining read-only client must not become a fallback size owner.
        assert tmux('display-message', '-p', '-t', f'={name}:', '#{pane_width}x#{pane_height}') == '110x36'



@pytest.mark.parametrize('when', ['initial', 'midstream'])
async def test_reserved_remote_account_hides_the_bound_terminal(api, env, project, live, monkeypatch, when):  # noqa: F811
    from helpers import pause_supervisor
    from sqlalchemy import select

    from acc.server import group_remote
    from acc.server.models import User
    sid = await running(api, env, project)
    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        db.scalar(select(User).where(User.username == 'casey')).role = 'member'
        db.commit()
    monkeypatch.setattr(Session, 'host', property(lambda _: 'remote-example'))
    reserved = False
    monkeypatch.setattr(group_remote, 'reserved_for_other', lambda *args: reserved)
    ctx = api.app.state.ctx
    original = ctx.broker.open_stream
    original_call = ctx.broker.call
    async def local_call(method, *, account=None, host=None, **params):
        return await original_call(method, account=account, host='local', **params)
    monkeypatch.setattr(ctx.broker, 'call', local_call)
    async def local_fake(account, host):
        return await original(account, 'local')
    monkeypatch.setattr(ctx.broker, 'open_stream', local_fake)
    ctx.terminal_recheck = .05
    ref = (await api.get(f'/api/sessions/{sid}/terminal-reference')).json()
    reserved = when == 'initial'
    if reserved:
        assert (await api.get(f'/api/sessions/{sid}/terminal-reference')).status_code == 404
    cookie = '; '.join(f'{k}={v}' for k, v in api.cookies.items())
    url = live.replace('http', 'ws') + f'/api/sessions/{sid}/tty?mode=read&run={ref["runId"]}&instance={ref["installation"]}'
    async with websockets.connect(url, origin=ORIGIN['Origin'], additional_headers={'Cookie': cookie}) as ws:
        if when == 'midstream':
            while True:
                message = await asyncio.wait_for(ws.recv(), 3)
                if isinstance(message, str) and json.loads(message).get('t') == 'mode':
                    break
            reserved = True
        with pytest.raises(websockets.ConnectionClosed) as error:
            while True:
                await asyncio.wait_for(ws.recv(), 3)
        assert error.value.rcvd.code == 4404


@pytest.mark.parametrize('failure', ['old_broker', 'unbounded_geometry'])
async def test_unqualified_observer_geometry_refuses_before_opening_stream(api, env, project, live, monkeypatch, failure):  # noqa: F811
    from acc.server.broker_client import BrokerError
    sid = await running(api, env, project)
    ref = (await api.get(f'/api/sessions/{sid}/terminal-reference')).json()
    broker = api.app.state.ctx.broker
    real_call = broker.call
    async def call(method, **params):
        if method == 'terminal_observer_geometry':
            if failure == 'old_broker':
                raise BrokerError('unknown method')
            return {'geometry': 'manual-v1', 'rows': 10**9, 'cols': 10**9}
        return await real_call(method, **params)
    async def forbidden(*args):
        pytest.fail('an unqualified broker must never receive attach')
    monkeypatch.setattr(broker, 'call', call)
    monkeypatch.setattr(broker, 'open_stream', forbidden)
    headers = {'Cookie': f'acc_session={api.cookies.get("acc_session")}', **ORIGIN}
    async with websockets.connect(f'{live}/api/sessions/{sid}/tty?mode=read&run={ref["runId"]}&instance={ref["installation"]}', additional_headers=headers) as ws:
        with pytest.raises(websockets.ConnectionClosed) as error:
            await ws.recv()
        assert error.value.rcvd.code == 4404
