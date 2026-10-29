"""HTTPS/terminating-proxy cookie-family recovery; only isolated app state."""
from dataclasses import replace

import httpx
import pytest
from test_auth import cookie_client, do_setup

from acc.server.app import create_app


@pytest.fixture
async def secure_api(env):
    settings = replace(env['settings'], cookie_secure=True, allowed_origins=('http://acc', 'https://acc'))
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://acc') as client:
        client.app = app
        try:
            yield client
        finally:
            await app.state.supervisor.broker.close()
            app.state.db.kw['bind'].dispose()


def expired_plain(response):
    cookies = response.headers.get_list('set-cookie')
    for name in ('acc_session', 'acc_csrf'):
        matching = [c for c in cookies if c.startswith(name+'=')]
        assert matching and all('Max-Age=0' in c and 'Secure' in c and 'Path=/' in c for c in matching)


async def test_https_sign_in_issues_one_family_and_authenticated_responses_expire_legacy(secure_api, env):
    await do_setup(secure_api, env)
    api = cookie_client(secure_api)
    for path in ('/api/auth/state', '/api/sessions'):
        response = await api.get(path)
        assert response.status_code == 200, response.text
        expired_plain(response)
    refused = await api.post('/api/sessions/preview', headers={'Origin': 'https://acc', 'X-ACC-CSRF': 'legacy'},
                             json={'provider': 'claude', 'name': 'Probe'})
    assert refused.status_code == 403
    assert refused.headers['x-acc-csrf-error'] == 'mismatch'
    expired_plain(refused)


async def test_terminating_proxy_keeps_prefixed_session_and_logout_works(secure_api, env):
    await do_setup(secure_api, env)
    session = secure_api.cookies.get('__Host-acc_session')
    cookie = f'acc_session=older; acc_csrf=older; __Host-acc_session={session}'
    headers = {'Cookie': cookie, 'X-Forwarded-Proto': 'https'}
    response = await secure_api.get('http://acc/api/auth/csrf', headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['csrf'] == secure_api.cookies.get('__Host-acc_csrf')
    expired_plain(response)
    for origin in ('http://acc', 'https://evil.example'):
        refused = await secure_api.post('http://acc/api/auth/logout', headers={**headers, 'Origin': origin})
        assert refused.status_code == 403
    result = await secure_api.post('http://acc/api/auth/logout', headers={**headers, 'Origin': 'https://acc'})
    assert result.status_code == 200, result.text
    expired_plain(result)
    assert (await secure_api.get('http://acc/api/auth/csrf', headers=headers)).status_code == 401


async def test_unauthenticated_get_does_not_issue_any_cookie_family(secure_api):
    response = await secure_api.get('/api/auth/csrf')
    assert response.status_code == 401
    assert not response.headers.get_list('set-cookie')
