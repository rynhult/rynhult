"""Test-owned local owners for parallel browser journeys; never installed in ACC."""
import secrets
import uuid

from starlette.responses import JSONResponse
from starlette.routing import Route

from acc.server import auth
from acc.server.models import User


def install(app, capability):
    def identity(request):
        if not secrets.compare_digest(request.headers.get('x-e2e-isolation', ''), capability):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        username = 'fixture-' + uuid.uuid4().hex
        secret = auth.new_totp_secret()
        # Match the browser fixture's artificial password. No real account is touched.
        password_hash = auth.hash_password('e2e correct horse battery staple')
        with app.state.db() as db:
            user = User(username=username, role='owner', password_hash=password_hash, totp_secret=secret)
            db.add(user)
            db.flush()
            token, csrf = auth.create_session(db, user, 'Playwright isolated owner')
            db.commit()
        response = JSONResponse({'username': username, 'secret': secret})
        app.state.ctx.set_login_cookies(response, token, csrf)
        return response

    app.router.routes.insert(0, Route('/e2e-test/identity', identity, methods=['POST']))
