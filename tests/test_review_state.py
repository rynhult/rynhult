"""Review signals use fixture evidence only; never contact GitHub."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from acc.server.github import GitHub
from acc.server.review_state import pull_lifecycle, ready_for_review


def test_ready_requires_finished_fresh_branch_changes():
    stamp = datetime.now(timezone.utc)
    values = dict(management='managed', desired_state='running', observed_state='idle', activity=None,
                  status_at=stamp, changes_at=stamp, changes={'git': True, 'branch': 'acc/task', 'files': 1})
    assert ready_for_review(SimpleNamespace(**values))
    for patch in ({'observed_state': 'waiting_input'}, {'observed_state': 'active'},
                  {'desired_state': 'archived'}, {'management': 'external'},
                  {'changes_at': stamp - timedelta(seconds=1)}, {'activity': 'API error'},
                  {'changes': {'git': True, 'branch': 'main', 'files': 1}},
                  {'changes': {'git': True, 'branch': 'acc/task', 'files': 0}}):
        assert not ready_for_review(SimpleNamespace(**(values | patch)))
    assert ready_for_review(SimpleNamespace(**(values | {'changes': {'git': True, 'branch': 'acc/task', 'commits': 1}})))


@pytest.mark.parametrize(('pull', 'checks', 'reviews', 'expected'), [
    ({'state': 'closed', 'merged_at': 'today'}, [], [], 'merged'),
    ({'state': 'closed'}, [], [], 'closed'),
    ({'draft': True}, [], [], 'draft'),
    ({}, [], [], 'open'),
    ({}, [{'status': 'in_progress'}], [], 'checks_running'),
    ({}, [{'status': 'completed', 'conclusion': 'failure'}], [], 'checks_failed'),
    ({}, [{'status': 'completed', 'conclusion': 'success'}], [], 'checks_passed'),
    ({}, [], [{'user': {'login': 'person'}, 'state': 'APPROVED'}], 'approved'),
    ({}, [], [{'user': {'login': 'person'}, 'state': 'APPROVED'},
              {'user': {'login': 'person'}, 'state': 'CHANGES_REQUESTED'}], 'changes_requested'),
    ({}, [], [{'user': {'login': 'person'}, 'state': 'APPROVED'},
              {'user': {'login': 'person'}, 'state': 'DISMISSED'}], 'open'),
])
def test_lifecycle(pull, checks, reviews, expected):
    assert pull_lifecycle(pull, checks, reviews) == expected


async def test_snapshot_reader_uses_cached_read_only_requests():
    from test_github import FakeGitHubAPI
    fake = FakeGitHubAPI()
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == '/repos/o/r/pulls' and not request.url.params.get('head'):
            return httpx.Response(200, json=[{'number': 99, 'title': 'Fork contribution', 'user': {'login': 'contributor'},
                'state': 'open', 'draft': True, 'head': {'ref': 'acc/task', 'repo': {'full_name': 'outsider/r'}},
                'html_url': 'https://github.com/o/r/pull/99', 'updated_at': 'today'}])
        if request.url.path == '/repos/o/r/pulls':
            return httpx.Response(200, json=[{'number': 1, 'title': 'Change', 'user': {'login': 'person'},
                'state': 'open', 'head': {'ref': 'acc/task', 'sha': 'abc', 'repo': {'full_name': 'O/R'}}, 'html_url': 'https://github.com/o/r/pull/1',
                'updated_at': 'today'}])
        if request.url.path.endswith('/check-runs'):
            return httpx.Response(200, json={'check_runs': [{'status': 'completed', 'conclusion': 'failure'}]})
        if request.url.path.endswith('/status'):
            return httpx.Response(200, json={'statuses': []})
        if request.url.path.endswith('/reviews'):
            return httpx.Response(200, json=[])
        return fake.handler(request)

    github = GitHub('fixture', transport=httpx.MockTransport(handler))
    try:
        first = await github.snapshot('o/r', ('acc/task',))
        count = len(calls)
        assert {p['number'] for p in first['pulls']} == {1, 99}
        assert next(p for p in first['pulls'] if p['number'] == 1)['lifecycle'] == 'checks_failed'
        assert await github.snapshot('o/r') == first
        assert len(calls) == count
        assert all(request.method == 'GET' for request in calls)
    finally:
        await github.close()


async def test_notice_once_per_run_dismissible_and_private(api, project):
    from helpers import pause_supervisor
    from sqlalchemy import select
    from test_supervisor import launch

    from acc.server import attention
    from acc.server.db import WRITE_INTENT
    from acc.server.models import AttentionItem, Event, Session, SessionRun, User
    from acc.server.review_state import sync_notice

    session = await launch(api, project)
    await pause_supervisor(api)
    intent = WRITE_INTENT.set(True)
    try:
        with api.app.state.db() as db:
            row = db.get(Session, session['id'])
            db.add(User(username='reviewer', role='member', password_hash='x', totp_secret='x'))
            db.flush()
            row.owner = 'reviewer'
            row.observed_state, row.activity = 'idle', None
            row.status_at = row.changes_at = datetime.now(timezone.utc)
            row.changes = {'git': True, 'branch': 'acc/task', 'files': 2}
            assert not ready_for_review(row, db)  # initial idle is not completion
            db.add(Event(session_id=row.id, source='fixture', kind='Stop', payload={}))
            db.flush()
            sync_notice(db, row)
            db.flush()
            item = db.scalar(select(AttentionItem).where(AttentionItem.kind == 'ready_for_review'))
            assert item.owner == 'reviewer' and item.severity == 'info'
            assert attention.bulk_dismissible(item)
            item.acked_at = datetime.now(timezone.utc)
            sync_notice(db, row)
            db.flush()
            assert len(list(db.scalars(select(AttentionItem).where(AttentionItem.kind == 'ready_for_review')))) == 1
            assert item.acked_at
            row.observed_state = 'waiting_input'
            sync_notice(db, row)
            assert item.resolved_at
            row.observed_state = 'idle'
            db.add(SessionRun(session_id=row.id, reason='resume', started_at=datetime.now(timezone.utc)))
            db.flush()
            db.add(Event(session_id=row.id, source='fixture', kind='Stop', payload={}))
            db.flush()
            sync_notice(db, row)
            db.flush()
            notices = list(db.scalars(select(AttentionItem).where(AttentionItem.kind == 'ready_for_review')))
            assert len(notices) == 2
            assert sum(notice.resolved_at is None for notice in notices) == 1
            question = attention.raise_item(db, 'needs_input:' + row.id, kind='needs_input', title='Question',
                                            source='fixture', session_id=row.id)
            db.flush()
            question.acked_at = datetime.now(timezone.utc)
            assert not ready_for_review(row, db)  # dismissing a question never answers it
            db.rollback()
    finally:
        WRITE_INTENT.reset(intent)


async def test_review_notice_bulk_dismiss_and_undo(api):
    from helpers import pause_supervisor, write_db

    from acc.server import attention

    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        attention.raise_item(db, 'review:fixture:1', kind='ready_for_review', severity='info',
                             source='workspace changes', title='Review fixture')
        db.commit()
    items = (await api.get('/api/attention')).json()
    item = next(item for item in items if item['kind'] == 'ready_for_review')
    assert item['bulk_dismissible']
    response = await api.post('/api/attention/dismiss', json={'items': [
        {'id': item['id'], 'updated_at': item['updated_at']}]})
    assert response.status_code == 200 and response.json()['dismissed'] == 1
    assert not [item for item in (await api.get('/api/attention')).json() if item['kind'] == 'ready_for_review']
    undo = await api.post('/api/attention/dismiss/undo', json={'batch': response.json()['batch']})
    assert undo.status_code == 200 and undo.json()['restored'] == 1


async def test_lifecycle_cache_does_not_follow_project_ownership_or_repo_changes(api, project):
    from helpers import pause_supervisor, write_db

    from acc.server.models import Observation, Project, User
    from acc.server.review_state import cached_pulls

    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        row = db.get(Project, project['id'])
        row.repo = 'example/demo'
        db.add(Observation(key=f'github.{row.id}', source='fixture', observed_at=0,
                           value={'repo': {'full_name': row.repo}, 'pulls': [{'number': 42}]}))
        db.flush()
        assert cached_pulls(db, row.id) == [{'number': 42}]
        row.repo = 'example/other'
        assert cached_pulls(db, row.id) == []
        row.repo = 'example/demo'
        db.add(User(username='member', role='member', password_hash='x', totp_secret='x'))
        db.flush()
        row.owner = 'member'
        assert cached_pulls(db, row.id) == []
        db.rollback()


async def test_rate_limit_waits_without_a_network_request():
    from acc.server.github import GitHubError

    def forbidden(request):
        pytest.fail('rate-limited reader must not contact GitHub')

    github = GitHub('fixture', transport=httpx.MockTransport(forbidden))
    github.rate = {'remaining': '0', 'reset': str(datetime.now(timezone.utc).timestamp() + 60)}
    try:
        with pytest.raises(GitHubError, match='rate limit'):
            await github.get('/repos/example/demo')
    finally:
        await github.close()


async def test_session_list_batches_review_evidence_and_rejects_fork_heads(api, project):
    from datetime import timedelta

    from helpers import pause_supervisor, write_db
    from sqlalchemy import event
    from test_supervisor import launch

    from acc.server.models import AttentionItem, Event, Observation, Project, Session, SessionRun
    from acc.server.review_state import linked_pull

    launched = await launch(api, project)
    await pause_supervisor(api)
    stamp = datetime.now(timezone.utc)
    with write_db(api.app.state.db) as db:
        template = db.get(Session, launched['id'])
        template.desired_state = 'archived'
        repo = db.get(Project, project['id'])
        repo.repo = 'Example/Demo'
        db.add(Observation(key=f'github.{repo.id}', source='fixture', observed_at=0,
            value={'repo': {'full_name': 'example/demo'}, 'pulls': [
                {'number': 99, 'head': 'feature', 'head_repo': 'outsider/demo', 'state': 'open', 'url': 'https://github.com/example/demo/pull/99'},
                {'number': 42, 'head': 'feature', 'head_repo': 'EXAMPLE/DEMO', 'state': 'open', 'url': 'https://github.com/example/demo/pull/42'}]}))
        spec, account = template.launch_spec_id, template.run_as
        db.commit()

    def add_rows(start, stop):
        with write_db(api.app.state.db) as db:
            for index in range(start, stop):
                sid = f'list-review-{index}'
                db.add(Session(id=sid, project_id=project['id'], provider='codex', name=sid, tmux_name=sid,
                    launch_spec_id=spec, run_as=account, desired_state='running', observed_state='idle',
                    status_at=stamp, changes_at=stamp, changes={'git': True, 'branch': 'feature', 'files': 1}))
                db.flush()
                db.add(SessionRun(session_id=sid, reason='launch', started_at=stamp - timedelta(seconds=2)))
                if index != 1:  # one initial idle prompt must stay idle
                    db.add(Event(session_id=sid, source='fixture', kind='Stop', ts=stamp - timedelta(seconds=1), payload={}))
            db.commit()

    engine = api.app.state.db.kw['bind']
    statements = []

    def count(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)

    add_rows(0, 1)
    event.listen(engine, 'before_cursor_execute', count)
    try:
        first = (await api.get('/api/sessions')).json()
        assert next(r for r in first if r['id'] == 'list-review-0')['ready_for_review']
        small = len(statements)
        event.remove(engine, 'before_cursor_execute', count)
        add_rows(1, 300)
        with write_db(api.app.state.db) as db:
            db.add(AttentionItem(dedupe_key='list-question', kind='needs_input', title='Question', source='fixture',
                                 session_id='list-review-2', acked_at=stamp))
            db.commit()
        statements.clear()
        event.listen(engine, 'before_cursor_execute', count)
        response = await api.get('/api/sessions')
        assert response.status_code == 200
        large = len(statements)
        rows = {row['id']: row for row in response.json()}
        assert large <= small + 2 and large < 25, (small, large)
        assert not rows['list-review-1']['ready_for_review']
        assert not rows['list-review-2']['ready_for_review']
        assert rows['list-review-299']['ready_for_review']
        assert rows['list-review-299']['pull_lifecycle']['number'] == 42
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    with api.app.state.db() as db:
        assert linked_pull(db, db.get(Session, 'list-review-299'))['number'] == 42
