"""Real cookie/CSRF HTTP and private broker binding; fake CLI ownership only."""
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from test_session_relay_ownership import SID, ownership_case  # noqa: F401
from test_session_relay_public_writer import attach, call
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server import auth
from acc.server.api.context import Context
from acc.server.api.session_relay import router
from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelaySession, User
from acc.session_relay import protocol as p


@pytest.fixture
async def public_claude(ownership_case, monkeypatch):  # noqa: F811
    c = ownership_case
    c.broker_state = next(iter(c.factory.active.values()))
    ctx = Context(replace(c.ctx.settings, cookie_secure=False, allowed_origins=('http://fixture',)),
        c.db, c.ctx.supervisor, c.ctx.broker, None, 'fixture-api-token')
    ctx.relay_runtime = c.manager
    c.manager.ctx = c.launch.ctx = c.launch.owner_hub.ctx = ctx
    c.ctx = ctx
    @write_intent
    def qualify():
        with c.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified',
                'switch.to_chat': 'qualified', 'switch.to_terminal': 'qualified'}
            db.commit()
    @write_intent
    def login():
        with c.db() as db:
            token, csrf = auth.create_session(db, db.get(User, 1), 'fixture')
            db.commit()
            return token, csrf
    c.token, csrf = login()
    qualify()
    c.qualify = qualify
    c.enable = lambda: monkeypatch.setattr('acc.server.session_relay.claude_public.PUBLIC_CLAUDE_QUALIFIED', True)
    app = FastAPI()
    app.include_router(router(ctx))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
        headers={'Origin': 'http://fixture', 'X-ACC-CSRF': csrf}, cookies={ctx.session_cookie: c.token}) as client:
        c.client = client
        yield c


async def mutate(c, method, payload, key, *, status=200):
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), **payload}
    registration = await call(c.client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
        'registrationKey': key, 'method': method, 'payloadDigest': p.operation_digest(method, params)})
    assert 'result' in registration, registration
    identifier = registration['result']['operationId']
    response = await c.client.post(f'/api/sessions/{SID}/relay', json={'jsonrpc': '2.0', 'id': 'fixture-request',
        'method': 'ry/' + method, 'params': {**params, 'operationId': identifier}})
    assert response.status_code == status, response.text
    return response.json(), identifier


async def prepare(c, target='terminal', key='prepare'):
    with c.db() as db:
        revision = db.get(RelaySession, SID).revision
    return await mutate(c, 'switch.prepare', {'target': target, 'expectedRevision': revision}, key)


async def commit(c, plan, key='commit', *, status=200):
    return await mutate(c, 'switch.commit', {'planId': plan['planId'], 'planDigest': plan['planDigest'],
        'expectedRevision': plan['revision']}, key, status=status)


async def test_public_claude_stays_unavailable_with_fabricated_capabilities(public_claude):
    c = public_claude
    await attach(c.client, 'unqualified-claude', ['switch'])
    with c.db() as db:
        revision = db.get(RelaySession, SID).revision
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'target': 'terminal', 'expectedRevision': revision}
    reply = await call(c.client, 'operation.register', {'expectedRunId': str(c.launch.run_id), 'registrationKey': 'blocked',
        'method': 'switch.prepare', 'payloadDigest': p.operation_digest('switch.prepare', params)})
    assert reply['error']['data']['code'] == 'UNSUPPORTED'
    assert len(c.owned_factory.owners) == 1


