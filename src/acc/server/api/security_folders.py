"""Allowed-folder owner editor. Desired DB state never substitutes for target acknowledgement."""
import uuid
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from acc import folder_channel
from acc.folder_policy import FolderError, literal_path

from .. import security_settings as policy
from ..broker_client import BrokerError, BrokerUnavailable
from ..models import AuditLog, Node, Project, SecurityChange, SecurityFolderPolicy, SecurityRevision, utcnow
from ..public_errors import unknown_method
from .security_settings import ApplyIn, audited_router, browser, human


class Coordinates(BaseModel):
    model_config = ConfigDict(extra='forbid')
    machine: str = Field(min_length=1, max_length=128)
    account: str = Field(min_length=1, max_length=32)


class CheckIn(Coordinates):
    path: str = Field(min_length=1, max_length=4096)


class RecoverIn(ApplyIn):
    recovery_digest: str = Field(pattern='^[0-9a-f]{64}$')


class PreviewIn(Coordinates):
    roots: list[str] = Field(min_length=1, max_length=128)
    revision: int = Field(ge=0, strict=True)


def scope_for(ctx, db, machine, account):
    from ..agent_setup import valid_agent_account
    if machine == 'local' and account in ctx.broker.accounts() and valid_agent_account(account) and account not in ('root', 'acc', 'acc-node'):
        return policy.account_scope(db, machine, account)
    try:
        return policy.validate_scope(db, {'kind': 'account', 'machine': machine, 'account': account})
    except ValueError as error:
        raise HTTPException(422, str(error)) from None

async def request_target(ctx, coordinates, message):
    try:
        if coordinates['machine'] == 'local':
            return await folder_channel.request(message)
        link = ctx.broker.hub.get(coordinates['machine'])
        if 'folder-policy-v1' not in (link.info.get('capabilities') or []):
            raise OSError('Unsupported target.')
        return await link.folder_policy(message)
    except (OSError, ConnectionError, TimeoutError):
        raise HTTPException(503, 'The folder service is unavailable. Keep the draft and ask the administrator to qualify or reconnect it.') from None
    except ValueError:
        raise HTTPException(409, 'The target refused this folder change. Check access and review a fresh preview.') from None

def active_gate_ids(db, coordinates):
    from ..orchestrator.gates import GateJob
    return list(db.scalars(select(GateJob.id).where(GateJob.host == coordinates['machine'],
        GateJob.account == coordinates['account'], GateJob.state.in_(('queued', 'running')))))


