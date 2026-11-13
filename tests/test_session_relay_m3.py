"""M3 boundary regressions. Synthetic authority only; no native provider inference."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from relay_helpers import DIGEST, setup

from acc.server import claude_subscription as consent
from acc.server.admin_features import Features
from acc.server.config import Settings
from acc.server.db import write_intent
from acc.server.session_relay import store
from acc.server.session_relay.connections import Connections
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import AdapterOperation


def operation(params, method='decision.reply'):
    return AdapterOperation('op', 'relay', '1', method, p.operation_digest(method, params),
                            p.Actor(p.ActorKind.PERSON, 'alex', p.Channel.CHAT),
                            p.Fence(1, 'broker', 'hub'), 9999999999999, params)


def test_operation_keeps_complete_digest_and_immutable_payload():
    params = dict(sessionId='relay', expectedRunId='1', text='hello',
                  expectedConfigurationRevision=3, ifBusy='queue')
    op = operation(params, 'turn.submit')
    params['text'] = 'changed after authorization'
    assert op.native_params({'text'}, optional={'expectedConfigurationRevision', 'ifBusy'}) == {'text': 'hello'}
    assert p.operation_digest(op.method, op.params) == op.payload_digest
    assert p.to_wire(op.params)['text'] == 'hello'
    with pytest.raises(TypeError):
        op.params['text'] = 'changed during await'
    with pytest.raises(p.RelayError):
        replace(op, session_id='other').native_params({'text'}, optional={'expectedConfigurationRevision', 'ifBusy'})
    with pytest.raises(p.RelayError):
        replace(op, payload_digest=DIGEST).native_params({'text'}, optional={'expectedConfigurationRevision', 'ifBusy'})
    nested = operation({'nested': [{'value': 'original'}]})
    with pytest.raises(TypeError):
        nested.params['nested'][0]['value'] = 'mutated'


def test_observer_reconnect_replaces_previous_token_across_history_resets():
    connections = Connections()
    params = dict(major=1, minor=0, features=[], clientId='tab')
    previous = None
    for generation in range(600):
        token = connections.initialize(f'history-{generation}', params, owner_scope='login/session')['connectionToken']
        assert previous not in connections.entries
        previous = token
    assert len(connections.entries) == 1
    # Same client ID from another authenticated login cannot evict this observer.
    connections.initialize('other', params, owner_scope='other-login/session')
    assert previous in connections.entries
    assert len(connections.entries) == 2
    for index in range(15):
        connections.initialize('other', {**params, 'clientId': f'tab-{index}'}, owner_scope='other-login/session')
    with pytest.raises(p.RelayError) as error:
        connections.initialize('other', {**params, 'clientId': 'overflow'}, owner_scope='other-login/session')
    assert error.value.code is p.ErrorCode.LIMIT
    assert previous in connections.entries


def test_subscription_deny_uses_server_effect_and_username(tmp_path):
    case = setup(tmp_path)
    features = Features(Settings(), {'session_relay': False})
    policy = consent.relay_policy(case.db, features)
    spec = SimpleNamespace(spec={'auth_mode': 'subscription', 'person_id': 1})
    @write_intent
    def seed():
        with case.db() as db:
            store.add_decision(db, store.lock(db, 'relay'), p.Decision('decision', 'relay', '1',
                p.DecisionKind.TOOL_APPROVAL, DIGEST, 'Synthetic request',
                (p.DecisionChoice('deny', 'Allow', p.ChoiceEffect.ALLOW_ONCE),
                 p.DecisionChoice('reject', 'Deny', p.ChoiceEffect.DENY)),
                9999999999999, 0))
            consent.update(db, SimpleNamespace(user_id=1, username='alex'), True, consent.VERSION)
            db.commit()
    try:
        seed()
        params = dict(decisionId='decision', choiceId='deny', actionDigest=DIGEST, expectedRevision=0)
        assert not policy(spec, operation(params))
        assert policy(spec, operation({**params, 'choiceId': 'reject'}))
        assert not policy(spec, operation({**params, 'choiceId': 'reject', 'actionDigest': 'wrong'}))
        assert not policy(spec, replace(operation({**params, 'choiceId': 'reject'}), run_id='old'))
        features.choices['session_relay'] = True
        assert policy(spec, operation({'text': 'hello'}, 'turn.submit'))
        assert not policy(spec, replace(operation({'text': 'hello'}, 'turn.submit'),
                          actor=p.Actor(p.ActorKind.PERSON, '1', p.Channel.CHAT)))
    finally:
        case.engine.dispose()