async def test_http_switch_replaces_attachment_scope_and_keeps_owner_cookie_revocable(public_claude):
    c = public_claude
    c.enable()
    await attach(c.client, 'first-claude', ['switch'])
    first = c.owned_factory.current
    prepared, prepare_id = await prepare(c)
    assert 'result' in prepared, prepared
    assert first.process.returncode is None
    committed, commit_id = await commit(c, prepared['result'])
    assert 'result' in committed, committed
    assert committed['result']['view'] == 'terminal'
    assert first.process.returncode is not None and len(c.owned_factory.owners) == 2
    assert await c.broker_state.owner_authorized() is True
    # Old attachment is ended, but the durable result was not misreported lost.
    assert 'error' in await call(c.client, 'session.snapshot', {})
    for identifier in (prepare_id, commit_id):
        with c.db() as db:
            assert db.get(RelayOperation, identifier).receipt['state'] == 'accepted'
    c.qualify()
    await attach(c.client, 'second-claude', ['switch'])
    prepared, _ = await prepare(c, 'chat', 'back-prepare')
    committed, _ = await commit(c, prepared['result'], 'back-commit')
    assert committed['result']['view'] == 'chat' and len(c.owned_factory.owners) == 3
    assert await c.broker_state.owner_authorized() is True
    @write_intent
    def revoke():
        with c.db() as db:
            auth.end_session(db, c.token)
            db.commit()
    revoke()
    with pytest.raises(ConnectionError):
        await c.broker_state.owner_authorized()
    assert len(c.owned_factory.owners) == 3


async def test_http_cancel_survives_feature_off_without_restarting_source(public_claude):
    c = public_claude
    c.enable()
    await attach(c.client, 'cancel-claude', ['switch'])
    prepared, _ = await prepare(c)
    c.ctx.settings.feature_overrides['session_relay'] = False
    result, identifier = await mutate(c, 'switch.cancel', {'planId': prepared['result']['planId']}, 'cancel')
    assert result['result']['phase'] == 'owned'
    assert len(c.owned_factory.owners) == 1 and c.owned_factory.current.process.returncode is None
    with c.db() as db:
        assert db.get(RelayOperation, identifier).receipt['state'] == 'accepted'


async def test_http_other_attachment_cannot_commit_same_person_plan(public_claude):
    c = public_claude
    c.enable()
    await attach(c.client, 'holder-claude', ['switch'])
    prepared, _ = await prepare(c)
    await attach(c.client, 'other-claude', ['switch'])
    reply, _ = await commit(c, prepared['result'])
    assert reply['error']['data']['code'] == 'STALE_AUTHORITY'
    assert len(c.owned_factory.owners) == 1 and c.owned_factory.current.process.returncode is None


async def test_http_lost_commit_reply_observes_accepted_without_replay(public_claude):
    c = public_claude
    c.enable()
    await attach(c.client, 'lost-claude', ['switch'])
    prepared, _ = await prepare(c)
    original = c.launch.peer.call
    async def lose(method, params):
        result = await original(method, params)
        if method == 'owner.select':
            raise ConnectionError('Artificial lost answer after real owned checkpoint')
        return result
    c.launch.peer.call = lose
    reply, identifier = await commit(c, prepared['result'])
    assert reply['result']['receipt']['state'] == 'accepted'
    assert reply['result']['receipt']['operationId'] == identifier
    assert len(c.owned_factory.owners) == 2
    assert 'error' in await call(c.client, 'session.snapshot', {})


@pytest.mark.parametrize('withdrawal', ['capability', 'feature', 'cookie'])
async def test_http_authority_loss_at_native_prepare_preserves_source(public_claude, withdrawal):
    c = public_claude
    c.enable()
    await attach(c.client, 'withdrawn-claude', ['switch'])
    prepared, _ = await prepare(c)
    source = c.owned_factory.current
    original = source.prepare_switch
    async def withdraw():
        result = await original()
        @write_intent
        def clear():
            with c.db() as db:
                if withdrawal == 'capability':
                    db.get(RelaySession, SID).capabilities = {}
                elif withdrawal == 'feature':
                    c.ctx.settings.feature_overrides['session_relay'] = False
                else:
                    auth.end_session(db, c.token)
                db.commit()
        clear()
        return result
    source.prepare_switch = withdraw
    reply, identifier = await commit(c, prepared['result'], status=401 if withdrawal == 'cookie' else 200)
    if withdrawal == 'cookie':
        assert 'detail' in reply and 'result' not in reply
    else:
        assert reply['error']['data']['code'] == 'POLICY_REFUSED'
    assert source.process.returncode is None and len(c.owned_factory.owners) == 1
    with c.db() as db:
        assert db.get(RelayOperation, identifier).receipt['state'] == 'failed'


