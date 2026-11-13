"""Current consent admission for bounded typed operations; never arbitrary commands."""
from __future__ import annotations

import re
import uuid
from datetime import timedelta

from sqlalchemy import select

from . import approvals
from .models import ApprovalConsent, AuditLog, DelegatedAction, PortReservation, Session, utcnow


def admit(db, caller_id, request_key, action, payload):
    approvals.security.lock_revision(db)
    if not isinstance(request_key, str) or not re.fullmatch('[0-9a-f]{32}', request_key):
        raise approvals.Refused('use a stable request key', 422)
    caller = db.get(Session, caller_id)
    if caller is None or caller.team_id != caller_id or caller.parent_session_id is not None:
        raise approvals.Refused('only the bound lead may decide', 403)
    # Inspection/replay returns the recorded outcome without new execution, even
    # after disabling availability. Changed identities/payloads never borrow it.
    old = db.scalar(select(DelegatedAction).where(DelegatedAction.lead_id == caller_id,
                                                DelegatedAction.request_key == request_key))
    identity = approvals.digest({'action': action, 'payload': payload})
    if old:
        if old.digest != identity:
            raise approvals.Refused('request key reused with a changed payload')
        return old, False
    snapshot = approvals.authority(db, caller.owner, caller.project_id, caller_id)
    if not approvals.enabled(db):
        raise approvals.Refused('delegation is disabled')
    if caller.host != 'local' or caller.container or caller.project.containers:
        raise approvals.Refused('this action requires a qualified local native execution scope')
    if action == 'port.reserve':
        if not isinstance(payload, dict) or set(payload) != {'range'} or payload['range'] not in ('dev', 'prod'):
            raise approvals.Refused('reserve only the next free dev or prod port', 422)
    elif action == 'image.check':
        if (not isinstance(payload, dict) or set(payload) != {'provider', 'model'}
                or payload['provider'] not in ('claude', 'codex', 'copilot')
                or not isinstance(payload['model'], str)
                or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/+-]{0,159}', payload['model'])):
            raise approvals.Refused('unknown image action/provider; a person must decide', 422)
        from .session_caps import authorize
        try:
            authorize(db, caller, 'providers', payload['provider'])
            authorize(db, caller, 'models', payload['provider'] + ':' + payload['model'])
        except ValueError as error:
            raise approvals.Refused(str(error)) from None
        inputs = caller.launch_spec.inputs
        # No new provider/account or custom endpoint from this capability.
        if inputs.get('endpoint') or inputs.get('provider', caller.provider) != payload['provider'] or inputs.get('model') != payload['model']:
            raise approvals.Refused('image check must match the lead execution provider and exact model')
    else:
        raise approvals.Refused('unknown action family; a person must decide', 422)
    candidates = list(db.scalars(select(ApprovalConsent).where(ApprovalConsent.team_id == caller_id,
        ApprovalConsent.project_id == caller.project_id, ApprovalConsent.action == action,
        ApprovalConsent.duration != 'project', ApprovalConsent.state == 'active').order_by(ApprovalConsent.created_at)))
    consent = None
    for row in candidates:
        matches = (row.constraints.get('range') == payload['range'] if action == 'port.reserve' else all(row.constraints.get(k) == v for k, v in payload.items()))
        if not matches or row.request_digest is not None and row.request_digest != identity:
            continue
        try:
            approvals.current(db, row)
        except approvals.Refused:
            continue
        consent = row
        break
    if consent is None:
        raise approvals.Refused('matching current consent required')
    if action == 'port.reserve':
        # Count retained admissions against the parent across all team children;
        # releasing a reservation or binding another child never resets the cap.
        parent = consent.parent_id or consent.id
        family = select(ApprovalConsent.id).where((ApprovalConsent.id == parent) | (ApprovalConsent.parent_id == parent))
        used = list(db.scalars(select(DelegatedAction).where(DelegatedAction.consent_id.in_(family),
                                                           DelegatedAction.action == action)))
        if len(used) >= consent.constraints['max_reservations']:
            raise approvals.Refused('port reservation allowance exhausted')
    else:
        day = utcnow() - timedelta(hours=24)
        records = list(db.scalars(select(DelegatedAction).where(DelegatedAction.action == action,
                                                               DelegatedAction.created_at >= day)))
        account = snapshot['placement']
        same_account = [r for r in records if r.authority['placement'] == account]
        if len(same_account) >= 5 or sum(r.payload == payload for r in same_account) >= 2:
            raise approvals.Refused('image check allowance exhausted for this account/model')
    if consent.remaining is not None:
        consent.remaining -= 1
    consent.last_used_at = utcnow()
    decision = {'source': 'C', 'issuer': consent.issuer, 'lead_id': caller_id,
                'consent_id': consent.id, 'consent_revision': consent.revision, 'reason': 'exact_saved_consent'}
    row = DelegatedAction(id=uuid.uuid4().hex, lead_id=caller_id, request_key=request_key,
        action=action, payload=payload, digest=identity, consent_id=consent.id, consent_revision=consent.revision,
        decision=decision, authority=snapshot)
    db.add(row)
    db.flush()
    approvals.audit(db, 'session:' + caller_id, 'use', row.id, decision | {'digest': identity, 'action': action})
    return row, True


