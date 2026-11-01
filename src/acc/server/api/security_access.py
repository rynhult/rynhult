"""Separately consented OS access, with durable per-account admission fences."""
import hashlib
import json
import uuid
from datetime import timedelta
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy import select, update

from .. import security_settings as policy
from ..models import AuditLog, SecurityChange, SecurityRevision, utcnow
from .security_folders import CheckIn, active_gate_ids, request_target, scope_for
from .security_settings import ApplyIn, audited_router, browser


class AccessIn(CheckIn):
    method: Literal['named_acl', 'shared_group'] = 'named_acl'
    write: bool = Field(strict=True, default=False)
    revision: int = Field(strict=True, ge=0)


def warning(account):
    return (f'{account} will be able to read everything in this folder, including .env files, keys and untracked files. '
        'Write access also permits changes to Git metadata and hooks. Secret-read guardrails are a safety net, not a filesystem boundary. '
        'Moving secrets out of this folder is safer. Removing access cannot recall copied data. '
        'Default ACLs do not guarantee access to restrictive or replaced files. A recursive grant is not one atomic transaction.')


def router(ctx):
    api = audited_router(ctx)

    async def target(coordinates, message, actor):
        return await request_target(ctx, coordinates, message)

    def scoped(db, machine, account):
        return scope_for(ctx, db, machine, account)

    def owned(db, operation, request, who, approved=None):
        row = db.get(SecurityChange, operation)
        if (not row or row.payload.get('lane') != 'access' or row.actor != who.username
                or (row.session_hash != request.state.session_hash and not (approved is True and row.applied))
                or (approved is not None and row.applied != approved)):
            raise HTTPException(404, 'No such access review in this sign-in.')
        coordinates = scoped(db, row.payload['scope']['machine'], row.payload['scope']['account'])
        if coordinates != row.payload['scope']:
            raise HTTPException(409, 'The machine identity changed. Review again.')
        return row, coordinates

    def save_review(db, request, who, coordinates, message, reviewed, revision, *, undo_of=None):
        operation = uuid.uuid4().hex
        payload = {'lane': 'access', 'scope': coordinates, 'access_key': policy.scope_key(coordinates),
            'message': message, 'preview': reviewed, 'revision': revision, 'access_state': 'preview', 'undo_of': undo_of}
        hashed = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        row = SecurityChange(id=operation, actor=who.username, session_hash=request.state.session_hash,
            digest=hashed, payload=payload, expires_at=utcnow() + timedelta(minutes=5), applied=False)
        db.add(row)
        db.add(AuditLog(actor=who.username, action='security.access.preview', target=operation,
            detail={'scope': coordinates, 'method': reviewed.get('method'), 'path': reviewed.get('path'),
                'count': reviewed.get('count'), 'uid': reviewed.get('agent_uid'), 'identity': reviewed.get('identity'),
                'digest': reviewed.get('digest'), 'revision': revision, 'undo': bool(undo_of)}))
        db.commit()
        return {'id': operation, 'digest': hashed, 'preview': reviewed, 'scope': coordinates,
            'active_sessions': [s.id for s in policy.affected(db, coordinates)],
            'active_gates': active_gate_ids(db, coordinates), 'undo': bool(undo_of),
            'warning': warning(coordinates['account']) if not undo_of else
                'Undo only restores unchanged recorded permissions. Stop all account processes first. Other access, copied data and unowned changes may remain. Repository files are kept.'}

    @api.get('/api/security/access')
    def grants(machine: str, account: str, request: Request, who=Depends(ctx.owner)):
        with ctx.db() as db:
            if not getattr(request.state, 'session_hash', None):
                raise HTTPException(403, 'Use your signed-in owner browser to inspect access approvals.')
            coordinates = scoped(db, machine, account)
            rows = db.scalars(select(SecurityChange).where(SecurityChange.actor == who.username,
                SecurityChange.payload['lane'].as_string() == 'access',
                SecurityChange.payload['access_key'].as_string() == policy.scope_key(coordinates)))
            return {'operations': [{'id': r.id, 'state': r.payload['access_state'], 'approved': r.applied,
                'undo': bool(r.payload.get('undo_of')), 'preview': r.payload['preview'], 'result': r.payload.get('result')}
                for r in rows if r.applied]}

    @api.post('/api/security/access/preview')
    async def preview(body: AccessIn, request: Request, who=Depends(ctx.owner)):
        with ctx.db() as db:
            browser(db, request, who)
            coordinates = scoped(db, body.machine, body.account)
            if policy.revision(db) != body.revision or policy.access_pending(db, coordinates):
                raise HTTPException(409, 'Security or access state changed. Refresh and check pending access first.')
        base = {'machine_id': coordinates['machine_id'], 'account': body.account, 'operation_id': uuid.uuid4().hex, 'revision': 0}
        status = await target(coordinates, base | {'action': 'status'}, who.username)
        if status.get('qualified') is not True or type(status.get('access_revision')) is not int:
            raise HTTPException(409, 'Automatic access is not qualified on this machine. Use existing access or local setup.')
        message = base | {'action': 'preview', 'revision': status['access_revision'], 'method': body.method, 'write': body.write, 'path': body.path}
        reviewed = await target(coordinates, message, who.username)
        with ctx.db() as db:
            if policy.revision(db) != body.revision:
                raise HTTPException(409, 'Security settings changed during the preview. Review again.')
            # Bind the main approval to the exact target digest, not caller ACLs.
            apply_message = {k: v for k, v in message.items() if k not in ('path', 'method', 'write')}
            apply_message.update(action='apply', digest=reviewed['digest'])
            return save_review(db, request, who, coordinates, apply_message, reviewed, body.revision)

    @api.post('/api/security/access/{operation_id}/undo-preview')
    async def undo_preview(operation_id: str, request: Request, who=Depends(ctx.owner)):
        with ctx.db() as db:
            browser(db, request, who)
            row, coordinates = owned(db, operation_id, request, who, True)
            if row.payload.get('undo_of') or row.payload['access_state'] not in ('applied', 'partial', 'repair', 'pending'):
                raise HTTPException(409, 'This grant cannot be undone. Check its status first.')
            message, reviewed, revision = dict(row.payload['message']), dict(row.payload['preview']), policy.revision(db)
        status = await target(coordinates, {k: v for k, v in message.items() if k != 'digest'} | {'action': 'status'}, who.username)
        if not status.get('operation') or status['operation']['state'] not in ('applied', 'partial', 'applying', 'repair'):
            raise HTTPException(409, 'No target access change is confirmed. Refresh the operation before continuing.')
        message.update(action='undo', revision=status['access_revision'])
        with ctx.db() as db:
            if policy.revision(db) != revision:
                raise HTTPException(409, 'Security changed during Undo review. Review again.')
            return save_review(db, request, who, coordinates, message, reviewed, revision, undo_of=operation_id)

    def record(db, row, reply, actor):
        state = reply.get('state')
        if state not in ('applied', 'undone', 'partial', 'repair', 'cancelled'):
            raise HTTPException(409, 'The machine has not confirmed the access change. Check its status; new work stays blocked.')
        if row.payload['message']['action'] == 'undo' and state in ('applied', 'cancelled'):
            raise HTTPException(409, 'Undo is not confirmed. New work remains blocked.')
        if state == 'applied' and (not isinstance(reply.get('verification'), dict) or reply['verification'].get('read') is not True
                or reply['verification'].get('write') is not row.payload['preview']['write']
                or reply['verification'].get('checked') != row.payload['preview']['count']):
            raise HTTPException(409, 'The machine has not confirmed agent access. New work stays blocked.')
        row.payload = {**row.payload, 'access_state': state, 'result': reply}
        if row.payload.get('undo_of'):
            original = db.get(SecurityChange, row.payload['undo_of'])
            original.payload = {**original.payload, 'access_state': state, 'result': reply}
        db.add(AuditLog(actor=actor, action='security.access.' + state, target=row.id,
            detail={'scope': row.payload['scope'], 'method': row.payload['preview']['method'],
                'path': row.payload['preview']['path'], 'count': row.payload['preview']['count'],
                'uid': row.payload['preview'].get('agent_uid'), 'identity': row.payload['preview']['identity'],
                'old_revision': row.payload['message']['revision'], 'revision': reply.get('revision'), 'conflicts': reply.get('conflicts', 0)}))
        db.commit()
        return reply

    @api.post('/api/security/access/{operation_id}/apply')
    async def apply(operation_id: str, body: ApplyIn, request: Request, who=Depends(ctx.owner)):
        with ctx.db() as db:
            browser(db, request, who)
            policy.lock_revision(db)
            row, coordinates = owned(db, operation_id, request, who)
            if row.applied:
                raise HTTPException(409, 'This access review was already used. Check its status.')
            if row.digest != body.digest or row.expires_at <= utcnow() or policy.revision(db) != row.payload['revision']:
                raise HTTPException(409, 'The access review expired or changed. Review again.')
            if policy.affected(db, coordinates) or active_gate_ids(db, coordinates):
                raise HTTPException(409, 'Stop affected sessions and gates, then review access again.')
            if policy.access_pending(db, coordinates) and not row.payload.get('undo_of'):
                raise HTTPException(409, 'Another access change is pending. Check it first.')
            row.applied, row.payload = True, {**row.payload, 'access_state': 'pending'}
            changed = db.execute(update(SecurityRevision).where(SecurityRevision.id == 1,
                SecurityRevision.revision == row.payload['revision']).values(revision=row.payload['revision'] + 1))
            if changed.rowcount != 1:
                raise HTTPException(409, 'Security changed. Review again.')
            message = row.payload['message']
            db.add(AuditLog(actor=who.username, action='security.access.requested', target=row.id,
                detail={'scope': coordinates, 'path': row.payload['preview']['path'], 'method': row.payload['preview']['method'],
                    'count': row.payload['preview']['count'], 'uid': row.payload['preview'].get('agent_uid'),
                    'digest': row.digest, 'undo': bool(row.payload.get('undo_of'))}))
            db.commit()  # launch/resume/schedule/helper/files admission is frozen BEFORE IPC
        reply = await target(coordinates, message, who.username)
        with ctx.db() as db:
            row, _ = owned(db, operation_id, request, who, True)
            return record(db, row, reply, who.username)

    @api.post('/api/security/access/{operation_id}/refresh')
    async def refresh(operation_id: str, request: Request, who=Depends(ctx.owner)):
        with ctx.db() as db:
            browser(db, request, who)
            row, coordinates = owned(db, operation_id, request, who, True)
            message = row.payload['message']
        reply = await target(coordinates, {k: v for k, v in message.items() if k != 'digest'} | {'action': 'status'}, who.username)
        actual = reply.get('operation')
        if not actual or actual['state'] in ('preview', 'applying') or (message['action'] == 'undo' and actual['state'] == 'applied'):
            return {'state': 'pending', 'message': 'No completed target result yet. Access remains blocked; review local recovery.'}
        with ctx.db() as db:
            row, _ = owned(db, operation_id, request, who, True)
            return record(db, row, actual, who.username)

    @api.post('/api/security/access/{operation_id}/cancel')
    async def cancel(operation_id: str, request: Request, who=Depends(ctx.owner)):
        with ctx.db() as db:
            browser(db, request, who)
            row, coordinates = owned(db, operation_id, request, who, True)
            if row.payload.get('undo_of') or row.payload['access_state'] != 'pending':
                raise HTTPException(409, 'Only an unstarted grant can be cancelled. Check its result or review Undo.')
            message = {**row.payload['message'], 'action': 'cancel'}
        reply = await target(coordinates, message, who.username)
        if reply.get('state') != 'cancelled':
            raise HTTPException(409, 'Cancellation is not confirmed. New work remains blocked.')
        with ctx.db() as db:
            row, _ = owned(db, operation_id, request, who, True)
            return record(db, row, reply, who.username)

    return api
