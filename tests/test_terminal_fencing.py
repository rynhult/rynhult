"""P2 control uses the existing arbiter and rechecks after every awaited safety step."""
import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect

from acc.server.terminal_fencing import fenced_bridge
from acc.server.tty import HEADER, Writers, _frame


class Browser:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.messages = asyncio.Queue()
        self.output = bytearray()

    async def send_text(self, value):
        await self.messages.put(json.loads(value))

    async def send_bytes(self, value):
        self.output.extend(value)

    async def receive_text(self):
        value = await self.incoming.get()
        if value is None:
            raise WebSocketDisconnect()
        return json.dumps(value)

    async def close(self, **kwargs):
        await self.incoming.put(None)


class Broker:
    def __init__(self, wheel=False):
        self.wheel = wheel
        self.scrolls = []
        self.reader = asyncio.StreamReader()
        self.inputs = []
        self.sizes = []
        self.attaches = []

    async def connect(self):
        return self.reader, self

    def write(self, data):
        if data.startswith(b'{'):
            self.attaches.append(json.loads(data))
            self.reader.feed_data(json.dumps({'ok': True, 'readonly': True, 'fenced_control': True, 'wheel': self.wheel}).encode() + b'\n')
            return
        kind, length = HEADER.unpack(data[:HEADER.size])
        payload = data[HEADER.size:]
        assert len(payload) == length
        if kind == b'm':
            self.reader.feed_data(_frame(b'm', payload))
        elif kind == b'd':
            self.inputs.append(payload)
        elif kind == b'w':
            self.scrolls.append(json.loads(payload))
        elif kind == b'r':
            self.sizes.append(payload)

    async def drain(self):
        pass

    def close(self):
        pass


async def receive(ws, kind):
    while True:
        message = await asyncio.wait_for(ws.messages.get(), 2)
        if message['t'] == kind:
            return message


async def true():
    return True


