import os

import httpx
from helpers import write_db
from sqlalchemy import select
from test_admin_features import owner_browser

from acc.server import auth
from acc.server.api import github_token
from acc.server.models import AuditLog, AuthSession, User

TOKEN = 'github_pat_private_test_1234'
PATH = '/api/providers/github-token'


async def test_token_lifecycle(api, monkeypatch):
    headers = await owner_browser(api)
    async def validate(token):
        assert token in (TOKEN, TOKEN + '5678')
        return {'login': 'octocat', 'scopes': 'repo'}
    monkeypatch.setattr(github_token, 'validate', validate)
    result = await api.put(PATH, headers=headers, json={'token': TOKEN})
    assert result.status_code == 403
    assert (await api.delete(PATH, headers=headers)).status_code == 403
    with write_db(api.app.state.db) as db:
        db.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = auth.now()
        db.commit()
    for token in (TOKEN, TOKEN + '5678'):
        result = await api.put(PATH, headers=headers, json={'token': token})
        assert result.status_code == 200, result.text
        assert result.json()['login'] == 'octocat'
        assert result.json()['scopes'] == 'repo'
        assert token not in result.text
        path = api.app.state.ctx.settings.github_token_path
        assert path.read_text().strip() == token
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_uid == os.getuid()
        assert token not in (await api.get(PATH, headers=headers)).text
    result = await api.delete(PATH, headers=headers)
    assert result.json() == {'set': False, 'suffix': None}
    assert not path.exists()
    assert not api.app.state.supervisor.github_available()
    with api.app.state.db() as db:
        rows = list(db.scalars(select(AuditLog).where(AuditLog.action.like('github_token.%'))))
        assert [r.action for r in rows] == ['github_token.save', 'github_token.save', 'github_token.remove']
        assert TOKEN not in repr([r.detail for r in rows])


async def test_members_and_installation_token_refused(api):
    assert (await api.put(PATH, json={'token': TOKEN})).status_code == 403
    headers = await owner_browser(api)
    with write_db(api.app.state.db) as db:
        user = db.scalar(select(User).where(User.username == 'casey'))
        user.role = 'member'
        db.commit()
    for method in ('GET', 'PUT', 'DELETE'):
        result = await api.request(method, PATH, headers=headers, **({'json': {'token': TOKEN}} if method == 'PUT' else {}))
        assert result.status_code == 403
        assert TOKEN not in result.text


async def test_invalid_token_not_written(api, monkeypatch):
    headers = await owner_browser(api)
    with write_db(api.app.state.db) as db:
        db.get(AuthSession, auth.digest(api.cookies.get('acc_session'))).reauthenticated_at = auth.now()
        db.commit()
    original = httpx.AsyncClient
    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(lambda req: httpx.Response(401, json={'message': TOKEN})))
    monkeypatch.setattr(github_token.httpx, 'AsyncClient', client)
    result = await api.put(PATH, headers=headers, json={'token': TOKEN})
    assert result.status_code == 400
    assert TOKEN not in result.text
    assert not api.app.state.ctx.settings.github_token_path.exists()


async def test_malformed_input_never_echoed(api):
    headers = await owner_browser(api)
    for payload in ({'token': {'secret': TOKEN}}, {'other': TOKEN}):
        result = await api.put(PATH, headers=headers, json=payload)
        assert result.status_code == 422
        assert TOKEN not in result.text


async def test_validation_reads_login_and_scopes(monkeypatch):
    original = httpx.AsyncClient
    def respond(request):
        assert request.url == 'https://api.github.com/user'
        assert request.headers['authorization'] == f'Bearer {TOKEN}'
        return httpx.Response(200, json={'login': 'octocat'}, headers={'x-oauth-scopes': 'repo, read:org'})
    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(github_token.httpx, 'AsyncClient', client)
    assert await github_token.validate(TOKEN) == {'login': 'octocat', 'scopes': 'repo, read:org'}
