"""Permanent personal-project deletion, serialized with workspace lifecycle operations."""
import posixpath
from contextlib import AsyncExitStack

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update

from ..broker_client import BrokerError
from ..models import (
    AuditLog,
    Base,
    Observation,
    Placement,
    PortReservation,
    Project,
    PublishRequest,
    Schedule,
    ScheduleRun,
    Session,
    Setting,
    Team,
)
from ..orchestrator.gates import COMMANDS_KEY, FLAKES_KEY, GateJob


class DeleteProjectIn(BaseModel):
    confirm: str = Field(max_length=64)


def register(api, ctx):
    def project_for(s, who, project_id):
        project = ctx.project_for(s, who, project_id)
        if project.builtin or project.private_workspace:
            raise HTTPException(409, 'This built-in workspace project cannot be deleted here.')
        return project

    async def check_references(project_id, identity, account, host):
        """DB ownership is independent of agent-writable Git administration."""
        if identity is None:
            return
        with ctx.db() as s:
            references = [(p.workspace_path, p.host or 'local') for p in s.scalars(
                select(Project).where(Project.id != project_id))]
            for session in s.scalars(select(Session).where(
                    (Session.project_id != project_id) | Session.project_id.is_(None))):
                references.extend((value, session.host) for value in
                                  (session.cwd, session.worktree, session.workdir, session.team_repo) if value)
            references.extend((p.path, p.host) for p in s.scalars(
                select(Placement).where(Placement.project_id != project_id)))
        targets = [posixpath.normpath(item['path']) for item in identity['folders']]
        for value, machine in set(references):
            if machine != host:
                continue
            canonical = await ctx.broker.call('workspace_identity', account=account, host=host, path=value)
            reference = posixpath.normpath(canonical['path'])
            if any(reference == target or reference.startswith(target + '/') or target.startswith(reference.rstrip('/') + '/')
                   for target in targets):
                raise HTTPException(409, 'A deletion folder is referenced by another registered project or session. Resolve that reference before deleting this project.')

    async def inspect(project_id, who):
        with ctx.db() as s:
            p = project_for(s, who, project_id)
            sessions = list(s.scalars(select(Session).where(Session.project_id == project_id)))
            if any(x.desired_state == 'running' or x.team_stop_pending or x.multirun_stop_pending or
                   x.observed_state not in ('stopped', 'exited', 'archived', 'recovery_needed') for x in sessions):
                raise HTTPException(409, 'This project has live sessions or a team. Use Stop all first, then wait for every session to stop.')
            if any((team := s.get(Team, x.id)) and not team.stopped for x in sessions):
                raise HTTPException(409, 'This project has a live team. Use Stop all first.')
            if s.scalar(select(GateJob.id).where(GateJob.project_id == project_id, GateJob.state.in_(('queued', 'running')))):
                raise HTTPException(409, 'This project has a queued or running gate. Cancel it or wait for it to finish first.')
            if s.scalar(select(PublishRequest.id).where((PublishRequest.project_id == project_id) |
                    PublishRequest.session_id.in_([x.id for x in sessions]) |
                    PublishRequest.port.in_(select(PortReservation.port).where(PortReservation.project_id == project_id)),
                    (PublishRequest.status.in_(('pending_approval', 'approved', 'applying')) |
                     ((PublishRequest.status == 'applied') & (PublishRequest.action == 'publish'))))):
                raise HTTPException(409, 'This project has a published site or an unfinished publishing request. Unpublish through Publishing, or resolve the request, before deleting it.')
            path, account, host = p.workspace_path, p.account, p.host or 'local'
            remove = p.managed_checkout and p.import_mode == 'managed'
            worktrees = list(dict.fromkeys(x.worktree for x in sessions if x.worktree))
            slug, pinned = p.slug, bool(p.compose_pin or p.compose_autostart)
            processes = {}
            for session in sessions:
                processes.setdefault((session.host, session.run_as), set()).add(session.tmux_name)
        try:
            # Even record-only deletion requires the owning machine to be online.
            await ctx.broker.call('ping', account=account, host=host)
            for (machine, run_as), names in processes.items():
                panes = await ctx.broker.call('list', account=run_as, host=machine)
                if any(pane['tmux_name'] in names and not pane['dead'] for pane in panes):
                    raise HTTPException(409, 'A session process is still live. Use Stop all first and wait for its machine to confirm stopping.')
            identity = await ctx.broker.call('project_delete_identity', account=account, host=host,
                        path=path, worktrees=worktrees) if remove else None
            if identity is not None and identity.get('version') != 2:
                raise HTTPException(409, 'The agent service needs an update before it can authenticate project worktrees for deletion. Update its account service, then retry.')
            await check_references(project_id, identity, account, host)
            # The agent broker reports only whether compose files exist. It never
            # runs arbitrary compose configuration under the services account.
            runtime = await ctx.broker.call('project_delete_runtime', account=account, host=host, path=path)
            if runtime['compose'] or pinned:
                rows = await ctx.broker.call('compose_ps', account=ctx.settings.services_owner if pinned else account,
                                             host=host, path=path)
                if any(str(row.get('State', '')).lower() not in ('exited', 'dead', 'removed') for row in rows):
                    raise HTTPException(409, 'This project has running containers. Stop its compose stack before deleting it.')
        except BrokerError as error:
            raise HTTPException(409, 'Cannot safely delete this project: its machine or account service is offline, unavailable, or refused the folder checks. ' + str(ctx.refused(error).detail)) from error
        return {'slug': slug, 'remove_folder': bool(remove), 'path': path,
                'folders': [x['path'] for x in identity['folders']] if identity else [],
                'sessions_archived': len(sessions)}, identity, worktrees, account, host

    @api.get('/api/projects/{project_id}/delete')
    async def deletion_plan(project_id: int, who=Depends(ctx.principal)):
        with ctx.db() as s:
            p = project_for(s, who, project_id)
            path, host = p.workspace_path, p.host or 'local'
        async def plan():
            return (await inspect(project_id, who))[0]
        return await ctx.supervisor.workspace_operations.run(path, plan, host)

    @api.post('/api/projects/{project_id}/delete')
    async def delete_project(project_id: int, body: DeleteProjectIn, who=Depends(ctx.principal)):
        with ctx.db() as s:
            p = project_for(s, who, project_id)
            if body.confirm != p.slug:
                raise HTTPException(422, 'Type the project slug exactly to confirm deletion.')
            path, host = p.workspace_path, p.host or 'local'
        async def remove():
            # Persist the launch barrier before any broker await. Supervisor launch
            # re-reads archived under its publication transaction. A crash leaves
            # the project safely archived, and deletion can be retried.
            with ctx.db() as s:
                p = project_for(s, who, project_id)
                previously_archived, previously_deleting = p.archived, p.deleting
                p.archived = True
                p.deleting = True
                s.commit()
            try:
                plan, identity, worktrees, account, host = await inspect(project_id, who)
            except BaseException:
                with ctx.db() as s:
                    p = s.get(Project, project_id)
                    if p:
                        p.archived = previously_archived
                        p.deleting = previously_deleting
                        s.commit()
                raise
            async with AsyncExitStack() as locks:
                if identity:
                    for item in sorted(identity['folders'], key=lambda item: item['path']):
                        if item['path'] == path:
                            continue  # the outer lifecycle operation already holds this one
                        held = await locks.enter_async_context(ctx.supervisor.workspace_operations.hold(
                            item['path'], host, wait=False, kind='delete'))
                        if not held:
                            raise HTTPException(409, 'A deletion folder has another operation in progress. Wait for it to finish, then retry.')
                    await check_references(project_id, identity, account, host)
                if identity:
                    try:
                        await ctx.broker.call('delete_project_checkout', account=account, host=host,
                            path=path, worktrees=worktrees, expected=identity)
                    except BrokerError as error:
                        raise ctx.refused(error)
                with ctx.db() as s:
                    p = project_for(s, who, project_id)
                    for session in s.scalars(select(Session).where(Session.project_id == project_id)):
                        from ..security_settings import clear_stop_marker
                        clear_stop_marker(s, session, actor=who.username, reason='project_delete_checked')
                        session.deleted_project = {'slug': p.slug, 'name': p.name, 'host': session.host}
                        session.project_id = None
                        session.placement_id = None
                        session.desired_state = session.observed_state = 'archived'
                        session.worktree = session.workdir = None
                    s.flush()
                    s.execute(delete(PortReservation).where(PortReservation.project_id == project_id))
                    s.execute(delete(Placement).where(Placement.project_id == project_id))
                    # Keep historical and Inbox rows; remove only their project link.
                    # All remaining required personal-project references use CASCADE.
                    for table in Base.metadata.sorted_tables:
                        column = table.c.get('project_id')
                        if column is not None and column.nullable and table.name not in ('session', 'port_reservation'):
                            s.execute(update(table).where(column == project_id).values(project_id=None))
                    for schedule in s.scalars(select(Schedule)):
                        if schedule.launch.get('project_id') == project_id:
                            schedule.enabled = False
                            schedule.deleted = True
                            schedule.next_run_at = None
                    for run in s.scalars(select(ScheduleRun).where(ScheduleRun.status.in_(('pending', 'claimed')))):
                        if run.launch.get('project_id') == project_id:
                            run.status, run.reason = 'cancelled', 'Project deleted'
                    s.execute(delete(Setting).where(Setting.key == f'tools.project.{project_id}'))
                    for key in (COMMANDS_KEY, FLAKES_KEY):
                        row = s.get(Setting, key)
                        if row and isinstance(row.value, dict):
                            row.value = {k: v for k, v in row.value.items() if k != str(project_id)}
                    s.execute(delete(Observation).where(Observation.key == f'github.{project_id}'))
                    s.delete(p)
                    s.add(AuditLog(actor=who.username, action='project.delete', target=plan['slug'], detail=plan))
                    s.commit()
                return plan
        return await ctx.supervisor.workspace_operations.run(path, remove, host, kind='delete')