@pytest.mark.parametrize('change', ['writer', 'run'])
async def test_late_input_cannot_cross_writer_epoch_or_run_after_safety_await(change):
    writers, ws, broker = Writers(), Browser(), Broker()
    safe, proceed = asyncio.Event(), asyncio.Event()
    valid = True

    async def validate():
        return valid

    async def safety():
        safe.set()
        await proceed.wait()

    task = asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
        run_id='7', rows=40, cols=120, valid=validate, on_input=safety))
    try:
        initial = await receive(ws, 'mode')
        assert initial['write'] is False and not writers.by_session
        await ws.incoming.put({'t': 'control', 'run_id': '7', 'epoch': initial['epoch'], 'take': False})
        mode = await receive(ws, 'mode')
        assert mode['write']
        await ws.incoming.put({'t': 'i', 'run_id': '7', 'epoch': mode['epoch'], 'd': 'late'})
        await asyncio.wait_for(safe.wait(), 2)
        if change == 'writer':
            writers.claim('session', object(), True)
        else:
            valid = False
        proceed.set()
        await receive(ws, 'ack')
        assert broker.inputs == []
        assert len(broker.attaches) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_observer_cannot_resize_or_type_and_stale_control_does_not_claim():
    writers, ws, broker = Writers(), Browser(), Broker()
    task = asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
        run_id='7', rows=40, cols=120, valid=true))
    try:
        initial = await receive(ws, 'mode')
        await ws.incoming.put({'t': 'r', 'run_id': '7', 'epoch': initial['epoch'], 'rows': 2, 'cols': 2})
        await ws.incoming.put({'t': 'i', 'run_id': '7', 'epoch': initial['epoch'], 'd': 'forged'})
        await receive(ws, 'ack')
        await ws.incoming.put({'t': 'control', 'run_id': 'old-run', 'epoch': initial['epoch']})
        refused = await receive(ws, 'mode')
        assert not refused['write'] and not writers.by_session
        assert broker.inputs == [] and broker.sizes == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_takeover_refuses_pending_input_then_demotes_without_reconnecting():
    writers = Writers()
    first, second, a, b = Browser(), Browser(), Broker(), Broker()
    tasks = [asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
        run_id='7', rows=40, cols=120, valid=true)) for ws, broker in [(first, a), (second, b)]]
    try:
        initial = await receive(first, 'mode')
        await receive(second, 'mode')
        await first.incoming.put({'t': 'control', 'run_id': '7', 'epoch': initial['epoch'], 'take': False})
        owned = await receive(first, 'mode')
        observer = await receive(second, 'mode')
        assert owned['write'] and not observer['write'] and observer['held']
        await first.incoming.put({'t': 'pins', 'run_id': '7', 'epoch': owned['epoch'], 'busy': True})
        await first.incoming.put({'t': 'status'})
        await receive(first, 'mode')
        take = {'t': 'control', 'run_id': '7', 'epoch': observer['epoch'], 'take': True, 'reason': 'manual_handover'}
        await second.incoming.put(take)
        refused = await receive(second, 'mode')
        assert not refused['write'] and 'pending input' in refused['reason']
        assert writers.by_session['session'] is first
        # The legacy terminal and automated delivery use this same arbiter.
        assert writers.claim('session', object(), True) == (False, None)
        assert writers.claim('session', object(), False) == (False, None)
        await first.incoming.put({'t': 'pins', 'run_id': '7', 'epoch': owned['epoch'], 'busy': False, 'partial': False})
        await first.incoming.put({'t': 'status'})
        await receive(first, 'mode')
        await second.incoming.put(take)
        granted = await receive(second, 'mode')
        assert granted['write'] and granted['epoch'] != owned['epoch']
        assert not (await receive(first, 'mode'))['write']
        await first.incoming.put({'t': 'i', 'run_id': '7', 'epoch': owned['epoch'], 'd': 'stale'})
        await receive(first, 'ack')
        assert not a.inputs and len(a.attaches) == len(b.attaches) == 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_same_real_fixture_pty_promotes_from_observer_and_submits_once(api, project, env):
    from helpers import wait_for
    from test_supervisor import launch, state_is
    other = await launch(api, project, name='other')
    await wait_for(state_is(api, other['id'], 'idle'))
    session = await launch(api, project)
    await wait_for(state_is(api, session['id'], 'idle'))
    reference = await api.get(f"/api/sessions/{session['id']}/terminal-reference")
    assert reference.status_code == 200
    assert reference.headers['cache-control'] == 'no-store'
    ref = reference.json()
    ws, writers = Browser(), Writers()
    broker = api.app.state.supervisor.broker
    task = asyncio.create_task(fenced_bridge(ws, lambda: broker.open_stream(ref['account'], ref['node']),
        session['id'], session['tmux_name'], writers, run_id=ref['runId'], rows=40, cols=120, valid=true))
    try:
        mode = await receive(ws, 'mode')
        await ws.incoming.put({'t': 'control', 'run_id': ref['runId'], 'epoch': mode['epoch'], 'take': False})
        owned = await receive(ws, 'mode')
        assert owned['write']
        from acc.broker.ops import tmux
        clients = tmux(env['broker'], 'list-clients', '-F', '#{client_tty}:#{client_flags}').stdout
        assert 'read-only' not in clients, clients
        assert tmux(env['broker'], 'list-clients', '-F', '#{session_name}').stdout.strip() == session['tmux_name']
        await ws.incoming.put({'t': 'i', 'run_id': ref['runId'], 'epoch': owned['epoch'], 'd': 'same attachment 日本\r'})
        await receive(ws, 'ack')
        path = env['run_dir'] / session['id'] / 'fake.received'
        async def received():
            return path.read_text() if path.exists() else None
        assert await wait_for(received) == 'same attachment 日本\n'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_resize_rechecks_epoch_after_authorization_await():
    writers, ws, broker = Writers(), Browser(), Broker()
    checked, proceed = asyncio.Event(), asyncio.Event()
    block = False

    async def validate():
        if block:
            checked.set()
            await proceed.wait()
        return True

    task = asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
        run_id='7', rows=40, cols=120, valid=validate))
    try:
        initial = await receive(ws, 'mode')
        await ws.incoming.put({'t': 'control', 'run_id': '7', 'epoch': initial['epoch'], 'take': False})
        owned = await receive(ws, 'mode')
        block = True
        await ws.incoming.put({'t': 'r', 'run_id': '7', 'epoch': owned['epoch'], 'rows': 10, 'cols': 20})
        await asyncio.wait_for(checked.wait(), 2)
        writers.claim('session', object(), True)
        proceed.set()
        await ws.incoming.put({'t': 'status'})
        assert not (await receive(ws, 'mode'))['write']
        assert broker.sizes == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_partial_prompt_warns_but_explicit_take_and_release_preserve_bytes_and_block_delivery():
    writers = Writers()
    first, second, a, b = Browser(), Browser(), Broker(), Broker()
    audit = []
    async def record(event):
        audit.append(event)
    tasks = [asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
        run_id='7', rows=40, cols=120, valid=true, on_control=record, actor=actor, actor_id={'alex': 101, 'blair': 202}.get(actor)))
        for ws, broker, actor in [(first, a, 'alex'), (second, b, 'blair')]]
    try:
        initial = await receive(first, 'mode'); await receive(second, 'mode')
        await first.incoming.put({'t': 'control', 'run_id': '7', 'epoch': initial['epoch']})
        owned = await receive(first, 'mode'); observer = await receive(second, 'mode')
        await first.incoming.put({'t': 'pins', 'run_id': '7', 'epoch': owned['epoch'], 'busy': False, 'partial': True})
        await first.incoming.put({'t': 'status'}); await receive(first, 'mode')
        take = {'t': 'control', 'run_id': '7', 'epoch': observer['epoch'], 'take': True, 'reason': 'controller_unavailable'}
        await second.incoming.put(take)
        assert 'partial' in (await receive(second, 'mode'))['reason'].lower()
        await second.incoming.put({**take, 'confirm_partial': True})
        granted = await receive(second, 'mode')
        assert granted['write'] and granted['partial']
        assert not (await receive(first, 'mode'))['write']
        assert audit[-1]['previous_actor'] == 'alex' and audit[-1]['previous_channel'] == 'terminal'
        assert audit[-1]['outcome'] == 'granted' and audit[-1]['new_channel'] == 'terminal'
        assert audit[-2]['outcome'] == 'refused'
        assert a.inputs == b.inputs == []  # no clearing, copying or replay on handover
        await second.incoming.put({'t': 'release', 'run_id': '7', 'epoch': granted['epoch']})
        assert (await receive(second, 'mode'))['write']
        await second.incoming.put({'t': 'release', 'run_id': '7', 'epoch': granted['epoch'], 'confirm_partial': True})
        assert not (await receive(second, 'mode'))['write']
        assert 'session' not in writers.by_session
        assert writers.claim('session', object(), False) == (False, None)
        assert writers.claim('session', object(), True, human=True)[0]
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('reason', ['', 'free text', 'x' * 501])
async def test_take_rejects_unbounded_reason_and_audits_refusal(reason):
    writers, ws, broker = Writers(), Browser(), Broker()
    audit = []
    async def record(event): audit.append(event)
    task = asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
        run_id='7', rows=40, cols=120, valid=true, actor='alex', actor_id=101, on_control=record))
    try:
        mode = await receive(ws, 'mode')
        await ws.incoming.put({'t': 'control', 'run_id': '7', 'epoch': mode['epoch'], 'take': True, 'reason': reason})
        assert not (await receive(ws, 'mode'))['write']
        assert audit[-1]['outcome'] == 'refused' and audit[-1]['reason'] is None
        assert reason not in json.dumps(audit) if reason else True
    finally:
        task.cancel(); await asyncio.gather(task, return_exceptions=True)