async def test_http_revoked_cookie_after_owned_cannot_observe_or_drive_new_owner(public_claude):
    c = public_claude
    c.enable()
    await attach(c.client, 'revoked-after-owned', ['switch'])
    prepared, _ = await prepare(c)
    original = c.launch.peer.call
    async def revoke_after_owned(method, params):
        result = await original(method, params)
        if method == 'owner.select':
            @write_intent
            def revoke():
                with c.db() as db:
                    auth.end_session(db, c.token)
                    db.commit()
            revoke()
        return result
    c.launch.peer.call = revoke_after_owned
    reply, identifier = await commit(c, prepared['result'], status=401)
    assert 'detail' in reply and 'result' not in reply
    with c.db() as db:
        assert db.get(RelayOperation, identifier).receipt['state'] == 'accepted'
    with pytest.raises(ConnectionError):
        await c.broker_state.owner_authorized()
    assert len(c.owned_factory.owners) == 2


async def test_http_target_launch_failure_after_verified_source_exit_stays_uncertain(public_claude):
    c = public_claude
    c.enable()
    await attach(c.client, 'post-stop-failure', ['switch'])
    prepared, _ = await prepare(c)
    source = c.owned_factory.current
    launches = 0
    async def fail_target(spec):
        nonlocal launches
        launches += 1
        assert source.process.returncode is not None
        raise ConnectionError('Injected target launch failure after verified source exit')
    c.owned_factory.terminal = fail_target
    reply, identifier = await commit(c, prepared['result'])
    assert reply['error']['data']['code'] == 'OUTCOME_UNKNOWN', reply
    assert reply['error']['data']['receipt']['state'] == 'uncertain'
    assert reply['error']['data']['receipt']['operationId'] == identifier
    assert launches == 1 and len(c.owned_factory.owners) == 1
    assert source.process.returncode is not None
    with c.db() as db:
        from acc.server.models import RelayOwnership
        assert db.get(RelaySession, SID).sealed
        assert db.get(RelayOwnership, SID).phase == 'recovery_required'
        assert db.get(RelayOperation, identifier).receipt['state'] == 'uncertain'
    retained = await c.client.get(f'/api/sessions/{SID}/relay/operations/{identifier}')
    assert retained.status_code == 200 and retained.json()['state'] == 'uncertain'
    again = await call(c.client, 'switch.commit', {'sessionId': SID,
        'expectedRunId': str(c.launch.run_id), 'planId': prepared['result']['planId'],
        'planDigest': prepared['result']['planDigest'], 'expectedRevision': prepared['result']['revision'],
        'operationId': identifier})
    assert 'error' in again and launches == 1


async def ownership_read(c, view):
    return await c.client.get(f'/api/sessions/{SID}/relay/ownership', params={
        'attachmentId': view['attachmentId'], 'expectedRunId': str(c.launch.run_id)})


async def test_public_idle_observation_never_prepares_or_freezes_owner(public_claude):
    c = public_claude
    c.enable()
    view = await attach(c.client, 'idle-observer', ['switch'])
    with c.db() as db:
        before = db.get(RelaySession, SID).revision
        count = db.query(RelayOperation).count()
    response = await ownership_read(c, view)
    assert response.status_code == 200, response.text
    facts = response.json()
    assert facts['ownershipRevision'] == before
    assert facts['selector'] == {'shared_live': False, 'owner': 'chat', 'may_control': True,
        'blockers': [], 'switch_state': 'owned', 'idle_verified': True}
    assert len(c.owned_factory.owners) == 1 and not c.launch.prepared_plans
    assert not c.launch.owner_observations
    with c.db() as db:
        assert db.get(RelaySession, SID).revision == before
        assert db.query(RelayOperation).count() == count
    c.owned_factory.current.active = True
    response = await ownership_read(c, view)
    assert response.status_code == 200
    assert response.json()['selector']['idle_verified'] is False
    assert response.json()['selector']['blockers'] == ['active_turn']
    c.owned_factory.current.active = False


