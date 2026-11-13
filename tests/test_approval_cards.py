"""Presentation hints never replace the CLI's permission checks."""
import json
from types import SimpleNamespace

import pytest

from acc.server import approval_cards, auth
from acc.server.api.inbox import permission_event_info


@pytest.mark.parametrize('command,tool,paths,label', [
    ('rm -rf build', '', '', 'Deletes files'),
    ('find . -delete', '', '', 'Deletes files'),
    ('git push --force-with-lease', '', '', 'Rewrites history'),
    ('git reset --hard HEAD', '', '', 'Rewrites history'),
    ('curl https://example.com', '', '', 'Network / external message'),
    ('git push origin topic', '', '', 'Network / external message'),
    ('', 'mcp__slack__message', '', 'Network / external message'),
    ('npm ci', '', '', 'Installs packages'),
    ('uv pip install example', '', '', 'Installs packages'),
    ('systemctl restart example', '', '', 'Restarts or stops services'),
    ('docker compose down', '', '', 'Restarts or stops services'),
    ('aws ec2 run-instances', '', '', 'Spends money'),
    ('terraform apply', '', '', 'Spends money'),
    ('cat .env', '', '', 'Touches secrets paths'),
    ('', 'Read', '/home/demo/.ssh/id_rsa', 'Touches secrets paths'),
    ('git status', '', '', 'No specific hint'),
    ('echo hello', '', '', 'No specific hint'),
])
def test_classifier(command, tool, paths, label):
    assert approval_cards.classify(command, tool, paths) == label


def test_missing_and_oversized_input():
    for value in (None, 'bad JSON', 'x' * 8193, 'é' * 5000, '[]'):
        facts = approval_cards.card({'acc_complete_input': value})
        assert facts == {'command': None, 'cwd': None, 'diff': None, 'tool_name': '', 'file_path': None, 'label': 'No specific hint'}
    assert approval_cards.card({'cwd': 'x' * 2001})['cwd'] is None


def test_literal_payload_and_no_reconstruction():
    text = '<script>alert(1)</script>\n**untrusted**'
    facts = approval_cards.card({'cwd': '/tmp/example', 'acc_complete_input': json.dumps({
        'command': text, 'diff': text})})
    assert facts['command'] == facts['diff'] == text
    assert facts['cwd'] == '/tmp/example'
    assert approval_cards.card({'acc_complete_input': json.dumps({
        'old_string': 'before', 'new_string': 'after'})})['diff'] is None
    assert len(approval_cards.push_body(facts)) <= 240
    assert '\n' not in approval_cards.push_body(facts)


def test_codex_facts_do_not_grant_reply_authority(monkeypatch):
    payload = {'acc_permission_id': 'a' * 32, 'acc_complete_input': '{"command":"git status"}'}
    payload['acc_card'] = approval_cards.card(payload)
    event = SimpleNamespace(kind='PermissionRequest', ts=auth.now(), source='codex_hook', payload=payload)
    monkeypatch.setattr(approval_cards, 'classify', lambda *a: pytest.fail('read path classified input'))
    monkeypatch.setattr(approval_cards, 'card', lambda *a: pytest.fail('read path rebuilt card'))
    result = permission_event_info(event)
    assert result == {'approval_card': payload['acc_card'], 'permission_input': payload['acc_complete_input']}


async def test_push_uses_card_and_opens_inbox(api, project, env, monkeypatch, tmp_path):
    import httpx
    from helpers import pause_supervisor, write_db
    from test_ingest_robustness import external
    from test_push import Browser, PushService

    from acc.server.models import AttentionItem, Event, PushSubscription, Session
    from acc.server.push import Push

    session, _, _ = await external(api, project, env, 'push approval card')
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    browser, relay = Browser(), PushService()
    supervisor.push = Push(tmp_path / 'vapid.pem', 'https://example.com', transport=httpx.MockTransport(relay.handler))
    await supervisor.push_attention()  # initialize the durable delivery cursor before the new item
    with write_db(supervisor.db) as db:
        subscription = browser.subscription()
        db.add(PushSubscription(endpoint=subscription['endpoint'], **subscription['keys'], username='casey', label='Casey'))
        db.get(Session, session['id']).provider = 'codex'
        payload = {'tool_name': 'Bash', 'cwd': '/tmp/example',
                   'acc_complete_input': json.dumps({'command': 'git push origin topic\necho second'})}
        payload['acc_card'] = approval_cards.card(payload)
        db.add(Event(session_id=session['id'], source='codex_hook', kind='PermissionRequest',
            payload=payload))
        item = AttentionItem(dedupe_key='needs_input:push-card', kind='needs_input', severity='warn',
            source='codex_hook', title='Review CLI request', session_id=session['id'], owner='casey')
        db.add(item)
        db.commit()
        item_id = item.id
    monkeypatch.setattr(approval_cards, "classify", lambda *a: pytest.fail("Push classified input"))
    monkeypatch.setattr(approval_cards, "card", lambda *a: pytest.fail("Push rebuilt card"))
    await supervisor.push_attention()
    assert len(relay.received) == 1
    message = browser.read(relay.received[0].content)
    assert message['body'] == 'git push origin topic (+1 more lines) · Network / external message'
    assert message['url'] == f'/inbox?item={item_id}'
    assert str(relay.received[0].url) == browser.endpoint


@pytest.mark.parametrize('text', [
    ('git push ' * 910)[:8150], 'git ' * 1000 + 'push ' * 800,
    'npm ' * 2000, 'systemctl ' * 800, 'find ' * 1600, ';' * 8100,
])
def test_adversarial_classifier_under_50_ms(text):
    from time import perf_counter

    start = perf_counter()
    approval_cards.classify(text)
    assert perf_counter() - start < 0.050