async def test_legacy_explicit_take_can_handover_partial_but_not_unacknowledged_input():
    from types import SimpleNamespace

    from acc.server.tty import bridge
    writers, old = Writers(), Browser()
    writers.by_session['session'] = old
    writers.partial['session'] = True
    writers.controllers[old] = SimpleNamespace(busy=True, actor='alex', channel='terminal')
    for busy, expected in [(True, False), (False, True)]:
        writers.controllers[old].busy = busy
        ws, broker, audit = Browser(), Broker(), []
        async def record(event): audit.append(event)
        task = asyncio.create_task(bridge(ws, broker.connect, 'session', 'acc-test', writers,
            want_write=True, take=True, rows=40, cols=120, take_reason='controller_unavailable',
            confirm_partial=True, actor='blair', actor_id=202, on_control=record))
        try:
            mode = await receive(ws, 'mode')
            assert mode['write'] is expected
            assert audit[-1]['outcome'] == ('granted' if expected else 'refused')
            assert audit[-1]['previous_actor'] == 'alex'
            assert audit[-1]['previous_channel'] == 'terminal'
            assert not broker.inputs
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)


def test_partial_prompt_survives_same_run_but_cannot_block_a_new_run():
    writers = Writers()
    writers.bind_run('session', '7')
    writers.partial['session'] = True
    writers.bind_run('session', '7')
    assert writers.claim('session', object(), False) == (False, None)
    writers.bind_run('session', '8')
    assert writers.claim('session', object(), False)[0]


