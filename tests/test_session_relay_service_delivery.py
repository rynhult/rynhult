"""Trusted service origin over the real main/account-broker grant and journal path."""
import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelayOwnership, RelaySession, Session
from acc.server.orchestrator import delivery
from acc.server.session_relay import delivery as service
from acc.server.supervisor import LaunchRefused
from acc.session_relay import protocol as p

SID = 'abc123def456'


@pytest.fixture
async def service_case(runtime_case, monkeypatch):  # noqa: F811
    c = runtime_case
    await c.launch.launch(SID)
    return configure(c, monkeypatch)


def configure(c, monkeypatch):
    c.ctx.relay_runtime = c.manager
    c.ctx.token = "synthetic-delivery-token"
    supervisor = c.ctx.supervisor
    supervisor.db, supervisor.relay_manager, supervisor.broker = c.db, c.manager, c.ctx.broker
    supervisor._get = lambda db, identifier: db.get(Session, identifier)
    state = next(iter(c.factory.active.values()))
    state.adapter.record = replace(state.adapter.record, qualification_id='fixture-only', qualification_date='2026-11-12',
        operations={'text': p.CapabilityEntry(p.Support.QUALIFIED, evidence=('synthetic owner only',))})
    monkeypatch.setattr(service, 'DELIVERY_QUALIFIED', True)
    monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
    monkeypatch.setattr(delivery, 'enabled', lambda: True)
    @write_intent
    def qualify():
        with c.db() as db:
            db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
            db.commit()
    qualify()
    c.service_supervisor, c.service_state = supervisor, state
    return c


@write_intent
def enqueue(c, text='artificial service task', *, actor='@api', trusted=True):
    return delivery.enqueue(c.service_supervisor, SID, actor, text, relay_service=trusted)


async def test_api_service_keeps_real_actor_and_existing_person_writer(service_case):
    c = service_case
    queued = enqueue(c)
    await asyncio.wait_for(delivery._deliver(c.service_supervisor, queued['id']), 3)
    with c.db() as db:
        message = db.get(delivery.MessageDelivery, queued['id'])
        op = service.operation(db, message.id)
        assert message.state == 'acknowledged' and message.ack == 'relay_admitted' and message.body == '', (message.error, op.receipt, op.authority)
        assert op.receipt['state'] == 'accepted'
        assert op.receipt['actor'] == p.to_wire(p.Actor(p.ActorKind.SERVICE, '@api', p.Channel.API))
        assert op.receipt['payloadDigest'] == p.operation_digest('turn.submit', {
            'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'text': 'artificial service task',
            'expectedConfigurationRevision': 0, 'expectedControlRevision': 1, 'ifBusy': 'queue'})
        assert op.authority['delivery']['writer']['principal'] == 'alex'
        assert db.get(RelaySession, SID).controller['principal'] == 'alex'
        assert db.get(RelaySession, SID).control_revision == 1
    assert c.service_state.writers.gate.lease.actor.principal == 'alex'
    assert c.service_state.writers.gate.epoch == 1
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1
    await delivery._deliver(c.service_supervisor, queued['id'])
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1


async def test_waiting_service_returns_without_waiting_for_active_turn(service_case):
    c = service_case
    first, second = enqueue(c, 'first service task'), enqueue(c, 'second service task')
    await delivery._deliver(c.service_supervisor, first['id'])
    unrelated = []
    async def progress():
        unrelated.append(True)
    await asyncio.wait_for(asyncio.gather(delivery._deliver(c.service_supervisor, second['id']), progress()), 3)
    with c.db() as db:
        assert db.get(delivery.MessageDelivery, second['id']).state == 'queued'
        op = service.operation(db, second['id'])
        assert op.queue_seq is not None and op.receipt['state'] == 'queued'
        active = db.get(RelaySession, SID).active_operation_id
    assert unrelated and len(c.launch.grants) == 1
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1
    await c.service_state.events.put({'method': 'turn/completed', 'params': {
        'threadId': 'fixture-native-id', 'turn': {'id': c.service_state.adapter.turns[active], 'status': 'completed'}}})
    for _ in range(200):
        with c.db() as db:
            receipt = service.operation(db, second['id']).receipt
        if receipt['state'] == 'accepted':
            break
        await asyncio.sleep(.01)
    assert receipt['state'] == 'accepted'
    await delivery._deliver(c.service_supervisor, second['id'])
    with c.db() as db:
        assert db.get(delivery.MessageDelivery, second['id']).state == 'acknowledged'
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 2


