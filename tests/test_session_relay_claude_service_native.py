"""API-key-only native SDK service admission; no subscription qualification."""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from test_session_relay_claude_churn import test_production_claude_owner_hook_churn as run_churn
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_in_thread, write_intent
from acc.server.models import Session
from acc.server.orchestrator import delivery
from acc.server.session_relay import delivery as service
from acc.server.session_relay import store
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p

pytestmark = pytest.mark.relay_native


async def test_native_claude_api_service_uses_current_sdk_and_late_admission(runtime_case, monkeypatch, record_property):  # noqa: F811
    await run_native_service(runtime_case, monkeypatch, record_property)


async def run_native_service(c, monkeypatch, record_property, *, team=False, revoke_source=False):
    c.ctx.relay_runtime, c.ctx.token = c.manager, 'synthetic-claude-service-token'
    supervisor = c.ctx.supervisor
    supervisor.db, supervisor.relay_manager = c.db, c.manager
    supervisor._get = lambda db, identifier: db.get(Session, identifier)
    monkeypatch.setattr(service, 'DELIVERY_QUALIFIED', True)
    monkeypatch.setattr(service, 'CLAUDE_SERVICE_QUALIFIED', True)
    monkeypatch.setattr(delivery, 'enabled', lambda: True)
    if team:
        original_launch = c.manager.new_exclusive_launch
        def team_launch(resolve):
            from acc.server.models import SessionRun, Team, utcnow
            from acc.server.orchestrator.assignments import Assignment, Attempt
            @write_intent
            def seed():
                with c.db() as db:
                    target = db.get(Session, 'abc123def456')
                    lead = Session(id='fixture-team-lead', provider='claude', name='Artificial team lead',
                        tmux_name='fixture-lead', launch_spec_id=target.launch_spec_id, owner='alex',
                        run_as=target.run_as, team_id='fixture-team-lead')
                    db.add(lead)
                    db.flush()
                    db.add(Team(id=lead.id, limits={'machines': ['local'], 'agents': [{'agent': 'claude',
                        'profile': None, 'inputs': dict(target.launch_spec.inputs)}], 'at_once': 2, 'total': 3, 'depth': 2}))
                    from relay_helpers import replace_unlaunched_session
                    target = replace_unlaunched_session(db, target, team_id=lead.id, parent_session_id=lead.id)
                    db.add(SessionRun(session_id=lead.id, reason='fixture', verified_at=utcnow()))
                    db.add(Assignment(id='a'*32, team_id=lead.id, created_by=lead.id, task_type='build',
                        goal='Artificial native team task', acceptance=[], requirement_ids=[], need={}, state='running'))
                    db.flush()
                    db.add(Attempt(id='b'*32, assignment_id='a'*32, session_id=target.id, sequence=1, state='running'))
                    db.commit()
            seed()
            return original_launch(resolve)
        monkeypatch.setattr(c.manager, 'new_exclusive_launch', team_launch)
        monkeypatch.setattr(delivery, 'start_worker', lambda _: None)
    original = c.manager.submit
    routed = []
    async def route(resolve, session_id, params):
        if routed:
            return await original(resolve, session_id, params)
        routed.append(None)  # prevent recursive routing through the real manager
        # Replace only this fixture's unused person registration with a real
        # trusted service enqueue. It never reaches provider IO or changes actor.
        await write_in_thread(lambda: Hub(c.ctx).cancel(resolve, session_id, params['operationId'], 0))
        @write_intent
        def enqueue():
            return delivery.enqueue(supervisor, session_id, 'session:fixture-team-lead' if team else '@api',
                                        params['text'], relay_service=True)
        if team:
            queued = (await delivery.team_message(supervisor, 'fixture-team-lead',
                {'helper': session_id, 'text': params['text']}))['delivery']
        else:
            queued = await write_in_thread(enqueue)
        routed[0] = queued['id']
        if revoke_source:
            from sqlalchemy import select

            from acc.server.models import SessionRun, utcnow
            @write_intent
            def revoke():
                with c.db() as db:
                    source = db.scalar(select(SessionRun).where(SessionRun.session_id == 'fixture-team-lead')
                        .order_by(SessionRun.id.desc()).limit(1))
                    source.ended_at = utcnow()
                    db.commit()
            await write_in_thread(revoke)
        await delivery._deliver(supervisor, queued['id'])
        for _ in range(500):
            await delivery._deliver(supervisor, queued['id'])
            with c.db() as db:
                op = service.operation(db, queued['id'])
                message = db.get(delivery.MessageDelivery, queued['id'])
                receipt = store.receipt_from(op.receipt)
                done = message.state == ('failed' if revoke_source else 'acknowledged')
            if done:
                break
            await asyncio.sleep(.03)
        assert done and receipt.state is (p.ReceiptState.FAILED if revoke_source else p.ReceiptState.ACCEPTED)
        assert receipt.actor == p.Actor(p.ActorKind.SERVICE, 'session:fixture-team-lead' if team else '@api',
                                       p.Channel.TEAM if team else p.Channel.API)
        if team:
            with c.db() as db:
                scope = service.operation(db, queued['id']).authority['delivery']
                assert scope['attempt'] == 'b'*32 and scope['assignment']['id'] == 'a'*32
                assert scope['sourceRun'] is not None
        with c.db() as db:
            private = Path(db.get(Session, session_id).run_dir) / 'relay'
        with sqlite3.connect('file:' + str(private / 'journal/admission.sqlite3') + '?mode=ro', uri=True) as journal:
            assert journal.execute('SELECT state FROM admission WHERE operation=?', (receipt.operation_id,)).fetchone() == (None if revoke_source else ('accepted',))
        assert not list((private / 'payloads').glob('*.payload'))
        await delivery._deliver(supervisor, queued['id'])
        record_property('native_service_receipt', p.canonical_json(p.to_wire(receipt)).decode())
        return receipt
    monkeypatch.setattr(c.manager, 'submit', route)
    # Existing production isolated account broker/SDK/CLI fixture also checks
    # real background cleanup and preserves unknown person permission receipts.
    await run_churn(c, monkeypatch, record_property, cycles=0)
    assert len(routed) == 1 and routed[0]
    with c.db() as db:
        op = service.operation(db, routed[0])
        assert op.receipt['state'] == ('failed' if revoke_source else 'accepted') and op.receipt['actor']['kind'] == 'service'
    record_property('scope', 'actual Claude2.1.308/SDK0.1.50/account broker; synthetic API key/model/person; local transport, no subscription or enrolled TLS claim')
