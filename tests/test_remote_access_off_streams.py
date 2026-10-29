"""Turning public access off ends public requests and streams inside accd, at once.

Caddy 2.6.2 closes upgraded WebSockets when the public route is unloaded, but keeps streaming an HTTP response
(an event stream, a long download) that started before OFF; see test_remote_access_machines. accd therefore treats
"public access off" as a reason to refuse and close, independently of the edge.
"""
import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException
from helpers import write_db
from test_passkeys import setup

from acc.server import auth, edge, remote_access
from acc.server.models import Device, Setting, User

BROWSER = 'b' * 64


def runtime(api, **value):
    with write_db(api.app.state.db) as s:
        s.merge(Setting(key='remote_access_runtime', value={**remote_access.DEFAULT, **value}))
        s.commit()


def browser_device(api):
    with write_db(api.app.state.db) as s:
        person = s.query(User).filter_by(role='owner').first().username
        s.add(Device(id=BROWSER[:32], label='Phone', kind='browser', status='active', certificate_pem='-',
                     fingerprint=BROWSER, serial=BROWSER[:20], expires_at=auth.now() + timedelta(days=1), person=person))
        s.commit()


async def status(api, fingerprint=BROWSER):
    return (await api.get('/api/devices', headers={'X-Acc-Edge-Client': fingerprint})).status_code


async def test_public_requests_are_refused_while_public_access_is_off(api, env, monkeypatch):
    await setup(api, env, monkeypatch)
    browser_device(api)
    runtime(api, desired_enabled=True, enabled=True)
    assert await status(api) == 200
    runtime(api, desired_enabled=False, enabled=True, pending=True)  # Turn off pressed, edge not yet confirmed
    assert await status(api) == 403
    runtime(api)  # the default: never enabled
    assert await status(api) == 403
    assert (await api.get('/api/devices')).status_code == 200  # private access is unaffected


async def open_public_websocket(ctx):
    async def app(scope, receive, send):
        assert (await receive())['type'] == 'websocket.connect'
        await send({'type': 'websocket.accept'})
        while (await receive())['type'] != 'websocket.disconnect':
            pass
    inbox, sent = asyncio.Queue(), []

    async def send(message):
        sent.append(message)
    scope = {'type': 'websocket', 'path': '/api/events', 'headers': [(b'x-acc-edge-client', BROWSER.encode())]}
    task = asyncio.create_task(edge.EdgeGuard(app, ctx)(scope, inbox.get, send))
    await inbox.put({'type': 'websocket.connect'})
    for _ in range(100):
        if sent:
            break
        await asyncio.sleep(0.01)
    assert sent == [{'type': 'websocket.accept'}]
    return task, sent


@pytest.mark.parametrize('helper', ['verified', 'unreachable'])
async def test_turning_off_closes_open_public_streams_without_waiting_for_the_sweep(api, env, monkeypatch, helper):
    await setup(api, env, monkeypatch)
    ctx = api.app.state.ctx
    browser_device(api)
    runtime(api, desired_enabled=True, enabled=True, last_applied_revision='r1', registry_revision='x')
    task, sent = await open_public_websocket(ctx)

    async def request(message):
        assert message == {'action': 'off'}
        if helper == 'unreachable':
            raise OSError('helper down')
        return {'enabled': False, 'hostname': '', 'last_applied_revision': 'r1', 'devices_allowed': 0,
                'last_reload_result': 'verified'}
    monkeypatch.setattr(remote_access, 'request', request)
    if helper == 'verified':
        await ctx.remote_access.change(False, 'owner')
    else:
        with pytest.raises(HTTPException):
            await ctx.remote_access.change(False, 'owner')
    # No 15-second sweep ran: the Off action itself ended the stream.
    await asyncio.wait_for(task, 2)
    assert sent[-1] == {'type': 'websocket.close', 'code': 4403}
    assert ctx.edge.open == set()