async def acknowledge_switch(ctx, coordinates, *, actor, session_hash, revision):
    """Deliver the reviewed switch with unchanged target roots, without quiescing processes.

    A failed/unavailable acknowledgement leaves the durable admission fence in place.
    The normal reviewed folder retry can finish an already-published operation.
    """
    with ctx.db() as db:
        row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
        if (not row or row.state != 'pending_policy' or row.detail.get('policy_revision') != revision
                or policy.revision(db) != revision):
            return None
        roots, expected = list(row.roots), row.applied_revision
        allowed = policy.resolve(db, machine=coordinates['machine'], account=coordinates['account'])['switches']['allowed_folders']
    try:
        operation = uuid.uuid4().hex
        # Every automatic publication needs a recovery-capable fixed worker,
        # including On. An older worker must leave this intent unpublished so
        # the owner can still restore its acknowledgement without target IPC.
        observed = await request_target(ctx, coordinates, {'action': 'status', 'operation_id': operation,
            'machine_id': coordinates['machine_id'], 'account': coordinates['account'], 'revision': expected})
        if (observed.get('policy_recovery') is not True or type(observed.get('revision')) is not int
                or observed['revision'] != expected):
            return {'state': 'pending'}
        roots = observed.get('roots', [])
        if not roots:
            return {'state': 'pending'}
        message = {'action': 'policy_preview', 'operation_id': operation, 'machine_id': coordinates['machine_id'],
                   'account': coordinates['account'], 'revision': expected, 'roots': roots, 'allowed_folders': allowed}
        reviewed = await request_target(ctx, coordinates, message)
        with ctx.db() as db:
            policy.lock_revision(db)
            row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
            if (policy.revision(db) != revision or row.state != 'pending_policy'
                    or row.applied_revision != expected or policy.access_pending(db, coordinates)):
                return {'state': 'pending'}
            db.execute(update(SecurityRevision).where(SecurityRevision.id == 1,
                SecurityRevision.revision == revision).values(revision=revision + 1))
            row.roots, row.revision, row.state = roots, expected + 1, 'pending'
            row.detail = {'allowed_folders': allowed, 'operation_id': operation,
                          'message': 'Waiting for target and account-service acknowledgement.'}
            payload = {'lane': 'folders', 'scope': coordinates, 'message': message, 'revision': revision,
                       'active_sessions': [s.id for s in policy.affected(db, coordinates)],
                       'active_gates': active_gate_ids(db, coordinates), 'preview': reviewed}
            db.add(SecurityChange(id=operation, actor=actor, session_hash=session_hash, digest=reviewed['digest'],
                payload=payload, expires_at=utcnow() + timedelta(minutes=5), applied=True))
            db.add(AuditLog(actor=actor, action='security.folders.requested', target=operation,
                detail={'scope': coordinates, 'revision': expected + 1, 'switch_only': True}))
            db.commit()
        reply = await request_target(ctx, coordinates,
            {k: v for k, v in message.items() if k not in ('roots', 'allowed_folders')} |
            {'action': 'policy_apply', 'digest': reviewed['digest']})
        with ctx.db() as db:
            policy.lock_revision(db)
            row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
            if (not row or row.state != 'pending' or row.detail.get('operation_id') != operation or row.revision != expected + 1
                    or type(reply.get('revision')) is not int or reply['revision'] != row.revision
                    or reply.get('state') != 'applied'):
                return {'state': 'pending'}
            row.applied_revision, row.state = row.revision, 'applied'
            db.execute(update(SecurityRevision).where(SecurityRevision.id == 1).values(revision=SecurityRevision.revision + 1))
            db.add(AuditLog(actor=actor, action='security.folders.applied', target=operation,
                detail={'scope': coordinates, 'revision': row.revision, 'switch_only': True}))
            db.commit()
        return {'state': 'applied'}
    except (HTTPException, BrokerError, OSError, ValueError, KeyError, TypeError):
        return {'state': 'pending'}


