"""Bounded attachment wheel scheduling without live tmux sessions."""
import asyncio
import threading
import time

import pytest

from acc.broker import ops
from acc.broker.attach import AttachmentScroll


@pytest.mark.parametrize('enabled', [False, True])
async def test_plain_input_never_calls_tmux(monkeypatch, enabled):
    def unexpected(*args):
        pytest.fail('plain input must not call tmux')

    monkeypatch.setattr(ops, 'cancel_copy_mode', unexpected)
    monkeypatch.setattr(ops, 'scroll_history', unexpected)
    scroll = AttachmentScroll(None, 'test', enabled)
    for _ in range(10):
        assert await scroll.before_input()
    await scroll.close()


async def test_coalesces_limits_and_offloads(monkeypatch):
    calls = []
    cancels = []
    main_thread = threading.get_ident()

    def record(config, name, lines):
        assert threading.get_ident() != main_thread
        calls.append((time.monotonic(), lines))

    def cancel(*args):
        assert threading.get_ident() != main_thread
        cancels.append(True)

    monkeypatch.setattr(ops, 'scroll_history', record)
    monkeypatch.setattr(ops, 'cancel_copy_mode', cancel)
    scroll = AttachmentScroll(None, 'test', True)
    for _ in range(100):
        scroll.enqueue(-3)
    await scroll.task
    assert len(calls) == 1 and calls[0][1] == -200
    deadline = time.monotonic() + .12
    while time.monotonic() < deadline:
        scroll.enqueue(-1)
        await asyncio.sleep(.001)
    await scroll.task
    assert len(calls) < 8
    assert all(b[0] - a[0] >= 1 / 30 for a, b in zip(calls, calls[1:]))
    assert await scroll.before_input()
    assert await scroll.before_input()
    assert cancels == [True]
    await scroll.close()


@pytest.mark.parametrize('operation', ['scroll_history', 'cancel_copy_mode'])
async def test_transient_error_drops_frame_and_worker_survives(monkeypatch, caplog, operation):
    calls = []

    def fail_once(*args):
        calls.append(operation)
        if len(calls) == 1:
            raise ops.OpError('transient failure')

    monkeypatch.setattr(ops, 'scroll_history', lambda *args: None)
    monkeypatch.setattr(ops, 'cancel_copy_mode', lambda *args: None)
    monkeypatch.setattr(ops, operation, fail_once)
    scroll = AttachmentScroll(None, 'test', True)
    scroll.enqueue(-3)
    await scroll.task
    assert await scroll.before_input() is (operation != 'cancel_copy_mode')
    assert 'failed for test' in caplog.text
    scroll.enqueue(-3)
    await scroll.task
    assert await scroll.before_input()
    assert len(calls) == 2
    await scroll.close()


