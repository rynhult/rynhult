"""Authorized, read-only membership/location views. No preparation or launch."""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select

from ..models import Group, GroupMembership, Project
from ..source_views import directory, snapshot
from .context import Context


def router(ctx: Context):
    def no_store(response: Response):
        response.headers['Cache-Control'] = 'private, no-store'

    api = APIRouter(prefix='/api/source-views', dependencies=[Depends(no_store)])

    @api.get('/projects/{project_id}')
    def project_view(project_id: int, who=Depends(ctx.principal)):
        with ctx.db() as db:
            project = ctx.project_for(db, who, project_id)
            return snapshot(db, project, person=None if who.is_owner else who.username, is_owner=who.is_owner)

    @api.get('/groups/{group_id}/projects/{project_id}')
    def group_project_view(group_id: str, project_id: int, who=Depends(ctx.principal)):
        with ctx.db() as db:
            group = db.get(Group, group_id)
            role = db.scalar(select(GroupMembership.role).where(GroupMembership.group_id == group_id,
                                                               GroupMembership.user_id == who.user_id))
            if group is None or (not who.is_owner and role is None):
                raise HTTPException(404, 'Group not found, or you do not have access.')
            project = db.get(Project, project_id)
            if project is None or project.group_id != group_id:
                raise HTTPException(404, 'Project not found in this group.')
            return snapshot(db, project, person=None if who.is_owner else who.username,
                            include_origin=who.is_owner, locations_allowed=who.is_owner or role != 'viewer', is_owner=who.is_owner)

    @api.get('/directory')
    def project_directory(q: str = Query('', max_length=200), source_owner: str | None = Query(None, max_length=36),
                          host_id: str | None = Query(None, max_length=36), offset: int = Query(0, ge=0, le=100000),
                          limit: int = Query(30, ge=1, le=100), projects_only: bool = False,
                          include_archived: bool = True, who=Depends(ctx.principal)):
        with ctx.db() as db:
            return directory(db, who, q=q, source_owner=source_owner, host_id=host_id, offset=offset, limit=limit,
                             projects_only=projects_only, include_archived=include_archived)

    return api