async def legacy_bridge(writers, actor, **kwargs):
    from acc.server.tty import bridge
    ws, broker = Browser(), Broker()
    task = asyncio.create_task(bridge(ws, broker.connect, 'session', 'acc-test', writers, want_write=True,
        take=False, rows=40, cols=120, actor=actor, actor_id={'alex': 101, 'blair': 202}.get(actor), **kwargs))
    return ws, broker, task, await receive(ws, 'mode')


async def stop(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def leave_partial_input(writers, actor):
    ws, broker, task, mode = await legacy_bridge(writers, actor)
    assert mode['write'] is True
    await ws.incoming.put({'t': 'pins', 'busy': False, 'partial': True})
    await asyncio.sleep(0.01)
    assert writers.partial['session'] is True
    await stop(task)
    assert 'session' not in writers.by_session


async def test_same_person_reconnect_reclaims_with_partial_kept_and_automation_blocked():
    writers = Writers()
    await leave_partial_input(writers, 'alex')
    ws, broker, task, mode = await legacy_bridge(writers, 'alex')
    try:
        assert mode['write'] is True and mode['partial'] is True
        assert 'inspect' in mode['reason'].lower()
        assert writers.partial['session'] is True
        assert writers.claim('session', object(), True) == (False, None)  # automation never takes it
        assert not broker.inputs  # nothing cleared or replayed
    finally:
        await stop(task)


async def test_automation_stays_blocked_after_the_reclaiming_person_leaves_again():
    writers = Writers()
    await leave_partial_input(writers, 'alex')
    ws, broker, task, mode = await legacy_bridge(writers, 'alex')
    await stop(task)
    assert writers.claim('session', object(), False) == (False, None)


async def test_another_person_cannot_reclaim_someone_elses_partial_input():
    writers = Writers()
    await leave_partial_input(writers, 'alex')
    ws, broker, task, mode = await legacy_bridge(writers, 'blair')
    try:
        assert mode['write'] is False and 'partial' in mode['reason'].lower()
        assert 'session' not in writers.by_session
    finally:
        await stop(task)


async def test_partial_input_of_unknown_origin_is_not_reclaimed():
    writers = Writers()
    writers.partial['session'] = True
    ws, broker, task, mode = await legacy_bridge(writers, 'alex')
    try:
        assert mode['write'] is False and 'partial' in mode['reason'].lower()
    finally:
        await stop(task)


async def test_same_person_second_window_stays_read_only_with_the_servers_reason():
    writers = Writers()
    first, _, first_task, _ = await legacy_bridge(writers, 'alex')
    await first.incoming.put({'t': 'pins', 'busy': False, 'partial': True})
    await asyncio.sleep(0.01)
    second, _, second_task, mode = await legacy_bridge(writers, 'alex')
    try:
        assert mode['write'] is False and mode['reason']
        assert writers.by_session['session'] is first
    finally:
        await stop(second_task)
        await stop(first_task)


async def test_read_only_mode_always_carries_a_reason():
    writers = Writers()
    first, _, first_task, _ = await legacy_bridge(writers, 'alex')
    second, _, second_task, mode = await legacy_bridge(writers, 'blair')
    try:
        assert mode['write'] is False and mode['reason'] == 'another window has control'
    finally:
        await stop(second_task)
        await stop(first_task)


async def test_a_new_run_forgets_who_left_partial_input():
    writers = Writers()
    writers.bind_run('session', '7')
    await leave_partial_input(writers, 'alex')
    writers.bind_run('session', '8')
    writers.partial['session'] = True
    ws, broker, task, mode = await legacy_bridge(writers, 'alex')
    try:
        assert mode['write'] is False
    finally:
        await stop(task)


async def test_wheel_requires_current_writer_epoch_and_valid_bounded_lines():
    writers, ws, broker = Writers(), Browser(), Broker(wheel=True)
    task = asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
        run_id='7', rows=40, cols=120, valid=true))
    try:
        initial = await receive(ws, 'mode')
        assert initial['wheel'] is True
        await ws.incoming.put({'t': 'wheel', 'run_id': '7', 'epoch': initial['epoch'], 'lines': -3})
        await ws.incoming.put({'t': 'status'})
        await receive(ws, 'mode')
        assert broker.scrolls == []
        await ws.incoming.put({'t': 'control', 'run_id': '7', 'epoch': initial['epoch'], 'take': False})
        mode = await receive(ws, 'mode')
        assert mode['write']
        for lines in [True, 0, 201, -201, 1.5, '3']:
            await ws.incoming.put({'t': 'wheel', 'run_id': '7', 'epoch': mode['epoch'], 'lines': lines})
        await ws.incoming.put({'t': 'wheel', 'run_id': '7', 'epoch': mode['epoch'] - 1, 'lines': -3})
        await ws.incoming.put({'t': 'wheel', 'run_id': '7', 'epoch': mode['epoch'], 'lines': -3})
        await ws.incoming.put({'t': 'status'})
        await receive(ws, 'mode')
        assert broker.scrolls == [-3]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('fenced', [False, True])