async def test_input_discards_pending_and_waits_for_inflight(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    events = []

    def slow_scroll(*args):
        entered.set()
        assert release.wait(2)
        events.append('scroll')

    monkeypatch.setattr(ops, 'scroll_history', slow_scroll)
    monkeypatch.setattr(ops, 'cancel_copy_mode', lambda *args: events.append('cancel'))
    scroll = AttachmentScroll(None, 'test', True)
    scroll.enqueue(-3)
    assert await asyncio.to_thread(entered.wait, 2)
    scroll.enqueue(-100)
    input_task = asyncio.create_task(scroll.before_input())
    await asyncio.sleep(0)
    assert not input_task.done()
    release.set()
    assert await input_task
    assert events == ['scroll', 'cancel']
    await scroll.close()


@pytest.mark.parametrize('operation', ['scroll_history', 'cancel_copy_mode'])
async def test_broker_attachment_survives_tmux_error(api, project, env, monkeypatch, operation):
    from helpers import wait_for
    from test_supervisor import launch, state_is

    from acc.broker.attach import attach, frame

    session = await launch(api, project, provider='codex')
    await wait_for(state_is(api, session['id'], 'idle'))
    reader = asyncio.StreamReader()
    attempts = []

    def fail(*args):
        attempts.append(True)
        raise ops.OpError('temporary tmux failure')

    original = getattr(ops, operation)
    monkeypatch.setattr(ops, operation, fail)

    class Writer:
        def __init__(self):
            self.ready = asyncio.Event()

        def write(self, data):
            if data.startswith(b'{'):
                self.ready.set()

        async def drain(self):
            pass

    writer = Writer()
    task = asyncio.create_task(attach(env['broker'], {
        'tmux_name': session['tmux_name'], 'readonly': False,
    }, reader, writer))
    try:
        await asyncio.wait_for(writer.ready.wait(), 5)
        reader.feed_data(frame(b'w', b'-3'))
        async def attempted():
            return bool(attempts)
        if operation == 'scroll_history':
            await wait_for(attempted)
        else:
            # Wait for the private pane to enter copy mode before sending input.
            async def in_mode():
                result = await asyncio.to_thread(ops.tmux, env['broker'], 'display-message',
                                               '-p', '-t', f"={session['tmux_name']}:", '#{pane_in_mode}')
                return result.stdout.strip() == '1'
            await wait_for(in_mode)
            reader.feed_data(frame(b'd', b'dropped'))
            await wait_for(attempted)
        assert not task.done()
        # A later frame still traverses the same attachment after the failed op.
        monkeypatch.setattr(ops, operation, original)
        reader.feed_data(frame(b'd', b'after-error\r'))
        received = env['run_dir'] / session['id'] / 'fake.received'
        async def delivered():
            return received.exists() and 'after-error' in received.read_text()
        await wait_for(delivered)
        assert not task.done()
    finally:
        reader.feed_data(frame(b'x', b''))
        await asyncio.wait_for(task, 5)


@pytest.mark.parametrize('persistent', [False, True])
async def test_observer_geometry_retries_with_backoff(api, project, env, monkeypatch, caplog, persistent):
    from helpers import wait_for
    from test_supervisor import launch, state_is

    import acc.broker.attach as attachment

    session = await launch(api, project, provider='codex')
    await wait_for(state_is(api, session['id'], 'idle'))
    original = attachment.observer_geometry
    attempts = []
    recovered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def geometry(config, name, *, pin=True):
        if pin:
            return original(config, name, pin=pin)
        attempts.append(time.monotonic())
        if persistent or len(attempts) in (1, 3):
            raise ops.OpError('temporary geometry failure')
        result = original(config, name, pin=False)
        if len(attempts) >= 4:
            loop.call_soon_threadsafe(recovered.set)
        return result

    delays = []
    original_sleep = asyncio.sleep

    async def sleep(delay):
        if asyncio.current_task().get_coro().__name__ == 'follow_geometry':
            delays.append(delay)
        await original_sleep(delay)

    monkeypatch.setattr(attachment.asyncio, 'sleep', sleep)
    monkeypatch.setattr(attachment, 'observer_geometry', geometry)
    reader = asyncio.StreamReader()

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

    task = asyncio.create_task(attachment.attach(env['broker'], {
        'tmux_name': session['tmux_name'], 'readonly': True,
    }, reader, Writer()))
    try:
        if persistent:
            await asyncio.wait_for(asyncio.shield(task), 12)
            assert len(attempts) == 5
            assert attempts[-1] - attempts[-2] >= 3.9
        else:
            await asyncio.wait_for(recovered.wait(), 6)
            assert not task.done()
            assert attempts[1] - attempts[0] >= .49
            assert attempts[2] - attempts[1] >= .24
            # Requested delays prove recovery resets backoff without a scheduler-dependent upper bound.
            assert delays[:4] == [.25, .5, .25, .5]
        assert 'Terminal observer geometry failed' in caplog.text
    finally:
        reader.feed_data(attachment.frame(b'x', b''))
        await asyncio.wait_for(task, 5)
