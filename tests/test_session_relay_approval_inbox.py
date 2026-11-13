"""Native display projection is exact, redacted, bounded and separate from authority."""
import asyncio
import json

import pytest
from fastapi import FastAPI
from test_session_relay_claude_public import SID, public_claude  # noqa: F401
from test_session_relay_claude_public_decisions import pending_hook, qualify
from test_session_relay_ownership import ownership_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.broker.claude_owner import ClaudeOwnerFactory
from acc.server.api.inbox import router
from acc.server.db import write_intent
from acc.server.models import AttentionItem, Event, Project, RelaySession, Session
from acc.server.session_relay import approval_inbox
from acc.session_relay.approval_facts import complete, native_facts

SDK_PUBLISH = ClaudeOwnerFactory.sdk_publish


async def test_native_review_uses_shipped_inbox_facts_and_never_a_legacy_token(public_claude, monkeypatch):  # noqa: F811
    c = public_claude
    qualify(c, monkeypatch)
    # Register the shipped Inbox routes on the same cookie-authenticated app.
    app = c.client._transport.app
    assert isinstance(app, FastAPI)
    app.include_router(router(c.ctx))
    @write_intent
    def project():
        with c.db() as db:
            project = Project(slug='fixture-native-inbox', name='Fixture Inbox', workspace_path='/tmp/example', account='fixture', owner='alex')
            db.add(project)
            db.flush()
            db.get(Session, SID).project_id = project.id
            db.commit()
    project()
    task, decision = await pending_hook(c)
    try:
        response = await c.client.get('/api/attention', params={'session_id': SID})
        assert response.status_code == 200, response.text
        items = response.json()
        item = next(item for item in items if item.get('relay_decision'))
        assert item['relay_decision'] == decision
        assert item['approval_card']['command'] == 'echo artificial'
        assert item['approval_card']['tool_name'] == 'Bash'
        assert item['approval_card']['diff'] is None
        assert item['relay_decision_available'] is True
        assert 'permission_id' not in item
        denied = await c.client.post(f"/api/attention/{item['id']}/permission",
            json={'token': 'a' * 32, 'decision': 'allow'})
        assert denied.status_code == 409
        assert not task.done()
        # A listed item alone is insufficient: missing session scope reveals no facts.
        with c.db() as db:
            rows = list(db.query(AttentionItem).all())
            assert approval_inbox.projections(c.ctx, db, rows, {}) == {}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('inputs', [None, {'command': 'é' * 5000}])
def test_incomplete_native_input_never_becomes_a_partial_command(inputs):
    assert native_facts('Bash', inputs, '/tmp/example')['acc_complete_input'] is None


async def test_projection_masks_secrets_and_rejects_old_run_facts(public_claude):  # noqa: F811
    c = public_claude
    @write_intent
    def write():
        with c.db() as db:
            row = db.get(RelaySession, SID)
            approval_inbox.publish(db, row, 'display-only', native_facts('Bash', {'command': 'echo artificial-secret'}, '/tmp/example'),
                lambda text: text.replace('artificial-secret', '[REDACTED]'))
            db.commit()
            event = db.query(Event).filter(Event.kind == 'RelayApproval').one()
            assert 'artificial-secret' not in str(event.payload)
            assert event.payload['approval_card']['command'] == 'echo [REDACTED]'
            assert event.payload['permission_input'] == '{"command":"echo [REDACTED]"}'
            assert approval_inbox.projections(c.ctx, db, list(db.query(AttentionItem).all()), {SID}) == {}
    write()


@pytest.mark.parametrize('value', [None, '', 'bad JSON', '[]', '{} ' + ' ' * 8192])
def test_malformed_display_input_is_not_complete(value):
    assert complete({'acc_complete_input': value}) is False