def reserve_port(db, caller_id, request_key, pool):
    row, fresh = admit(db, caller_id, request_key, 'port.reserve', {'range': pool})
    if not fresh:
        return row
    from .api.ports import RANGES
    from .session_caps import authorize, effective
    caller = db.get(Session, caller_id)
    try:
        authorize(db, caller, 'expires_at')
    except ValueError as error:
        raise approvals.Refused(str(error)) from None
    allowed_ports = effective(db, caller)['ports']
    taken = set(db.scalars(select(PortReservation.port)))
    port = next((p for p in RANGES[pool] if p not in taken and (allowed_ports is None or p in allowed_ports)), None)
    if port is None:
        # Transaction rolls admission and consumption back with this refusal.
        raise approvals.Refused('port pool exhausted')
    caller = db.get(Session, caller_id)
    db.add(PortReservation(port=port, project_id=caller.project_id, owner=caller.project.owner))
    db.add(AuditLog(actor='session:' + caller_id, action='port.reserve', target=str(port),
                    detail={'project': caller.project.slug, 'delegated': row.id}))
    row.state, row.result = 'completed', {'port': port, 'range': pool, 'project_id': caller.project_id}
    approvals.audit(db, 'session:' + caller_id, 'result', row.id, row.result)
    return row


def out(row):
    return {'id': row.id, 'action': row.action, 'state': row.state, 'decision': row.decision,
            'result': row.result, 'created_at': row.created_at}


# Persist only fixed server-owned explanations, never exception or request prose.
REFUSAL_REASONS = {
    'matching current consent required': 'No current consent',
    'delegation is disabled': 'Delegation disabled',
    'port reservation allowance exhausted': 'Allowance exhausted',
    'image check allowance exhausted for this account/model': 'Allowance exhausted',
    'port pool exhausted': 'Port pool exhausted',
}


def failure_reason(error):
    from fastapi import HTTPException
    if isinstance(error, approvals.Refused):
        from ..install_artifact import REASONS
        if str(error) in REASONS:
            return str(error)
        return REFUSAL_REASONS.get(str(error), 'Scope or consent mismatch')
    if isinstance(error, HTTPException):
        return {429: 'Image check cooldown', 409: 'Image check unavailable',
                422: 'Invalid image check request'}.get(error.status_code, 'Image check unavailable')
    return 'Internal error'


def audit_refusal(db, caller_id, action, reason):
    """Own transaction after failed admission rolls back; bounded per caller/action/reason."""
    approvals.security.lock_revision(db)
    caller = db.get(Session, caller_id)
    if caller is None:
        return
    recent = db.scalar(select(AuditLog.id).where(AuditLog.action == 'approval.refused',
        AuditLog.actor == 'session:' + caller_id, AuditLog.ts >= utcnow() - timedelta(seconds=60),
        AuditLog.detail['action'].as_string() == action, AuditLog.detail['reason'].as_string() == reason).limit(1))
    if recent is None:
        approvals.audit(db, 'session:' + caller_id, 'refused', caller_id,
            {'action': action, 'reason': reason, 'owner': caller.owner, 'project_id': caller.project_id})


def recover_images(db, *, at=None):
    """Lease (30s) plus probe deadline (30s); keep history and quota consumption."""
    approvals.security.lock_revision(db)
    rows = list(db.scalars(select(DelegatedAction).where(DelegatedAction.action == 'image.check',
        DelegatedAction.state == 'dispatched',
        DelegatedAction.created_at <= (at or utcnow()) - timedelta(seconds=60)).with_for_update()))
    for row in rows:
        row.state, row.result = 'uncertain', {'image_input': None, 'reason': 'Image check interrupted'}
        approvals.audit(db, 'system', 'result', row.id, {'state': row.state, 'result': row.result})
    return len(rows)


async def recover(supervisor):
    from .db import write_in_thread
    def work():
        with supervisor.db() as db:
            recover_images(db)
            from .account_installs import recover as recover_installs
            recover_installs(db)
            db.commit()
    await write_in_thread(work)