@pytest.mark.parametrize('trusted,actor', [(False, '@api'), (True, 'alex')])
async def test_no_grant_from_unproven_or_person_shaped_legacy_origin(service_case, trusted, actor):
    with pytest.raises(LaunchRefused):
        enqueue(service_case, actor=actor, trusted=trusted)
    with service_case.db() as db:
        assert not list(db.scalars(__import__('sqlalchemy').select(RelayOperation)))


@pytest.mark.parametrize('mutation', ['holder', 'revision', 'run', 'payload', 'terminal', 'budget', 'credential'])
async def test_changed_binding_refuses_before_native_dispatch(service_case, monkeypatch, mutation):
    c = service_case
    queued = enqueue(c)
    @write_intent
    def change():
        with c.db() as db:
            row = db.get(RelaySession, SID)
            if mutation == 'holder':
                row.controller = {**row.controller, 'principal': 'sam'}
            elif mutation == 'revision':
                row.control_revision += 1
            elif mutation == 'run':
                row.authority_epoch += 1
            elif mutation == 'payload':
                db.get(delivery.MessageDelivery, queued['id']).body = 'changed after registration'
            elif mutation == 'credential':
                c.ctx.token = 'synthetic-rotated-token'
            elif mutation == 'terminal':
                db.get(RelayOwnership, SID).view = 'terminal'
            db.commit()
    change()
    if mutation == 'budget':
        monkeypatch.setattr(service.budgets, 'paused', lambda *_args, **_kwargs: ['fixture budget'])
    await delivery._deliver(c.service_supervisor, queued['id'])
    with c.db() as db:
        assert db.get(delivery.MessageDelivery, queued['id']).state == 'failed'
        assert service.operation(db, queued['id']).receipt['state'] == 'failed'
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)


@pytest.fixture
async def team_case(runtime_case, monkeypatch):  # noqa: F811
    from acc.server.models import SessionRun, Team, utcnow
    from acc.server.orchestrator.assignments import Assignment, Attempt

    c = runtime_case
    monkeypatch.setattr(service, 'DELIVERY_QUALIFIED', True)
    @write_intent
    def seed():
        with c.db() as db:
            db.delete(db.get(RelaySession, SID))
            db.delete(db.get(SessionRun, c.launch.run_id))
            db.flush()
            target = db.get(Session, SID)
            lead = Session(id='fixture-team-lead', provider='codex', name='Fixture lead', tmux_name='fixture-lead',
                launch_spec_id=target.launch_spec_id, run_as=target.run_as, owner='alex', team_id='fixture-team-lead')
            db.add(lead)
            db.flush()
            db.add(Team(id=lead.id, limits={'at_once': 2, 'total': 3, 'depth': 2, 'machines': ['local'],
                'agents': [{'agent': 'codex', 'profile': None, 'inputs': {'provider': 'codex'}}]}))
            from relay_helpers import replace_unlaunched_session
            target = replace_unlaunched_session(db, target, team_id=lead.id, parent_session_id=lead.id)
            db.add(SessionRun(session_id=lead.id, reason='fixture', verified_at=utcnow()))
            assignment = Assignment(id='a' * 32, team_id=lead.id, created_by=lead.id, task_type='build',
                goal='Artificial assignment', acceptance=[], requirement_ids=[], plan_version=1, need={}, state='running')
            db.add(assignment)
            db.flush()
            db.add(Attempt(id='b' * 32, assignment_id=assignment.id, session_id=SID, sequence=1, state='running'))
            db.commit()
        c.launch = c.manager.new_launch(c.resolve)
        with c.db() as db:
            c.launch.reserve(db, db.get(Session, SID))
            db.commit()
    seed()
    await c.launch.launch(SID)
    return configure(c, monkeypatch)


