"""Audit search remains owner-only and searches before bounded pagination."""
from helpers import write_db
from test_auth import cookie_client, do_setup
from test_users import browser, csrf, join

from acc.server.models import AuditLog


async def test_audit_search_fields_literal_matching_and_pages(api):
    with write_db(api.app.state.db) as db:
        for actor, action, target, detail in [('needle', 'demo.a', None, {}), ('other', 'needle.b', None, {}),
                                               ('other', 'demo.c', 'needle', {}), ('other', 'demo.d', None, {'note': 'needle_% café \"quote\"'})]:
            db.add(AuditLog(actor=actor, action=action, target=target, detail=detail))
        db.commit()
    first = (await api.get('/api/audit', params={'q': 'needle', 'limit': 2, 'page': True})).json()
    assert first['has_more'] is True and len(first['items']) == 2
    assert 'total' not in first
    second = (await api.get('/api/audit', params={'q': 'needle', 'limit': 2, 'before_id': first['next_before'], 'page': True})).json()
    assert len(second['items']) == 2 and second['has_more'] is False and second['next_before'] is None
    assert not {i['id'] for i in first['items']} & {i['id'] for i in second['items']}
    assert len((await api.get('/api/audit', params={'q': '_%', 'page': True})).json()['items']) == 1
    assert len((await api.get('/api/audit', params={'q': 'café', 'page': True})).json()['items']) == 1
    assert len((await api.get('/api/audit', params={'q': '"quote"', 'page': True})).json()['items']) == 1
    assert len((await api.get('/api/audit', params={'q': 'NEEDLE', 'actor': 'needle', 'page': True})).json()['items']) == 1
    assert (await api.get('/api/audit', params={'q': 'x' * 257})).status_code == 422
    assert (await api.get('/api/audit', params={'before_id': 0})).status_code == 422
    assert (await api.get('/api/audit', params={'before_id': 2**63})).status_code == 422
    assert isinstance((await api.get('/api/audit')).json(), list)


async def test_member_cannot_search_audit(api, env, member_accounts):
    await do_setup(api, env)
    owner = cookie_client(api)
    link = (await owner.post('/api/users/invites', headers=csrf(owner), json={'account': 'dana-agent'})).json()['link']
    async with browser(api) as member:
        assert (await join(member, link, 'riley')).status_code == 200
        assert (await member.get('/api/audit', params={'q': 'needle', 'page': True})).status_code == 403


async def test_large_history_uses_one_keyset_query_without_count_or_offset(api):
    from sqlalchemy import event, insert

    from acc.server.models import utcnow

    with write_db(api.app.state.db) as db:
        db.execute(insert(AuditLog), [dict(ts=utcnow(), actor='history', action='demo.history',
                                         target=str(index), detail={'text': 'record'}) for index in range(20000)])
        db.commit()
    engine = api.app.state.db.kw['bind']
    statements = []

    def record(_connection, _cursor, sql, _parameters, _context, _many):
        if 'audit_log' in sql.lower():
            statements.append((sql.lower(), _parameters))

    event.listen(engine, 'before_cursor_execute', record)
    try:
        first = await api.get('/api/audit', params={'page': True, 'limit': 10, 'q': 'record'})
        assert first.status_code == 200, first.text
        assert first.json()['has_more'] and len(first.json()['items']) == 10
        cursor = first.json()['next_before']
        # Newly arriving events must not duplicate/shift forward pages.
        with write_db(api.app.state.db) as db:
            db.add(AuditLog(actor='new', action='demo.new', detail={'text': 'record'}))
            db.commit()
        statements.clear()
        second = await api.get('/api/audit', params={'page': True, 'limit': 10, 'q': 'record', 'before_id': cursor})
        assert second.status_code == 200, second.text
        assert all(item['id'] < cursor for item in second.json()['items'])
        assert len(statements) == 1
        sql, parameters = statements[0]
        assert 'count(' not in sql and 'audit_log.id <' in sql and 'limit' in sql
        # SQLite's compiler may append OFFSET 0; it must never skip history.
        if engine.dialect.name == 'sqlite':
            assert parameters[-2:] == (11, 0)
        else:
            assert 'offset' not in sql
    finally:
        event.remove(engine, 'before_cursor_execute', record)


async def test_large_no_match_scan_is_interrupted_and_connection_is_reusable(api, monkeypatch):
    from sqlalchemy import insert

    from acc.server import audit_search
    from acc.server.models import utcnow

    with write_db(api.app.state.db) as db:
        db.execute(insert(AuditLog), [dict(ts=utcnow(), actor='history', action='demo.history',
                                         detail={'text': 'record'}) for _ in range(20000)])
        db.commit()
    monkeypatch.setattr(audit_search, 'MAX_SQLITE_STEPS', 2000)
    response = await api.get('/api/audit', params={'page': True, 'q': 'absent-canary'})
    assert response.status_code == 503 and 'Narrow' in response.text, response.text
    # The progress callback is removed even when SQLite interrupts its statement.
    monkeypatch.setattr(audit_search, 'MAX_SQLITE_STEPS', 500000)
    assert (await api.get('/api/audit', params={'page': True, 'limit': 1})).status_code == 200
