"""Typed approval authority. Text, risk labels and guard switches never mint consent.

The evaluator is deliberately independent of execution qualification. Phase one
can record a matching policy while refusing an unqualified executor.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import timedelta

from sqlalchemy import select, update

from .. import tooling
from . import security_settings as security
from .models import (
    API_PRINCIPAL,
    ApprovalBatch,
    ApprovalConsent,
    ApprovalOperation,
    AuditLog,
    Project,
    Session,
    Setting,
    Team,
    ToolRequest,
    User,
    utcnow,
)

ACTIONS = frozenset({'tool.install', 'main.merge', 'main.push'})
REGISTRY_VERSION = 1
# Deliberately empty until a typed broker adapter proves canonical provenance,
# account UID/destination, bounded footprint and fresh execution admission.
QUALIFIED_TOOL_EXECUTORS = frozenset()


class Refused(ValueError):
    def __init__(self, reason, status=409):
        super().__init__(reason)
        self.status = status


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def audit(db, actor, action, target, detail=None):
    db.add(AuditLog(actor=actor, action='approval.' + action, target=target, detail=detail or {}))


def enabled(db):
    from .admin_features import CURRENT
    features = CURRENT.get()
    if features is not None and not features.enabled('teams'):
        return False
    availability = db.get(Setting, 'admin_features')
    if availability and availability.value.get('choices', {}).get('teams') is False:
        return False
    row = db.get(Setting, 'approvals.enabled')
    return row is None or row.value is True


def person(db, actor):
    if actor == API_PRINCIPAL or actor.startswith('session:'):
        raise Refused('a signed-in person must issue consent', 403)
    user = db.scalar(select(User).where(User.username == actor))
    if user is None:
        raise Refused('the person no longer exists', 403)
    return user


def scope(db, actor, project_id):
    user = person(db, actor)
    if project_id is None:
        raise Refused('the project is unavailable for this consent', 404)
    project = db.get(Project, project_id)
    # Group consent needs merge/account authority beyond ordinary project ownership.
    if (project is None or project.group_id is not None or project.archived or project.deleting
            or project.import_mode != 'managed' or not project.trusted
            or user.role != 'owner' and (project.owner != actor or project.account != user.account)):
        raise Refused('the project is unavailable for this consent', 404)
    if project.host != 'local':
        from .group_remote import reserved_for_other
        if reserved_for_other(db, project.host, project.account, actor):
            raise Refused('the machine/account is unavailable', 404)
    return user, project


def authority(db, actor, project_id, team_id):
    user, project = scope(db, actor, project_id)
    lead, team = db.get(Session, team_id), db.get(Team, team_id)
    if (not lead or not team or lead.team_id != team.id or lead.parent_session_id is not None
            or lead.owner != actor or lead.project_id != project_id
            or lead.run_as != project.account or lead.host != project.host
            or lead.placement is not None and lead.placement.account != lead.run_as
            or lead.desired_state != 'running' or team.stopped or lead.team_stop_pending
            or team.expires_at is not None and utcnow() >= team.expires_at
            or team.limits.get('run_mode', 'build') != 'build'):
        raise Refused('the lead is outside its active Build team grant')
    inputs = lead.launch_spec.inputs
    from .agent_contract import read_only
    from .compiler import LaunchInputs
    if read_only(LaunchInputs.from_dict(inputs)):
        raise Refused('read-only assignments cannot receive write consent')
    placement = security.account_scope(db, lead.host, lead.run_as)
    if placement['machine_id'] == 'unavailable':
        raise Refused('the enrolled machine identity is unavailable')
    resolved = security.resolve(db, project_id=project_id, owner=actor, machine=lead.host, account=lead.run_as)
    if resolved['enforcement']['allowed_folders'] != 'applied':
        raise Refused('the account security policy is not applied')
    return project_authority(db, actor, project_id) | {
            'team_id': team.id, 'team_revision': team.lifetime_revision, 'grant_digest': digest(team.limits),
            'launch_digest': lead.launch_spec.digest, 'placement_id': lead.placement_id,
            'container': lead.container, 'container_generation': lead.container_generation,
            'run_id': lead.runs[-1].id if lead.runs else None}


def project_authority(db, actor, project_id):
    user, project = scope(db, actor, project_id)
    placement = security.account_scope(db, project.host, project.account)
    if placement['machine_id'] == 'unavailable':
        raise Refused('the enrolled machine identity is unavailable')
    return {'registry_version': REGISTRY_VERSION, 'issuer_id': user.id, 'issuer_role': user.role,
            'issuer_account': user.account, 'project_owner': project.owner, 'project_id': project.id,
            'workspace': project.workspace_path, 'remote': project.remote_url,
            'repository': project.repo, 'private_workspace': project.private_workspace,
            'containers': project.containers,
            'placement': placement, 'security_revision': security.revision(db)}


def validate(action, constraints):
    if action not in ACTIONS or not isinstance(constraints, dict):
        raise Refused('unknown action class; a person must decide', 422)
    if action == 'tool.install':
        if set(constraints) != {'manager', 'name', 'version', 'registry'}:
            raise Refused('review exact tool coordinates and registry', 422)
        try:
            tooling.validate(constraints['manager'], constraints['name'], constraints['version'])
        except ValueError as error:
            raise Refused(str(error), 422) from None
        if constraints['manager'] != 'npm' or constraints['registry'] != 'https://registry.npmjs.org' or constraints['version'] is None:
            raise Refused('only exact npm registry coordinates can be recorded in phase one', 422)
    else:
        if set(constraints) != {'repository', 'remote', 'ref', 'verification_policy'} or any(
                not isinstance(v, str) or not v or len(v) > 500 for v in constraints.values()):
            raise Refused('review exact repository, remote, default ref and verification policy', 422)
        if not re.fullmatch(r'refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*', constraints['ref']) or any(
                s in constraints['ref'] for s in ('..', '//', '@{')) or constraints['ref'].endswith(('/', '.', '.lock')):
            raise Refused('invalid exact default ref', 422)
    return dict(constraints)


def grant(db, *, actor, project_id, team_id, action, constraints, duration,
          expires_at=None, request_digest=None, verification_ref=None):
    security.lock_revision(db)
    if not enabled(db):
        raise Refused('delegation is disabled')
    # Check principal before fields so installation tokens cannot probe policy.
    snapshot = authority(db, actor, project_id, team_id)
    constraints = validate(action, constraints)
    if duration not in ('once', 'team', 'project'):
        raise Refused('choose once, team or project duration', 422)
    if duration == 'once' and not (isinstance(request_digest, str) and re.fullmatch('[0-9a-f]{64}', request_digest)):
        raise Refused('once consent needs the exact reviewed request digest', 422)
    if duration != 'once' and request_digest is not None:
        raise Refused('request digests belong to once consent', 422)
    project = db.get(Project, project_id)
    if action.startswith('main.') and project.private_workspace:
        raise Refused('private workspace promotion is required before main consent')
    if action.startswith('main.') and (constraints['repository'] != project.workspace_path or
                                      constraints['remote'] != project.remote_url):
        raise Refused('repository or remote identity changed')
    if expires_at is not None and expires_at <= utcnow():
        raise Refused('consent expiry must be in the future', 422)
    team = db.get(Team, team_id)
    if duration != 'project' and team.expires_at is not None:
        expires_at = min(expires_at, team.expires_at) if expires_at else team.expires_at
    if duration == 'project':
        snapshot = project_authority(db, actor, project_id)
    row = ApprovalConsent(id=uuid.uuid4().hex, issuer=actor, project_id=project_id, team_id=team_id,
        action=action, constraints=constraints, authority=snapshot, duration=duration,
        request_digest=request_digest, expires_at=expires_at, remaining=1 if duration == 'once' else None,
        verification_ref=verification_ref)
    db.add(row)
    db.flush()
    audit(db, actor, 'grant', row.id, {'action': action, 'scope': snapshot, 'constraints': constraints,
                                     'duration': duration, 'digest': digest(constraints)})
    return row


def current(db, row):
    # A caller may have loaded the row before waiting for the revision fence.
    # READ COMMITTED must re-read it rather than trust SQLAlchemy's identity map.
    if row is not None:
        row = db.get(ApprovalConsent, row.id, populate_existing=True)
    if row is None or row.state != 'active':
        raise Refused('consent is revoked or unavailable')
    if not enabled(db):
        raise Refused('delegation is disabled')
    if row.expires_at is not None and utcnow() >= row.expires_at:
        raise Refused('consent expired')
    if row.remaining is not None and row.remaining < 1:
        raise Refused('consent is exhausted')
    expected = (project_authority(db, row.issuer, row.project_id) if row.duration == 'project'
                else authority(db, row.issuer, row.project_id, row.team_id))
    if row.authority != expected:
        raise Refused('consent authority changed; review a fresh consent')
    if row.parent_id:
        current(db, db.get(ApprovalConsent, row.parent_id))
    validate(row.action, row.constraints)
    return row


def bind(db, consent_id, lead_id, *, actor):
    """A durable policy can only issue a same-scope, live-team child."""
    security.lock_revision(db)
    person(db, actor)
    parent = current(db, db.get(ApprovalConsent, consent_id))
    if parent.issuer != actor:
        raise Refused('only the person who issued the policy can bind it', 403)
    if parent.duration != 'project' or parent.parent_id is not None:
        raise Refused('only a durable project policy can issue a team child')
    snapshot = authority(db, parent.issuer, parent.project_id, lead_id)
    existing = db.scalar(select(ApprovalConsent).where(ApprovalConsent.parent_id == parent.id,
                                                     ApprovalConsent.team_id == lead_id))
    if existing:
        return current(db, existing)
    team = db.get(Team, lead_id)
    expiry = min(t for t in (parent.expires_at, team.expires_at) if t is not None) if (
        parent.expires_at or team.expires_at) else None
    row = ApprovalConsent(id=uuid.uuid4().hex, parent_id=parent.id, issuer=parent.issuer,
        project_id=parent.project_id, team_id=lead_id, action=parent.action, constraints=parent.constraints,
        authority=snapshot, duration='team', expires_at=expiry, verification_ref=parent.verification_ref)
    db.add(row)
    db.flush()
    audit(db, actor, 'bind', row.id, {'parent_id': parent.id, 'lead_id': lead_id, 'scope': snapshot})
    return row


def revoke(db, consent_id, actor, expected_revision):
    security.lock_revision(db)
    row = db.scalar(select(ApprovalConsent).where(ApprovalConsent.id == consent_id).with_for_update()
                    .execution_options(populate_existing=True))
    user = person(db, actor)
    if row is None or row.issuer != actor and user.role != 'owner':
        raise Refused('no such consent', 404)
    if row.revision != expected_revision:
        raise Refused('consent changed; refresh before revoking')
    if row.state == 'revoked':
        return row
    row.state, row.revoked_at, row.revision = 'revoked', utcnow(), row.revision + 1
    db.execute(update(ApprovalOperation).where(ApprovalOperation.consent_id == row.id,
        ApprovalOperation.state == 'queued').values(state='cancelled'))
    children = list(db.scalars(select(ApprovalConsent).where(ApprovalConsent.parent_id == row.id)))
    for child in children:
        revoke(db, child.id, actor, child.revision)
    audit(db, actor, 'revoke', row.id, {'revision': row.revision,
        'effect': 'new admission denied; dispatched work may still finish'})
    return row


def tool_payload(row):
    return {key: getattr(row, key) for key in ('id', 'session_id', 'project_id', 'owner', 'account',
            'host', 'manager', 'name', 'version', 'operation', 'reason')}


def person_decision(reason):
    return {'source': 'P', 'reason': reason, 'consent_id': None, 'lead_id': None}


def match(db, lead_id, project_id, action, constraints, request_digest):
    for row in db.scalars(select(ApprovalConsent).where(ApprovalConsent.team_id == lead_id,
            ApprovalConsent.project_id == project_id, ApprovalConsent.action == action,
            ApprovalConsent.duration != 'project', ApprovalConsent.state == 'active').order_by(ApprovalConsent.created_at)):
        if row.constraints != constraints or row.request_digest is not None and row.request_digest != request_digest:
            continue
        try:
            current(db, row)
        except (Refused, ValueError):
            continue
        return {'source': 'C', 'reason': 'exact_saved_consent', 'consent_id': row.id,
                'consent_revision': row.revision, 'lead_id': lead_id, 'issuer': row.issuer}
    return person_decision('matching_current_consent_required')


def evaluate_tool(db, request, lead_id):
    if request.operation != 'install' or request.manager != 'npm' or request.version is None:
        return person_decision('person_only_tool_operation')
    lead = db.get(Session, lead_id)
    worker = db.get(Session, request.session_id) if request.session_id else None
    if (not lead or not worker or lead.team_id != lead_id or worker.team_id != lead_id
            or worker.project_id != lead.project_id or worker.owner != lead.owner
            or request.project_id != lead.project_id or request.owner != lead.owner
            or request.account != lead.run_as or request.host != lead.host
            or worker.run_as != request.account or worker.host != request.host
            or worker.desired_state != 'running' or worker.team_stop_pending):
        return person_decision('outside_frozen_team_scope')
    from .agent_contract import read_only
    from .compiler import LaunchInputs
    if read_only(LaunchInputs.from_dict(worker.launch_spec.inputs)):
        return person_decision('read_only_worker')
    return match(db, lead_id, request.project_id, 'tool.install', {'manager': request.manager,
        'name': request.name, 'version': request.version, 'registry': 'https://registry.npmjs.org'}, digest(tool_payload(request)))


def claim_tool(db, request, lead_id):
    security.lock_revision(db)
    existing = db.scalar(select(ApprovalOperation).where(ApprovalOperation.request_id == request.id).with_for_update()
                         .execution_options(populate_existing=True))
    if existing:
        if existing.digest != digest(tool_payload(request)) or existing.lead_id != lead_id:
            raise Refused('request identity was reused with a changed payload or lead')
        return existing
    result = evaluate_tool(db, request, lead_id)
    if result['source'] != 'C' or request.status != 'pending_approval':
        raise Refused(result['reason'])
    row = ApprovalOperation(id=uuid.uuid4().hex, request_id=request.id, digest=digest(tool_payload(request)),
        consent_id=result['consent_id'], consent_revision=result['consent_revision'], lead_id=lead_id,
        expires_at=utcnow() + timedelta(seconds=30), reason=result['reason'])
    db.add(row)
    db.flush()
    audit(db, 'session:' + lead_id, 'claim', row.id, result | {'request_id': request.id, 'digest': row.digest})
    return row


def consume(db, operation_id):
    security.lock_revision(db)
    op = db.scalar(select(ApprovalOperation).where(ApprovalOperation.id == operation_id).with_for_update()
                   .execution_options(populate_existing=True))
    if op is None:
        raise Refused('unknown operation')
    consent = current(db, db.get(ApprovalConsent, op.consent_id))
    if op.state != 'queued' or utcnow() >= op.expires_at or op.consent_revision != consent.revision:
        raise Refused('operation expired, cancelled or already consumed')
    request = db.get(ToolRequest, op.request_id)
    result = evaluate_tool(db, request, op.lead_id)
    if (op.digest != digest(tool_payload(request)) or request.status != 'pending_approval'
            or result['consent_id'] != consent.id):
        raise Refused('request or authority changed')
    if request.manager not in QUALIFIED_TOOL_EXECUTORS:
        raise Refused('a qualified canonical-registry executor is required')
    if consent.remaining is not None:
        claimed = db.execute(update(ApprovalConsent).where(ApprovalConsent.id == consent.id,
            ApprovalConsent.remaining > 0, ApprovalConsent.revision == op.consent_revision,
            ApprovalConsent.state == 'active').values(remaining=ApprovalConsent.remaining - 1))
        if not claimed.rowcount:
            raise Refused('consent exhausted or revoked')
    consent.last_used_at = utcnow()
    op.state = 'dispatched'
    request.status, request.decided_by, request.decision = 'approved', 'session:' + op.lead_id, result | {'operation_id': op.id}
    audit(db, request.decided_by, 'use', op.id, request.decision)
    return request


def evaluate_main(db, lead_id, action, constraints, request_digest):
    if action not in ('main.merge', 'main.push'):
        return person_decision('unknown_main_action')
    lead = db.get(Session, lead_id)
    if not lead:
        return person_decision('unknown_lead')
    return match(db, lead_id, lead.project_id, action, constraints, request_digest)


def claim_main(db, lead_id, action, constraints, request_digest):
    # Never treat an agent report or ordinary gate result as an atomic, trusted
    # candidate/base/export-bound finalization proof. No ref update path yet.
    raise Refused('a qualified main-finalization verifier and integration fence are required')


def preview_batch(db, actor, ids):
    security.lock_revision(db)
    if not isinstance(ids, list) or not 1 <= len(ids) <= 50 or len(set(ids)) != len(ids):
        raise Refused('select 1–50 distinct exact pending requests', 422)
    from .tools import Tools
    payload = []
    for rid in ids:
        row = db.get(ToolRequest, rid)
        if row is None or not Tools.can_see_actor(db, row, actor):
            raise Refused('no such pending request', 404)
        if row.status != 'pending_approval':
            raise Refused('request is no longer pending')
        payload.append({'request_id': rid, 'digest': digest(tool_payload(row))})
    # Root/system and routine account operations are separate previews.
    families = {db.get(ToolRequest, rid).manager in tooling.SYSTEM for rid in ids}
    if len(families) != 1:
        raise Refused('review system and account tools in separate batches', 422)
    row = ApprovalBatch(id=uuid.uuid4().hex, actor=actor, payload=payload,
        security_revision=security.revision(db), digest=digest(payload), expires_at=utcnow() + timedelta(minutes=5))
    db.add(row)
    db.flush()
    audit(db, actor, 'batch.preview', row.id, {'digest': row.digest, 'count': len(payload)})
    return row


def approve_batch(db, batch_id, actor, expected_digest):
    security.lock_revision(db)
    batch = db.scalar(select(ApprovalBatch).where(ApprovalBatch.id == batch_id).with_for_update()
                      .execution_options(populate_existing=True))
    if batch is None or batch.actor != actor:
        raise Refused('no such approval preview', 404)
    if batch.used or utcnow() >= batch.expires_at or batch.digest != expected_digest or batch.security_revision != security.revision(db):
        raise Refused('approval preview changed, expired or already used')
    from .tools import Tools
    rows = []
    for item in batch.payload:
        row = db.scalar(select(ToolRequest).where(ToolRequest.id == item['request_id']).with_for_update()
                        .execution_options(populate_existing=True))
        if (row is None or row.status != 'pending_approval' or digest(tool_payload(row)) != item['digest']
                or not Tools.can_see_actor(db, row, actor)):
            raise Refused('a reviewed request changed; preview the batch again')
        Tools.validate_person(db, row)
        rows.append(row)
    # All validation precedes any status changes. The caller commits once.
    from . import attention
    for row in rows:
        row.status, row.decided_by = 'approved', actor
        row.decision = {'source': 'P', 'reason': 'exact_person_batch', 'issuer': 'Owner via API' if actor == API_PRINCIPAL else actor,
                        'batch_id': batch.id, 'digest': digest(tool_payload(row))}
        attention.resolve(db, 'tool:' + row.id)
        db.add(AuditLog(actor=actor, action='tool.approve', target=row.id, detail=row.decision))
    batch.used = True
    audit(db, actor, 'batch.use', batch.id, {'digest': batch.digest, 'requests': [r.id for r in rows]})
    return [r.id for r in rows]
