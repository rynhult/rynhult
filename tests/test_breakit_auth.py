"""Impatient enrolment retries must not invalidate an authenticator already scanned."""
import asyncio

import pytest
from helpers import TOTP_AT, verified_post
from test_auth import ORIGIN, PASSWORD, do_setup
from test_users import browser

from acc.server import auth


@pytest.mark.parametrize('flow', ['setup', 'invite'])
async def test_repeated_enrolment_begin_keeps_first_qr_usable(api, env, flow):
    if flow == 'setup':
        token = env['settings'].setup_token_path.read_text().strip()
        body = {'setup_token': token}
    else:
        await do_setup(api, env)
        invite = (await verified_post(api, '/api/users/invites', {'role': 'owner'})).json()
        body = {'token': invite['link'].rsplit('/', 1)[1]}
    path = f'/api/auth/{flow}'
    async with browser(api) as person:
        first = await person.post(path + '/begin', json=body, headers=ORIGIN)
        assert first.status_code == 200
        secret = first.json()['totp_secret']
        retries = await asyncio.gather(*(person.post(path + '/begin', json=body, headers=ORIGIN) for _ in range(4)))
        assert all(r.status_code == 200 for r in retries)
        assert {r.json()['totp_secret'] for r in retries} == {secret}
        done = await person.post(path + '/complete', headers=ORIGIN, json={
            **body, 'username': 'alex', 'password': PASSWORD, 'code': auth.totp(secret, TOTP_AT)})
        assert done.status_code == 200, done.text
        assert (await person.get('/api/auth/state')).json()['username'] == 'alex'


async def test_setup_retries_do_not_extend_enrolment_expiry(api, env, monkeypatch):
    from types import SimpleNamespace

    from acc.server.api import auth as auth_api
    clock = [1000.0]
    monkeypatch.setattr(auth_api, 'time', SimpleNamespace(time=lambda: clock[0]))
    body = {'setup_token': env['settings'].setup_token_path.read_text().strip()}
    first = (await api.post('/api/auth/setup/begin', json=body, headers=ORIGIN)).json()['totp_secret']
    clock[0] = 1899.0
    assert (await api.post('/api/auth/setup/begin', json=body, headers=ORIGIN)).json()['totp_secret'] == first
    clock[0] = 1901.0
    expired = await api.post('/api/auth/setup/complete', headers=ORIGIN, json={
        **body, 'username': 'alex', 'password': PASSWORD, 'code': auth.totp(first, TOTP_AT)})
    assert expired.status_code == 409
    renewed = await api.post('/api/auth/setup/begin', json=body, headers=ORIGIN)
    assert renewed.json()['totp_secret'] != first