async def test_observer_geometry_reaches_browser_without_control_or_program_resize(fenced):
    from acc.server.tty import bridge

    class GeometryBroker(Broker):
        def write(self, data):
            if data.startswith(b'{'):
                self.attaches.append(json.loads(data))
                self.reader.feed_data(b'{"ok":true,"readonly":true,"fenced_control":true,"wheel":true,"rows":40,"cols":154}\n')
            else:
                super().write(data)

    writers, ws, broker = Writers(), Browser(), GeometryBroker()
    if fenced:
        operation = fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
            run_id='7', rows=17, cols=77, valid=true)
    else:
        operation = bridge(ws, broker.connect, 'session', 'acc-test', writers,
            want_write=False, take=False, rows=17, cols=77)
    task = asyncio.create_task(operation)
    try:
        mode = await receive(ws, 'mode')
        assert (mode['write'], mode['rows'], mode['cols']) == (False, 40, 154)
        assert mode['wheel'] is True
        broker.reader.feed_data(_frame(b'g', b'{"rows":50,"cols":180}'))
        assert await receive(ws, 'geometry') == {'t': 'geometry', 'rows': 50, 'cols': 180}
        assert not writers.by_session and not broker.sizes and not broker.inputs
    finally:
        await ws.close()
        await asyncio.wait_for(task, 2)


