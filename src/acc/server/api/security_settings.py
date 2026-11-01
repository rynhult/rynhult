"""Owner browser changes, principal-bound previews and effective-policy reads."""
import hashlib
import json
import logging
import uuid
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select

from .. import passkeys
from .. import security_settings as policy
from ..models import (
    API_PRINCIPAL,
    AuditLog,
    Node,
    SecurityChange,
    SecurityFolderPolicy,
    SecurityPolicy,
    Session,
    User,
    utcnow,
)


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: dict
    overrides: dict[str, StrictBool] = Field(default_factory=dict, max_length=11)
    preset: Literal['custom', 'unrestricted', 'restore'] = 'custom'
    revision: int = Field(ge=0, strict=True)


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    digest: str = Field(pattern='^[0-9a-f]{64}$')


class GoneIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm_machine: str = Field(min_length=1, max_length=128)


class SecurityRefusal(HTTPException):
    def __init__(self, status, detail, reason):
        super().__init__(status, detail)
        self.security_reason = reason


def browser(db, request, who):
    if who.username == API_PRINCIPAL:
        raise SecurityRefusal(403, 'Change Security from an owner’s signed-in browser.', 'human_required')
    try:
        passkeys.recently_verified(db, getattr(request.state, 'session_hash', None),
                                  db.get(User, who.user_id) if who.user_id is not None else None)
    except HTTPException as error:
        raise SecurityRefusal(error.status_code, error.detail, 'verification_required') from None


def human(who):
    if who.username == API_PRINCIPAL:
        raise SecurityRefusal(403, 'Inspect Security from a signed-in person browser, not an installation token.', 'human_required')
    return who


def audit_outcome(ctx, request, operation, status, reason='authorization_required'):
    """Separate transaction after policy rollback; never copy request data/errors."""
    outcome = 'conflict' if status == 409 else 'failed' if status >= 500 else 'refused'
    actor = getattr(getattr(request.state, 'principal', None), 'username', 'unknown')
    try:
        with ctx.db() as db:
            db.add(AuditLog(actor=actor, action='security.policy.' + outcome, target='security',
                detail={'operation': operation, 'outcome': outcome, 'status': status, 'reason': reason,
                        'revision': policy.revision(db)}))
            db.commit()
    except Exception:
        logging.getLogger(__name__).error('Security outcome audit could not be recorded')


def audited_router(ctx, *, audit_paths=None, **kwargs):
    class AuditedRoute(APIRoute):
        def get_route_handler(self):
            handler = super().get_route_handler()
            if audit_paths is not None and self.path not in audit_paths:
                return handler
            operation = next((name for name in ('preview', 'apply', 'retry', 'recover') if self.path.endswith('/' + name)), 'read')

            if self.methods - {'GET', 'HEAD'}:
                operation = {'/api/tools/policy': 'legacy_tools', '/api/guardrails': 'patterns'}.get(self.path, operation)

            async def audited(request):
                try:
                    return await handler(request)
                except Exception as error:
                    status = getattr(error, 'status_code', 422 if isinstance(error, RequestValidationError) else 500)
                    audit_outcome(ctx, request, operation, status, getattr(error, 'security_reason',
                        'invalid_input' if status == 422 else 'internal_failure' if status >= 500 else 'authorization_required'))
                    raise
            return audited

    return APIRouter(route_class=AuditedRoute, **kwargs)


