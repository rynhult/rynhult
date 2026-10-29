"""Shown-item bulk dismissal is scoped, reversible and never a decision."""
from datetime import timedelta

from helpers import pause_supervisor, write_db
from test_auth import cookie_client, do_setup
from test_users import browser, csrf, join

from acc.server import attention, auth
from acc.server.models import AttentionItem


async def seed(api, *, owner=None):
    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        for key, kind in [('note', 'guardrail_blocked'), ('note2', 'state'), ('question', 'needs_input'),
                          ('approval', 'publish_request'), ('tool', 'tool_request'), ('unknown', 'future_decision')]:
            attention.raise_item(db, f'ui-small:{key}', kind=kind, title=key, source='test', owner=owner)
        attention.raise_item(db, 'backup:failed:demo', kind='backup', title='persistent', source='test', owner=owner)
        db.commit()
    return (await api.get('/api/attention')).json()


def snapshot(items):
    return [{'id': row['id'], 'updated_at': row['updated_at']} for row in items]


async def test_shown_notices_only_decisions_skipped_and_real_undo(api):
    items = await seed(api)
    shown = [row for row in items if row['title'] != 'note2']
    response = await api.post('/api/attention/dismiss', json={'items': snapshot(shown)})
    assert response.status_code == 200, response.text
    batch = response.json()
    assert batch['dismissed'] == 1 and batch['skipped'] == 5
    remaining = (await api.get('/api/attention')).json()
    assert 'note' not in {row['title'] for row in remaining}
    assert {'note2', 'question', 'approval', 'tool', 'unknown', 'persistent'} <= {row['title'] for row in remaining}
    undo = await api.post('/api/attention/dismiss/undo', json={'batch': batch['batch']})
    assert undo.status_code == 200 and undo.json()['restored'] == 1, undo.text
    assert 'note' in {row['title'] for row in (await api.get('/api/attention')).json()}


async def test_stale_confirmation_and_changed_notice_undo_cannot_overwrite_new_state(api):
    items = await seed(api)
    note = next(row for row in items if row['title'] == 'note')
    with write_db(api.app.state.db) as db:
        db.get(AttentionItem, note['id']).updated_at += timedelta(seconds=1)
        db.commit()
    refused = await api.post('/api/attention/dismiss', json={'items': [snapshot([note])[0]]})
    assert refused.json()['dismissed'] == 0
    current = next(row for row in (await api.get('/api/attention')).json() if row['id'] == note['id'])
    done = (await api.post('/api/attention/dismiss', json={'items': snapshot([current])})).json()
    with write_db(api.app.state.db) as db:
        item = db.get(AttentionItem, note['id'])
        item.resolved_at = attention.now()
        db.commit()
    undone = await api.post('/api/attention/dismiss/undo', json={'batch': done['batch']})
    assert undone.json()['restored'] == 0
    with api.app.state.db() as db:
        assert db.get(AttentionItem, note['id']).resolved_at is not None


async def test_undo_expires_and_a_batch_cannot_be_reused_to_restore_a_later_dismissal(api, monkeypatch):
    items = await seed(api)
    note = next(row for row in items if row['title'] == 'note')
    done = (await api.post('/api/attention/dismiss', json={'items': snapshot([note])})).json()
    assert (await api.post('/api/attention/dismiss/undo', json={'batch': done['batch']})).json()['restored'] == 1
    current = next(row for row in (await api.get('/api/attention')).json() if row['id'] == note['id'])
    newer = (await api.post('/api/attention/dismiss', json={'items': snapshot([current])})).json()
    assert (await api.post('/api/attention/dismiss/undo', json={'batch': done['batch']})).json()['restored'] == 0
    future = auth.now() + timedelta(seconds=11)
    monkeypatch.setattr(auth, 'now', lambda: future)
    expired = await api.post('/api/attention/dismiss/undo', json={'batch': newer['batch']})
    assert expired.status_code == 409


async def test_member_cannot_dismiss_foreign_items_or_undo_another_viewers_batch(api, env, member_accounts):
    await do_setup(api, env)
    owner = cookie_client(api)
    link = (await owner.post('/api/users/invites', headers=csrf(owner), json={'account': 'dana-agent'})).json()['link']
    riley = browser(api)
    assert (await join(riley, link, 'riley')).status_code == 200
    items = await seed(api)
    note = next(row for row in items if row['title'] == 'note')
    denied = await riley.post('/api/attention/dismiss', headers=csrf(riley), json={'items': snapshot([note])})
    assert denied.status_code == 200 and denied.json()['dismissed'] == 0
    done = (await owner.post('/api/attention/dismiss', headers=csrf(owner), json={'items': snapshot([note])})).json()
    undo = await riley.post('/api/attention/dismiss/undo', headers=csrf(riley), json={'batch': done['batch']})
    assert undo.status_code == 404


async def test_bulk_body_is_bounded_and_validated_and_operations_audited(api):
    from sqlalchemy import select

    from acc.server.models import AuditLog

    items = await seed(api)
    note = next(row for row in items if row['title'] == 'note')
    for body in ({'items': []}, {'items': snapshot([note, note])},
                 {'items': snapshot([note]) * 501}, {'items': [{'id': True, 'updated_at': note['updated_at']}]},
                 {'items': [{'id': note['id'], 'updated_at': '2026-10-29T12:00:00'}]}):
        assert (await api.post('/api/attention/dismiss', json=body)).status_code == 422
    done = (await api.post('/api/attention/dismiss', json={'items': snapshot([note])})).json()
    assert (await api.post('/api/attention/dismiss/undo', json={'batch': done['batch']})).status_code == 200
    with api.app.state.db() as db:
        batch = db.get(AuditLog, done['batch'])
        assert batch.action == 'attention.dismiss' and batch.detail['items'] == [note['id']]
        undo = db.scalar(select(AuditLog).where(AuditLog.action == 'attention.dismiss_undo'))
        assert undo.actor == batch.actor and undo.detail == {'restored': 1}
