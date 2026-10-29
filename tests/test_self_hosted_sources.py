"""Owner consent, pinned network policy and shared provider launch boundaries."""
import asyncio
import socket

import httpx
import pytest
from test_issue_sources import TOKEN, make_key

BASE = 'https://git.example.lan'


def dns(monkeypatch, addresses):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))
                for ip in addresses]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)


async def test_owner_confirms_dns_and_audits_without_ca(api, monkeypatch):
    dns(monkeypatch, ['10.0.0.8'])
    root = '/api/issue-servers'
    resolved = await api.post(root + '/resolve', json={'base_url': BASE})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()['addresses'] == ['10.0.0.8']
    stale = await api.post(root, json={'base_url': BASE, 'addresses': ['10.0.0.9']})
    assert stale.status_code == 422
    saved = await api.post(root, json={'base_url': BASE, 'addresses': ['10.0.0.8']})
    assert saved.status_code == 201, saved.text
    server = saved.json()
    assert server['id'] and server['addresses'] == ['10.0.0.8']
    assert 'issue_server.add' in (await api.get('/api/audit')).text
    assert (await api.post(root + '/' + server['id'] + '/remove')).status_code == 200
    assert (await api.get(root)).json() == []


@pytest.mark.parametrize('address', ['169.254.169.254', '127.0.0.1', '::1', 'fe80::1', '::ffff:10.0.0.8', '0.0.0.0'])
async def test_owner_cannot_allow_metadata_or_special_addresses(api, monkeypatch, address):
    dns(monkeypatch, [address])
    response = await api.post('/api/issue-servers', json={'base_url': BASE, 'addresses': [address]})
    assert response.status_code == 422


async def test_member_cannot_resolve_or_add_server(api, member_accounts, env):
    from test_auth import do_setup
    from test_users import browser, csrf, join
    await do_setup(api, env)
    link = (await api.post('/api/users/invites', json={'role': 'member', 'account': 'alice-agent'})).json()['link']
    async with browser(api) as alice:
        await join(alice, link, 'alice')
        for path in ('', '/resolve', '/unknown/remove'):
            response = await alice.post('/api/issue-servers' + path, headers=csrf(alice), json={
                'base_url': BASE, 'addresses': ['10.0.0.8']})
            assert response.status_code == 403


async def test_private_transport_pins_then_refuses_rebinding(monkeypatch):
    from acc.server.issue_sources import IssueSourceError, IssueSources
    dns(monkeypatch, ['10.0.0.8'])
    received = []
    async def network(self, request):
        received.append(request)
        assert request.url.host == '10.0.0.8'
        assert request.headers['host'] == 'git.example.lan'
        assert request.extensions['sni_hostname'] == 'git.example.lan'
        return httpx.Response(200, json=[])
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', network)
    server = {'base_url': BASE, 'addresses': ['10.0.0.8']}
    service = IssueSources()
    await service.request('gitlab', BASE, TOKEN, '/api/v4/issues', server=server)
    dns(monkeypatch, ['10.0.0.9'])
    with pytest.raises(IssueSourceError, match='confirm'):
        await service.request('gitlab', BASE, TOKEN, '/api/v4/issues?page=2', server=server)
    assert len(received) == 1
    with pytest.raises(IssueSourceError):
        await service.request('gitlab', 'https://other.example.lan', TOKEN, '/api/v4/issues', server=server)


@pytest.mark.parametrize('source,kind', [('gitlab', 'issue'), ('gitlab', 'merge_request'),
                                          ('gitea', 'issue'), ('gitea', 'pull_request')])