def router(ctx):
    api = audited_router(ctx)

    def principal(request: Request):
        return human(ctx.principal(request))

    def owner(request: Request):
        return human(ctx.owner(request))


    @api.get('/api/security/effective')
    def effective(project_id: int | None = None, machine: str | None = None, account: str | None = None, who=Depends(principal)):
        with ctx.db() as db:
            if machine is not None or account is not None:
                if not who.is_owner:
                    raise HTTPException(403, 'Only an owner can inspect account policy.')
                if project_id is not None:
                    raise HTTPException(422, 'Choose a project or a machine/account, not both.')
                try:
                    policy.validate_scope(db, {'kind': 'account', 'machine': machine, 'account': account}, default_account=ctx.settings.default_account)
                except ValueError as error:
                    raise HTTPException(422, str(error)) from None
            if project_id is not None:
                ctx.project_for(db, who, project_id)
            elif not who.is_owner:
                raise HTTPException(403, 'Choose one of your projects.')
            result = policy.inspect_policy(db, project_id=project_id, machine=machine, account=account, default_account=ctx.settings.default_account)
            if who.is_owner:
                scope = {'kind': 'project', 'project_id': project_id} if project_id is not None else policy.account_scope(db, machine, account) if machine and account else {'kind': 'instance'}
                if scope['kind'] == 'account':
                    folders = db.get(SecurityFolderPolicy, policy.scope_key(scope))
                    result['can_confirm_folder_recovery'] = bool(folders and folders.state == 'pending_policy'
                        and folders.revision == folders.applied_revision
                        and folders.detail.get('allowed_folders') is result['switches']['allowed_folders']
                        and not policy.access_pending(db, scope))
                result['older_sessions'] = policy.older_sessions(db, scope)
                result['unconfirmed_stops'] = [s.id for s in policy.affected(db, scope) if (s.last_error or '').startswith('stop not confirmed:')]
            if not who.is_owner:
                result.pop('sources', None)  # no other-person legacy preference identifiers
            return result

    @api.get('/api/security/scopes')
    def scopes(who=Depends(owner)):
        with ctx.db() as db:
            return {'revision': policy.revision(db), 'registry_version': policy.REGISTRY_VERSION, 'supported_bypass_providers': ['claude', 'codex'],
                    'registry': [{'id': k, 'label': v, 'default': True, 'account_only': k == 'allowed_folders'}
                                 for k, v in policy.REGISTRY.items()],
                    'policies': [{'scope': r.scope, 'overrides': r.overrides,
                                  'can_restore': r.previous is not None} for r in db.scalars(select(SecurityPolicy))]}

    @api.post('/api/security/changes/preview')
    async def preview(body: PreviewIn, request: Request, who=Depends(owner)):
        def prepare():
            with ctx.db() as db:
                browser(db, request, who)
                try:
                    scope, before, after = policy.planned(db, body.scope, body.overrides, body.preset, default_account=ctx.settings.default_account)
                except policy.Conflict as error:
                    raise SecurityRefusal(409, str(error), error.code) from None
                except ValueError as error:
                    raise HTTPException(422, str(error)) from None
                if policy.revision(db) != body.revision:
                    raise SecurityRefusal(409, 'Security settings changed. Refresh and review again.', 'stale_revision')
                folders = db.get(SecurityFolderPolicy, policy.scope_key(scope)) if scope['kind'] == 'account' else None
                restoring_ack = bool(folders and folders.state != 'pending' and folders.detail.get('allowed_folders') is False)
                check_target = (scope['kind'] == 'account' and not restoring_ack and not policy.resolve(db,
                    machine=scope['machine'], account=scope['account'], policy_override=(scope, after))['switches']['allowed_folders'])
                return scope, before, after, check_target
        scope, before, after, check_target = await run_in_threadpool(prepare)
        if check_target:
            from .security_folders import request_target
            try:
                observed = await request_target(ctx, scope, {'action': 'status', 'operation_id': uuid.uuid4().hex,
                    'machine_id': scope['machine_id'], 'account': scope['account'], 'revision': 0})
                if observed.get('policy_qualified') is not True or observed.get('policy_recovery') is not True:
                    raise ValueError('unqualified or outdated')
            except (HTTPException, ValueError):
                raise HTTPException(409, 'Allowed folders cannot be turned Off until this machine has an updated, connected, qualified folder service. Keep it On and reconnect, update or qualify the service.') from None
        def record():
            with ctx.db() as db:
                browser(db, request, who)
                if policy.revision(db) != body.revision:
                    raise SecurityRefusal(409, 'Security settings changed. Refresh and review again.', 'stale_revision')
                payload = body.model_dump()
                payload['scope'] = scope
                digest = hashlib.sha256(json.dumps({'payload': payload, 'before': before, 'after': after}, sort_keys=True).encode()).hexdigest()
                row = SecurityChange(id=uuid.uuid4().hex, actor=who.username, session_hash=request.state.session_hash,
                                     digest=digest, payload=payload, expires_at=utcnow() + timedelta(minutes=5), applied=False)
                db.add(row)
                db.add(AuditLog(actor=who.username, action='security.policy.preview', target=row.id,
                                detail={'scope': scope, 'revision': body.revision}))
                db.commit()
                return {'id': row.id, 'digest': digest, 'before': before, 'after': after,
                        'scope': scope, 'revision': body.revision, 'expires_at': row.expires_at.isoformat(),
                        'active_sessions': [s.id for s in policy.affected(db, scope)],
                        'older_sessions': policy.older_sessions(db, scope, overrides=after),
                        'requires_stopped_sessions_for_tightening': False}
        return await run_in_threadpool(record)

    @api.post('/api/security/changes/{change_id}/apply')
    async def apply(change_id: str, body: ApplyIn, request: Request, who=Depends(owner)):
        with ctx.db() as db:
            browser(db, request, who)
            row = db.get(SecurityChange, change_id)
            if not row or row.actor != who.username or row.session_hash != request.state.session_hash or row.payload.get('lane') in ('folders', 'access'):
                raise SecurityRefusal(404, 'No such Security preview.', 'preview_unavailable')
            if row.applied or row.expires_at <= utcnow() or row.digest != body.digest:
                reason = 'replayed' if row.applied else 'expired' if row.expires_at <= utcnow() else 'digest_mismatch'
                raise SecurityRefusal(409, 'This Security preview expired, changed or was already applied. Review again.', reason)
            payload = row.payload
            try:
                new_revision = policy.save(db, payload['scope'], payload['overrides'], actor=who.username,
                                           expected_revision=payload['revision'], preset=payload['preset'], default_account=ctx.settings.default_account)
            except policy.Conflict as error:
                raise SecurityRefusal(409, str(error), error.code) from None
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            row = db.get(SecurityChange, change_id)
            row.applied = True
            db.commit()
        folder_application = None
        if payload['scope']['kind'] == 'account':
            from .security_folders import acknowledge_switch
            folder_application = await acknowledge_switch(ctx, payload['scope'], actor=who.username,
                session_hash=request.state.session_hash, revision=new_revision)
        with ctx.db() as db:
            if folder_application is not None:
                db.add(AuditLog(actor=who.username, action='security.folders.switch_' + folder_application['state'],
                    target=policy.scope_key(payload['scope']), detail={'scope': payload['scope'], 'policy_revision': new_revision}))
                db.commit()
            return {'id': change_id, 'revision': policy.revision(db), 'state': 'applied',
                    'folder_application': folder_application,
                    'running_sessions': 'unchanged', 'scope': payload['scope'],
                    'older_sessions': policy.older_sessions(db, payload['scope'])}

    @api.post('/api/security/sessions/{session_id}/machine-gone')
    def machine_gone(session_id: str, body: GoneIn, request: Request, who=Depends(owner)):
        with ctx.db() as db:
            browser(db, request, who)
            session = db.get(Session, session_id)
            if session is None:
                raise HTTPException(404, 'No such session.')
            machine = session.host
            if machine == 'local' or body.confirm_machine != machine:
                raise HTTPException(422, 'Confirm the exact removed machine. This cannot clear a local stop.')
            node = db.scalar(select(Node).where(Node.name == machine, Node.status != 'revoked'))
            hub = getattr(ctx.broker, 'hub', None)
            if node is not None or machine in (getattr(hub, 'links', {}) or {}):
                raise HTTPException(409, 'Remove this machine first. Connected or enrolled machines must confirm stops normally.')
            if session.desired_state == 'running' or not (session.last_error or '').startswith('stop not confirmed:'):
                raise HTTPException(409, 'This session has no unconfirmed stop to clear.')
            policy.clear_stop_marker(db, session, actor=who.username, reason='owner_confirmed_machine_gone')
            db.commit()
            return {'state': 'record_cleared', 'machine': machine, 'stop_proven': False}

    @api.get('/api/security/changes/{change_id}')
    def change(change_id: str, who=Depends(owner)):
        with ctx.db() as db:
            row = db.get(SecurityChange, change_id)
            if row is None or row.actor != who.username or row.payload.get('lane') == 'access':
                raise SecurityRefusal(404, 'No such Security preview.', 'preview_unavailable')
            return {'id': row.id, 'scope': row.payload['scope'], 'state': 'applied' if row.applied else 'preview',
                    'expires_at': row.expires_at.isoformat()}

    return api
