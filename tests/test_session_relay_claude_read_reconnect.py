"""Fresh screen readers do not inherit or renew a detached writer's grant."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from acc.broker import ops, relay_terminal_read
from acc.broker.relay_observer import OwnedInputAttachment, OwnedObserver
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import TerminalRun


@pytest.fixture
def reader(monkeypatch):
    run = TerminalRun('fixture', '2')
    writer = AsyncMock(return_value=False)
    observer = OwnedObserver(None, 'acc-fixture', run, writer, lambda: True)
    capture = Mock(return_value=SimpleNamespace(returncode=0, stdout='owned screen'))
    monkeypatch.setattr(ops, 'tmux', capture)
    authority = {'sessionId': run.session_id, 'runId': run.run_id, 'bindingId': 'binding', 'epoch': 2}
    runtime = SimpleNamespace(
        guard_check=Mock(), broker=SimpleNamespace(link=(SimpleNamespace(authority_epoch=2),)),
        peer=SimpleNamespace(call=AsyncMock(return_value=authority), closed=False),
        spec=SimpleNamespace(spec={'session_id': run.session_id}, run_id=run.run_id),
        binding=SimpleNamespace(provider='claude', binding_id='binding'), owned_view=None,
        owners=SimpleNamespace(observer=observer, guard_check=Mock()),
        controller=SimpleNamespace(switch=SimpleNamespace(view='terminal')))
    return SimpleNamespace(run=run, observer=observer, writer=writer, capture=capture, runtime=runtime)


async def test_new_reader_reads_existing_owner_without_renewing_writer(reader):
    c = reader
    assert await relay_terminal_read.read(c.runtime, {'grant': 'opaque-read'}) == {
        'runId': '2', 'text': 'owned screen', 'truncated': False}
    c.writer.assert_not_called()
    assert c.capture.call_count == 1
    assert all(call.args == ('authority.terminal_read', {'grant': 'opaque-read'})
               for call in c.runtime.peer.call.call_args_list)
    assert not await c.observer.current(c.run)
    with pytest.raises(p.RelayError):
        await c.observer.open(c.run)
    # The writable opener uses only its original writer grant, never a read grant.
    input_view = OwnedInputAttachment(None, 'acc-fixture', c.run, c.writer, lambda: True)
    with pytest.raises(p.RelayError):
        await input_view.open(c.run)
    assert not input_view.tasks and not c.observer.tasks


@pytest.mark.parametrize('loss', ['read', 'link', 'peer', 'owner', 'view', 'guard', 'process', 'closed'])
async def test_reader_loss_after_native_capture_withholds_screen(reader, loss):
    c = reader
    original = c.capture.return_value
    def capture(*args):
        if loss == 'read':
            c.runtime.peer.call.return_value = {**c.runtime.peer.call.return_value, 'epoch': 3}
        elif loss == 'link':
            c.runtime.broker.link = None
        elif loss == 'peer':
            c.runtime.peer.closed = True
        elif loss == 'owner':
            c.runtime.owners.observer = None
        elif loss == 'view':
            c.runtime.controller.switch.view = 'chat'
        elif loss == 'guard':
            c.runtime.owners.guard_check.side_effect = p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        elif loss == 'process':
            c.observer.same_process = lambda: False
        elif loss == 'closed':
            c.observer.closed = True
        return original
    c.capture.side_effect = capture
    with pytest.raises(p.RelayError):
        await relay_terminal_read.read(c.runtime, {'grant': 'opaque-read'})
    c.writer.assert_not_called()
    assert c.capture.call_count == 1


@pytest.mark.parametrize('loss', ['run', 'process', 'closed'])
async def test_invalid_existing_owner_is_never_captured(reader, loss):
    c = reader
    if loss == 'run':
        c.observer.run = TerminalRun('fixture', '1')
    elif loss == 'process':
        c.observer.same_process = lambda: False
    else:
        c.observer.closed = True
    with pytest.raises(p.RelayError):
        await relay_terminal_read.read(c.runtime, {'grant': 'opaque-read'})
    c.capture.assert_not_called()
    c.writer.assert_not_called()
