"""Captured stock envelopes can retain frozen settings, never override them."""
import copy
import json
from pathlib import Path

import pytest

from acc.session_relay.native_turn import NativeTurnPolicy
from acc.session_relay.protocol import RelayError

pytestmark = pytest.mark.relay_native


@pytest.fixture
def captured():
    evidence = Path(__file__).resolve().parents[1] / 'docs/handoffs/2026-11-11-relay-m3-codex-owned-native.json'
    value = json.loads(evidence.read_text())
    params = next(item['params'] for item in value['requests'] if item['method'] == 'turn/start')
    resume = next(reply['result'] for reply in value['replies'] if 'modelProvider' in reply.get('result', {}))
    return params, resume


def test_stock_envelope_projects_only_text_and_cannot_mutate_frozen_policy(captured):
    params, start = captured
    policy = NativeTurnPolicy.from_start(params['threadId'], start)
    assert policy.project(params) == {'text': 'artificial unauthorized input'}
    start['model'] = 'mutated-owner-object'
    params['model'] = 'mutated-owner-object'
    with pytest.raises(RelayError):
        policy.project(params)


@pytest.mark.parametrize('field,value', [
    ('model', 'unreviewed'), ('approvalPolicy', 'never'), ('approvalsReviewer', 'guardian_subagent'),
    ('cwd', '/other'), ('runtimeWorkspaceRoots', ['/other']), ('serviceTier', 'fast'),
    ('effort', 'high'), ('permissions', {'type': 'fullAccess'}), ('parentTurnId', 'other-turn'),
    ('responsesapiClientMetadata', {'actor': 'forged'}), ('actor', None),
    ('inputEpoch', 2), ('turnTrigger', 'automated'), ('disabledPluginIds', ['other']),
    ('input', [{'type': 'image', 'url': 'file:///fixture.png'}]),
    ('input', [{'type': 'text', 'text': 'fixture', 'text_elements': [{'start': 0, 'end': 1}]}]),
    ('input', [{'type': 'text', 'text': 'x' * 8193}]),
    ('input', [{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': 'b'}]),
])
def test_stock_envelope_refuses_config_rich_input_and_forged_identity(captured, field, value):
    params, start = captured
    policy = NativeTurnPolicy.from_start(params['threadId'], start)
    altered = copy.deepcopy(params)
    altered[field] = value
    with pytest.raises(RelayError):
        policy.project(altered)


def test_stock_envelope_refuses_foreign_thread_even_with_same_frozen_settings(captured):
    params, start = captured
    policy = NativeTurnPolicy.from_start(params['threadId'], start)
    params['threadId'] = 'other-thread'
    with pytest.raises(RelayError):
        policy.project(params)