async def test_blind_native_allow_is_removed(public_claude):  # noqa: F811
    c = public_claude
    c.launch.publication({'kind': 'decision', 'decisionId': 'blind-display',
        'actionDigest': 'sha256:' + 'a' * 64, 'choices': ['accept', 'decline'],
        'expiresAtMs': 9999999999999, 'approval': {'acc_complete_input': 'bad JSON'}})
    from acc.server.models import RelayDecision
    with c.db() as db:
        record = db.get(RelayDecision, (SID, 'blind-display'))
        assert [choice['effect'] for choice in record.decision['choices']] == ['deny']
        event = db.query(Event).filter(Event.kind == 'RelayApproval').one()
        assert event.payload['permission_input'] is None
        assert event.payload['approval_card']['command'] is None
        assert json.dumps(event.payload)


@pytest.mark.parametrize('source', ['native_hook', 'sdk'])
@pytest.mark.parametrize('fault', ['none', 'input', 'tool', 'source', 'native_id'])
def test_complete_display_facts_must_match_exact_native_action(source, fault):
    from acc.session_relay import protocol as p
    from acc.session_relay.approval_facts import matches_action
    facts = native_facts('Bash', {'command': 'echo artificial'}, '/tmp/example', source=source)
    body = {'tool': 'Bash', 'input': {'command': 'echo artificial'}}
    if source == 'native_hook':
        body['nativeToolId'] = None
    digest = p.sha256_digest(p.canonical_json(body))
    if fault == 'input':
        facts['acc_complete_input'] = '{"command":"different"}'
    elif fault == 'tool':
        facts['tool_name'] = 'Write'
    elif fault == 'source':
        facts['source'] = 'unqualified'
    elif fault == 'native_id':
        facts['native_tool_id'] = 'different-tool'
    assert matches_action(facts, digest) is (fault == 'none' or source == 'sdk' and fault == 'native_id')


@pytest.mark.parametrize('only_allow', [False, True])
async def test_unbound_complete_card_never_offers_allow(public_claude, only_allow):  # noqa: F811
    from acc.server.models import RelayDecision
    from acc.session_relay import protocol as p
    c = public_claude
    value = {'kind': 'decision', 'decisionId': 'unbound-display', 'actionDigest': p.sha256_digest(b'another action'),
        'choices': ['accept'] if only_allow else ['accept', 'decline'], 'expiresAtMs': 9999999999999,
        'approval': native_facts('Bash', {'command': 'echo artificial'}, '/tmp/example')}
    if only_allow:
        with pytest.raises(p.RelayError):
            c.launch.publication(value)
    else:
        c.launch.publication(value)
    with c.db() as db:
        record = db.get(RelayDecision, (SID, 'unbound-display'))
        if only_allow:
            assert record is None
        else:
            assert [choice['effect'] for choice in record.decision['choices']] == ['deny']
            assert 'only denial' in record.decision['description']
            event = db.query(Event).filter(Event.kind == 'RelayApproval').one()
            assert event.payload['approval_card']['command'] is None
            assert event.payload['permission_input'] is None


async def test_production_sdk_card_producer_preserves_the_native_action_binding(public_claude):  # noqa: F811
    from types import SimpleNamespace

    from acc.server.models import RelayDecision
    from acc.session_relay import protocol as p
    c = public_claude
    inputs = {'command': 'echo artificial-sdk'}
    digest = p.sha256_digest(p.canonical_json({'tool': 'Bash', 'input': inputs}))
    await SDK_PUBLISH(SimpleNamespace(runtime=c.broker_state), {
        'kind': 'decision', 'id': 'sdk-bound-display', 'tool': 'Bash', 'input': inputs,
        'actionDigest': digest, 'expiresAtMs': 9999999999999})
    with c.db() as db:
        record = db.get(RelayDecision, (SID, 'sdk-bound-display'))
        assert record.decision['actionDigest'] == digest
        assert [choice['effect'] for choice in record.decision['choices']] == ['allow_once', 'deny']
        event = db.query(Event).filter(Event.kind == 'RelayApproval').one()
        assert event.payload['approval_card']['command'] == 'echo artificial-sdk'
        assert event.payload['approval_card']['cwd'] == c.broker_state.spec.binding.workspace