async def fenced_leaves_partial(writers, actor):
    """A run-bound writer types without Enter (paste-to-upload) and its tab closes."""
    ws, broker = Browser(), Broker()
    task = asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
        run_id='7', rows=40, cols=120, valid=true, actor=actor, actor_id={'alex': 101, 'blair': 202}.get(actor)))
    initial = await receive(ws, 'mode')
    await ws.incoming.put({'t': 'control', 'run_id': '7', 'epoch': initial['epoch']})
    owned = await receive(ws, 'mode')
    assert owned['write'] and not owned['reclaim']
    await ws.incoming.put({'t': 'pins', 'run_id': '7', 'epoch': owned['epoch'], 'busy': False, 'partial': True})
    await ws.incoming.put({'t': 'status'}); await receive(ws, 'mode')
    await ws.incoming.put(None)
    await asyncio.wait_for(task, 2)
    assert writers.partial['session'] is True and 'session' not in writers.by_session


async def test_fenced_same_person_reclaims_own_partial_input_without_confirmation():
    writers = Writers()
    await fenced_leaves_partial(writers, 'alex')
    ws, broker = Browser(), Broker()
    task = asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
        run_id='7', rows=40, cols=120, valid=true, actor='alex', actor_id=101))
    try:
        initial = await receive(ws, 'mode')
        assert initial['write'] is False and initial['partial'] is True and initial['reclaim'] is True
        await ws.incoming.put({'t': 'control', 'run_id': '7', 'epoch': initial['epoch']})
        granted = await receive(ws, 'mode')
        assert granted['write'] is True and granted['partial'] is True and granted['reclaim'] is False
        assert broker.inputs == []  # the unsent text stays in the prompt: nothing cleared or replayed
    finally:
        await stop(task)


async def test_fenced_other_person_or_unknown_partial_still_needs_confirmation():
    writers = Writers()
    await fenced_leaves_partial(writers, 'alex')
    for actor in ['blair', None]:
        ws, broker = Browser(), Broker()
        task = asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
            run_id='7', rows=40, cols=120, valid=true, actor=actor, actor_id={'alex': 101, 'blair': 202}.get(actor)))
        try:
            initial = await receive(ws, 'mode')
            assert initial['partial'] is True and initial['reclaim'] is False
            await ws.incoming.put({'t': 'control', 'run_id': '7', 'epoch': initial['epoch']})
            refused = await receive(ws, 'mode')
            assert refused['write'] is False and 'partial' in refused['reason'].lower()
        finally:
            await stop(task)
    unknown = Writers(); unknown.partial['session'] = True  # e.g. automation: no recorded person
    ws, broker = Browser(), Broker()
    task = asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', unknown,
        run_id='7', rows=40, cols=120, valid=true, actor='alex', actor_id=101))
    try:
        assert (await receive(ws, 'mode'))['reclaim'] is False
    finally:
        await stop(task)


