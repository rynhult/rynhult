"""Browser-only, project-authorized preview discovery and credential minting."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.responses import JSONResponse

from .. import preview_elements, preview_remote
from ..models import PortReservation
from ..preview import TTL, host_for, listening, now, sign


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    port: int = Field(ge=8000, le=8199)


def router(ctx):
    api = APIRouter()

    @api.get('/api/projects/{project_id}/preview')
    async def discover(project_id: int, who=Depends(ctx.principal)):
        with ctx.db() as s:
            project = ctx.project_for(s, who, project_id)
            machine = project.host or 'local'
            if machine != 'local':
                try:
                    preview_remote.link_for(ctx, machine)
                except ConnectionError as error:
                    return {'available': False, 'reason': str(error), 'ports': []}
            if project.archived:
                return {'available': False, 'reason': 'Unarchive this project to preview it.', 'ports': []}
            ports = list(s.scalars(select(PortReservation.port).where(PortReservation.project_id == project_id,
                PortReservation.port >= 8000, PortReservation.port <= 8199).order_by(PortReservation.port)))
        active = [port for port in ports if await (listening(port) if machine == 'local'
                  else preview_remote.listening(ctx, project_id, port))]
        return {'available': True, 'ports': active}

    @api.post('/api/projects/{project_id}/preview')
    async def mint(project_id: int, body: PreviewIn, request: Request, who=Depends(ctx.principal)):
        if not getattr(request.state, 'session_hash', None):
            raise HTTPException(403, 'Sign in with a browser to open a preview.')
        with ctx.db() as s:
            project = ctx.project_for(s, who, project_id)
            reservation = s.get(PortReservation, body.port)
            if reservation is None or reservation.project_id != project_id or project.archived:
                raise HTTPException(404, 'No preview for this port.')
        machine = project.host or 'local'
        if machine != 'local':
            try:
                preview_remote.link_for(ctx, machine)
            except ConnectionError as error:
                raise HTTPException(409, str(error)) from None
        if not await (listening(body.port) if machine == 'local' else preview_remote.listening(ctx, project_id, body.port)):
            raise HTTPException(409, 'Nothing is listening on this port. Start the dev server first.')
        host = host_for(ctx.settings, project_id, body.port)
        scheme = 'https' if ctx.settings.cookie_secure else 'http'
        # Local TCP test instances use the same ingress port on another hostname.
        ingress = '' if ctx.settings.cookie_secure or request.url.port is None else f':{request.url.port}'
        token = sign(ctx, {'project': project_id, 'port': body.port, 'host': host,
                          'machine': machine, 'login': request.state.session_hash, 'exp': int(now().timestamp()) + TTL})
        response = JSONResponse(
            {'url': f'{scheme}://{host}{ingress}/__acc_preview/start#{token}',
             'frame_url': f'{scheme}://{host}{ingress}/__acc_preview/start?frame=1#{token}', 'expires_in': TTL})
        response.headers['Cache-Control'] = 'no-store'
        return response

    api.include_router(preview_elements.router(ctx))
    return api
