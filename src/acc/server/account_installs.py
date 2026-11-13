"""Accd-owned admission state. Socket waits never occur inside these transactions."""
from __future__ import annotations

import pwd
import re
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update

from .. import install_artifact as artifact
from ..broker.account_install import (
    PROTOCOL,
    local_machine_id,
    non_root_uid,
    publication_plan,
    qualification_fingerprint,
    temporary_path,
)
from . import approvals, install_catalog
from .models import (
    AccountInstallJournal,
    ApprovalConsent,
    AuditLog,
    DelegatedAction,
    InstalledTool,
    InstallRecipe,
    Session,
    ToolRequest,
    utcnow,
)

ACTIVE = ('dispatched', 'staged', 'publishing', 'uncertain')


def account_uid(account):
    """Resolve local identity in accd, independently of agent broker claims."""
    try:
        uid = pwd.getpwnam(account).pw_uid
    except (KeyError, OSError):
        raise approvals.Refused('authority_changed') from None
    if not non_root_uid(uid):
        raise approvals.Refused('authority_changed')
    return uid


def check_uid(value, placement):
    if placement['machine'] != 'local' or value['uid'] != account_uid(placement['account']):
        raise approvals.Refused('authority_changed')


def constraints(value):
    fields = {'recipe_id', 'recipe_digest', 'uid', 'runtime_id', 'max_installs'}
    if (not isinstance(value, dict) or set(value) - {'limits'} != fields
            or not isinstance(value['recipe_id'], str) or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,63}', value['recipe_id'])
            or not isinstance(value['recipe_digest'], str) or not re.fullmatch('[0-9a-f]{64}', value['recipe_digest'])
            or not non_root_uid(value['uid'])
            or not isinstance(value['runtime_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['runtime_id'])
            or type(value['max_installs']) is not int or not 1 <= value['max_installs'] <= 5):
        raise approvals.Refused('review exact recipe digest and account runtime', 422)
    try:
        ceilings = artifact.limits(value.get('limits'))
    except artifact.InvalidArtifact:
        raise approvals.Refused('resource_limit', 422) from None
    return dict(value) | {'limits': ceilings}


def qualified(recipe_digest, runtime_id):
    if not approvals.QUALIFIED_TOOL_EXECUTORS:
        raise approvals.Refused('runtime_unqualified')
    try:
        fingerprint = qualification_fingerprint(recipe_digest, runtime_id)
    except (OSError, artifact.InvalidArtifact):
        raise approvals.Refused('runtime_unqualified') from None
    if fingerprint not in approvals.QUALIFIED_TOOL_EXECUTORS:
        raise approvals.Refused('runtime_unqualified')
    return fingerprint


def recipe_for(db, value):
    recipe = install_catalog.resolve(db, value['recipe_id'], value['recipe_digest'])
    if recipe.recipe['runtime_id'] != value['runtime_id']:
        raise approvals.Refused('runtime_unqualified')
    if any(v > recipe.recipe['limits'][k] for k, v in value.get('limits', artifact.LIMITS).items()):
        raise approvals.Refused('resource_limit')
    qualified(recipe.digest, value['runtime_id'])
    return recipe


def account_scope(snapshot):
    return artifact.digest(snapshot['placement'])


def managed_versions(db, account_digest, account, package):
    """Root-owned pins and completed observations, including person removals.

    Same-version no-op observations do not create a new mutation revision. Person
    operation identity remains in the snapshot so a concurrent person change is
    fenced even when it leaves the version string unchanged.
    """
    pin = db.scalar(select(InstalledTool).where(InstalledTool.host == 'local',
        InstalledTool.account == account, InstalledTool.manager == 'npm', InstalledTool.name == package)
        .execution_options(populate_existing=True))
    operations = db.scalars(select(ToolRequest).where(ToolRequest.host == 'local', ToolRequest.account == account,
        ToolRequest.manager == 'npm', ToolRequest.name == package, ToolRequest.status == 'applied',
        ToolRequest.completed_at.is_not(None)).order_by(ToolRequest.completed_at.desc())
        .execution_options(populate_existing=True))
    person = next((r for r in operations if r.decision is None or r.decision.get('source') == 'P'), None)
    previous = db.execute(select(DelegatedAction, InstallRecipe).join(AccountInstallJournal,
        AccountInstallJournal.operation_id == DelegatedAction.id).join(InstallRecipe,
        AccountInstallJournal.recipe_digest == InstallRecipe.digest).where(
        AccountInstallJournal.account_digest == account_digest, DelegatedAction.state == 'completed',
        InstallRecipe.metadata_entry['name'].as_string() == package)
        .order_by(DelegatedAction.created_at.desc()).limit(1)).first()
    events = []
    if pin:
        events.append((pin.installed_at, pin.version))
    if person:
        events.append((person.completed_at, None if person.operation == 'remove' else (person.resolved_version or person.version or 'unknown')))
    if previous:
        events.append((previous[0].created_at, previous[1].recipe['version']))
    version = max(events, key=lambda event: event[0])[1] if events else None
    return {'version': version,
        'person_pin': {'id': pin.id, 'version': pin.version, 'at': pin.installed_at.isoformat()} if pin else None,
        'person_operation': {'id': person.id, 'operation': person.operation,
            'at': person.completed_at.isoformat()} if person else None}


def _cas(db, row, before, after):
    changed = db.execute(update(DelegatedAction).where(DelegatedAction.id == row.id,
        DelegatedAction.state == before).values(state=after))
    if changed.rowcount != 1:
        raise approvals.Refused('authority_changed')
    db.refresh(row)


def admit(db, caller_id, request_key, payload):
    approvals.security.lock_revision(db)
    if (not isinstance(request_key, str) or not re.fullmatch('[0-9a-f]{32}', request_key)
            or not isinstance(payload, dict) or set(payload) != {'recipe_id', 'recipe_digest'}
            or not all(isinstance(v, str) for v in payload.values())):
        raise approvals.Refused('artifact_invalid', 422)
    caller = db.get(Session, caller_id)
    if caller is None or caller.team_id != caller_id or caller.parent_session_id is not None:
        raise approvals.Refused('authority_changed', 403)
    identity = approvals.digest({'action': 'tool.install', 'payload': payload})
    existing = db.scalar(select(DelegatedAction).where(DelegatedAction.lead_id == caller_id,
                                                     DelegatedAction.request_key == request_key))
    if existing:
        if existing.digest != identity:
            raise approvals.Refused('authority_changed')
        return existing, False
    snapshot = approvals.authority(db, caller.owner, caller.project_id, caller_id)
    if (not approvals.enabled(db) or caller.host != 'local' or caller.container or caller.project.containers):
        raise approvals.Refused('authority_changed')
    candidates = db.scalars(select(ApprovalConsent).where(ApprovalConsent.team_id == caller_id,
        ApprovalConsent.project_id == caller.project_id, ApprovalConsent.action == 'tool.install',
        ApprovalConsent.duration != 'project', ApprovalConsent.state == 'active').order_by(ApprovalConsent.created_at))
    selected = None
    for consent in candidates:
        if any(consent.constraints.get(k) != v for k, v in payload.items()):
            continue
        if consent.request_digest is not None and consent.request_digest != identity:
            continue
        try:
            approvals.current(db, consent)
            constraints(consent.constraints)
            recipe = recipe_for(db, consent.constraints)
        except (approvals.Refused, artifact.InvalidArtifact):
            continue
        selected = consent
        break
    if selected is None:
        raise approvals.Refused('authority_changed')
    key = account_scope(snapshot)
    managed = managed_versions(db, key, caller.run_as, recipe.recipe['name'])
    if managed['version'] is not None and managed['version'] != recipe.recipe['version']:
        raise approvals.Refused('collision')
    outstanding = db.scalar(select(AccountInstallJournal.operation_id).join(DelegatedAction,
        DelegatedAction.id == AccountInstallJournal.operation_id).where(AccountInstallJournal.account_digest == key,
        DelegatedAction.state.in_(ACTIVE)).limit(1))
    if outstanding:
        raise approvals.Refused('collision')
    operation_id = uuid.uuid4().hex
    row = DelegatedAction(id=operation_id, lead_id=caller_id, request_key=request_key,
        action='tool.install', payload=payload, digest=identity, consent_id=selected.id,
        consent_revision=selected.revision, authority=snapshot, state='queued',
        decision={'source': 'C', 'issuer': selected.issuer, 'lead_id': caller_id,
            'consent_id': selected.id, 'consent_revision': selected.revision, 'reason': 'exact_recipe_digest'})
    db.add(row)
    db.flush()
    ceilings = selected.constraints.get('limits', artifact.LIMITS)
    deadline = utcnow() + timedelta(seconds=ceilings['execution_seconds'] + ceilings['verification_seconds'] + 10)
    envelope = {'protocol': PROTOCOL, 'operation_id': operation_id, 'nonce': uuid.uuid4().hex,
        'uid': selected.constraints['uid'], 'runtime_id': selected.constraints['runtime_id'], 'host': 'local',
        'recipe_digest': recipe.digest, 'lease_deadline': int(deadline.timestamp()),
        'artifact_sha512': recipe.recipe['artifact_sha512'], 'manifest_digest': recipe.recipe['manifest_digest'],
        'metadata': recipe.metadata_entry, 'limits': ceilings, 'provenance': {
            'request_digest': identity, 'source': 'native-team-spool', 'requesting_session': caller_id,
            'lead_id': caller_id, 'team_id': caller_id, 'team_generation': snapshot['team_revision'],
            'project_id': caller.project_id, 'grant_digest': snapshot['grant_digest'],
            'security_revision': snapshot['security_revision'], 'machine_id': local_machine_id(),
            'account': caller.run_as, 'managed_versions': managed, 'consent_id': selected.id, 'consent_revision': selected.revision,
            'issuer': selected.issuer, 'registry_version': approvals.REGISTRY_VERSION,
            'recipe_revision': recipe.recipe['revision'], 'qualification': qualified(recipe.digest, selected.constraints['runtime_id'])}}
    journal = AccountInstallJournal(operation_id=operation_id, recipe_digest=recipe.digest,
        nonce=envelope['nonce'], account_digest=key, lease_deadline=deadline, envelope=envelope)
    db.add(journal)
    db.flush()
    return row, True


def current(db, operation_id):
    approvals.security.lock_revision(db)
    row = db.get(DelegatedAction, operation_id, populate_existing=True)
    journal = db.get(AccountInstallJournal, operation_id, populate_existing=True)
    if row is None or journal is None:
        raise approvals.Refused('authority_changed')
    if utcnow() >= journal.lease_deadline:
        raise approvals.Refused('lease_expired')
    consent = approvals.current(db, db.get(ApprovalConsent, row.consent_id), check_remaining=row.state == 'queued')
    if (consent.revision != row.consent_revision
            or row.authority != approvals.authority(db, consent.issuer, consent.project_id, row.lead_id)):
        raise approvals.Refused('authority_changed')
    from .session_caps import authorize
    try:
        caps = authorize(db, db.get(Session, row.lead_id), 'installs', 'npm')
        if caps['folders'] is not None:
            raise ValueError('The account extractor cannot publish outside a scoped session folder grant.')
    except ValueError as error:
        raise approvals.Refused(str(error)) from None
    recipe = recipe_for(db, consent.constraints)
    if (recipe.digest != journal.recipe_digest
            or qualified(recipe.digest, consent.constraints['runtime_id']) != journal.envelope['provenance']['qualification']):
        raise approvals.Refused('authority_changed')
    provenance = journal.envelope['provenance']
    if managed_versions(db, journal.account_digest, provenance['account'], recipe.recipe['name']) != provenance['managed_versions']:
        raise approvals.Refused('footprint_mismatch')
    return row, journal, consent, recipe


def dispatched_count(db, account_digest):
    return db.scalar(select(func.count()).select_from(AccountInstallJournal).where(
        AccountInstallJournal.account_digest == account_digest,
        AccountInstallJournal.dispatched_at >= utcnow() - timedelta(hours=24)))


def dispatch(db, operation_id):
    row, journal, consent, recipe = current(db, operation_id)
    if row.state != 'queued':
        raise approvals.Refused('authority_changed')
    if dispatched_count(db, journal.account_digest) >= min(5, consent.constraints['max_installs']):
        raise approvals.Refused('resource_limit')
    # Global revision lock serializes cross-consent account admission and counters.
    active = db.scalar(select(AccountInstallJournal.operation_id).join(DelegatedAction,
        DelegatedAction.id == AccountInstallJournal.operation_id).where(
        AccountInstallJournal.account_digest == journal.account_digest,
        DelegatedAction.state.in_(ACTIVE), DelegatedAction.id != operation_id).limit(1))
    if active:
        raise approvals.Refused('collision')
    approvals.audit(db, 'session:' + row.lead_id, 'claim', row.id,
                    row.decision | {'digest': row.digest, 'recipe_digest': recipe.digest})
    _cas(db, row, 'queued', 'dispatched')
    journal.dispatched_at = utcnow()
    ceilings = journal.envelope['limits']
    journal.lease_deadline = utcnow() + timedelta(seconds=ceilings['execution_seconds'] + ceilings['verification_seconds'] + 10)
    journal.envelope = journal.envelope | {'lease_deadline': int(journal.lease_deadline.timestamp())}
    if consent.remaining is not None:
        consent.remaining -= 1
    consent.last_used_at = utcnow()
    approvals.audit(db, 'session:' + row.lead_id, 'use', row.id,
                    row.decision | {'digest': row.digest, 'recipe_digest': recipe.digest, 'nonce': journal.nonce})
    return journal.envelope, recipe.artifact


def staged(db, operation_id, report):
    row, journal, _, recipe = current(db, operation_id)
    expected = publication_plan({'package': recipe.metadata_entry, 'manifest': recipe.recipe['manifest'],
                                 'bins': recipe.recipe['bins']})
    if (not isinstance(report, dict) or set(report) != {'operation_id', 'manifest', 'manifest_digest', 'plan', 'home_identity'}
            or report['operation_id'] != operation_id or report['manifest'] != recipe.recipe['manifest']
            or report['manifest_digest'] != recipe.recipe['manifest_digest'] or report['plan'] != expected
            or not isinstance(report['home_identity'], list) or len(report['home_identity']) != 3
            or any(type(v) is not int for v in report['home_identity'])
            or report['home_identity'][2] != journal.envelope['uid']
            or journal.before and report['home_identity'] != journal.before['home_identity']):
        raise approvals.Refused('footprint_mismatch')
    _cas(db, row, 'dispatched', 'staged')
    journal.staged = report


def start_publish(db, operation_id):
    row, journal, _, _ = current(db, operation_id)
    _cas(db, row, 'staged', 'publishing')
    return journal.envelope['manifest_digest']


def intent(db, operation_id, index):
    row, journal, _, _ = current(db, operation_id)
    if (row.state != 'publishing' or type(index) is not int or index != len(journal.steps)
            or index >= len(journal.staged['plan'])
            or index and 'observed' not in journal.steps[-1]):
        raise approvals.Refused('authority_changed')
    entry = journal.staged['plan'][index]
    planned = dict(entry)
    if entry['type'] != 'directory':
        planned['temporary'] = temporary_path(operation_id, index, entry)
    journal.steps = journal.steps + [{'index': index, 'intent': planned, 'at': utcnow().isoformat()}]
    return entry


def observed(db, operation_id, index, report):
    # Record effects even after revoke/expiry. Never authorize another step here.
    approvals.security.lock_revision(db)
    journal = db.get(AccountInstallJournal, operation_id, populate_existing=True)
    if journal is None or type(index) is not int or index != len(journal.steps) - 1:
        raise approvals.Refused('authority_changed')
    entry = journal.steps[-1]['intent']
    if (not isinstance(report, dict) or set(report) != {'created', 'path', 'parent_identity', 'identity'}
            or report['path'] != entry['path'] or type(report['created']) is not bool
            or not isinstance(report['parent_identity'], list) or len(report['parent_identity']) != 3
            or any(type(v) is not int for v in report['parent_identity'])
            or report['parent_identity'][2] != journal.envelope['uid']
            or not isinstance(report['identity'], list) or len(report['identity']) != 3
            or any(type(v) is not int for v in report['identity'])
            or report['identity'][2] != journal.envelope['uid']
            or not report['created'] and not entry.get('shared')):
        raise approvals.Refused('footprint_mismatch')
    if 'observed' in journal.steps[-1]:
        raise approvals.Refused('authority_changed')
    journal.steps = journal.steps[:-1] + [journal.steps[-1] | {'observed': report}]


def tool_result(db, row, journal, *, actor=None):
    success = row.state == 'completed'
    detail = dict(row.result or {}) | {'ok': success,
        'version': journal.envelope['metadata']['version'] if success else None,
        'detail': (row.result or {}).get('reason', (row.result or {}).get('state', row.state)),
        'decision': row.decision}
    db.add(AuditLog(actor=actor or 'session:' + row.lead_id, action='tool.result', target=row.id, detail=detail))


def finish(db, operation_id, report):
    row, journal, _, _ = current(db, operation_id)
    if (row.state != 'publishing' or len(journal.steps) != len(journal.staged['plan'])
            or any('observed' not in step for step in journal.steps)):
        raise approvals.Refused('footprint_mismatch')
    expected = [{k: v for k, v in e.items() if k not in ('source', 'shared')} | {'uid': journal.envelope['uid']}
                for e in journal.staged['plan'] if not e.get('shared')]
    if (not isinstance(report, dict) or set(report) != {'state', 'footprint', 'footprint_digest'}
            or report['state'] != 'installed' or report['footprint'] != expected
            or report['footprint_digest'] != artifact.digest(expected)):
        raise approvals.Refused('footprint_mismatch')
    _cas(db, row, 'publishing', 'completed')
    row.result = {'state': 'installed', 'recipe_digest': journal.recipe_digest,
                  'footprint_digest': report['footprint_digest'], 'cleanup': journal.cleanup}
    approvals.audit(db, 'session:' + row.lead_id, 'result', row.id, row.result)
    tool_result(db, row, journal)
    return row


def fail(db, operation_id, reason):
    approvals.security.lock_revision(db)
    row = db.get(DelegatedAction, operation_id, populate_existing=True)
    journal = db.get(AccountInstallJournal, operation_id, populate_existing=True)
    if row is None or journal is None or row.state in ('completed', 'failed', 'uncertain'):
        return row
    # A lost reply or any possible prefix writes is uncertain, blocks this account,
    # and requires person reconciliation. Never infer that revoke removed effects.
    refused_admission = row.state == 'queued'
    uncertain = row.state == 'publishing' or reason == 'transport_lost'
    row.state = 'uncertain' if uncertain else 'failed'
    journal.cleanup = 'person_required' if uncertain else 'not_needed'
    row.result = {'reason': reason if reason in artifact.REASONS else 'internal_error',
                  'cleanup': journal.cleanup, 'recipe_digest': journal.recipe_digest}
    if refused_admission:
        from .delegated_execution import audit_refusal
        audit_refusal(db, row.lead_id, 'tool.install', row.result['reason'])
        return row
    approvals.audit(db, 'session:' + row.lead_id, 'result', row.id, row.result | {'state': row.state})
    tool_result(db, row, journal)
    return row


def recover(db):
    approvals.security.lock_revision(db)
    expired = list(db.scalars(select(AccountInstallJournal).join(DelegatedAction,
        DelegatedAction.id == AccountInstallJournal.operation_id).where(
        DelegatedAction.state.in_(('dispatched', 'staged', 'publishing')),
        AccountInstallJournal.lease_deadline <= utcnow())))
    for journal in expired:
        fail(db, journal.operation_id, 'transport_lost')
    return len(expired)


def preview(db, operation_id):
    row, journal, _, recipe = current(db, operation_id)
    if row.state != 'queued':
        raise approvals.Refused('authority_changed')
    return journal.envelope, recipe.recipe['manifest']


def footprint(db, operation_id, report):
    row, journal, consent, recipe = current(db, operation_id)
    if row.state != 'queued' or not isinstance(report, dict) or report.get('state') not in ('absent', 'already_installed'):
        raise approvals.Refused('footprint_mismatch')
    home = report.get('home_identity')
    if (not isinstance(home, list) or len(home) != 3 or any(type(v) is not int for v in home)
            or home[2] != journal.envelope['uid']):
        raise approvals.Refused('footprint_mismatch')
    journal.before = report
    if report['state'] == 'absent':
        if set(report) != {'state', 'home_identity'}:
            raise approvals.Refused('footprint_mismatch')
        if journal.envelope['provenance']['managed_versions']['version'] is not None:
            raise approvals.Refused('collision')  # Missing managed paths require a person, never repair.
        return False
    plan = publication_plan({'package': recipe.metadata_entry, 'manifest': recipe.recipe['manifest'], 'bins': recipe.recipe['bins']})
    expected = [{k: v for k, v in e.items() if k not in ('source', 'shared')} | {'uid': journal.envelope['uid']}
                for e in plan if not e.get('shared')]
    if (set(report) != {'state', 'home_identity', 'footprint', 'footprint_digest'}
            or report['footprint'] != expected or report['footprint_digest'] != artifact.digest(expected)):
        raise approvals.Refused('collision')
    hour = utcnow() - timedelta(hours=1)
    count = len(list(db.scalars(select(DelegatedAction).join(AccountInstallJournal,
        AccountInstallJournal.operation_id == DelegatedAction.id).where(
        AccountInstallJournal.account_digest == journal.account_digest,
        DelegatedAction.created_at >= hour, DelegatedAction.state == 'completed',
        AccountInstallJournal.dispatched_at.is_(None)))))
    if count >= 10:
        raise approvals.Refused('resource_limit')
    if consent.remaining is not None:
        consent.remaining -= 1
    consent.last_used_at = utcnow()
    approvals.audit(db, 'session:' + row.lead_id, 'claim', row.id,
                    row.decision | {'digest': row.digest, 'recipe_digest': recipe.digest, 'noop': True})
    _cas(db, row, 'queued', 'completed')
    row.result = {'state': 'already_installed', 'recipe_digest': recipe.digest,
                  'footprint_digest': report['footprint_digest'], 'cleanup': 'not_needed'}
    approvals.audit(db, 'session:' + row.lead_id, 'use', row.id, row.decision | {'noop': True})
    approvals.audit(db, 'session:' + row.lead_id, 'result', row.id, row.result)
    tool_result(db, row, journal)
    return True


def cleanup_intent(db, operation_id):
    approvals.security.lock_revision(db)
    journal = db.get(AccountInstallJournal, operation_id, populate_existing=True)
    row = db.get(DelegatedAction, operation_id, populate_existing=True)
    if journal is None or row is None or row.state != 'uncertain':
        return None
    if any('observed' not in step for step in journal.steps):
        return None  # Lost publication reply: no proof of what to compensate.
    for index in reversed(range(len(journal.steps))):
        step = journal.steps[index]
        if not step['observed']['created']:
            continue
        if 'cleanup_observed' in step:
            if step['cleanup_observed'] != {'state': 'removed'}:
                return None
            continue
        if 'cleanup_intent' in step:
            return None  # A lost cleanup reply never authorizes automatic retry.
        journal.steps = journal.steps[:index] + [step | {'cleanup_intent': utcnow().isoformat()}] + journal.steps[index + 1:]
        return {'index': index, 'evidence': step['observed']}
    journal.cleanup = 'restored'
    row.state = 'failed'
    row.result = row.result | {'cleanup': 'restored'}
    approvals.audit(db, 'system', 'reconcile', row.id, {'cleanup': 'restored', 'state': 'failed'})
    return None


def cleanup_observed(db, operation_id, index, report):
    approvals.security.lock_revision(db)
    journal = db.get(AccountInstallJournal, operation_id, populate_existing=True)
    if (journal is None or type(index) is not int or not 0 <= index < len(journal.steps)
            or 'cleanup_intent' not in journal.steps[index]
            or 'cleanup_observed' in journal.steps[index]
            or report not in ({'state': 'removed'}, {'state': 'changed'}, {'state': 'nonempty'})):
        raise approvals.Refused('footprint_mismatch')
    journal.steps = journal.steps[:index] + [journal.steps[index] | {'cleanup_observed': report}] + journal.steps[index + 1:]


def person_inspection(db, actor, operation_id):
    """Person account authority survives stopped teams/revoked consent for recovery."""
    from .models import InstallRecipe
    row = db.get(DelegatedAction, operation_id, populate_existing=True)
    journal = db.get(AccountInstallJournal, operation_id, populate_existing=True)
    if row is None or journal is None:
        raise approvals.Refused('no such install operation', 404)
    consent = db.get(ApprovalConsent, row.consent_id)
    user, project = approvals.scope(db, actor, consent.project_id)
    if user.username != consent.issuer and user.role != 'owner':
        raise approvals.Refused('no such install operation', 404)
    if project.host != 'local' or project.account != journal.envelope['provenance']['account']:
        raise approvals.Refused('authority_changed')
    recipe = db.get(InstallRecipe, journal.recipe_digest)
    if (recipe is None or artifact.digest(recipe.recipe) != recipe.digest
            or artifact.digest(recipe.metadata_entry) != recipe.recipe['metadata_digest']):
        raise approvals.Refused('artifact_invalid')
    install_catalog.validate(recipe.recipe)
    snapshot = approvals.project_authority(db, actor, project.id)
    envelope = journal.envelope | {'lease_deadline': int((utcnow() + timedelta(seconds=30)).timestamp())}
    return row, journal, recipe, snapshot, envelope


def reconcile_person(db, actor, operation_id, snapshot, report, expected_digest):
    approvals.security.lock_revision(db)
    row, journal, recipe, current_snapshot, _ = person_inspection(db, actor, operation_id)
    if snapshot != current_snapshot or row.state != 'uncertain' or artifact.digest(report) != expected_digest:
        raise approvals.Refused('authority_changed')
    home = report.get('home_identity')
    if (not isinstance(home, list) or len(home) != 3 or any(type(v) is not int for v in home)
            or home[2] != journal.envelope['uid']
            or journal.before and home != journal.before['home_identity']):
        raise approvals.Refused('footprint_mismatch')
    if report.get('state') == 'absent' and set(report) == {'state', 'home_identity'}:
        row.state, journal.cleanup = 'failed', 'person_reconciled'
        row.result = {'state': 'absent', 'cleanup': journal.cleanup, 'recipe_digest': recipe.digest}
    elif report.get('state') == 'already_installed' and set(report) == {'state', 'home_identity', 'footprint', 'footprint_digest'}:
        plan = publication_plan({'package': recipe.metadata_entry, 'manifest': recipe.recipe['manifest'], 'bins': recipe.recipe['bins']})
        expected = [{k: v for k, v in e.items() if k not in ('source', 'shared')} | {'uid': journal.envelope['uid']}
                    for e in plan if not e.get('shared')]
        if report['footprint'] != expected or report['footprint_digest'] != artifact.digest(expected):
            raise approvals.Refused('footprint_mismatch')
        row.state, journal.cleanup = 'completed', 'person_reconciled'
        row.result = {'state': 'installed', 'cleanup': journal.cleanup, 'recipe_digest': recipe.digest,
                      'footprint_digest': report['footprint_digest']}
    else:
        raise approvals.Refused('footprint_mismatch')
    journal.before = journal.before or report
    journal.steps = journal.steps + [{'person_reconciliation': {'actor': actor, 'report': report,
                                                               'at': utcnow().isoformat()}}]
    approvals.audit(db, actor, 'reconcile', row.id, row.result | {'state': row.state})
    tool_result(db, row, journal, actor=actor)
    return row