async def test_real_team_message_entry_binds_current_assignment_and_source_run(team_case, monkeypatch):
    from sqlalchemy import select

    from acc.server.models import AuditLog

    c = team_case
    monkeypatch.setattr(delivery, 'start_worker', lambda _: None)
    result = await delivery.team_message(c.service_supervisor, 'fixture-team-lead', {'helper': SID, 'text': 'Team task fixture'})
    identifier = result['delivery']['id']
    await delivery._deliver(c.service_supervisor, identifier)
    with c.db() as db:
        message = db.get(delivery.MessageDelivery, identifier)
        op = service.operation(db, identifier)
        assert message.state == 'acknowledged'
        assert op.receipt['actor'] == p.to_wire(p.Actor(p.ActorKind.SERVICE, 'session:fixture-team-lead', p.Channel.TEAM))
        assert op.authority['delivery']['attempt'] == 'b' * 32
        assert op.authority['delivery']['assignment']['id'] == 'a' * 32
        assert op.authority['delivery']['sourceRun'] is not None
        audits = db.scalars(select(AuditLog).where(AuditLog.action == 'relay.delivery.settled')).all()
        assert len(audits) == 1 and audits[0].detail['previous']['principal'] == 'alex'
        assert audits[0].detail['channel'] == 'team'
    assert c.service_state.writers.gate.lease.actor.principal == 'alex'


@pytest.mark.parametrize('change', ['attempt', 'assignment', 'source_run', 'source_unverified', 'team', 'owner', 'machine', 'permission'])
async def test_team_binding_revocation_before_admission(team_case, change):
    from acc.server.models import SessionRun, Team, utcnow
    from acc.server.orchestrator.assignments import Assignment, Attempt

    c = team_case
    queued = enqueue(c, actor='session:fixture-team-lead')
    @write_intent
    def revoke():
        with c.db() as db:
            if change == 'attempt':
                db.add(Attempt(id='c' * 32, assignment_id='a' * 32, session_id=SID, sequence=2, state='running'))
            elif change == 'assignment':
                db.get(Assignment, 'a' * 32).plan_version += 1
            elif change == 'source_run':
                db.add(SessionRun(session_id='fixture-team-lead', reason='replacement', verified_at=utcnow()))
            elif change == 'source_unverified':
                db.scalars(select(SessionRun).where(SessionRun.session_id == 'fixture-team-lead')).one().verified_at = None
            elif change == 'team':
                db.get(Team, 'fixture-team-lead').stopped = True
            elif change == 'machine':
                team = db.get(Team, 'fixture-team-lead')
                team.limits = {**team.limits, 'machines': ['other-machine']}
            elif change == 'permission':
                team = db.get(Team, 'fixture-team-lead')
                team.limits = {**team.limits, 'agents': [{'agent': 'codex', 'profile': None,
                    'inputs': {'provider': 'codex', 'sandbox': 'read-only'}}]}
            else:
                db.get(Session, 'fixture-team-lead').owner = 'sam'
            db.commit()
    revoke()
    await delivery._deliver(c.service_supervisor, queued['id'])
    with c.db() as db:
        assert db.get(delivery.MessageDelivery, queued['id']).state == 'failed'
        assert service.operation(db, queued['id']).receipt['state'] == 'failed'
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)


