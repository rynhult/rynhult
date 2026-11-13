"""Parent grants fence descendants and keep child requests with the root person."""
import json
from types import SimpleNamespace

import pytest
from helpers import write_db
from sqlalchemy import select
from test_team import lead, request

from acc.server import child_caps
from acc.server.compiler import LaunchInputs
from acc.server.models import Session, ToolRequest


@pytest.mark.parametrize('dimension,asked,parent,expected', [
    ('tools', ['read', 'write'], ['read'], ['read']),
    ('folders', ['/project', '/private'], ['/project'], ['/project']),
    ('network', True, False, False),
    ('installs', True, False, False),
    ('ports', [8000, 8001], [8001], [8001]),
    ('security', {'bypass': True}, {'bypass': False}, {'bypass': False}),
    ('providers', ['claude', 'codex'], ['claude'], ['claude']),
    ('models', ['large', 'small'], ['small'], ['small']),
    ('seconds', 600, 30, 30),
])
def test_intersection(dimension, asked, parent, expected):
    assert child_caps.intersect({dimension: asked}, {dimension: parent}) == {dimension: expected}


def test_unknown_dimensions_fail_closed():
    with pytest.raises(child_caps.Refused):
        child_caps.intersect({'network': True}, {})


async def test_nested_proofs_and_parent_change_fence_live_children(api, project, env):
    root = await lead(api, project, env)
    a = await request(api, env, root, 'start', name='A', agent='claude', profile=None, task='work')
    assert a['ok'], a
    b = await request(api, env, a['result'], 'start', name='B', agent='claude', profile=None, task='work')
    assert b['ok'], b
    sup = api.app.state.supervisor
    with write_db(sup.db) as db:
        child = db.get(Session, b['result']['id'])
        parent = db.get(Session, a['result']['id'])
        saved = json.loads(child.launch_spec.files[child_caps.FILE])
        assert saved['chain'] == [root['id'], parent.id]
        child_caps.check(db, child)
        before = child_caps.grant_digest(db, child)
        # A changed frozen generation cannot be borrowed by its old descendants.
        parent.launch_spec.digest = 'changed-parent-generation'
        db.commit()
        assert child_caps.grant_digest(db, child) != before
        with pytest.raises(child_caps.Refused, match=f'limited by {parent.id}'):
            child_caps.check(db, child)
    # Reconciliation enforces the fence on running children as well as resumes.
    async def stopped():
        response = await api.get(f"/api/sessions/{b['result']['id']}")
        return response.json()['desired_state'] == 'stopped'
    from helpers import wait_for
    await wait_for(stopped)


async def test_parent_is_immutable_and_agent_cannot_answer_child_request(api, project, env):
    root = await lead(api, project, env)
    child = await request(api, env, root, 'start', name='A', agent='claude', profile=None, task='work')
    assert child['ok'], child
    sid = child['result']['id']
    service = api.app.state.supervisor.tools
    rid = service.request_session(sid, manager='npm', name='prettier', version='3.6.2', reason='format')
    with write_db(api.app.state.db) as db:
        row = db.get(ToolRequest, rid)
        assert row.owner == db.get(Session, root['id']).owner
        from acc.server.models import AttentionItem
        card = db.scalar(select(AttentionItem).where(AttentionItem.dedupe_key == 'tool:' + rid))
        assert root['id'] in card.detail and sid in card.detail
        from acc.server import approvals
        assert approvals.evaluate_tool(db, row, root['id'])['source'] == 'P'
    from acc.server.tools import ToolRefused
    with pytest.raises(ToolRefused):
        service.decide(rid, 'session:' + root['id'], True)
    with write_db(api.app.state.db) as db:
        row = db.get(Session, sid)
        row.parent_session_id = None
        with pytest.raises(child_caps.Refused, match='immutable'):
            db.flush()
        db.rollback()


def test_read_only_parent_cannot_launch_writer(monkeypatch):
    parent = SimpleNamespace(id='parent', launch_spec=SimpleNamespace(inputs=LaunchInputs(provider='claude', permission_mode='plan').as_dict()))
    monkeypatch.setattr(child_caps, 'check', lambda *_: None)
    with pytest.raises(child_caps.Refused, match='limited by parent: parent is read-only'):
        child_caps.constrain(None, parent, LaunchInputs(provider='claude'), {})


