"""An isolated browser owner cannot consume another test's authenticator/session."""
from types import SimpleNamespace

import httpx
from e2e_identity import install
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from acc.server import auth
from acc.server.db import make_engine
from acc.server.models import AuthSession, User


async def test_identity_requires_capability_and_keeps_owners_independent(tmp_path):
    engine = make_engine(f'sqlite:///{tmp_path}/identity.db')
    User.__table__.create(engine)
    AuthSession.__table__.create(engine)
    app = FastAPI()
    app.state.db = sessionmaker(engine)

    def cookies(response, token, csrf):
        response.set_cookie('acc_session', token, httponly=True)
        response.set_cookie('acc_csrf', csrf)

    app.state.ctx = SimpleNamespace(set_login_cookies=cookies)
    install(app, 'test-capability')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://example.lan') as client:
        assert (await client.post('/e2e-test/identity')).status_code == 403
        assert (await client.post('/e2e-test/identity', headers={'x-e2e-isolation': 'wrong'})).status_code == 403
        with app.state.db() as db:
            assert list(db.scalars(select(User))) == []
        headers = {'x-e2e-isolation': 'test-capability'}
        first = (await client.post('/e2e-test/identity', headers=headers)).json()
        first_token = client.cookies['acc_session']
        second = (await client.post('/e2e-test/identity', headers=headers)).json()
        second_token = client.cookies['acc_session']
        assert first['username'] != second['username']
        assert first['secret'] != second['secret']
        with app.state.db() as db:
            one = auth.lookup_session(db, first_token)
            two = auth.lookup_session(db, second_token)
            assert one.user.username == first['username']
            assert two.user.username == second['username']
            assert one.user.role == two.user.role == 'owner'
            assert one.reauthenticated_at is None  # The fixture does not bypass real reauthentication.
            assert two.reauthenticated_at is None
            auth.end_session(db, second_token)
            db.commit()
        with app.state.db() as db:
            assert auth.lookup_session(db, second_token) is None
            assert auth.lookup_session(db, first_token) is not None
