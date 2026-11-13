"""Test-only team arrangements through the disposable server's writer admission."""
import secrets
import uuid
from datetime import timedelta

from starlette.responses import JSONResponse
from starlette.routing import Route

from acc.server.db import write_intent
from acc.server.models import Session, Team, utcnow
from acc.server.orchestrator.assignments import Assignment, Attempt
from acc.server.orchestrator.delivery import TaskReport


def install(app, capability):
    @write_intent
    def arrange(request):
        if not secrets.compare_digest(request.headers.get('x-e2e-isolation', ''), capability):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        lead_id, helper_id = request.path_params['lead'], request.path_params['helper']
        action = request.path_params['action']
        if action not in ('member', 'history'):
            return JSONResponse({'detail': 'Unknown team fixture.'}, status_code=400)
        with app.state.db() as db:
            lead, helper = db.get(Session, lead_id), db.get(Session, helper_id)
            if (lead is None or helper is None or lead_id == helper_id or db.get(Team, lead_id) is None
                    or lead.team_id != lead_id or lead.project_id != helper.project_id):
                return JSONResponse({'detail': 'Fixture requires a lead and its project helper.'}, status_code=404)
            if action == 'member':
                if helper.parent_session_id != lead_id:
                    return JSONResponse({'detail': 'Create the helper with its parent; reparenting is forbidden.'}, status_code=409)
                helper.team_id = lead_id
                helper.team_depth = lead.team_depth + 1
            else:
                if helper.team_id != lead_id:
                    return JSONResponse({'detail': 'History helper must belong to this team.'}, status_code=409)
                for i in range(49):
                    assignment = Assignment(id=uuid.uuid4().hex, team_id=lead_id, created_by=lead_id,
                        task_type='implementation', goal=f'Synthetic history {i:02d} ' + 'x'*120,
                        need={'role': 'general'}, state='reported', created_at=utcnow()+timedelta(seconds=i))
                    db.add(assignment)
                    db.flush()
                    for j in range(10):
                        attempt = Attempt(id=uuid.uuid4().hex, assignment_id=assignment.id, session_id=helper_id,
                            sequence=2+i*10+j, state='reported', branch='acc/synthetic-history', head='a'*40)
                        db.add(attempt)
                        db.flush()
                        for version in range(1, 4):
                            db.add(TaskReport(session_id=helper_id, team_id=lead_id, task_id=f'{attempt.id}:{version}',
                                attempt_id=attempt.id, version=version, status='done',
                                summary='<img src=x onerror=window.pwned=1> '+('evidence'*80),
                                findings_md='# Synthetic findings', branch='acc/synthetic-history', head='a'*40))
            db.commit()
        return JSONResponse({'ok': True})

    app.router.routes.insert(0, Route('/e2e-test/teams/{lead}/fixtures/{action}/{helper}', arrange, methods=['POST']))

    async def create_helper(request):
        if not secrets.compare_digest(request.headers.get('x-e2e-isolation', ''), capability):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        from acc.server.compiler import LaunchInputs
        lead_id = request.path_params['lead']
        with app.state.db() as db:
            lead = db.get(Session, lead_id)
            if lead is None or db.get(Team, lead_id) is None or lead.team_id != lead_id:
                return JSONResponse({'detail': 'Fixture requires a team lead.'}, status_code=404)
            inputs = LaunchInputs.from_dict(lead.launch_spec.inputs)
            project_id, owner, cwd = lead.project_id, lead.owner, lead.cwd
        sid = await app.state.supervisor.create_session(project_id, inputs, 'Riley helper',
            actor=owner, owner=owner, workdir=cwd, parent_session_id=lead_id)
        return JSONResponse({'id': sid})

    app.router.routes.insert(0, Route('/e2e-test/teams/{lead}/helpers', create_helper, methods=['POST']))