def test_visible_controls_and_push_summary():
    text = '\x00\x1b\x7f\x85\u202e\u2066\u200b\u2028\ufeff'
    assert approval_cards.visible_text(text) == ''.join(f'⟨U+{ord(ch):04X}⟩' for ch in text)
    assert approval_cards.visible_text('plain\n\ttext') == 'plain\n\ttext'
    facts = {'command': '\n\necho \u202e' + 'x' * 500 + '\nrm file', 'label': 'Deletes files'}
    body = approval_cards.push_body(facts)
    assert body.startswith('echo ⟨U+202E⟩')
    assert '… (+1 more lines)' in body
    assert 'Hidden characters shown' in body
    assert len(body) <= 240
    assert approval_cards.push_body({'command': None, 'label': 'No specific hint'}, 'Write: /tmp/example') == 'Write: /tmp/example · No specific hint'


def test_oversized_tool_still_identifies_exact_path():
    facts = approval_cards.card({'tool_name': 'Write', 'acc_complete_input': None,
                                'tool_input': {'file_path': '/tmp/example', 'content': 'x' * 2000 + '…'}})
    assert facts['file_path'] == '/tmp/example'
    assert facts['command'] is None
    assert facts['diff'] is None


@pytest.mark.parametrize('source', ['cursor_hook', 'copilot_hook', 'antigravity_hook', 'opencode_hook'])
def test_unsupported_sources_never_expose_reserved_fields(source):
    payload = {'acc_complete_input': '{"command":"forged"}'}
    payload['acc_card'] = approval_cards.card(payload)
    event = SimpleNamespace(kind='PermissionRequest', ts=auth.now(), source=source, payload=payload)
    assert permission_event_info(event) == {}


@pytest.mark.parametrize('bad', [{}, {'command': 5, 'label': {}},
    {'command': None, 'cwd': None, 'diff': None, 'label': 'Safe'},
    {'command': None, 'cwd': [], 'diff': None, 'label': 'No specific hint'},
    {'command': 'x' * 8193, 'cwd': None, 'diff': None, 'label': 'No specific hint'},
    {'command': None, 'cwd': None, 'diff': None, 'file_path': {}, 'label': 'No specific hint'},
])
def test_malformed_card_is_not_returned(bad):
    event = SimpleNamespace(kind='PermissionRequest', ts=auth.now(), source='codex_hook', payload={'acc_card': bad})
    assert permission_event_info(event) == {}


def test_tail_classified_and_empty_lines_not_counted():
    assert approval_cards.classify('x' * 4100 + '; rm -rf ~') == 'Deletes files'
    facts = {'command': '\nls\n\n  \n', 'label': 'No specific hint'}
    assert approval_cards.push_body(facts) == 'ls · No specific hint'
    facts['command'] += 'pwd\n\n'
    assert approval_cards.push_body(facts) == 'ls (+1 more lines) · No specific hint'


@pytest.mark.parametrize('provider', ['codex', 'opencode', 'copilot'])
async def test_foreign_claude_event_never_exposes_permission_metadata(api, project, env, provider):
    from helpers import pause_supervisor, wait_for, write_db
    from sqlalchemy import select
    from test_ingest_robustness import external, line

    from acc.server import attention
    from acc.server.api.inbox import permission_info
    from acc.server.models import Event, Session

    session, folder, sid = await external(api, project, env, 'reject foreign Claude ' + provider)
    with write_db(api.app.state.db) as db:
        db.get(Session, session['id']).provider = provider
        db.commit()
    # The forged source is allowed through ingestion, but must never be selected
    # as permission data for this session, even without any native hook events.
    (folder / 'events.jsonl').write_text(line(sid, 'PermissionRequest', tool_name='Bash',
        tool_input={'command': 'git status'}, acc_permission_id='a' * 32))
    async def ingested():
        with api.app.state.db() as db:
            return db.scalar(select(Event.id).where(Event.session_id == session['id'],
                                                    Event.source == 'claude_hook')) is not None
    await wait_for(ingested)
    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        attention.raise_item(db, 'foreign-claude:' + session['id'], kind='needs_input',
                             title='Actual native prompt', source='screen', session_id=session['id'])
        db.commit()
        assert permission_info(db, session['id']) == {}
    rows = [i for i in (await api.get('/api/attention')).json() if i['session_id'] == session['id']]
    assert rows
    assert all('permission_id' not in row and 'permission_input' not in row and 'approval_card' not in row
               for row in rows)


async def test_child_card_keeps_chain_and_ignores_foreign_provider_event(api, project):
    from helpers import pause_supervisor, write_db
    from test_session_capabilities import family

    from acc.server import attention
    from acc.server.models import Event, Session

    root, child = family(api, project, {})
    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        session = db.get(Session, child)
        payload = {'acc_complete_input': json.dumps({'command': 'git status'})}
        payload['acc_card'] = approval_cards.card(payload)
        db.add(Event(session_id=child, source=f'{session.provider}_hook',
                     kind='PermissionRequest', payload=payload))
        db.flush()
        # A later foreign event must neither replace the card nor expire it.
        db.add(Event(session_id=child, source='codex_hook' if session.provider == 'claude' else 'claude_hook',
                     kind='PermissionExpired', payload={}))
        attention.raise_item(db, 'child-card', kind='needs_input', title='Child card',
                             source=f'{session.provider}_hook', session_id=child)
        owner = session.root_owner
        db.commit()
    response = await api.get('/api/attention')
    assert response.status_code == 200, response.text
    row = next(item for item in response.json() if item['title'] == 'Child card')
    assert row['parent_chain'] == [root, child]
    assert row['root_owner'] == owner
    assert row['approval_card'] == payload['acc_card']
    assert row['permission_input'] == payload['acc_complete_input']