async def test_allowed_server_listing_detail_and_launch(api, project, monkeypatch, source, kind):
    from acc.server.issue_sources import IssueSources
    dns(monkeypatch, ['10.0.0.8'])
    server = (await api.post('/api/issue-servers', json={'base_url': BASE, 'addresses': ['10.0.0.8']})).json()
    key = await make_key(api, project)
    root = f"/api/projects/{project['id']}/issue-sources"
    response = await api.post(root, json={'remote_url': BASE + '/example/demo.git',
        source: {'server_id': server['id'], 'key_id': key}})
    assert response.status_code == 200, response.text
    item = {'number': 7, 'title': 'Fix forge parser', 'body': 'Untrusted instructions', 'state': 'open',
            'labels': [{'name': 'bug'}], 'assignees': [{'login': 'alex'}],
            'html_url': BASE + '/example/demo/' + ('issues' if kind == 'issue' else 'pulls') + '/7'}
    if kind == 'pull_request':
        item['pull_request'] = {'merged': False}
    def handler(request):
        if source == 'gitlab':
            from test_issue_sources import gitlab_issue
            assert request.headers['PRIVATE-TOKEN'] == TOKEN
            result = gitlab_issue(title='Fix forge parser')
            return httpx.Response(200, json=result if request.url.path.endswith('/7') else [result])
        assert request.headers['Authorization'] == 'token ' + TOKEN
        if request.url.path.endswith('/7'):
            return httpx.Response(200, json=item)
        assert request.url.path == '/api/v1/repos/example/demo/issues'
        assert request.url.params['type'] == ('issues' if kind == 'issue' else 'pulls')
        assert request.url.params['limit'] == '30'
        return httpx.Response(200, json=[item])
    api.app.state.supervisor.issue_sources = IssueSources(httpx.MockTransport(handler))
    listed = await api.get(root + '/' + source, params={'kind': kind})
    assert listed.status_code == 200, listed.text
    assert listed.json()['items'][0]['title'] == 'Fix forge parser'
    body = {'project_id': project['id'], 'provider': 'claude', 'name': 'forge-session',
            'issue_source': {'source': source, 'kind': kind, 'id': '7'}}
    preview = await api.post('/api/sessions/preview', json=body)
    assert preview.status_code == 200, preview.text
    assert 'untrusted external issue data' in preview.text and TOKEN not in preview.text
    launch = await api.post('/api/sessions', json=body)
    assert launch.status_code == 201, launch.text
    await api.post('/api/issue-servers/' + server['id'] + '/remove')
    assert (await api.get(root + '/' + source, params={'kind': kind})).status_code == 422


async def test_custom_ca_is_scoped_and_invalid_pem_is_rejected(api, monkeypatch):
    from e2e_server import demo_root

    from acc.server.issue_sources import IssueSourceError, PublicIssueTransport, server_context
    dns(monkeypatch, ['10.0.0.8'])
    assert (await api.post('/api/issue-servers', json={
        'base_url': BASE, 'addresses': ['10.0.0.8'], 'ca_pem': 'invalid PEM'})).status_code == 422
    ca = demo_root().decode()
    server = {'base_url': BASE, 'addresses': ['10.0.0.8'], 'ca_pem': ca}
    response = await api.post('/api/issue-servers', json=server)
    assert response.status_code == 201, response.text
    assert ca not in response.text and ca not in (await api.get('/api/audit')).text
    import ssl
    der = ssl.PEM_cert_to_DER_cert(ca)
    assert der in server_context(server).get_ca_certs(binary_form=True)
    assert der not in server_context(None).get_ca_certs(binary_form=True)
    async with PublicIssueTransport(server) as transport:
        with pytest.raises(IssueSourceError, match='host'):
            await transport.handle_async_request(httpx.Request('GET', 'https://other.example.lan/api/v1/repos'))


async def test_reconfirm_changes_pins_without_reconnecting_project(api, project, monkeypatch):
    dns(monkeypatch, ['10.0.0.8'])
    server = (await api.post('/api/issue-servers', json={'base_url': BASE, 'addresses': ['10.0.0.8']})).json()
    key = await make_key(api, project)
    root = f"/api/projects/{project['id']}/issue-sources"
    assert (await api.post(root, json={'remote_url': BASE + '/example/demo.git',
        'gitlab': {'server_id': server['id'], 'key_id': key}})).status_code == 200
    dns(monkeypatch, ['10.0.0.9'])
    confirm = await api.post('/api/issue-servers/' + server['id'] + '/confirm', json={
        'base_url': BASE, 'addresses': ['10.0.0.9']})
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()['addresses'] == ['10.0.0.9']
    from acc.server.issue_sources import source_key
    from acc.server.models import Project
    with api.app.state.db() as db:
        config, _ = source_key(db, db.get(Project, project['id']), 'gitlab', '@api', api.app.state.supervisor.vault)
        assert config['_server']['addresses'] == ['10.0.0.9']


async def test_member_private_url_requires_selection(api, member_accounts, env, monkeypatch):
    from test_auth import do_setup
    from test_users import browser, csrf, join
    dns(monkeypatch, ['10.0.0.8'])
    entry = (await api.post('/api/issue-servers', json={'base_url': BASE, 'addresses': ['10.0.0.8']})).json()
    await do_setup(api, env)
    link = (await api.post('/api/users/invites', json={'role': 'member', 'account': 'alice-agent'})).json()['link']
    async with browser(api) as alice:
        await join(alice, link, 'alice')
        headers = csrf(alice)
        mine = (await alice.post('/api/projects', headers=headers, json={
            'slug': 'alice-forge', 'name': 'Forge', 'workspace_path': str(env['workspaces'] / 'demo')})).json()
        key = (await alice.post('/api/vault/keys', headers=headers, json={
            'name': 'Reader', 'variable': 'FORGE_TOKEN', 'value': TOKEN})).json()['id']
        await alice.post(f'/api/vault/keys/{key}/give', headers=headers, json={'project_id': mine['id']})
        root = f"/api/projects/{mine['id']}/issue-sources"
        raw = {'remote_url': BASE + '/example/demo.git', 'gitlab': {'key_id': key, 'base_url': BASE}}
        assert (await alice.post(root, headers=headers, json=raw)).status_code == 422
        raw['gitlab'] = {'key_id': key, 'server_id': entry['id']}
        assert (await alice.post(root, headers=headers, json=raw)).status_code == 200
        view = (await alice.get('/api/issue-servers')).json()[0]
        assert 'addresses' not in view


