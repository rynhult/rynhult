"""Placement/auth-profile stubs only: no node, CLI, credentials or provider IO."""
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from acc.server.models import Node, RelayRun, RelaySession, Session, SessionRun
from acc.server.session_relay import stop_proof, store
from acc.server.session_relay.runtime import RuntimeManager
from acc.session_relay import protocol as p


@pytest.mark.parametrize('profile', ['api_key', 'subscription'])
@pytest.mark.parametrize('fault', ['none', 'node-offline', 'account', 'link-closed', 'new-link',
                                  'node-key', 'leader-epoch', 'leader-incarnation', 'leader-denied',
                                  'leader-lost', 'container', 'management', 'team', 'automation'])
async def test_stop_fact_placement_stubs_never_mint_execution(profile, fault):
    session = SimpleNamespace(host='example-node', run_as='example-agent', cwd='/workspace/example',
        placement_id=None, launch_spec_id=1, launch_spec=SimpleNamespace(digest='fixture-launch'),
        native_id='fixture-native', run_dir='/run/fixture/session', desired_state='stopped',
        provider='claude', claude_auth=profile, management='acc', container=None,
        team_id=None, multirun_id=None, automation=False)
    row = SimpleNamespace(current_run_id=1, binding_id='fixture-binding', sealed=True,
        enrollment='fixture-enrollment', authority_epoch=2)
    record = SimpleNamespace(run_id=1, session_id='fixture', binding_id=row.binding_id,
        frozen_policy={'authMode': profile, 'personId': 1}, identity={})
    native = SimpleNamespace(session_id='fixture', verified_at=datetime.now(timezone.utc), ended_at=datetime.now(timezone.utc))
    node = SimpleNamespace(id=1, name=session.host, public_key='fixture-public-key', status='active')
    link = SimpleNamespace(closed=False, accounts={'example-agent'})
    leader = SimpleNamespace(epoch=3, incarnation='fixture-leader', check=Mock())
    broker = SimpleNamespace(authority=leader, hub=SimpleNamespace(get=lambda _: link), runtime_managers=set(),
        fresh=Mock(side_effect=AssertionError('A Stop fact never opens a producer')),
        call=AsyncMock(side_effect=AssertionError('No generic kill/query/relaunch fallback')))
    class Database:
        info = {}
        def scalar(self, query):
            entity = query.column_descriptions[0]['entity']
            if entity is RelaySession:
                return row
            if entity is Node:
                return node
            raise AssertionError('Exit facts cannot request admission/consent/budget authority')
        def get(self, model, key):
            return {Session: session, RelayRun: record, SessionRun: native}[model]
    db = Database()
    @contextmanager
    def database():
        yield db
    # Both execution feature and subscription consent are absent. Confirmation
    # of the old dead enclosure grants nothing and must remain available.
    ctx = SimpleNamespace(db=database, broker=broker, settings=SimpleNamespace(feature_overrides={}))
    manager = RuntimeManager(ctx)
    record.identity = {'binding': {'provider': session.provider}, 'coordinates': store.coordinates(session), 'placement': stop_proof.placement(manager, db, session),
        'processIdentity': 'fixture-process', 'hubIncarnation': 'fixture-main', 'brokerIncarnation': 'fixture-broker'}
    record.identity[stop_proof.KEY] = stop_proof.stamp(row, record)
    if fault == 'node-offline':
        node.status = 'offline'
    elif fault == 'account':
        link.accounts = set()
    elif fault == 'link-closed':
        link.closed = True
    elif fault == 'new-link':
        replacement = SimpleNamespace(closed=False, accounts={'example-agent'})
        broker.hub.get = lambda _: replacement
    elif fault == 'node-key':
        node.public_key = 'replacement-public-key'
    elif fault == 'leader-epoch':
        leader.epoch += 1
    elif fault == 'leader-incarnation':
        leader.incarnation = 'replacement-leader'
    elif fault == 'leader-denied':
        leader.check.side_effect = p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    elif fault == 'leader-lost':
        broker.authority = None
    elif fault == 'container':
        session.container = 'replacement-container'
    elif fault == 'management':
        session.management = 'external'
    elif fault == 'team':
        session.team_id = 'replacement-team'
    elif fault == 'automation':
        session.automation = True
    try:
        assert await manager.stop('fixture') is (fault == 'none')
        assert not manager.runs and not manager.launches
        broker.fresh.assert_not_called()
        broker.call.assert_not_called()
    finally:
        await manager.close()