async def test_fenced_second_live_window_of_the_same_person_does_not_reclaim():
    writers = Writers()
    first, second, a, b = Browser(), Browser(), Broker(), Broker()
    tasks = [asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
        run_id='7', rows=40, cols=120, valid=true, actor='alex', actor_id=101)) for ws, broker in [(first, a), (second, b)]]
    try:
        initial = await receive(first, 'mode'); await receive(second, 'mode')
        await first.incoming.put({'t': 'control', 'run_id': '7', 'epoch': initial['epoch']})
        owned = await receive(first, 'mode'); observer = await receive(second, 'mode')
        await first.incoming.put({'t': 'pins', 'run_id': '7', 'epoch': owned['epoch'], 'busy': False, 'partial': True})
        await first.incoming.put({'t': 'status'}); await receive(first, 'mode')
        await second.incoming.put({'t': 'status'})
        assert (await receive(second, 'mode'))['reclaim'] is False  # the first window still holds it
        await second.incoming.put({'t': 'control', 'run_id': '7', 'epoch': observer['epoch']})
        assert (await receive(second, 'mode'))['write'] is False
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('actor', ['blair', None])
async def test_fenced_confirmed_partial_handover_records_new_controller_before_input(actor):
    writers = Writers()
    await fenced_leaves_partial(writers, 'alex')
    ws, broker = Browser(), Broker()
    task = asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
        run_id='7', rows=40, cols=120, valid=true, actor=actor, actor_id={'alex': 101, 'blair': 202}.get(actor)))
    initial = await receive(ws, 'mode')
    await ws.incoming.put({'t': 'control', 'run_id': '7', 'epoch': initial['epoch'], 'confirm_partial': True})
    granted = await receive(ws, 'mode')
    assert granted['write'] and granted['partial']
    # Disconnect without sending pins or input: only the confirmed person can
    # reclaim this prompt, and an unknown controller cannot confer that right.
    await ws.incoming.put(None)
    await asyncio.wait_for(task, 2)
    assert writers.partial['session'] is True
    assert not writers.may_reclaim('session', 101)
    assert writers.may_reclaim('session', {'blair': 202}.get(actor)) is bool(actor)
    assert broker.inputs == []


@pytest.mark.parametrize('fenced', [False, True])
async def test_recreated_username_cannot_reclaim_but_same_id_with_new_name_can(fenced):
    from acc.server.tty import bridge

    writers = Writers()
    if fenced:
        await fenced_leaves_partial(writers, 'alex')
    else:
        await leave_partial_input(writers, 'alex')
    for actor, actor_id, allowed in [('alex', 303, False), ('renamed', 101, True)]:
        ws, broker = Browser(), Broker()
        if fenced:
            operation = fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
                run_id='7', rows=40, cols=120, valid=true, actor=actor, actor_id=actor_id)
        else:
            operation = bridge(ws, broker.connect, 'session', 'acc-test', writers,
                want_write=True, take=False, rows=40, cols=120, actor=actor, actor_id=actor_id)
        task = asyncio.create_task(operation)
        try:
            mode = await receive(ws, 'mode')
            if fenced:
                assert mode['reclaim'] is allowed
                await ws.incoming.put({'t': 'control', 'run_id': '7', 'epoch': mode['epoch']})
                mode = await receive(ws, 'mode')
            assert mode['write'] is allowed
            assert mode['partial'] is True
            assert broker.inputs == []
        finally:
            await stop(task)


async def test_fenced_reconnect_audits_grant_without_partial_text_or_previous_holder():
    writers = Writers()
    await fenced_leaves_partial(writers, 'alex')
    audit = []

    async def record(event):
        audit.append(event)

    ws, broker = Browser(), Broker()
    task = asyncio.create_task(fenced_bridge(ws, broker.connect, 'session', 'acc-test', writers,
        run_id='7', rows=40, cols=120, valid=true, actor='alex', actor_id=101, on_control=record))
    try:
        mode = await receive(ws, 'mode')
        await ws.incoming.put({'t': 'control', 'run_id': '7', 'epoch': mode['epoch']})
        assert (await receive(ws, 'mode'))['write'] is True
        assert audit == [{'take': False, 'reason': 'same_person_reconnect', 'partial': True,
                          'previous_actor': None, 'previous_channel': None, 'new_channel': 'terminal',
                          'outcome': 'granted', 'code': None}]
        # The API callback supplies the authenticated display actor and session target.
        assert broker.inputs == []
    finally:
        await stop(task)