async def test_actual_api_bearer_message_route_queues_scoped_service(service_case, monkeypatch):
    from types import MethodType

    import httpx
    from fastapi import FastAPI

    from acc.server.api.context import Context
    from acc.server.api.sessions import router
    from acc.server.supervisor import Supervisor

    c = service_case
    supervisor = c.service_supervisor
    supervisor.send_text = MethodType(Supervisor.send_text, supervisor)
    supervisor.broker = c.ctx.broker
    ctx = Context(c.ctx.settings, c.db, supervisor, c.ctx.broker, None, 'synthetic-delivery-token')
    ctx.relay_runtime = c.manager
    c.manager.ctx = c.launch.ctx = c.ctx = ctx
    monkeypatch.setattr(delivery, 'start_worker', lambda _: None)
    app = FastAPI()
    app.include_router(router(ctx))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
        denied = await client.post(f'/api/sessions/{SID}/message', json={'text': 'API route task'},
                                   headers={'Authorization': 'Bearer synthetic-invalid'})
        assert denied.status_code == 401
        result = await client.post(f'/api/sessions/{SID}/message', json={'text': 'API route task'},
                                   headers={'Authorization': 'Bearer synthetic-delivery-token'})
        assert result.status_code == 202, result.text
        identifier = result.json()['delivery']['id']
    await delivery._deliver(supervisor, identifier)
    with c.db() as db:
        assert db.get(delivery.MessageDelivery, identifier).state == 'acknowledged'
        assert service.operation(db, identifier).receipt['actor']['kind'] == 'service'
        assert service.operation(db, identifier).receipt['actor']['principal'] == '@api'


async def test_assignment_safety_uses_chat_hub_boundary(team_case):
    c = team_case
    assert await delivery.safety(c.service_supervisor, SID, actor='session:fixture-team-lead')
    queued = enqueue(c, actor='session:fixture-team-lead')
    assert not await delivery.safety(c.service_supervisor, SID, actor='session:fixture-team-lead')
    await delivery._deliver(c.service_supervisor, queued['id'])
    assert not await delivery.safety(c.service_supervisor, SID, actor='session:fixture-team-lead')


async def test_expired_waiting_delivery_reclaims_stage_and_keeps_terminal_receipt(service_case):
    from datetime import timedelta

    from acc.server.models import utcnow

    c = service_case
    first, second = enqueue(c, 'active for expiry'), enqueue(c, 'queued for expiry')
    await delivery._deliver(c.service_supervisor, first['id'])
    await delivery._deliver(c.service_supervisor, second['id'])
    assert len(c.launch.grants) == 1
    @write_intent
    def expire():
        with c.db() as db:
            db.get(delivery.MessageDelivery, second['id']).expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
    expire()
    await delivery.process_once(c.service_supervisor)
    with c.db() as db:
        assert db.get(delivery.MessageDelivery, second['id']).state == 'expired'
        receipt = service.operation(db, second['id']).receipt
        assert receipt['state'] == 'failed' and receipt['reason'] == 'expired'
    assert not c.launch.grants and not list(c.service_state.staging.root.glob('*.payload'))
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1


async def test_service_cancel_authorizes_before_reclaiming_another_actors_stage(service_case):
    c = service_case
    first, second = enqueue(c, 'active for cancel'), enqueue(c, 'queued for cancel')
    await delivery._deliver(c.service_supervisor, first['id'])
    await delivery._deliver(c.service_supervisor, second['id'])
    with c.db() as db:
        op = service.operation(db, second['id'])
        params = {'operationId': op.id, 'expectedRevision': op.receipt['revision']}
        resolve = service.DeliveryResolver(c.ctx, second['id'], op.authority['delivery'])
    with pytest.raises(p.RelayError) as denied:
        await c.manager.cancel(c.resolve, SID, params)
    assert denied.value.code is p.ErrorCode.NOT_FOUND_OR_DENIED
    assert len(c.launch.grants) == 1 and len(list(c.service_state.staging.root.glob('*.payload'))) == 1
    receipt = await c.manager.cancel(resolve, SID, params)
    assert receipt.state is p.ReceiptState.FAILED and receipt.reason is p.FailureReason.CANCELLED
    assert receipt.actor.kind is p.ActorKind.SERVICE
    assert not c.launch.grants and not list(c.service_state.staging.root.glob('*.payload'))
    await delivery._deliver(c.service_supervisor, second['id'])
    with c.db() as db:
        assert service.operation(db, second['id']).receipt == p.to_wire(receipt)
        assert db.get(delivery.MessageDelivery, second['id']).state == 'failed'
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1


