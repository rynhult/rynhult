"""Real ownership Hub/store; synthetic placement and process receipts, no native launch."""
from dataclasses import replace
from datetime import timedelta

import pytest
from relay_helpers import setup
from sqlalchemy import select

from acc.server import claude_subscription
from acc.server.db import write_intent
from acc.server.models import RelayOwnership, RelayRun, RelaySession, Session, SessionRun, utcnow
from acc.server.session_relay import store
from acc.server.session_relay.ownership import OwnershipHub
from acc.session_relay import protocol as p

NATIVE = '11111111-1111-4111-8111-111111111111'
POLICY = p.sha256_digest(b'artificial policy')


@pytest.fixture
def owner_case(tmp_path):
    c = setup(tmp_path)
    c.placement = {'node': 'fixture-node', 'account': 'fixture-account', 'leader': 1}
    c.ctx.settings = replace(c.ctx.settings, feature_overrides={
        'session_relay': True, 'claude_subscription_chat': True})
    @write_intent
    def seed():
        with c.db() as db:
            session = db.get(Session, 'relay')
            session.provider, session.native_id = 'claude', NATIVE
            run = db.get(RelayRun, 1)
            run.identity = {**run.identity, 'coordinates': store.coordinates(session),
                'placement': c.placement, 'processIdentity': 'fixture-original-process',
                'binding': {'native_conversation': NATIVE, 'launch_policy_digest': POLICY}}
            run.frozen_policy = {'authMode': 'api_key', 'ownershipMode': 'exclusive_idle', 'personId': 1}
            db.commit()
    seed()
    c.hub = OwnershipHub(c.ctx, placement=lambda *_: dict(c.placement))
    c.hub.bind(c.resolve, 'relay', view='chat')
    yield c
    c.engine.dispose()


def prepare(c, key='switch'):
    with c.db() as db:
        revision = db.get(RelaySession, 'relay').revision
    return c.hub.prepare(c.resolve, 'relay', target='terminal', revision=revision, request_key=key)


def claim(c, plan):
    return c.hub.claim(c.resolve, 'relay', plan['planId'], plan['planDigest'], revision=plan['revision'])


def checkpoint(c, plan, phase, evidence):
    return c.hub.checkpoint(c.resolve, 'relay', plan['planId'], plan['planDigest'], phase, evidence)


def test_hub_exact_same_id_exit_receipts_fence_old_run_and_duplicate_plan(owner_case):
    c = owner_case
    plan = prepare(c)
    assert prepare(c) == plan
    claimed = claim(c, plan)
    proof = {'idle': True, 'native_conversation': NATIVE, 'policy_digest': POLICY, 'exit': 'running'}
    checkpoint(c, plan, 'stopping', proof)
    checkpoint(c, plan, 'starting', {**proof, 'exit': 'exited'})
    final = checkpoint(c, plan, 'owned', {'binding_id': 'binding', 'run_id': claimed['targetRunId'],
        'native_conversation': NATIVE, 'process_identity': 'fixture-replacement-process'})
    assert final['view'] == 'terminal' and final['runId'] != '1'
    with c.db() as db:
        row = db.get(RelaySession, 'relay')
        assert row.capabilities == {} and not row.sealed
        assert db.get(SessionRun, 1).ended_at
        assert db.get(SessionRun, int(final['runId'])).verified_at
        assert db.get(Session, 'relay').native_id == NATIVE
    with pytest.raises(p.RelayError):
        c.hub.verify(c.resolve, 'relay', plan['planId'], plan['planDigest'])


@pytest.mark.parametrize('change', [{'idle': False}, {'native_conversation': 'wrong'},
    {'policy_digest': p.sha256_digest(b'wrong')}, {'exit': 'exited'}])
def test_hub_cannot_record_stopping_from_invalid_native_evidence(owner_case, change):
    c = owner_case
    plan = prepare(c)
    claim(c, plan)
    proof = {'idle': True, 'native_conversation': NATIVE, 'policy_digest': POLICY, 'exit': 'running'}
    with pytest.raises(p.RelayError):
        checkpoint(c, plan, 'stopping', {**proof, **change})
    with c.db() as db:
        assert db.get(RelayOwnership, 'relay').phase == 'draining'
        assert db.get(SessionRun, 1).ended_at is None
        assert db.get(RelaySession, 'relay').current_run_id == 1


@pytest.mark.parametrize('change', ['expiry', 'placement', 'consent', 'service'])
def test_claim_rechecks_current_person_and_placement_before_new_run(owner_case, change):
    c = owner_case
    if change == 'consent':
        @write_intent
        def consent(enabled):
            with c.db() as db:
                db.get(RelayRun, 1).frozen_policy = {'authMode': 'subscription',
                    'ownershipMode': 'exclusive_idle', 'personId': 1}
                claude_subscription.update(db, c.resolve()[0], enabled, claude_subscription.VERSION)
                db.commit()
        consent(True)
    plan = prepare(c)
    if change == 'consent':
        consent(False)
    elif change == 'placement':
        c.placement['leader'] = 2
    elif change == 'service':
        c.state['actor'] = p.Actor(p.ActorKind.SERVICE, 'alex', p.Channel.API)
    else:
        @write_intent
        def expire():
            with c.db() as db:
                db.get(RelayOwnership, 'relay').expires_at = utcnow() - timedelta(seconds=1)
                db.commit()
        expire()
    with pytest.raises(p.RelayError):
        claim(c, plan)
    with c.db() as db:
        assert len(list(db.scalars(select(SessionRun)))) == 1
        assert db.get(RelayOwnership, 'relay').phase == 'prepared'
        assert db.get(SessionRun, 1).ended_at is None