async def test_owner_observation_rechecks_cookie_after_native_await(public_claude):
    c = public_claude
    c.enable()
    view = await attach(c.client, 'revoked-observer', ['switch'])
    original = c.launch.peer.call
    async def revoke(method, params):
        result = await original(method, params)
        if method == 'owner.inspect':
            @write_intent
            def end():
                with c.db() as db:
                    auth.end_session(db, c.token)
                    db.commit()
            end()
        return result
    c.launch.peer.call = revoke
    response = await ownership_read(c, view)
    assert response.status_code == 401 and 'execution' not in response.json()
    assert not c.launch.owner_observations
    assert len(c.owned_factory.owners) == 1


async def test_owner_observer_cancellation_does_not_fence_execution_peer(public_claude):
    import asyncio
    c = public_claude
    c.enable()
    view = await attach(c.client, 'cancelled-observer', ['switch'])
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.launch.peer.call
    async def held(method, params):
        if method == 'owner.inspect':
            entered.set()
            await release.wait()
        return await original(method, params)
    c.launch.peer.call = held
    task = asyncio.create_task(ownership_read(c, view))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not c.launch.owner_observations and not c.launch.peer.closed
    assert len(c.launch.owner_observation_tasks) == 1
    release.set()
    await asyncio.gather(*c.launch.owner_observation_tasks, return_exceptions=True)
    assert not c.launch.peer.closed and len(c.owned_factory.owners) == 1


async def test_owner_observation_withdrawal_discards_idle_facts(public_claude):
    c = public_claude
    c.enable()
    view = await attach(c.client, 'withdrawn-observer', ['switch'])
    original = c.launch.peer.call
    async def withdrawn(method, params):
        result = await original(method, params)
        if method == 'owner.inspect':
            @write_intent
            def clear():
                with c.db() as db:
                    db.get(RelaySession, SID).capabilities = {}
                    db.commit()
            clear()
        return result
    c.launch.peer.call = withdrawn
    response = await ownership_read(c, view)
    assert response.status_code == 409 and 'selector' not in response.json()
    assert not c.launch.owner_observations and len(c.owned_factory.owners) == 1


async def test_owner_observation_capacity_refuses_without_claiming(public_claude):
    c = public_claude
    c.enable()
    view = await attach(c.client, 'bounded-observer', ['switch'])
    c.launch.owner_observation_tasks.update(object() for _ in range(16))
    try:
        response = await ownership_read(c, view)
        assert response.status_code == 409 and response.json()['error']['data']['code'] == 'LIMIT'
        assert not c.launch.owner_observations and not c.launch.prepared_plans
    finally:
        c.launch.owner_observation_tasks.clear()


async def test_native_owner_observer_drops_inherited_launch_write_intent(public_claude, monkeypatch):
    from acc.server.db import WORKER_WRITE, WRITE_INTENT
    from acc.server.session_relay import claude_observer
    c = public_claude
    c.enable()
    view = await attach(c.client, 'read-only-native-callback', ['switch'])
    original, original_verify = c.launch.peer.handle, claude_observer._verify
    samples = []
    def checked(*args):
        samples.append((WRITE_INTENT.get(), WORKER_WRITE.get()))
        return original_verify(*args)
    monkeypatch.setattr(claude_observer, '_verify', checked)
    async def inherited(method, params):
        if method != 'authority.owner_observation':
            return await original(method, params)
        writer, worker = WRITE_INTENT.set(True), WORKER_WRITE.set(True)
        try:
            return await original(method, params)
        finally:
            WORKER_WRITE.reset(worker)
            WRITE_INTENT.reset(writer)
    c.launch.peer.handle = inherited
    response = await ownership_read(c, view)
    assert response.status_code == 200, response.text
    assert samples and all(sample == (False, False) for sample in samples)
    assert len(c.owned_factory.owners) == 1 and not c.launch.prepared_plans


