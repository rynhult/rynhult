"""Disposable fake-provider enrollment for the real relay observer browser test.

Installed only by e2e_server, never the production app. Does not qualify a runtime.
"""
import secrets
import uuid

from starlette.responses import JSONResponse
from starlette.routing import Route

from acc.server import security_settings
from acc.server.db import write_intent
from acc.server.models import Session, SessionRun
from acc.server.session_relay import history, store


def install(app, capability):
    @write_intent
    def arrange(request):
        if not secrets.compare_digest(request.headers.get('x-e2e-isolation', ''), capability):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        identifier = request.path_params['session']
        with app.state.db() as db:
            session = db.get(Session, identifier)
            run = session.runs[-1] if session and session.runs else None
            if (session is None or session.provider != 'codex' or not session.native_id
                    or run is None or run.verified_at is None or run.ended_at is not None):
                return JSONResponse({'detail': 'A verified disposable Codex run is required.'}, status_code=409)
            assert isinstance(run, SessionRun)
            row = store.enroll(db, identifier, run.id, uuid.uuid4().hex,
                identity={'fixture': True}, frozen_policy={}, policy_revision=security_settings.revision(db))
            history.message(db, row, item_id='fixture-message', role='assistant',
                complete_text='Artificial retained hub message.', source_epoch=1, source_revision=1, mask=lambda text: text)
            db.commit()
        return JSONResponse({'ok': True})

    app.router.routes.insert(0, Route('/e2e-test/session-relay/{session}', arrange, methods=['POST']))
    from e2e_relay_native import install as install_native
    install_native(app, capability)

    from e2e_relay_claude import install as install_claude
    install_claude(app, capability)
