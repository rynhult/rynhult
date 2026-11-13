"""Bounded observer reads over production main/private broker, never input."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from test_session_relay_public_writer import attach, call, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import TerminalRun


async def read(client, params):
    response = await client.get('/api/sessions/abc123def456/relay/terminal', params=params)
    value = response.json()
    return {'result': value} if response.status_code == 200 else value


@pytest.fixture
async def read_case(public_case):  # noqa: F811
    c = public_case
    calls = []
    class Observer:
        async def capture(self, run):
            assert run == TerminalRun(c.launch.session_id, str(c.launch.run_id))
            calls.append(run)
            return {'text': 'Artificial native screen', 'truncated': False}
    c.broker_state.owned_view = Observer()
    c.read_calls = calls
    # This fake observer owns no native child; avoid teardown calling its close.
    async def close():
        return True
    c.broker_state.owned_view.close = close
    return c


async def test_public_read_unqualified_flag_off_never_mints_input_or_changes_lease(read_case):
    c = read_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'read-only')
        value = await read(client, {'attachmentId': view['attachmentId'], 'expectedRunId': str(c.launch.run_id)})
        assert value['result'] == {'runId': str(c.launch.run_id), 'text': 'Artificial native screen', 'truncated': False}
        assert (await call(client, 'session.snapshot', {}))['result']['terminalMediated'] is False
    assert c.read_calls and c.launch.terminal_grants.record is None
    assert c.broker_state.writers.gate.epoch == 1
    assert not c.launch.terminal_reads.grants
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)


async def test_revoked_observer_after_await_cannot_receive_or_replay_native_screen(read_case):
    c = read_case
    original = c.broker_state.owned_view.capture
    async def revoke(run):
        result = await original(run)
        c.state['revoked'] = True
        return result
    c.broker_state.owned_view.capture = revoke
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'revoked-read')
        value = await read(client, {'attachmentId': view['attachmentId'], 'expectedRunId': str(c.launch.run_id)})
        assert 'result' not in value and 'Artificial native screen' not in str(value)
    assert not c.launch.terminal_reads.grants


async def test_redaction_is_reloaded_after_native_read_await(read_case):
    c = read_case
    original = c.broker_state.owned_view.capture
    async def change_mask(run):
        result = await original(run)
        c.ctx.supervisor.vault.redactor = lambda *_: SimpleNamespace(text=lambda text: text.replace('Artificial', '[REDACTED]'))
        return result
    c.broker_state.owned_view.capture = change_mask
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'fresh-mask')
        value = await read(client, {'attachmentId': view['attachmentId'], 'expectedRunId': str(c.launch.run_id)})
        assert value['result']['text'] == '[REDACTED] native screen'
    assert not c.launch.terminal_reads.grants


async def test_other_attachment_and_wrong_run_never_reach_capture(read_case):
    c = read_case
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        view = await attach(client, 'scoped-read')
        for params in ({'attachmentId': 'other-tab', 'expectedRunId': str(c.launch.run_id)},
                       {'attachmentId': view['attachmentId'], 'expectedRunId': 'old-run'}):
            assert 'error' in await read(client, params)
    assert not c.read_calls


async def test_expired_read_grant_cannot_authorize_delayed_capture(read_case):
    from acc.server.session_relay.terminal_read import service
    c = read_case
    reads = service(c.launch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.broker_state.owned_view.capture
    async def blocked(run):
        entered.set()
        await release.wait()
        return await original(run)
    c.broker_state.owned_view.capture = blocked
    task = asyncio.create_task(reads.read(c.resolve, str(c.launch.run_id)))
    await asyncio.wait_for(entered.wait(), 5)
    token, grant = next(iter(reads.grants.items()))
    grant.deadline = 0
    with pytest.raises(p.RelayError):
        reads.verify(token)
    release.set()
    with pytest.raises((p.RelayError, ConnectionError)):
        await task
    assert not reads.grants


async def test_cancelled_viewer_retires_read_grant_without_fencing_execution(read_case):
    from acc.server.session_relay.terminal_read import service
    c = read_case
    reads = service(c.launch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.broker_state.owned_view.capture
    async def blocked(run):
        entered.set()
        await release.wait()
        return await original(run)
    c.broker_state.owned_view.capture = blocked
    task = asyncio.create_task(reads.read(c.resolve, str(c.launch.run_id)))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not reads.grants and len(reads.inflight) == 1
    release.set()
    await asyncio.gather(*reads.inflight, return_exceptions=True)
    await asyncio.sleep(0)
    assert not reads.inflight and not c.launch.peer.closed and not c.broker_state.peer.closed
    assert c.broker_state.writers.gate.epoch == 1


@pytest.mark.parametrize('change', ['authority', 'deadline', 'retired'])
async def test_final_redaction_refresh_cannot_outlive_read_grant(read_case, monkeypatch, change):
    """The last awaited mask refresh must retain the original observer stamp."""
    from dataclasses import replace

    from acc.server.session_relay.terminal_read import service

    c = read_case
    reads = service(c.launch)
    original_current, original_verify = reads.current, reads.verify
    final = False

    def verify(token):
        nonlocal final
        result = original_verify(token)
        # Only the main-side post-response verification starts the final refresh;
        # broker verification also calls this function while capture is pending.
        if c.read_calls and not final and all(task.done() for task in reads.inflight):
            final = True
            if change == 'deadline':
                reads.grants[token].deadline = 0
            elif change == 'retired':
                reads.grants.pop(token)
        return result

    def current(resolve):
        stamp, mask = original_current(resolve)
        if final and change == 'authority':
            stamp = replace(stamp, epoch=stamp.epoch + 1)
        return stamp, mask

    monkeypatch.setattr(reads, 'verify', verify)
    monkeypatch.setattr(reads, 'current', current)
    with pytest.raises(p.RelayError):
        await reads.read(c.resolve, str(c.launch.run_id))
    assert not reads.grants