@pytest.mark.parametrize('activity', ['native_registration', 'input_frame'])
async def test_owner_observation_native_registration_lock_is_busy_not_offline(public_claude, activity):
    c = public_claude
    c.enable()
    view = await attach(c.client, 'registration-race-observer', ['switch'])
    baseline = (await ownership_read(c, view)).json()
    original = c.launch.peer.handle
    checks = 0
    lock = c.broker_state.operation_lock if activity == 'native_registration' else c.launch.terminal_grants.frame_lock
    async def changed(method, params):
        nonlocal checks
        result = await original(method, params)
        if method == 'authority.owner_observation':
            checks += 1
            if checks == 2:
                await lock.acquire()
        return result
    c.launch.peer.handle = changed
    try:
        response = await ownership_read(c, view)
        assert checks >= 2
        assert response.status_code == 200, response.text
        facts = response.json()
        assert facts['execution'] == baseline['execution']
        assert facts['selector']['idle_verified'] is False
        assert 'uncertain_operation' in facts['selector']['blockers']
    finally:
        if lock.locked():
            lock.release()
    assert not c.launch.peer.closed and not c.launch.owner_observations
    again = await ownership_read(c, view)
    assert again.status_code == 200 and again.json()['selector']['idle_verified'] is True
    assert len(c.owned_factory.owners) == 1 and not c.launch.prepared_plans


@pytest.mark.parametrize('fault', ['replaced', 'fenced'])
async def test_owner_observation_replacement_or_fence_still_refuses(public_claude, fault):
    import copy
    c = public_claude
    c.enable()
    view = await attach(c.client, 'changed-owner-observer', ['switch'])
    original = c.launch.peer.handle
    controller = c.broker_state.controller
    checks = 0
    async def changed(method, params):
        nonlocal checks
        result = await original(method, params)
        if method == 'authority.owner_observation':
            checks += 1
            if checks == 2:
                if fault == 'replaced':
                    c.broker_state.controller = copy.copy(controller)
                else:
                    controller.fenced = True
        return result
    c.launch.peer.handle = changed
    try:
        response = await ownership_read(c, view)
        assert response.status_code == 409 and 'execution' not in response.json()
    finally:
        c.broker_state.controller = controller
        controller.fenced = False


async def test_owner_activity_during_idle_sample_cannot_publish_old_idle(public_claude):
    c = public_claude
    c.enable()
    view = await attach(c.client, 'idle-sample-race-observer', ['switch'])
    owner = c.owned_factory.current
    original = owner.prepare_switch
    async def changed():
        proof = await original()
        owner.active = True
        return proof
    owner.prepare_switch = changed
    try:
        response = await ownership_read(c, view)
        assert response.status_code == 200
        assert response.json()['selector']['idle_verified'] is False
    finally:
        owner.active = False
        owner.prepare_switch = original


@pytest.mark.parametrize('checkpoint', [1, 2])
async def test_owner_observation_new_input_revision_discards_idle_without_revoking_reader(public_claude, checkpoint):
    c = public_claude
    c.enable()
    view = await attach(c.client, 'revision-race-observer', ['switch'])
    baseline = (await ownership_read(c, view)).json()
    owner = c.owned_factory.current
    original = c.launch.peer.handle
    checks = 0
    async def changed(method, params):
        nonlocal checks
        result = await original(method, params)
        if method == 'authority.owner_observation':
            checks += 1
            if checks == checkpoint:
                @write_intent
                def advance():
                    with c.db() as db:
                        row = db.get(RelaySession, SID)
                        row.revision += 1
                        db.commit()
                advance()
                owner.active = True
        return result
    c.launch.peer.handle = changed
    response = await ownership_read(c, view)
    assert response.status_code == 200, response.text
    facts = response.json()
    assert facts['ownershipRevision'] == baseline['ownershipRevision'] + 1
    assert facts['execution'] == baseline['execution']
    assert facts['selector']['idle_verified'] is False
    assert 'uncertain_operation' in response.json()['selector']['blockers']
    assert not c.launch.peer.closed and not c.launch.owner_observations
    assert len(c.owned_factory.owners) == 1 and not c.launch.prepared_plans

    # A subsequent read during work must also remain readable and non-idle.
    # Only activity changed: the same authenticated attachment still reads it.
    c.launch.peer.handle = original
    busy = await ownership_read(c, view)
    assert busy.status_code == 200, busy.text
    assert busy.json()['selector']['idle_verified'] is False
    assert 'active_turn' in busy.json()['selector']['blockers']
    owner.active = False
    idle = await ownership_read(c, view)
    assert idle.status_code == 200 and idle.json()['selector']['idle_verified'] is True

