"""Orchestrator v2 API (gates, slice 5): the gate queue, the host test budget and per-project gate commands.

People only (cookie + CSRF): agents reach the queue through their team tools (orchestrator/gates.py). A person sees and
queues gates for projects they can see; the host budget and the commands a gate runs are owner settings.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..models import AuditLog, Project, Setting
from ..orchestrator import gates
from ..supervisor_common import LaunchRefused
from .context import Context


class GateIn(BaseModel):
    project: int
    branch: str = Field(max_length=100)
    suite: Literal['full', 'related'] = 'related'
    priority: Literal['merge', 'check'] = 'check'
    base: str = Field('main', max_length=100)


def router(ctx: Context) -> APIRouter:
    api = APIRouter()

    def scheduler():
        return gates.gates(ctx.supervisor)

    def jobs(s, who, project_id=None):
        query = select(gates.GateJob).join(Project, Project.id == gates.GateJob.project_id)
        query = ctx.scope(query, Project, who)
        if project_id is not None:
            query = query.where(gates.GateJob.project_id == project_id)
        rows = list(s.scalars(query.order_by(gates.GateJob.created_at.desc()).limit(200)))
        active = gates.ordered([j for j in rows if j.state in gates.ACTIVE])
        return active + [j for j in rows if j.state not in gates.ACTIVE][:50]

    async def current_heads(passes) -> dict:
        """A pass is a result for the commit it tested, not for the branch (threat review S5-3, S5-6): ask each
        project's machine where those branches are now. Unknown (machine away) stays unknown, never "current"."""
        found = {}
        for (host, account, path), branches in passes.items():
            try:
                answer = await ctx.supervisor.broker.call('gate_heads', account=account, host=host, path=path,
                                                          branches=sorted(branches))
                found.update({(host, path, b): h for b, h in (answer.get('heads') or {}).items()})
            except Exception:
                continue
        return found

    @api.get('/api/gates')
    async def listing(project: int | None = None, who=Depends(ctx.principal)):
        with ctx.db() as s:
            if project is not None:
                ctx.project_for(s, who, project)
            config = gates.budget(s)
            rows = jobs(s, who, project)
            # Slots are the host's, shared by every project: counts only, never other projects' jobs.
            running = list(s.scalars(select(gates.GateJob.suite).where(gates.GateJob.state == 'running')))
            result = {'budget': config, 'jobs': [gates.view(j) for j in rows], 'running': len(running),
                      'full_running': running.count('full')}
            passes, where, evidence = {}, {}, {}
            for job in rows:
                if job.state in (*gates.PASSED, 'retry-passed') and job.head:
                    project_row = s.get(Project, job.project_id)
                    key = (job.host, job.account, project_row.workspace_path)
                    passes.setdefault(key, set()).update((job.branch, job.base))
                    evidence[job.id] = (job, gates.commands(s, job.project_id))
                    where[job.id] = (job.host, project_row.workspace_path, job.branch)
        heads = await current_heads(passes) if passes else {}
        for job in result['jobs']:
            if job['id'] in where:
                current = heads.get(where[job['id']], 'unknown')
                job['current_head'] = None if current == 'unknown' else current
                row, command_set = evidence[job['id']]
                host, path, _ = where[job['id']]
                branch_heads = {branch: sha for (h, p, branch), sha in heads.items() if (h, p) == (host, path)}
                job['stale'] = gates.stale_reason(row, branch_heads, command_set)
        if any(j['state'] in gates.ACTIVE for j in result['jobs']):
            scheduler().wake()  # also resumes the queue after accd restarted
        return result

    @api.post('/api/gates', status_code=201)
    async def queue(body: GateIn, who=Depends(ctx.principal)):
        with ctx.db() as s:
            project = ctx.project_for(s, who, body.project)
            try:
                job = gates.enqueue(s, project, body.branch, body.suite, body.priority, who.username, settings=ctx.settings,
                                    base=body.base)
            except LaunchRefused as error:
                raise HTTPException(409, str(error)) from None
            s.commit()
            result = gates.view(job)
        scheduler().wake()
        return result

    @api.post('/api/gates/{job_id}/cancel')
    async def cancel(job_id: str, who=Depends(ctx.principal)):
        with ctx.db() as s:
            job = s.get(gates.GateJob, job_id)
            if job is None:
                raise HTTPException(404, 'no such gate job')
            ctx.project_for(s, who, job.project_id)
            if not gates.cancel(s, job, who.username):
                raise HTTPException(409, 'this gate job already finished')
            s.commit()
            result = gates.view(job)
        scheduler().wake()
        return result

    @api.get('/api/gates/budget')
    def get_budget(who=Depends(ctx.principal)):
        with ctx.db() as s:
            return gates.budget(s)

    @api.put('/api/gates/budget')
    async def put_budget(body: dict, who=Depends(ctx.owner)):
        body = dict(body) if isinstance(body, dict) else body
        trusted = isinstance(body, dict) and body.pop('confirm_branch_code', None) is True
        try:
            value = gates.check_budget(body)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        with ctx.db() as s:
            before = gates.budget(s)
            turning_on = value['enabled'] and not before['enabled']
            if turning_on and not trusted:  # gates trust branch code (review L-1): the owner says so, once per turn-on
                raise HTTPException(409, gates.TRUST_WARNING)
            s.merge(Setting(key=gates.BUDGET_KEY, value=value, updated_by=who.username))
            s.add(AuditLog(actor=who.username, action='host_budget.change', target=gates.BUDGET_KEY,
                           detail={'before': before, 'after': value, **({'branch_code_trusted': True} if turning_on else {})}))
            s.commit()
        scheduler().wake()
        return value

    @api.get('/api/gates/projects/{project_id}/commands')
    def get_commands(project_id: int, who=Depends(ctx.owner)):
        with ctx.db() as s:
            ctx.project_for(s, who, project_id)
            return {'commands': gates.commands(s, project_id)}

    @api.put('/api/gates/projects/{project_id}/commands')
    def put_commands(project_id: int, body: dict, who=Depends(ctx.owner)):
        try:
            value = gates.check_commands(body)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        with ctx.db() as s:
            ctx.project_for(s, who, project_id)
            row = s.get(Setting, gates.COMMANDS_KEY)
            current = dict(row.value) if row else {}
            current[str(project_id)] = value
            s.merge(Setting(key=gates.COMMANDS_KEY, value=current, updated_by=who.username))
            s.add(AuditLog(actor=who.username, action='gate.commands', target=str(project_id), detail=value))
            s.commit()
        return {'commands': value}

    @api.post('/api/gates/{job_id}/accept-retry')
    def accept_retry(job_id: str, body: dict, who=Depends(ctx.owner)):
        from ..orchestrator.worklist import WorkItem
        item_id = body.get('item')
        if set(body) != {'item'} or type(item_id) is not int:
            raise HTTPException(422, 'Choose the flake task on the working list.')
        with ctx.db() as s:
            job = s.get(gates.GateJob, job_id)
            if job is None:
                raise HTTPException(404, 'Gate job not found.')
            ctx.project_for(s, who, job.project_id)
            item = s.get(WorkItem, item_id)
            if (job.state != 'retry-passed' or job.team_id is None or item is None or
                    item.team_id != job.team_id or item.kind != 'task' or item.state == 'dismissed'):
                raise HTTPException(409, 'Accept a retry only with a flake task on this team’s working list.')
            job.results = {**job.results, 'accepted_flake': {'item': item_id, 'by': who.username}}
            s.add(AuditLog(actor=who.username, action='gate.accept_retry', target=job_id, detail={'item': item_id}))
            s.commit()
            return gates.view(job)

    @api.get('/api/gates/projects/{project_id}/flakes')
    def get_flakes(project_id: int, who=Depends(ctx.principal)):
        with ctx.db() as s:
            ctx.project_for(s, who, project_id)
            return {'tests': gates.flakes(s, project_id)}

    @api.put('/api/gates/projects/{project_id}/flakes')
    def put_flakes(project_id: int, body: dict, who=Depends(ctx.owner)):
        tests = body.get('tests')
        if (set(body) != {'tests'} or not isinstance(tests, list) or len(tests) > 200 or
                not gates._ids_ok(tests) or len(set(tests)) != len(tests)):
            raise HTTPException(422, 'Provide up to 200 unique test IDs.')
        with ctx.db() as s:
            ctx.project_for(s, who, project_id)
            row = s.get(Setting, gates.FLAKES_KEY)
            value = dict(row.value) if row else {}
            value[str(project_id)] = tests
            s.merge(Setting(key=gates.FLAKES_KEY, value=value, updated_by=who.username))
            s.add(AuditLog(actor=who.username, action='gate.flakes', target=str(project_id), detail={'tests': tests}))
            s.commit()
        return {'tests': tests}

    @api.get('/api/gates/wrapup')
    def get_wrapup(who=Depends(ctx.principal)):
        from ..orchestrator.verification import wrapup_minutes
        with ctx.db() as s:
            return {'minutes': wrapup_minutes(s)}

    @api.put('/api/gates/wrapup')
    def put_wrapup(body: dict, who=Depends(ctx.owner)):
        from ..orchestrator.verification import WRAPUP_KEY
        minutes = body.get('minutes')
        if set(body) != {'minutes'} or type(minutes) is not int or not 0 <= minutes <= 120:
            raise HTTPException(422, 'Wrap-up must be 0–120 whole minutes.')
        with ctx.db() as s:
            s.merge(Setting(key=WRAPUP_KEY, value=minutes, updated_by=who.username))
            s.add(AuditLog(actor=who.username, action='team.wrapup_setting', target=WRAPUP_KEY, detail=body))
            s.commit()
        return {'minutes': minutes}

    return api
