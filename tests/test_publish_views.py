"""Current publishing state and bounded, scoped historical browsing."""
from datetime import timedelta

import pytest
from helpers import pause_supervisor, write_db

from acc.server import auth
from acc.server.models import PublishRequest


async def seed(api):
    await pause_supervisor(api)
    with write_db(api.app.state.db) as db:
        for i in range(230):
            db.add(PublishRequest(id=f'pub-history-{i:04}', action='publish', host='old.example.com',
                                 port=8004, access='internal', requested_by='alex', owner='alex',
                                 status='failed', detail='test failure', created_at=auth.now()+timedelta(seconds=i)))
        for identifier, status, detail in [('live', 'applied', ''), ('pending', 'pending_approval', ''),
                                           ('working', 'applying', ''), ('replaced', 'removed', 'replaced by live (port 8004)'),
                                           ('removed', 'removed', 'removed by owner'), ('rejected', 'rejected', '')]:
            db.add(PublishRequest(id=identifier, action='publish', host=identifier+'.example.com',
                                 port=8004, access='internal', requested_by='alex', owner='alex',
                                 decided_by='casey', status=status, detail=detail))
        db.commit()


async def test_current_is_not_limited_by_newer_history_and_audit_is_paginated(api):
    await seed(api)
    current = (await api.get('/api/publish?view=current')).json()
    assert {row['id'] for row in current} == {'live', 'pending', 'working'}
    history = (await api.get('/api/publish?view=audit&status=failed&host=old.example.com&limit=20')).json()
    assert history['total'] == 230 and len(history['items']) == 20
    assert history['items'][0]['id'] == 'pub-history-0229'
    next_page = (await api.get('/api/publish?view=audit&status=failed&offset=20&limit=20')).json()
    assert next_page['items'][0]['id'] == 'pub-history-0209'
    assert not {r['id'] for r in history['items']} & {r['id'] for r in next_page['items']}
    for status in ['replaced', 'removed', 'rejected']:
        rows = (await api.get(f'/api/publish?view=audit&status={status}')).json()
        assert rows['total'] == 1 and rows['items'][0]['id'] == status
    assert (await api.get('/api/publish?view=audit&host=absent')).json()['total'] == 0
    assert (await api.get('/api/publish?view=audit&host=%25')).json()['total'] == 0
    assert (await api.get('/api/publish?view=audit&host=OLD.EXAMPLE')).json()['total'] == 230
    assert isinstance((await api.get('/api/publish')).json(), list), 'Legacy clients retain their array contract'


@pytest.mark.parametrize('query', ['limit=0', 'limit=101', 'offset=-1', 'offset=9223372036854775808', 'status=applying', 'view=unknown'])
async def test_history_rejects_invalid_bounds_and_filters(api, query):
    assert (await api.get('/api/publish?view=audit&'+query)).status_code == 422


async def test_current_is_bounded_and_every_page_and_exact_host_remain_reachable(api):
    await seed(api)
    with write_db(api.app.state.db) as db:
        for i in range(205):
            db.add(PublishRequest(id=f'pending-{i:04}', action='publish', host=f'pending-{i}.example.com',
                                 requested_by='alex', owner='alex', status='pending_approval',
                                 created_at=auth.now()+timedelta(seconds=i)))
        db.commit()
    assert len((await api.get('/api/publish?view=current&limit=1')).json()) == 1
    assert len((await api.get('/api/publish?view=current&limit=100')).json()) == 100
    seen = []
    for offset in range(0, 225, 25):
        rows = (await api.get(f'/api/publish?view=current&limit=25&offset={offset}')).json()
        seen.extend(row['id'] for row in rows)
    assert len(seen) == len(set(seen)) == 208
    assert {'live', 'pending', 'working'} <= set(seen)
    assert [row['id'] for row in (await api.get('/api/publish?view=current&host=LIVE.EXAMPLE.COM')).json()] == ['live']


@pytest.mark.parametrize('address', ['192.168.44.12', '2001:db8:44::12', '::ffff:192.168.44.12'])
def test_publish_serialization_redacts_legacy_text_and_structured_outcomes_by_principal(address):
    from acc.server.api.schemas import publish_out
    row = PublishRequest(id='old', action='publish', host='demo.example.com', status='removed',
                         requested_by='alex', detail=f'Route ready; DNS demo.example.com → {address} (DNS-only); HTTPS OK',
                         reason=f'Connect at [{address}]', context={'target_ip': address, 'outcome':{
                             'operation':'publish', 'dns':{'host':'demo.example.com','target_ip':address,'dns_only':True},
                             'https':{'checked':True,'detail':f'OK from {address}'}, 'route_detail':f'Proxy at {address}'}})
    member = publish_out(row, is_owner=False)
    assert address not in str(member)
    assert 'redacted' in member['detail'] and 'Route ready' in member['detail']
    assert member['outcome']['https'] == {'checked': True}
    assert 'target_ip' not in member
    owner = publish_out(row, is_owner=True)
    assert owner['target_ip'] == address and address in owner['detail']
    assert owner['outcome']['dns']['target_ip'] == address
    row.context = None  # Legacy rows may predate pinned target metadata entirely.
    assert address not in str(publish_out(row, is_owner=False))
    row.detail = f'Connect to https://[{address}]:443 or http://{address}:8004/path'
    assert address not in str(publish_out(row, is_owner=False))
