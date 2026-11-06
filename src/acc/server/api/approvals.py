"""Person consent records and bounded exact-request batch decisions."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select

from .. import approvals
from ..models import ApprovalBatch, ApprovalConsent, Project, Session, ToolRequest
from ..tools import Tools
from .security_settings import browser, human


class ConsentIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: int = Field(gt=0, strict=True)
    team_id: str = Field(min_length=1, max_length=32)
    action: Literal['tool.install', 'main.merge', 'main.push']
    constraints: dict
    duration: Literal['once', 'team', 'project'] = 'once'
    expires_at: datetime | None = None
    request_digest: str | None = Field(default=None, pattern='^[0-9a-f]{64}$')


class RevokeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1, strict=True)


class BatchIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    requests: list[str] = Field(min_length=1, max_length=50)


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    digest: str = Field(pattern='^[0-9a-f]{64}$')


class BindIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    team_id: str = Field(min_length=1, max_length=32)


def consent_out(row, db):
    result = {c.name: getattr(row, c.name) for c in row.__table__.columns if c.name != 'verification_ref'}
    try:
        approvals.current(db, row)
        result.update(effective_state='eligible', reason='execution_still_requires_qualification')
    except approvals.Refused as error:
        result.update(effective_state='blocked', reason=str(error))
    return result


def router(ctx):
    def principal(request: Request):
        return human(ctx.principal(request))

    def private_project(db, project_id):
        project = db.get(Project, project_id) if project_id is not None else None
        if project is not None and project.group_id is not None:
            raise HTTPException(404, 'That approval was not found.')

    def private_session(db, session_id):
        session = db.get(Session, session_id)
        if session is not None:
            private_project(db, session.project_id)

    def request_for(db, who, request_id):
        row = db.get(ToolRequest, request_id)
        if row is None or not Tools.can_see_actor(db, row, who.username):
            raise HTTPException(404, 'That tool request was not found.')
        private_project(db, row.project_id)
        if row.session_id:
            private_session(db, row.session_id)

    async def private_subjects(request: Request, who=Depends(principal)):
        """Refuse inaccessible resources before body/query field validation.

        These checks do not approve anything: locked mutation paths recheck
        authority. Inspect bounded typed coordinates, never request prose.
        """
        body = {}
        if request.method == 'POST':
            try:
                payload = await request.json()
                body = payload if isinstance(payload, dict) else {}
            except ValueError:
                pass  # leave malformed/missing input to the normal validator
        with ctx.db() as db:
            binding = request.url.path.endswith('/bind')
            if binding or (request.method == 'POST' and request.url.path.endswith('/consents')):
                browser(db, request, who)
            if consent_id := request.path_params.get('consent_id'):
                row = db.get(ApprovalConsent, consent_id)
                if row is None or (row.issuer != who.username and (binding or not who.is_owner)):
                    raise HTTPException(404, 'No such consent.')
                private_project(db, row.project_id)
                private_session(db, row.team_id)
            if batch_id := request.path_params.get('batch_id'):
                batch = db.get(ApprovalBatch, batch_id)
                if batch is None or batch.actor != who.username:
                    raise HTTPException(404, 'No such approval preview.')
                for item in batch.payload:
                    request_for(db, who, item['request_id'])
            project_id = body.get('project_id')
            if request.method == 'GET' and (query_id := request.query_params.get('project_id')):
                project_id = int(query_id) if query_id.isascii() and query_id.isdecimal() and len(query_id) <= 19 else None
            if type(project_id) is int and 0 < project_id < 2 ** 63:
                ctx.project_for(db, who, project_id)
            team_id = body.get('team_id')
            if isinstance(team_id, str) and 0 < len(team_id) <= 32:
                session = ctx.session_for(db, who, team_id)
                private_project(db, session.project_id)
            requests = body.get('requests')
            if isinstance(requests, list):
                for rid in requests[:50]:
                    if isinstance(rid, str) and 0 < len(rid) <= 40:
                        request_for(db, who, rid)

    api = APIRouter(prefix='/api/approvals', dependencies=[Depends(private_subjects)])

    def refused(error):
        return HTTPException(error.status if isinstance(error, approvals.Refused) else 422, str(error))

    @api.get('/consents')
    def consents(project_id: int | None = Query(default=None, gt=0),
                 state: Literal['active', 'revoked'] = 'active',
                 limit: int = Query(default=50, ge=1, le=200),
                 cursor: str | None = Query(default=None, pattern='^[0-9a-f]{32}$'),
                 who=Depends(principal)):
        with ctx.db() as db:
            query = select(ApprovalConsent)
            if project_id is not None:
                ctx.project_for(db, who, project_id)
                query = query.where(ApprovalConsent.project_id == project_id)
            # Owners may discover other issuers only within an explicitly chosen
            # project they can already access. The default stays personal.
            if not who.is_owner or project_id is None:
                query = query.where(ApprovalConsent.issuer == who.username)
            if cursor is not None:
                # Resolve against authorization scope before filtering state:
                # revoking a page's last row must not break its continuation.
                anchor = db.scalar(query.where(ApprovalConsent.id == cursor))
                if anchor is None:
                    raise HTTPException(422, 'That cursor is not available in this consent scope.')
                query = query.where(or_(ApprovalConsent.created_at < anchor.created_at,
                    and_(ApprovalConsent.created_at == anchor.created_at, ApprovalConsent.id < anchor.id)))
            rows = list(db.scalars(query.where(ApprovalConsent.state == state)
                .order_by(ApprovalConsent.created_at.desc(), ApprovalConsent.id.desc()).limit(limit + 1)))
            next_cursor = rows[limit - 1].id if len(rows) > limit else None
            return {'consents': [consent_out(r, db) for r in rows[:limit]],
                    'next_cursor': next_cursor, 'execution_qualified': False}

    @api.post('/consents', status_code=201)
    def grant(body: ConsentIn, request: Request, who=Depends(principal)):
        with ctx.db() as db:
            browser(db, request, who)
            try:
                row = approvals.grant(db, actor=who.username, **body.model_dump(),
                    verification_ref=getattr(request.state, 'session_hash', None))
                db.commit()
                return consent_out(row, db)
            except (ValueError, TypeError) as error:
                raise refused(error) from None

    @api.post('/consents/{consent_id}/revoke')
    def revoke(consent_id: str, body: RevokeIn, who=Depends(principal)):
        # Revocation remains available without step-up or feature availability.
        with ctx.db() as db:
            try:
                row = approvals.revoke(db, consent_id, who.username, body.revision)
                db.commit()
                return consent_out(row, db)
            except ValueError as error:
                raise refused(error) from None

    @api.post('/consents/{consent_id}/bind', status_code=201)
    def bind(consent_id: str, body: BindIn, request: Request, who=Depends(principal)):
        with ctx.db() as db:
            browser(db, request, who)
            row = db.get(ApprovalConsent, consent_id)
            if row is None or row.issuer != who.username:
                raise HTTPException(404, 'No such consent.')
            try:
                child = approvals.bind(db, consent_id, body.team_id, actor=who.username)
                db.commit()
                return consent_out(child, db)
            except ValueError as error:
                raise refused(error) from None

    @api.post('/batches/preview', status_code=201)
    def preview(body: BatchIn, who=Depends(principal)):
        with ctx.db() as db:
            try:
                row = approvals.preview_batch(db, who.username, body.requests)
                db.commit()
                return {'id': row.id, 'digest': row.digest, 'expires_at': row.expires_at,
                        'requests': [approvals.tool_payload(db.get(ToolRequest, i['request_id'])) for i in row.payload]}
            except ValueError as error:
                raise refused(error) from None

    @api.post('/batches/{batch_id}/approve')
    async def approve(batch_id: str, body: ApplyIn, who=Depends(principal)):
        with ctx.db() as db:
            try:
                ids = approvals.approve_batch(db, batch_id, who.username, body.digest)
                hosts = {rid: db.get(ToolRequest, rid).host for rid in ids}
                db.commit()
            except ValueError as error:
                raise refused(error) from None
        for rid in ids:
            ctx.supervisor.tools.schedule(rid, hosts[rid])
        return {'requests': ids, 'status': 'approved', 'execution': 'per-item outcomes in Tools'}

    return api