async def test_the_sweep_also_closes_public_streams_once_access_is_off(api, env, monkeypatch):
    # A failed apply or an edge restart can clear the intent outside change(); the sweep is the backstop.
    await setup(api, env, monkeypatch)
    ctx = api.app.state.ctx
    browser_device(api)
    runtime(api, desired_enabled=True, enabled=True)
    task, sent = await open_public_websocket(ctx)
    assert await ctx.edge.sweep() == 0
    runtime(api, desired_enabled=False, enabled=None, pending=True)
    assert await ctx.edge.sweep() == 1
    await asyncio.wait_for(task, 2)
    assert sent[-1] == {'type': 'websocket.close', 'code': 4403}


async def test_turning_off_does_not_wait_for_a_reconciliation_stuck_on_the_helper(api, env, monkeypatch):
    # A helper that accepts and never answers holds the reconciliation, and its lock, for up to 30 s per request
    # (minutes across status, review, apply and off). Turn off must refuse and close public use at once anyway,
    # and the reconciliation that was in flight must not enable it again.
    await setup(api, env, monkeypatch)
    ctx = api.app.state.ctx
    browser_device(api)
    runtime(api, desired_enabled=True, enabled=True, last_applied_revision='r1', registry_revision='x')
    task, sent = await open_public_websocket(ctx)
    stuck, release, actions = asyncio.Event(), asyncio.Event(), []

    async def request(message):
        actions.append(message['action'])
        if message['action'] == 'status':
            stuck.set()
            await release.wait()
            return {'enabled': True, 'last_applied_revision': 'r1'}
        if message['action'] == 'review':
            return {'revision': 'r2', 'configuration': {}, 'devices_allowed': 1}
        enabled = message['action'] == 'apply'
        return {'enabled': enabled, 'hostname': '', 'last_applied_revision': 'r2' if enabled else 'r1',
                'devices_allowed': 1 if enabled else 0, 'last_reload_result': 'verified'}
    monkeypatch.setattr(remote_access, 'request', request)
    reconciling = asyncio.create_task(ctx.remote_access.reconcile())
    await asyncio.wait_for(stuck.wait(), 2)
    off = asyncio.create_task(ctx.remote_access.change(False, 'owner'))
    await asyncio.wait_for(task, 2)  # ended while the helper still hangs
    assert sent[-1] == {'type': 'websocket.close', 'code': 4403}
    assert await status(api) == 403
    release.set()
    await asyncio.wait_for(asyncio.gather(reconciling, off), 5)
    assert 'apply' not in actions and actions[-1] == 'off'
    with api.app.state.db() as s:
        assert remote_access.state(s)['desired_enabled'] is False
    assert await status(api) == 403


async def test_turning_off_during_an_apply_wins_over_its_result_and_a_later_enable_works(api, env, monkeypatch):
    await setup(api, env, monkeypatch)
    ctx = api.app.state.ctx
    browser_device(api)
    runtime(api)
    applying, release = asyncio.Event(), asyncio.Event()

    async def request(message):
        if message['action'] == 'review':
            return {'revision': 'r2', 'configuration': {}, 'devices_allowed': 1}
        if message['action'] == 'apply':
            applying.set()
            await release.wait()
        enabled = message['action'] == 'apply'
        return {'enabled': enabled, 'hostname': '', 'last_applied_revision': 'r2' if enabled else None,
                'devices_allowed': 1 if enabled else 0, 'last_reload_result': 'verified'}
    monkeypatch.setattr(remote_access, 'request', request)
    enabling = asyncio.create_task(ctx.remote_access.change(True, 'owner', 'r2'))
    await asyncio.wait_for(applying.wait(), 2)
    off = asyncio.create_task(ctx.remote_access.change(False, 'owner'))
    for _ in range(100):
        if ctx.edge.halted:
            break
        await asyncio.sleep(0.01)
    assert await status(api) == 403  # the intent was set before the apply, and Off already refuses
    release.set()
    with pytest.raises(HTTPException):
        await asyncio.wait_for(enabling, 5)  # the verified apply is not kept: Off was pressed meanwhile
    await asyncio.wait_for(off, 5)
    with api.app.state.db() as s:
        assert remote_access.state(s)['desired_enabled'] is False and remote_access.state(s)['enabled'] is False
    assert await status(api) == 403
    release.set()
    assert (await ctx.remote_access.change(True, 'owner', 'r2'))['enabled'] is True  # the owner may enable again
    assert ctx.edge.halted is False and await status(api) == 200
