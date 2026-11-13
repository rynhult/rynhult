"""Person consent records and bounded exact-request batch decisions."""
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import and_, func, or_, select

from .. import approvals
from ..models import (
    ApprovalBatch,
    ApprovalConsent,
    AuditLog,
    DelegatedAction,
    InstallRecipe,
    Project,
    Session,
    Setting,
    ToolRequest,
    utcnow,
)
from ..session_caps import attribution as request_attribution
from ..tools import ToolRefused, Tools
from .security_settings import browser, human


class ConsentIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: int = Field(gt=0, strict=True)
    team_id: str = Field(min_length=1, max_length=32)
    action: Literal['tool.install', 'main.merge', 'main.push', 'port.reserve', 'image.check']
    constraints: dict
    duration: Literal['once', 'team', 'project'] = 'once'
    expires_at: datetime | None = None
    request_digest: str | None = Field(default=None, pattern='^[0-9a-f]{64}$')


class ConfigurationIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool
    revision: int = Field(ge=0, strict=True)
    security_revision: int = Field(ge=0, strict=True)
    consents: list[ConsentIn] = Field(default_factory=list, max_length=20)


class ReconcileIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_digest: str = Field(pattern='^[0-9a-f]{64}$')


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


def remaining_allowance(db, row):
    if row.action == 'port.reserve':
        root = row.parent_id or row.id
        family = select(ApprovalConsent.id).where((ApprovalConsent.id == root) | (ApprovalConsent.parent_id == root))
        used = db.scalar(select(func.count()).select_from(DelegatedAction).where(
            DelegatedAction.consent_id.in_(family), DelegatedAction.action == row.action))
        return max(0, row.constraints['max_reservations'] - used)
    if row.action == 'image.check':
        records = db.scalars(select(DelegatedAction).where(DelegatedAction.action == row.action,
            DelegatedAction.created_at >= utcnow() - timedelta(hours=24)))
        account = [r for r in records if r.authority['placement'] == row.authority['placement']]
        payload = {k: row.constraints[k] for k in ('provider', 'model')}
        return max(0, min(5 - len(account), 2 - sum(r.payload == payload for r in account)))
    if row.action == 'tool.install' and 'recipe_digest' in row.constraints:
        from ..account_installs import account_scope, dispatched_count
        return max(0, min(5, row.constraints['max_installs']) - dispatched_count(db, account_scope(row.authority)))
    return row.remaining