@pytest.mark.parametrize('base', ['http://git.example.lan', 'https://user:pass@git.example.lan', BASE + '/%2e%2e'])
async def test_allowlist_refuses_unsafe_urls(api, monkeypatch, base):
    dns(monkeypatch, ['10.0.0.8'])
    response = await api.post('/api/issue-servers', json={'base_url': base, 'addresses': ['10.0.0.8']})
    assert response.status_code == 422


async def test_allowed_private_redirect_is_never_followed(monkeypatch):
    from acc.server.issue_sources import IssueSourceError, IssueSources
    dns(monkeypatch, ['10.0.0.8'])
    received = []
    async def network(self, request):
        received.append(request)
        return httpx.Response(302, headers={'Location': 'https://169.254.169.254/metadata'})
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', network)
    with pytest.raises(IssueSourceError, match='redirect'):
        await IssueSources().request('gitea', BASE, TOKEN, '/api/v1/repos/example/demo/issues',
                                    server={'base_url': BASE, 'addresses': ['10.0.0.8']})
    assert len(received) == 1


async def test_gitea_paginates_locally_and_checks_kind_and_bounds():
    from acc.server.issue_sources import IssueSourceError, IssueSources
    item = {'number': 7, 'title': 'Fix parser', 'body': TOKEN, 'state': 'open',
            'html_url': BASE + '/example/demo/issues/7'}
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.params['labels'] == 'bug'
        assert request.url.params['assigned_by'] == 'alex'
        return httpx.Response(200, json=[item] * 30 if request.url.params['page'] == '1' else [],
                              headers={'Link': '<https://169.254.169.254/private>; rel="next"'})
    service = IssueSources(httpx.MockTransport(handler))
    first = await service.list('gitea', {'base_url': BASE}, BASE + '/example/demo.git', TOKEN, label='bug', assignee='alex')
    assert first['next_cursor'] == '2' and TOKEN not in str(first)
    second = await service.list('gitea', {'base_url': BASE}, BASE + '/example/demo.git', TOKEN,
                                label='bug', assignee='alex', cursor=first['next_cursor'])
    assert second['items'] == [] and second['next_cursor'] is None
    assert len(calls) == 2 and all(r.url.host == 'git.example.lan' for r in calls)
    with pytest.raises(IssueSourceError, match='page'):
        await service.list('gitea', {'base_url': BASE}, BASE + '/example/demo.git', TOKEN, cursor='1000000')
    with pytest.raises(IssueSourceError, match='kind'):
        service.gitea_item(item, 'pull_request')


@pytest.mark.parametrize('source', ['gitlab', 'gitea'])
async def test_both_sources_bound_size_and_total_deadline(monkeypatch, source):
    from acc.server import issue_sources
    class Large(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(33):
                yield b' ' * 65536
    service = issue_sources.IssueSources(httpx.MockTransport(lambda _: httpx.Response(200, stream=Large())))
    with pytest.raises(issue_sources.IssueSourceError, match='large'):
        await service.request(source, BASE, TOKEN, '/items')
    monkeypatch.setattr(issue_sources, 'REQUEST_TIMEOUT', .02)
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(.005)
                yield b' '
    service = issue_sources.IssueSources(httpx.MockTransport(lambda _: httpx.Response(200, stream=Slow())))
    with pytest.raises(issue_sources.IssueSourceError, match='cannot reach'):
        await service.request(source, BASE, TOKEN, '/items')


async def test_gitea_short_pages_honor_safe_continuation_links():
    from acc.server.issue_sources import IssueSources
    item = {'number': 7, 'title': 'Fix parser', 'state': 'open', 'html_url': BASE + '/example/demo/issues/7'}
    service = IssueSources(httpx.MockTransport(lambda request: httpx.Response(200, json=[item], headers={
        'Link': '<https://git.example.lan/api/v1/repos/example/demo/issues?page=2&limit=1>; rel="next"'})))
    result = await service.list('gitea', {'base_url': BASE}, BASE + '/example/demo.git', TOKEN)
    assert result['next_cursor'] == '2'
