"""Deadline controls installed only by the disposable browser harness.

Use the app's UTC clock and real expiry pass. Never change a global clock or the
instance wrap-up setting while independent browser workers share that server.
"""
from datetime import timedelta

from helpers import write_db
from starlette.responses import JSONResponse
from starlette.routing import Route

from acc.server.models import Session, Team
from acc.server.supervisor_common import now


def install(app):
    async def settle(request):
        supervisor = app.state.supervisor
        with write_db(app.state.db) as db:
            session = db.get(Session, request.path_params['session_id'])
            if session is None:
                return JSONResponse({'detail': 'No fixture session.'}, status_code=404)
            # The same hook/statusline readers and authorization as a normal
            # pass, scoped to the session whose fixture bytes were just written.
            supervisor._ingest(db, session)
            supervisor._observe_statusline(db, session)
            db.commit()
        with app.state.db() as db:
            session = db.get(Session, request.path_params['session_id'])
            supervisor.__dict__.setdefault('_changes_checked', {}).pop(session.id, None)
            await supervisor._check_changes(db, session)
            db.commit()
        # Uses the normal team lock, durable request/result rows and broker
        # reply publication. This never fabricates a successful tool response.
        await supervisor.process_team_requests()
        return JSONResponse({'ok': True})

    async def deadline(request):
        body = await request.json()
        if not isinstance(body, dict) or body.get('phase') not in ('warning', 'expired'):
            return JSONResponse({'detail': 'Choose warning or expired.'}, status_code=400)
        at = now()
        # Production wrap-up is bounded to 120 minutes. Make the owned expiry
        # older than every allowed setting, rather than setting the shared policy
        # to zero. No wall-clock sleeps or test-wide frozen time are needed.
        expires = at + timedelta(minutes=20) if body['phase'] == 'warning' else at - timedelta(days=1)
        with write_db(app.state.db) as db:
            team = db.get(Team, request.path_params['team_id'])
            if team is None:
                return JSONResponse({'detail': 'No fixture team.'}, status_code=404)
            team.expires_at = expires
            db.commit()
        await app.state.supervisor.expire_teams()
        return JSONResponse({'now': at.isoformat(), 'expires_at': expires.isoformat()})

    app.router.routes.insert(0, Route('/e2e-test/teams/{team_id}/deadline', deadline, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/sessions/{session_id}/settle', settle, methods=['POST']))