def consent_out(row, db):
    result = {c.name: getattr(row, c.name) for c in row.__table__.columns if c.name != 'verification_ref'}
    result['remaining_allowance'] = row.remaining
    try:
        approvals.current(db, row)
        allowance = remaining_allowance(db, row)
        result['remaining_allowance'] = min(allowance, row.remaining) if allowance is not None and row.remaining is not None else allowance
        result.update(effective_state='eligible', reason=('ready_for_bound_lead_admission'
            if row.action in ('port.reserve', 'image.check') else 'execution_still_requires_qualification'))
    except approvals.Refused as error:
        result.update(effective_state='blocked', reason=str(error))
    result['execution_qualified'] = row.action in ('port.reserve', 'image.check')
    if row.action == 'tool.install' and 'recipe_digest' in row.constraints:
        from ..account_installs import recipe_for
        try:
            recipe_for(db, row.constraints)
            result['execution_qualified'] = True
            if result['effective_state'] == 'eligible':
                result['reason'] = 'ready_for_bound_lead_admission'
        except ValueError:
            pass
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
            if binding or (request.method == 'POST' and request.url.path.endswith(('/consents', '/configuration', '/reconcile'))):
                browser(db, request, who)
            if operation_id := request.path_params.get('operation_id'):
                from ..account_installs import person_inspection
                try:
                    person_inspection(db, who.username, operation_id)
                except approvals.Refused as error:
                    raise HTTPException(error.status, str(error)) from None
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
            for item in body.get('consents', [])[:20] if isinstance(body.get('consents', []), list) else []:
                if isinstance(item, dict):
                    pid = item.get('project_id')
                    if type(pid) is int and 0 < pid < 2 ** 63:
                        ctx.project_for(db, who, pid)
                    sid = item.get('team_id')
                    if isinstance(sid, str) and 0 < len(sid) <= 32:
                        ctx.session_for(db, who, sid)
                        private_session(db, sid)
            project_id = body.get('project_id')
            if request.method == 'GET' and (query_id := request.query_params.get('project_id')):
                project_id = int(query_id) if query_id.isascii() and query_id.isdecimal() and len(query_id) <= 19 else None
            if type(project_id) is int and 0 < project_id < 2 ** 63:
                ctx.project_for(db, who, project_id)
            team_id = body.get('team_id') if request.method != 'GET' else request.query_params.get('team_id')
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
        return HTTPException(error.status if isinstance(error, (approvals.Refused, ToolRefused)) else 422, str(error))

    async def inspect_install(who, operation_id):
        from ..account_installs import person_inspection
        with ctx.db() as db:
            row, journal, recipe, snapshot, envelope = person_inspection(db, who.username, operation_id)
            account = envelope['provenance']['account']
            manifest = recipe.recipe['manifest']
        report = await ctx.supervisor.broker.call('account_install', account=account, host='local',
            phase='footprint', envelope=envelope, manifest=manifest)
        return snapshot, report

    @api.get('/installs/{operation_id}')
    async def install_evidence(operation_id: str, inspect: bool = False,
            offset: int = Query(default=0, ge=0, le=100000), limit: int = Query(default=100, ge=1, le=200),
            who=Depends(principal)):
        from ..account_installs import person_inspection
        with ctx.db() as db:
            row, journal, recipe, _, _ = person_inspection(db, who.username, operation_id)
            result = {'id': row.id, 'state': row.state, 'result': row.result, 'recipe_id': recipe.recipe_id,
                'recipe_digest': recipe.digest, 'uid': journal.envelope['uid'],
                'runtime_id': journal.envelope['runtime_id'], 'cleanup': journal.cleanup,
                'manifest': recipe.recipe['manifest'][offset:offset + limit],
                'steps': journal.steps[offset:offset + limit], 'offset': offset,
                'next_offset': offset + limit if offset + limit < max(len(recipe.recipe['manifest']), len(journal.steps)) else None}
        if inspect:
            try:
                _, observed = await inspect_install(who, operation_id)
                result['inspection'] = observed
                result['inspection_digest'] = approvals.digest(observed)
            except Exception:
                result['inspection'] = {'reason': 'executor_failed'}
        return result

    @api.post('/installs/{operation_id}/reconcile')
    async def reconcile_install(operation_id: str, body: ReconcileIn, request: Request, who=Depends(principal)):
        from ..account_installs import reconcile_person
        from ..db import write_in_thread
        with ctx.db() as db:
            browser(db, request, who)
        try:
            snapshot, report = await inspect_install(who, operation_id)
            def save():
                with ctx.db() as db:
                    browser(db, request, who)
                    row = reconcile_person(db, who.username, operation_id, snapshot, report, body.expected_digest)
                    db.commit()
                    from ..delegated_execution import out
                    return out(row)
            return await write_in_thread(save)
        except (ValueError, TypeError) as error:
            raise refused(error) from None

    @api.get('/configuration')
    async def configuration(team_id: str | None = Query(default=None, max_length=32), who=Depends(principal)):
        with ctx.db() as db:
            choice = db.get(Setting, 'approvals.enabled')
            revision = db.get(Setting, 'approvals.configuration_revision')
            result = {'enabled': choice is not None and choice.value is True,
                'available': approvals.available(db), 'revision': revision.value if revision else 0,
                'security_revision': approvals.security.revision(db), 'images': [], 'recipes': []}
            for row in db.scalars(select(InstallRecipe).where(InstallRecipe.withdrawn.is_(False))):
                from ..account_installs import qualified
                try:
                    qualified(row.digest, row.recipe['runtime_id'])
                except approvals.Refused:
                    continue
                result['recipes'].append({'digest': row.digest, 'recipe': row.recipe})
            if team_id is None:
                return result
            lead = ctx.session_for(db, who, team_id)
            try:
                snapshot = approvals.authority(db, who.username, lead.project_id, team_id)
                if lead.host != 'local' or lead.container or lead.project.containers:
                    raise approvals.Refused('unqualified scope')
            except approvals.Refused:
                return result
            account = lead.run_as
            provider = lead.launch_spec.inputs.get('provider', lead.provider)
            model = lead.launch_spec.inputs.get('model')
            result['scope'] = {'project_id': lead.project_id, 'team_id': lead.id, 'account': account,
                'machine': 'local', 'machine_id': snapshot['placement']['machine_id'], 'issuer': who.username}
        qualified_recipes = []
        for recipe in result['recipes']:
            try:
                identity = await ctx.supervisor.broker.call('account_install', account=account, host='local',
                    phase='identity', recipe_digest=recipe['digest'])
                from ..account_installs import account_uid
                uid = account_uid(account)
                if (type(identity.get('uid')) is int and identity['uid'] == uid
                        and identity.get('runtime_id') == recipe['recipe']['runtime_id']):
                    qualified_recipes.append(recipe)
                    result['install_identity'] = {'uid': uid, 'runtime_id': identity['runtime_id']}
            except Exception:
                pass
        result['recipes'] = qualified_recipes
        try:
            catalogue = await ctx.supervisor.broker.call('model_catalog', account=account, host='local')
            for candidate in catalogue.get(provider, []):
                if (candidate.get('id') == model and candidate.get('delegation_protocol') == 'image-check-delegation-v1'
                        and type(candidate.get('uid')) is int and candidate['uid'] > 0
                        and candidate.get('runtime_id')):
                    result['images'].append({'provider': provider, 'model': model, 'uid': candidate['uid'],
                                             'runtime_id': candidate['runtime_id']})
        except Exception:
            pass  # Unknown identity never becomes a usable consent option.
        return result

    @api.post('/configuration')
    def save_configuration(body: ConfigurationIn, request: Request, who=Depends(principal)):
        with ctx.db() as db:
            browser(db, request, who)
            approvals.security.lock_revision(db)
            revision = db.get(Setting, 'approvals.configuration_revision')
            choice = db.get(Setting, 'approvals.enabled')
            was_enabled = choice is not None and choice.value is True
            if body.revision != (revision.value if revision else 0) or body.security_revision != approvals.security.revision(db):
                raise HTTPException(409, 'Delegation changed; refresh and review your draft.')
            current_person = approvals.person(db, who.username)
            db.refresh(current_person)
            if body.enabled != was_enabled and current_person.role != 'owner':
                raise HTTPException(403, 'An installation owner changes delegation availability.')
            try:
                db.merge(Setting(key='approvals.enabled', value=body.enabled))
                db.flush()
                rows = [approvals.grant(db, actor=who.username, **item.model_dump(),
                    verification_ref=getattr(request.state, 'session_hash', None)) for item in body.consents]
                for row in list(rows):
                    if row.duration == 'project':
                        rows.append(approvals.bind(db, row.id, row.team_id, actor=who.username))
                db.merge(Setting(key='approvals.configuration_revision', value=body.revision + 1))
                approvals.audit(db, who.username, 'configure', 'delegation',
                    {'enabled': body.enabled, 'revision': body.revision + 1, 'consent_ids': [r.id for r in rows]})
                db.commit()
                return {'revision': body.revision + 1, 'consents': [consent_out(row, db) for row in rows]}
            except (ValueError, TypeError) as error:
                raise refused(error) from None

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

    @api.get('/activity')
    def activity(project_id: int | None = Query(default=None, gt=0),
                 limit: int = Query(default=50, ge=1, le=100), who=Depends(principal)):
        # Consent ownership scopes the digest even after revocation/disable.
        with ctx.db() as db:
            consent_scope = select(ApprovalConsent.id)
            if project_id is not None:
                ctx.project_for(db, who, project_id)
                consent_scope = consent_scope.where(ApprovalConsent.project_id == project_id)
            if not who.is_owner or project_id is None:
                consent_scope = consent_scope.where(ApprovalConsent.issuer == who.username)
            from ..delegated_execution import out
            rows = list(db.scalars(select(DelegatedAction).where(DelegatedAction.consent_id.in_(consent_scope))
                .order_by(DelegatedAction.created_at.desc(), DelegatedAction.id.desc()).limit(limit)))
            refused = select(AuditLog).where(AuditLog.action == 'approval.refused')
            if project_id is not None:
                refused = refused.where(AuditLog.detail['project_id'].as_integer() == project_id)
            if not who.is_owner or project_id is None:
                refused = refused.where(AuditLog.detail['owner'].as_string() == who.username)
            refusals = list(db.scalars(refused.order_by(AuditLog.ts.desc(), AuditLog.id.desc()).limit(limit)))
            return {'operations': [out(row) for row in rows], 'limit': limit,
                    'refusals': [{'id': r.id, 'at': r.ts, 'lead_id': r.target,
                                  'action': r.detail['action'], 'reason': r.detail['reason']} for r in refusals]}

    @api.post('/consents', status_code=201)
    def grant(body: ConsentIn, request: Request, who=Depends(principal)):
        with ctx.db() as db:
            browser(db, request, who)
            try:
                row = approvals.grant(db, actor=who.username, **body.model_dump(),
                    verification_ref=getattr(request.state, 'session_hash', None))
                revision = db.get(Setting, 'approvals.configuration_revision', populate_existing=True)
                db.merge(Setting(key='approvals.configuration_revision', value=(revision.value if revision else 0) + 1))
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
                revision = db.get(Setting, 'approvals.configuration_revision', populate_existing=True)
                db.merge(Setting(key='approvals.configuration_revision', value=(revision.value if revision else 0) + 1))
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
                revision = db.get(Setting, 'approvals.configuration_revision', populate_existing=True)
                db.merge(Setting(key='approvals.configuration_revision', value=(revision.value if revision else 0) + 1))
                db.commit()
                return consent_out(child, db)
            except ValueError as error:
                raise refused(error) from None

    @api.post('/batches/preview', status_code=201)
    def preview(body: BatchIn, request: Request, who=Depends(principal)):
        with ctx.db() as db:
            try:
                row = approvals.preview_batch(db, who.username, body.requests, browser_session=bool(getattr(request.state, 'session_hash', None)))
                db.commit()
                def reviewed(item):
                    request = db.get(ToolRequest, item['request_id'])
                    source = db.get(Session, request.session_id) if request.session_id else None
                    return approvals.tool_payload(request) | request_attribution(source)
                return {'id': row.id, 'digest': row.digest, 'expires_at': row.expires_at,
                        'requests': [reviewed(item) for item in row.payload]}
            except ValueError as error:
                raise refused(error) from None

    @api.post('/batches/{batch_id}/approve')
    async def approve(batch_id: str, body: ApplyIn, request: Request, who=Depends(principal)):
        with ctx.db() as db:
            try:
                ids = approvals.approve_batch(db, batch_id, who.username, body.digest, browser_session=bool(getattr(request.state, 'session_hash', None)))
                hosts = {rid: db.get(ToolRequest, rid).host for rid in ids}
                db.commit()
            except ValueError as error:
                raise refused(error) from None
        for rid in ids:
            ctx.supervisor.tools.schedule(rid, hosts[rid])
        return {'requests': ids, 'status': 'approved', 'execution': 'per-item outcomes in Tools'}

    return api