@pytest.mark.parametrize('revoked', [False, True])
@pytest.mark.parametrize('change', ['root_revision', 'frame_admission', 'none'])
async def test_lost_idle_observation_keeps_only_fresh_readonly_presentation(public_claude, revoked, change):
    c = public_claude
    c.enable()
    view = await attach(c.client, 'lost-observation-reader', ['switch'])
    original = c.launch.peer.call
    if change == 'frame_admission':
        import time
        from types import SimpleNamespace

        c.launch.terminal_grants.record = SimpleNamespace(sequence=2, processed=2, pending=None, entered_sequence=None, phase='active',
            expires=time.monotonic() + 60, value=SimpleNamespace(attachment_id=view['attachmentId'], epoch=1))
    async def lost(method, params):
        if method == 'owner.inspect':
            @write_intent
            def advance_input_revision():
                with c.db() as db:
                    row = db.get(RelaySession, SID)
                    row.revision += 1
                    db.commit()
            if change == 'root_revision':
                advance_input_revision()
            elif change == 'frame_admission':
                c.launch.terminal_grants.record.sequence = 4
            if revoked:
                @write_intent
                def end():
                    with c.db() as db:
                        auth.end_session(db, c.token)
                        db.commit()
                end()
            raise ConnectionError('Lost read-only observation; no native idle fact')
        return await original(method, params)
    c.launch.peer.call = lost
    response = await ownership_read(c, view)
    if revoked:
        assert response.status_code == 401 and 'execution' not in response.json()
    elif change == 'none':
        assert response.status_code == 409 and 'execution' not in response.json()
    else:
        assert response.status_code == 200, response.text
        facts = response.json()
        assert facts['execution']['available'] is False
        assert facts['selector']['may_control'] is False
        assert facts['selector']['idle_verified'] is False
        assert facts['selector']['blockers'] == ['uncertain_operation']
    if change == 'frame_admission':
        c.launch.terminal_grants.record = None
    assert not c.launch.peer.closed and not c.launch.owner_observations
    assert len(c.owned_factory.owners) == 1 and not c.launch.prepared_plans


@pytest.mark.parametrize('activity', ['sequence', 'processed', 'pending', 'entered_sequence'])
async def test_successful_idle_rpc_discards_changed_input_without_root_revision(public_claude, activity):
    import time
    from types import SimpleNamespace

    c = public_claude
    c.enable()
    view = await attach(c.client, 'successful-idle-input-race', ['switch'])
    stream = SimpleNamespace(sequence=2, processed=2, pending=None, entered_sequence=None,
        phase='active', expires=time.monotonic() + 60,
        value=SimpleNamespace(attachment_id=view['attachmentId'], epoch=1))
    c.launch.terminal_grants.record = stream
    with c.db() as db:
        revision = db.get(RelaySession, SID).revision
    original = c.launch.peer.call
    async def race(method, params):
        result = await original(method, params)
        if method == 'owner.inspect':
            assert result['idle'] is True
            setattr(stream, activity, 3)
        return result
    c.launch.peer.call = race
    response = await ownership_read(c, view)
    assert response.status_code == 200, response.text
    assert response.json()['selector']['idle_verified'] is False
    assert 'uncertain_operation' in response.json()['selector']['blockers']
    with c.db() as db:
        assert db.get(RelaySession, SID).revision == revision
    assert not c.launch.peer.closed and not c.launch.owner_observations
    assert len(c.owned_factory.owners) == 1 and not c.launch.prepared_plans