def router(ctx):
    api = audited_router(ctx)

    def scope(db, machine, account):
        return scope_for(ctx, db, machine, account)

    async def target(coordinates, message):
        return await request_target(ctx, coordinates, message)

    active_gates = active_gate_ids

    @api.get('/api/security/folders')
    async def folders(who=Depends(ctx.owner)):
        human(who)
        with ctx.db() as db:
            accounts = [('local', account) for account in ctx.broker.accounts()]
            for node in db.scalars(select(Node).where(Node.status == 'active')):
                accounts.extend((node.name, account) for account in (node.info or {}).get('accounts', []) if isinstance(account, str))
            rows = []
            for machine, account in sorted(set(accounts)):
                try:
                    coordinates = scope(db, machine, account)
                except HTTPException:
                    continue
                row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
                desired_allowed = policy.resolve(db, machine=machine, account=account)['switches']['allowed_folders']
                state = ('pending' if row.state == 'recovering' else row.state) if row else 'legacy'
                if (row and row.detail.get('allowed_folders') is not desired_allowed) or (not row and not desired_allowed):
                    state = 'pending'
                operation = db.get(SecurityChange, row.detail.get('operation_id')) if row and row.detail.get('operation_id') else None
                published = bool(row and row.state in ('pending', 'recovering') and operation and operation.applied
                    and operation.payload.get('lane') == 'folders' and operation.payload.get('scope') == coordinates
                    and operation.payload['message']['allowed_folders'] is desired_allowed)
                rows.append({'scope': coordinates, 'state': state, 'stored_state': row.state if row else 'legacy',
                    'retry_available': published, 'legacy': row is None, 'allowed_folders': desired_allowed,
                    'roots': row.roots if row else [], 'revision': row.revision if row else 0,
                    'applied_revision': row.applied_revision if row else 0,
                    'detail': row.detail if row else {'message': 'Installer roots remain in use; not yet imported into the folder service.'},
                    'projects': [{'id': p.id, 'name': p.name, 'path': p.workspace_path} for p in db.scalars(select(Project).where(Project.account == account, Project.host == machine))]})
            revision = policy.revision(db)
        for row in rows:
            try:
                observed = await ctx.broker.call('ping', host=row['scope']['machine'], account=row['scope']['account'])
                if row['legacy'] or not row['roots']:
                    row['roots'] = observed.get('roots', [])
                elif observed.get('folder_revision') != row['applied_revision']:
                    row['state'] = 'pending'
                    row['detail'] = {**row['detail'], 'message': 'The agent service needs its policy reloaded. ' + ('Review the pending application.' if row['retry_available'] else 'Review folders again to reload the saved policy.')}
            except BrokerError:
                row['state'] = 'unavailable'
                row['detail'] = {**row['detail'], 'message': 'The machine or account service is unavailable. Saved folders are shown; no change is confirmed.'}
        return {'accounts': rows, 'revision': revision}

    @api.post('/api/security/folders/check')
    async def check(body: CheckIn, request: Request, who=Depends(ctx.owner)):
        with ctx.db() as db:
            browser(db, request, who)
            coordinates = scope(db, body.machine, body.account)
        try:
            literal_path(body.path)
        except FolderError as error:
            raise HTTPException(422, str(error)) from None
        try:
            result = await ctx.broker.call('folder_check', host=body.machine, account=body.account, path=body.path)
        except BrokerUnavailable:
            result = {'state': 'unavailable', 'message': 'The machine or account service is unavailable. Reconnect it and check again; no OS access result is known.'}
        except BrokerError as error:
            if unknown_method(error) or 'Safe mount identity checks are unavailable' in str(error):
                result = {'state': 'unavailable', 'message': 'This account service cannot provide a qualified folder check. Update or use existing-access local setup; no OS access result is known.'}
            else:
                result = {'state': 'needs_access', 'message': 'The account cannot safely check this folder. Ask an owner to review local access; no permissions were changed.'}
        with ctx.db() as db:
            db.add(AuditLog(actor=who.username, action='security.folders.check', target=policy.scope_key(coordinates),
                detail={'state': result['state']}))
            db.commit()
        return result

    @api.post('/api/security/folders/preview')
    async def preview(body: PreviewIn, request: Request, who=Depends(ctx.owner)):
        with ctx.db() as db:
            browser(db, request, who)
            coordinates = scope(db, body.machine, body.account)
            if policy.access_pending(db, coordinates):
                raise HTTPException(409, 'Check the pending filesystem access change before reviewing folder policy.')
            if policy.revision(db) != body.revision:
                raise HTTPException(409, 'Security settings changed. Refresh and review again.')
            for path in body.roots:
                try:
                    literal_path(path)
                except FolderError as error:
                    raise HTTPException(422, str(error)) from None
            if len(set(body.roots)) != len(body.roots):
                raise HTTPException(422, 'List each folder once.')
            row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
            if row and row.state in ('pending', 'recovering'):
                raise HTTPException(409, 'A published folder operation is pending. Review or retry it before making a new preview.')
            expected = row.applied_revision if row else 0
            allowed = policy.resolve(db, machine=body.machine, account=body.account)['switches']['allowed_folders']
            operation = uuid.uuid4().hex
            message = {'action': 'policy_preview', 'operation_id': operation, 'machine_id': coordinates['machine_id'],
                'account': body.account, 'revision': expected, 'roots': body.roots, 'allowed_folders': allowed}
            active = [s.id for s in policy.affected(db, coordinates)]
            gates = active_gates(db, coordinates)
        capability = await target(coordinates, {k: v for k, v in message.items() if k not in ('roots', 'allowed_folders')} |
            {'action': 'status'})
        if capability.get('policy_recovery') is not True:
            raise HTTPException(409, 'Update the fixed folder service before changing folders; this worker cannot safely recover an interrupted delivery.')
        reviewed = await target(coordinates, message)
        with ctx.db() as db:
            if policy.revision(db) != body.revision:
                raise HTTPException(409, 'Security settings changed during preview. Review again.')
            payload = {'lane': 'folders', 'scope': coordinates, 'message': message,
                'revision': body.revision, 'active_sessions': active, 'active_gates': gates, 'preview': reviewed}
            row = SecurityChange(id=operation, actor=who.username, session_hash=request.state.session_hash,
                digest=reviewed['digest'], payload=payload, expires_at=utcnow() + timedelta(minutes=5), applied=False)
            db.add(row)
            db.add(AuditLog(actor=who.username, action='security.folders.preview', target=operation,
                detail={'scope': coordinates, 'roots': body.roots, 'revision': body.revision}))
            db.commit()
            return {'id': operation, 'digest': row.digest, 'active_sessions': active, 'active_gates': gates, 'preview': reviewed,
                'warning': 'Allow folder changes account-service policy only. OS access remains separate. Native agents are not sandboxed by this list.'}

    @api.post('/api/security/folders/{operation_id}/apply')
    async def apply(operation_id: str, body: ApplyIn, request: Request, who=Depends(ctx.owner)):
        operation = operation_id
        with ctx.db() as db:
            browser(db, request, who)
            policy.lock_revision(db)
            change = db.get(SecurityChange, operation)
            if (not change or change.actor != who.username or change.session_hash != request.state.session_hash
                    or change.payload.get('lane') != 'folders'):
                raise HTTPException(404, 'No such folder preview.')
            if change.applied or change.digest != body.digest or change.expires_at <= utcnow():
                raise HTTPException(409, 'This folder preview expired or was used. Review again.')
            payload = change.payload
            coordinates = scope(db, payload['scope']['machine'], payload['scope']['account'])
            if coordinates != payload['scope'] or policy.revision(db) != payload['revision']:
                raise HTTPException(409, 'The machine or Security revision changed. Review again.')
            if policy.access_pending(db, coordinates):
                raise HTTPException(409, 'Resolve pending filesystem access before changing folder policy.')
            message = payload['message']
            row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
            if row and row.state in ('pending', 'recovering'):
                raise HTTPException(409, 'A folder operation is already pending. Check its status before retrying.')
            desired = message['revision'] + 1
            if row is None:
                row = SecurityFolderPolicy(key=policy.scope_key(coordinates), machine_id=coordinates['machine_id'],
                    account=coordinates['account'], applied_revision=message['revision'])
                db.add(row)
            row.roots, row.revision, row.state = message['roots'], desired, 'pending'
            row.detail = {'allowed_folders': message['allowed_folders'], 'operation_id': operation,
                          'message': 'Waiting for target and account-service acknowledgement.'}
            row.updated_by = who.username
            changed = db.execute(update(SecurityRevision).where(SecurityRevision.id == 1,
                SecurityRevision.revision == payload['revision']).values(revision=payload['revision'] + 1))
            if changed.rowcount != 1:
                raise HTTPException(409, 'Security settings changed. Refresh and review again.')
            change.applied = True
            db.add(AuditLog(actor=who.username, action='security.folders.requested', target=operation,
                detail={'scope': coordinates, 'revision': desired, 'roots': row.roots}))
            db.commit()  # publish the admission fence BEFORE the root/network await
        reply = await target(coordinates, {k: v for k, v in message.items() if k not in ('roots', 'allowed_folders')} |
            {'action': 'policy_apply', 'digest': body.digest})
        with ctx.db() as db:
            policy.lock_revision(db)
            row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
            if (not row or row.state != 'pending' or row.detail.get('operation_id') != operation or row.revision != desired):
                raise HTTPException(409, 'This delivery was superseded by recovery. Refresh status.')
            if type(reply.get('revision')) is not int or reply.get('revision') != desired or reply.get('state') != 'applied':
                raise HTTPException(409, 'The target has not acknowledged this folder revision. New work remains blocked.')
            row.applied_revision, row.state = desired, 'applied'
            db.add(AuditLog(actor=who.username, action='security.folders.applied', target=operation,
                detail={'scope': coordinates, 'revision': desired, 'roots': row.roots}))
            db.commit()
        return {'state': 'applied', 'revision': desired}

    @api.get('/api/security/folders/operations/{operation_id}')
    async def reviewed_operation(operation_id: str, request: Request, who=Depends(ctx.owner)):
        with ctx.db() as db:
            browser(db, request, who)
            change = db.get(SecurityChange, operation_id)
            if not change or change.payload.get('lane') != 'folders' or not change.applied:
                raise HTTPException(404, 'No approved folder operation.')
            payload = change.payload
            coordinates = scope(db, payload['scope']['machine'], payload['scope']['account'])
            row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
            if (coordinates != payload['scope'] or not row or row.state not in ('pending', 'recovering')
                    or row.detail.get('operation_id') != operation_id):
                raise HTTPException(409, 'This operation is not awaiting acknowledgement. Refresh status.')
            result = {'id': change.id, 'digest': change.digest, 'approved': True, 'retry_available': row.state == 'pending' and policy.resolve(db, machine=coordinates['machine'], account=coordinates['account'])['switches']['allowed_folders'] is payload['message']['allowed_folders'],
                'active_sessions': [s.id for s in policy.affected(db, coordinates)],
                'active_gates': active_gates(db, coordinates), 'preview': payload['preview'], 'recovery': None,
                'warning': 'Retry delivers the approved policy. Recovery instead closes this delivery and reloads the target’s acknowledged roots and switch, which can change running sessions’ service access. Neither grants OS access.'}
        try:
            observed = await target(coordinates, {k: v for k, v in payload['message'].items()
                if k not in ('roots', 'allowed_folders')} | {'action': 'status'})
            operation = observed.get('operation') or {}
            result['retry_available'] = result['retry_available'] and (operation.get('state') == 'expired' or operation.get('can_complete') is not False)
            if observed.get('policy_recovery') is True:
                result['recovery'] = {'digest': observed['recovery_digest'], 'roots': observed['roots'],
                    'allowed_folders': observed['allowed_folders'], 'revision': observed['revision']}
        except HTTPException:
            result['recovery_unavailable'] = 'Reconnect or update the fixed folder service to review recovery; no outcome is confirmed.'
        return result

    @api.post('/api/security/folders/{operation_id}/recover')
    async def recover(operation_id: str, body: RecoverIn, request: Request, who=Depends(ctx.owner)):
        with ctx.db() as db:
            browser(db, request, who)
            policy.lock_revision(db)
            change = db.get(SecurityChange, operation_id)
            if (not change or change.payload.get('lane') != 'folders' or not change.applied
                    or change.digest != body.digest):
                raise HTTPException(404, 'No such published folder operation.')
            coordinates = scope(db, change.payload['scope']['machine'], change.payload['scope']['account'])
            row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
            if (coordinates != change.payload['scope'] or not row or row.state not in ('pending', 'recovering')
                    or row.detail.get('operation_id') != operation_id):
                raise HTTPException(409, 'This operation is not awaiting acknowledgement. Refresh status.')
            if policy.access_pending(db, coordinates):
                raise HTTPException(409, 'Resolve pending filesystem access before recovering folder policy.')
            abandoned = row.revision
            row.state = 'recovering'
            row.detail = {**row.detail, 'recovery_digest': body.recovery_digest,
                'message': 'Recovery requested; waiting for target acknowledgement. Review the pending application to finish recovery.'}
            db.execute(update(SecurityRevision).where(SecurityRevision.id == 1).values(revision=SecurityRevision.revision + 1))
            db.add(AuditLog(actor=who.username, action='security.folders.recovery_requested', target=operation_id,
                detail={'scope': coordinates, 'abandoned_revision': abandoned}))
            db.commit()  # admission remains fenced across the root/network call
        reply = await target(coordinates, {'action': 'policy_recover', 'operation_id': operation_id,
            'machine_id': coordinates['machine_id'], 'account': coordinates['account'], 'revision': abandoned,
            'digest': body.digest, 'recovery_digest': body.recovery_digest})
        if (reply.get('state') != 'recovered' or type(reply.get('revision')) is not int
                or reply['revision'] <= abandoned or type(reply.get('allowed_folders')) is not bool or not reply.get('roots')):
            raise HTTPException(409, 'Recovery has not been acknowledged. Keep the fence and review target status again.')
        with ctx.db() as db:
            policy.lock_revision(db)
            row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
            if (row.state != 'recovering' or row.detail.get('operation_id') != operation_id or row.revision != abandoned
                    or row.detail.get('recovery_digest') != body.recovery_digest):
                raise HTTPException(409, 'Another owner recovered this operation. Refresh status.')
            row.roots, row.revision, row.applied_revision = reply['roots'], reply['revision'], reply['revision']
            row.state = 'pending_policy'
            row.detail = {'allowed_folders': reply['allowed_folders'], 'recovered_from': operation_id,
                'message': 'Delivery closed; acknowledged folder policy reloaded at a newer revision. Review folders again, or restore the acknowledged switch in Security.'}
            db.execute(update(SecurityRevision).where(SecurityRevision.id == 1).values(revision=SecurityRevision.revision + 1))
            db.add(AuditLog(actor=who.username, action='security.folders.recovered', target=operation_id,
                detail={'scope': coordinates, 'abandoned_revision': abandoned, 'target_revision': reply['revision'], 'state': 'pending_policy'}))
            db.commit()
        return {'state': 'pending_policy', 'revision': reply['revision']}

    @api.post('/api/security/folders/{operation_id}/retry')
    async def retry(operation_id: str, body: ApplyIn, request: Request, who=Depends(ctx.owner)):
        operation = operation_id
        with ctx.db() as db:
            browser(db, request, who)
            change = db.get(SecurityChange, operation)
            if (not change or change.payload.get('lane') != 'folders' or not change.applied or change.digest != body.digest):
                raise HTTPException(404, 'No such approved folder operation.')
            payload = change.payload
            coordinates = scope(db, payload['scope']['machine'], payload['scope']['account'])
            row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
            message = payload['message']
            if (coordinates != payload['scope'] or not row or row.state != 'pending' or row.detail.get('operation_id') != operation
                    or policy.resolve(db, machine=coordinates['machine'], account=coordinates['account'])['switches']['allowed_folders'] is not message['allowed_folders']):
                raise HTTPException(409, 'The machine or desired policy changed. Review a new operation.')
            if policy.access_pending(db, coordinates):
                raise HTTPException(409, 'Resolve pending filesystem access before changing folder policy.')
            desired = row.revision
            security_revision = policy.revision(db)
        observed = await target(coordinates, {k: v for k, v in message.items() if k not in ('roots', 'allowed_folders')} |
            {'action': 'status'})
        operation_state = (observed.get('operation') or {}).get('state')
        if operation_state in (None, 'expired'):
            # The worker durably closes expired previews under its lock, so an
            # old delayed apply cannot race this recovery. Restore the actual
            # acknowledged baseline, then require a fresh owner review.
            if (type(observed.get('revision')) is not int or observed['revision'] != message['revision']
                    or type(observed.get('allowed_folders')) is not bool or not observed.get('roots')):
                raise HTTPException(409, 'Target policy changed. Refresh its status before retrying.')
            with ctx.db() as db:
                policy.lock_revision(db)
                row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
                if (row.state != 'pending' or row.detail.get('operation_id') != operation or row.revision != desired
                        or policy.revision(db) != security_revision or policy.access_pending(db, coordinates)):
                    raise HTTPException(409, 'Another operation replaced this one. Refresh.')
                row.roots = observed['roots']
                row.revision = row.applied_revision = observed['revision']
                row.state = 'pending_policy'
                row.detail = {'allowed_folders': observed['allowed_folders'], 'recovered_from': operation,
                    'message': 'Unstarted delivery recovered from target status. Review folders again, or restore the acknowledged switch in Security.'}
                db.execute(update(SecurityRevision).where(SecurityRevision.id == 1).values(revision=SecurityRevision.revision + 1))
                db.add(AuditLog(actor=who.username, action='security.folders.recovered', target=operation,
                    detail={'target_revision': observed['revision'], 'operation_state': operation_state, 'state': 'pending_policy'}))
                db.commit()
            return {'state': 'pending_policy', 'revision': observed['revision']}
        reply = await target(coordinates, {k: v for k, v in message.items() if k not in ('roots', 'allowed_folders')} |
            {'action': 'policy_apply', 'digest': body.digest})
        if type(reply.get('revision')) is not int or reply.get('revision') != desired or reply.get('state') != 'applied':
            raise HTTPException(409, 'The target has not acknowledged this folder revision.')
        with ctx.db() as db:
            policy.lock_revision(db)
            row = db.get(SecurityFolderPolicy, policy.scope_key(coordinates))
            if row.state != 'pending' or row.detail.get('operation_id') != operation or row.revision != desired:
                raise HTTPException(409, 'Another folder operation replaced this one. Refresh.')
            row.applied_revision, row.state = desired, 'applied'
            db.execute(update(SecurityRevision).where(SecurityRevision.id == 1).values(revision=SecurityRevision.revision + 1))
            db.add(AuditLog(actor=who.username, action='security.folders.applied', target=operation,
                detail={'scope': coordinates, 'revision': desired, 'retry': True}))
            db.commit()
        return {'state': 'applied', 'revision': desired}

    return api