async def test_legacy_person_text_stays_fenced_after_successful_service_delivery(service_case):
    from acc.server.supervisor import Supervisor

    c = service_case
    queued = enqueue(c)
    await delivery._deliver(c.service_supervisor, queued['id'])
    with pytest.raises(LaunchRefused, match='qualified hub input'):
        await Supervisor.send_text(c.service_supervisor, SID, 'alex', 'Never type legacy text')
    with c.db() as db:
        assert service.operation(db, queued['id']).receipt['state'] == 'accepted'
        assert db.get(RelaySession, SID).controller['principal'] == 'alex'
    assert sum(method == 'turn/start' for method, _ in c.owners[0].calls) == 1


async def test_service_scope_cannot_mint_generic_or_cancel_other_delivery(service_case):
    from acc.server.session_relay.hub import Hub

    c = service_case
    first, second = enqueue(c, 'first scoped delivery'), enqueue(c, 'second scoped delivery')
    with c.db() as db:
        one, two = service.operation(db, first['id']), service.operation(db, second['id'])
        resolve = service.DeliveryResolver(c.ctx, first['id'], one.authority['delivery'])
        other_id, revision = two.id, two.receipt['revision']
    with pytest.raises(p.RelayError) as denied:
        Hub(c.ctx).register(resolve, SID, str(c.launch.run_id), 'unrelated-key', 'turn.submit', p.sha256_digest(b'forged'))
    assert denied.value.code is p.ErrorCode.POLICY_REFUSED
    with pytest.raises(p.RelayError) as denied:
        await c.manager.cancel(resolve, SID, {'operationId': other_id, 'expectedRevision': revision})
    assert denied.value.code is p.ErrorCode.NOT_FOUND_OR_DENIED
    with c.db() as db:
        assert service.operation(db, first['id']).receipt['state'] == 'queued'
        assert service.operation(db, second['id']).receipt['state'] == 'queued'
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)


@pytest.mark.parametrize('mode,qualified', [('subscription', True), ('unknown', True), ('api_key', False)])
async def test_claude_service_never_bypasses_subscription_or_qualification_gate(service_case, monkeypatch, mode, qualified):
    c = service_case
    monkeypatch.setattr(service, 'CLAUDE_SERVICE_QUALIFIED', qualified)
    c.launch.provider, c.launch.frozen_policy = 'claude', {'authMode': mode}
    @write_intent
    def set_owner():
        with c.db() as db:
            db.get(Session, SID).provider = 'claude'
            owner = db.get(RelayOwnership, SID)
            owner.mode, owner.authority = 'exclusive_idle', {'actor': db.get(RelaySession, SID).controller}
            db.commit()
    set_owner()
    with pytest.raises(LaunchRefused):
        enqueue(c)
    with c.db() as db:
        assert not list(db.scalars(select(delivery.MessageDelivery)))
        assert not list(db.scalars(select(RelayOperation)))
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)


def test_subscription_claude_team_is_refused_before_enrollment(service_case, monkeypatch):
    c = service_case
    monkeypatch.setattr(service, 'CLAUDE_SERVICE_QUALIFIED', True)
    @write_intent
    def check():
        with c.db() as db:
            target = db.get(Session, SID)
            target.provider, target.claude_auth, target.team_id = 'claude', 'subscription', SID
            with pytest.raises(p.RelayError) as refused:
                c.manager.current_placement(db, target, provider='claude')
            assert refused.value.code is p.ErrorCode.POLICY_REFUSED
            db.rollback()
    check()
    assert len(c.owners) == 1
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)


async def test_unverified_source_run_cannot_register_team_delivery(team_case):
    from acc.server.models import SessionRun
    c = team_case
    @write_intent
    def unverify():
        with c.db() as db:
            source = db.scalars(select(SessionRun).where(SessionRun.session_id == 'fixture-team-lead')).one()
            source.verified_at = None
            db.commit()
    unverify()
    with pytest.raises(LaunchRefused):
        enqueue(c, actor='session:fixture-team-lead')
    with c.db() as db:
        assert not list(db.scalars(select(RelayOperation)))
    assert not any(method == 'turn/start' for method, _ in c.owners[0].calls)