@pytest.mark.parametrize('held', [False, True])
async def test_child_prompt_push_keeps_root_person_and_chain(api, project, env, tmp_path, held):
    import httpx
    from helpers import pause_supervisor
    from test_push import Browser, PushService

    from acc.server import approval_cards, attention
    from acc.server.models import Event, PushSubscription
    from acc.server.push import Push

    root = await lead(api, project, env)
    child = await request(api, env, root, 'start', name='A', agent='claude', profile=None, task='work')
    assert child['ok'], child
    sup = api.app.state.supervisor
    await pause_supervisor(api)
    browser, relay = Browser(), PushService()
    sup.push = Push(tmp_path / 'vapid.pem', 'https://example.com', transport=httpx.MockTransport(relay.handler))
    await sup.push_attention()
    with write_db(sup.db) as db:
        owner = db.get(Session, root['id']).owner
        subscription = browser.subscription()
        db.add(PushSubscription(endpoint=subscription['endpoint'], **subscription['keys'], username=owner, label='Root person'))
        if held:
            sup.push_presence.update(owner, 'login', 'tab', '/inbox', 0, True)
        facts = {'acc_complete_input': json.dumps({'command': 'git status'})}
        facts['acc_card'] = approval_cards.card(facts)
        db.add(Event(session_id=child['result']['id'], source='claude_hook', kind='PermissionRequest', payload=facts))
        attention.raise_item(db, 'child-prompt', kind='needs_input', source='claude_hook',
                             title='Approve child action', session_id=child['result']['id'])
        db.commit()
    await sup.push_attention()
    if held:
        assert relay.received == []
        sup.push_presence.update(owner, 'login', 'tab', '/inbox', 0, False)
        await sup.push_attention()
    assert len(relay.received) == 1
    payload = browser.read(relay.received[0].content)
    if held:
        assert payload == {'title': '1 item still needs you', 'url': '/inbox',
                           'tag': 'presence-summary', 'critical': False}
    else:
        assert payload['body'] == (f"git status · No specific hint · Parent chain: "
                                   f"{root['id']} → {child['result']['id']}")
    assert str(relay.received[0].url) == browser.endpoint
    await sup.push.close()
    sup.push = None


def test_launch_intersects_mcps_and_inherits_parent_security(monkeypatch):
    parent = SimpleNamespace(id='parent', project_id=1, owner='alex', host='local', run_as='agent', launch_spec=SimpleNamespace(
        inputs=LaunchInputs(provider='claude', mcps=('shared',)).as_dict()))
    monkeypatch.setattr(child_caps, 'check', lambda *_: None)
    from acc.server import security_settings
    monkeypatch.setattr(security_settings, 'frozen_protections', lambda *_: (
        {'tool_approval': True, 'local_only': True, 'custom': [{'pattern': 'blocked'}]},
        {'force_push': True, 'provider_prompts': True}))
    monkeypatch.setattr(security_settings, 'resolve', lambda *_, **__: {
        'switches': {'force_push': True, 'provider_prompts': True}})
    inputs, policy = child_caps.constrain(None, parent,
        LaunchInputs(provider='claude', mcps=('shared', 'private')), {
            'security': {'switches': {'force_push': False, 'provider_prompts': False}},
            'rules': {'force_push': False}, 'tool_approval': False, 'local_only': False, 'custom': []})
    assert inputs.mcps == ('shared',)
    assert policy['dropped_mcps'] == ['private']
    assert policy['rules']['force_push']
    assert policy['security']['switches']['provider_prompts']
    assert policy['tool_approval'] and policy['local_only']
    assert policy['custom'] == [{'pattern': 'blocked'}]


def test_parent_cannot_grant_provider_bypass(monkeypatch):
    parent = SimpleNamespace(id='parent', launch_spec=SimpleNamespace(
        inputs=LaunchInputs(provider='claude').as_dict()))
    monkeypatch.setattr(child_caps, 'check', lambda *_: None)
    with pytest.raises(child_caps.Refused, match='parent does not permit provider bypass'):
        child_caps.constrain(None, parent, LaunchInputs(provider='codex', approval='never'), {})


async def test_helper_reports_dropped_mcps_in_reply_team_and_audits(api, project, env):
    from acc.server.models import AuditLog
    root = await lead(api, project, env, agents=[{'agent': 'claude', 'profile': 'frontend'}])
    result = await request(api, env, root, 'start', name='Frontend helper', agent='claude', profile='frontend', task='work')
    assert result['ok'], result
    helper = result['result']
    assert helper['dropped_mcps'] == ['playwright']
    detail = (await api.get(f"/api/sessions/{helper['id']}")).json()
    assert detail['dropped_mcps'] == ['playwright']
    assert detail['launch_spec']['inputs']['mcps'] == []
    team = (await api.get(f"/api/sessions/{root['id']}/team")).json()
    assert next(row for row in team['members'] if row['id'] == helper['id'])['dropped_mcps'] == ['playwright']
    with api.app.state.db() as db:
        created = db.scalar(select(AuditLog).where(AuditLog.action == 'session.create', AuditLog.target == helper['id']))
        started = db.scalar(select(AuditLog).where(AuditLog.action == 'team.start', AuditLog.target == root['id']))
        assert created.detail['dropped_mcps'] == started.detail['dropped_mcps'] == ['playwright']
