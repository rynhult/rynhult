"""Independent review of rc-performance: the ETag'd sessions list and the narrowed Inbox read stay per viewer, revalidate
the way browsers and proxies send tags, and run on PostgreSQL too."""
import datetime as dt
import uuid

import pytest
from helpers import write_db
from postgres_matrix import backend_url, postgres_url  # noqa: F401 (fixtures)
from test_member_views import member  # noqa: F401 (fixture: a signed-in member, dana)

from acc.server.api.schemas import etag_matches
from acc.server.models import AttentionItem, LaunchSpec, Project, Session

NOW = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc)


def seed(api, owner_name, member_name):
    """One project and one session each for the owner and the member, and an Inbox item on each session."""
    ids = {}
    with write_db(api.app.state.db) as s:
        for who, account in ((owner_name, 'agent'), (member_name, 'dana-agent')):
            project = Project(slug=f'p-{who}', name=f'P {who}', workspace_path=f'/tmp/p-{who}', account=account, owner=who)
            session = Session(id=uuid.uuid4().hex[:12], project=project, provider='claude', name=f'{who}-work',
                              tmux_name=f't-{who}', run_as=account, owner=who,
                              launch_spec=LaunchSpec(provider='claude', inputs={}, files={}, digest='t'))
            s.add(session)
            s.flush()
            s.add(AttentionItem(dedupe_key=f'needs_input:{session.id}', kind='needs_input', source='hook', severity='warn',
                                title=f'{who} needs you', session_id=session.id, owner=who, created_at=NOW, updated_at=NOW))
            ids[who] = session.id
        s.commit()
    return ids


@pytest.mark.parametrize('header', ['W/{tag}', '"other", {tag}', '*', ' W/{tag} '])
async def test_revalidation_uses_the_weak_comparison_browsers_and_proxies_need(api, header):
    first = await api.get('/api/sessions')
    tag = first.headers['etag']
    again = await api.get('/api/sessions', headers={'If-None-Match': header.format(tag=tag)})
    assert again.status_code == 304 and again.headers['etag'] == tag
    assert etag_matches(header.format(tag=tag), tag)
    assert not etag_matches('W/"elsewhere"', tag) and not etag_matches('"' + tag.strip('"')[:-1] + '0"', tag)


async def test_each_viewer_gets_only_their_sessions_and_never_another_viewers_304(api, member):  # noqa: F811
    ids = seed(api, 'casey', 'dana')
    owner = await api.get('/api/sessions')
    mine = await member.get('/api/sessions')
    assert {x['id'] for x in owner.json()} >= {ids['casey'], ids['dana']}
    assert [x['id'] for x in mine.json()] == [ids['dana']]
    assert owner.headers['etag'] != mine.headers['etag']
    # A browser that cached the owner's list (same profile, another sign-in) revalidates with the owner's tag: the
    # member gets their own list, never a 304 that would show the owner's.
    crossed = await member.get('/api/sessions', headers={'If-None-Match': owner.headers['etag']})
    assert crossed.status_code == 200 and [x['id'] for x in crossed.json()] == [ids['dana']]
    assert (await member.get('/api/sessions', headers={'If-None-Match': mine.headers['etag']})).status_code == 304


async def test_the_inbox_names_only_sessions_the_viewer_may_see(api, member):  # noqa: F811
    ids = seed(api, 'casey', 'dana')
    with write_db(api.app.state.db) as s:  # an item of the member's that points at the owner's session
        s.add(AttentionItem(dedupe_key='needs_input:crossed', kind='needs_input', source='hook', severity='warn',
                            title='crossed', session_id=ids['casey'], owner='dana', created_at=NOW, updated_at=NOW))
        s.commit()
    items = {i['title']: i for i in (await member.get('/api/attention?scope=pending')).json()}
    assert items['dana needs you']['session_name'] == 'dana-work'
    assert not items['crossed'].get('session_name')  # the owner's session's name isn't revealed
    assert 'casey needs you' not in items


async def test_the_list_and_inbox_queries_run_on_both_databases(backend_url, api):  # noqa: F811
    with write_db(api.app.state.db) as s:
        project = Project(slug='pg', name='PG', workspace_path='/tmp/pg', account='agent', owner='casey')
        for n in range(3):
            session = Session(id=uuid.uuid4().hex[:12], project=project, provider='claude', name=f'w{n}', tmux_name=f't{n}',
                              run_as='agent', owner='casey',
                              launch_spec=LaunchSpec(provider='claude', inputs={}, files={}, digest='t'))
            s.add(session)
            s.flush()
            s.add(AttentionItem(dedupe_key=f'needs_input:{session.id}', kind='needs_input', source='hook', severity='warn',
                                title=f'w{n}', session_id=session.id, owner=None, created_at=NOW, updated_at=NOW))
        s.commit()
    listed = await api.get('/api/sessions')
    assert listed.status_code == 200 and {x['name'] for x in listed.json()} >= {'w0', 'w1', 'w2'}
    assert (await api.get('/api/sessions', headers={'If-None-Match': listed.headers['etag']})).status_code == 304
    inbox = (await api.get('/api/attention?scope=pending')).json()
    assert {i['session_name'] for i in inbox if i['title'] in ('w0', 'w1', 'w2')} == {'w0', 'w1', 'w2'}
