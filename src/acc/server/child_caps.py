"""Server-owned parent proofs. Changed authority fences a saved child generation.

This is an admission fence, not an OS sandbox for native same-account processes.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import event, func, inspect, select
from sqlalchemy.orm import Session as DBSession

from ..session_capabilities import Refused as Refused
from .models import API_PRINCIPAL, Session, Setting, Team, User, utcnow

FILE = 'parent-grant.json'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def intersect(asked, parent):
    """Closed permission algebra: allow booleans AND, sets intersect, limits min.

    Missing dimensions deny. Security protections must be expressed as denies
    (or converted to allow booleans before entering this algebra).
    """
    result = {}
    for key, value in asked.items():
        ceiling = parent.get(key)
        if type(value) is bool and type(ceiling) is bool:
            result[key] = value and ceiling
        elif isinstance(value, list) and isinstance(ceiling, list):
            result[key] = [v for v in value if v in ceiling]
        elif type(value) in (int, float) and type(ceiling) in (int, float):
            result[key] = min(value, ceiling)
        elif isinstance(value, dict) and isinstance(ceiling, dict):
            result[key] = intersect(value, ceiling)
        else:
            raise Refused(f'unknown or missing permission dimension: {key}')
    return result


def chain(db, session):
    state = inspect(session, raiseerr=False)
    recorded = getattr(session, 'parent_chain', None) is not None or state is not None and (state.persistent or state.detached)
    if recorded and (session.owner is None or session.root_owner is None):
        raise Refused(f'limited by {session.parent_session_id or session.id}: missing recorded owner/root person')
    rows, seen = [], {getattr(session, 'id', None)}
    parent_id = getattr(session, 'parent_session_id', None)
    while parent_id:
        if parent_id in seen:
            raise Refused('invalid parent chain')
        seen.add(parent_id)
        parent = next((row for row in db.new if isinstance(row, Session) and row.id == parent_id), None)
        parent = parent if parent is not None else db.get(Session, parent_id)
        parent_state = inspect(parent, raiseerr=False) if parent is not None else None
        if parent is not None and parent_state.persistent and (parent.owner is None or parent.root_owner is None):
            raise Refused(f'limited by {parent_id}: missing recorded owner/root person')
        if parent is None or (parent.owner or API_PRINCIPAL) != (session.owner or API_PRINCIPAL):
            raise Refused(f'limited by {parent_id}: parent missing or ownership changed')
        rows.append(parent)
        parent_id = parent.parent_session_id
    if getattr(session, 'parent_chain', None) is not None and session.parent_chain != [p.id for p in reversed(rows)]:
        raise Refused(f'limited by {session.parent_session_id or session.id}: recorded parent chain does not match its immutable links')
    if getattr(session, 'root_owner', None) is not None and session.root_owner != ((rows[-1].owner if rows else session.owner) or API_PRINCIPAL):
        raise Refused(f'limited by {rows[-1].id if rows else session.id}: root person changed; recorded lineage cannot be reassigned')
    return rows


def grant_digest(db, session):
    parents = chain(db, session)
    parent_digest = None
    for row in [*reversed(parents), session]:
        from .security_settings import resolve
        team = db.get(Team, row.team_id) if row.team_id else None
        person = db.scalar(select(User).where(User.username == row.owner))
        project = row.project
        guard = db.get(Setting, 'guardrails')
        security = resolve(db, project_id=row.project_id, owner=row.owner, machine=row.host, account=row.run_as)
        parent_digest = digest({'session': row.id, 'launch': row.launch_spec.digest,
            'owner': row.owner, 'account': row.run_as, 'machine': row.host,
            'inputs': row.launch_spec.inputs, 'files': digest(row.launch_spec.files),
            'capabilities': {'requested': row.cap_requested, 'limit': row.cap_limit},
            'custom_rules': (guard.value.get('custom') or []) if guard else [],
            'person': {'role': person.role, 'account': person.account} if person else None,
            'project': {'account': project.account, 'owner': project.owner, 'trusted': project.trusted,
                        'containers': project.containers, 'archived': project.archived,
                        'private_workspace': project.private_workspace} if project else None,
            'security': {key: security[key] for key in ('switches', 'enforcement')},
            # Liveness is checked separately: stopping a source must not revoke an explanation.
            'team': {'limits': team.limits, 'expires': str(team.expires_at)} if team else None,
            'parent': parent_digest})
    return parent_digest


def proof(db, parent):
    parents = chain(db, parent)
    return {'chain': [row.id for row in reversed(parents)] + [parent.id],
            'root_owner': (parents[-1] if parents else parent).owner,
            'parent_digest': grant_digest(db, parent)}


def check(db, session, *, require_running=True):
    if not getattr(session, 'parent_session_id', None):
        return
    rows = chain(db, session)
    explanation = not require_running or 'explanation.json' in session.launch_spec.files
    parent = rows[0]
    saved = json.loads(session.launch_spec.files.get(FILE, '{}'))
    if saved != proof(db, parent):
        raise Refused(f'limited by {parent.id}: parent grant changed; start a new child generation')
    from .security_settings import check_generation
    for row in reversed(rows):
        team = db.get(Team, row.team_id) if row.team_id else None
        if team and ((team.stopped and not explanation) or team.expires_at is not None and utcnow() >= team.expires_at):
            raise Refused(f'limited by {row.id}: parent team is stopped or expired')
        if not explanation and (row.desired_state != 'running' or row.team_stop_pending):
            raise Refused(f'limited by {row.id}: parent is stopped')
        try:
            check_generation(db, session, check_parent=False, authority=row)
        except ValueError as error:
            raise Refused(f'limited by {row.id}: {error}') from None


def constrain(db, parent, inputs, policy, *, explanation=False):
    """Reject an unrepresentable widening before compilation; never silently bypass."""
    from acc import providers

    from .agent_contract import read_only
    from .compiler import LaunchInputs
    from .security_settings import frozen_protections, resolve, validate_launch_inputs
    if explanation:
        check(db, parent, require_running=False)
    else:
        check(db, parent)
    saved_inputs = LaunchInputs.from_dict(parent.launch_spec.inputs)
    if read_only(saved_inputs) and not read_only(inputs):
        raise Refused(f'limited by {parent.id}: parent is read-only')
    provider = providers.get(inputs.provider)
    # Fixed read-only explanations deny every tool in compiled guardrails.
    # Codex never means deny escalation here, so unattended runs cannot stall.
    unattended = explanation and inputs.provider == 'codex' and inputs.sandbox == 'read-only' and inputs.approval == 'never' and not inputs.mcps
    if provider.dangerous(inputs) and not unattended:
        # Distinct bypass dimensions (sandbox and approval, for example) cannot
        # substitute for one another, nor for another provider's bypass flag.
        if inputs.provider != saved_inputs.provider or any(
                provider.option(inputs, option.key) in option.dangerous
                and provider.option(saved_inputs, option.key) not in option.dangerous
                for option in provider.options if option.dangerous):
            raise Refused(f'limited by {parent.id}: parent does not permit provider bypass options')
    same_provider = inputs.provider == saved_inputs.provider
    child_level = 0 if unattended else provider.permission_level(inputs)
    parent_level = providers.get(saved_inputs.provider).permission_level(saved_inputs)
    if not same_provider and child_level > parent_level:
        names = ('read-only', 'asks', 'edits without asking', 'auto', 'bypass')
        raise Refused(f'limited by {parent.id}: child requires {names[child_level]} (level {child_level}); '
                      f'parent permits {names[parent_level]} (level {parent_level})')
    for option in provider.options if same_provider else ():
        order = option.permission_order
        if not order:
            continue
        value = provider.option(inputs, option.key)
        if unattended and option.key == 'approval':
            continue
        ceiling = provider.option(saved_inputs, option.key) if inputs.provider == saved_inputs.provider else order[0]
        if value not in order or ceiling not in order or order.index(value) > order.index(ceiling):
            raise Refused(f'limited by {parent.id}: child {option.key} exceeds parent permission mode')
    if inputs.endpoint is not None and inputs.endpoint != saved_inputs.endpoint:
        raise Refused(f'limited by {parent.id}: child endpoint exceeds parent endpoint')
    if inputs.extra_args:
        raise Refused(f'limited by {parent.id}: raw provider overrides cannot prove containment')
    frozen, switches = frozen_protections(parent)
    current = resolve(db, project_id=parent.project_id, owner=parent.owner,
                      machine=parent.host, account=parent.run_as)
    allowed = intersect(
        {'security': {key: not value for key, value in policy['security']['switches'].items()},
         'tools': list(inputs.mcps)},
        {'security': {key: not (switches.get(key, True) or current['switches'][key])
                      for key in policy['security']['switches']},
         'tools': list(saved_inputs.mcps)})
    policy['security']['switches'] = {key: not value for key, value in allowed['security'].items()}
    from dataclasses import replace
    policy['dropped_mcps'] = sorted(set(inputs.mcps) - set(allowed['tools']))
    inputs = replace(inputs, mcps=tuple(allowed['tools']))
    policy['rules'] = {key: policy['security']['switches'][key] for key in policy['rules']}
    policy['tool_approval'] |= frozen.get('tool_approval', True)
    policy['local_only'] |= frozen.get('local_only', False)
    policy['custom'] += [rule for rule in frozen.get('custom', []) if rule not in policy['custom']]
    try:
        validate_launch_inputs(inputs, policy['security'], explanation_only=explanation)
    except ValueError as error:
        raise Refused(f'limited by {parent.id}: {error}') from None
    return inputs, policy


@event.listens_for(DBSession, 'before_flush')
def immutable_parent(db, flush_context, instances):
    for row in db.new:
        if isinstance(row, Session):
            row.owner = row.owner or API_PRINCIPAL
    for row in db.new:
        if isinstance(row, Session):
            parents = chain(db, row) if row.parent_session_id else []
            row.parent_chain = [p.id for p in reversed(parents)]
            row.root_owner = (parents[-1].owner if parents else row.owner) or API_PRINCIPAL
    for row in db.dirty:
        if isinstance(row, Session) and inspect(row).persistent:
            state = inspect(row)
            if any(state.attrs[key].history.has_changes() for key in ('parent_session_id', 'parent_chain')):
                raise Refused('parent session is immutable')
            if state.attrs.root_owner.history.has_changes():
                if not (db.info.get('transfer_session_owner') == row.owner
                        and state.attrs.owner.history.has_changes() and row.root_owner == row.owner):
                    raise Refused('parent session is immutable')


def proof_refresh_plan(db, rows):
    """Capture only currently valid proofs, never grant a stale generation new authority."""
    from .compiler import launch_digest
    rehash = {}
    # Snapshot before any ownership/project changes, including every ancestor's proof.
    for row in rows:
        if not row.parent_session_id:
            continue
        try:
            ancestors = chain(db, row)
            if all(json.loads(node.launch_spec.files.get(FILE, '{}')) == proof(db, db.get(Session, node.parent_session_id))
                   for node in [row, *ancestors] if node.parent_session_id):
                spec = row.launch_spec
                formulas = {
                    'compiler_inputs_files': lambda files, inputs=spec.inputs: launch_digest(inputs, files),
                    'files_json': lambda files: hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
                    'files_compact_json': digest,
                }
                for name, formula in formulas.items():
                    if formula(spec.files) == spec.digest:
                        rehash[row.id] = name, formula
                        break
                # Unknown digest schemes remain fenced rather than being silently converted.
        except (Refused, ValueError, TypeError):
            pass  # Broken/missing/stale evidence is preserved, never newly blessed.
    return rehash


def refresh_proofs(db, rows, rehash):
    """Apply a person-reviewed binding/deadline change using the original digest scheme."""
    from .models import LaunchSpec
    changes = []
    for row in sorted(rows, key=lambda row: len(row.parent_chain or [])):
        if row.id not in rehash:
            continue
        spec = row.launch_spec
        files = dict(spec.files)
        files[FILE] = json.dumps(proof(db, db.get(Session, row.parent_session_id)), sort_keys=True)
        scheme, formula = rehash[row.id]
        new_digest = formula(files)
        changes.append({'session': row.id, 'before': spec.digest, 'after': new_digest, 'scheme': scheme})
        # Normal specs have one owning session. Shared retained specs are copied only
        # while another session still references the original, so neither is orphaned.
        references = db.scalar(select(func.count(Session.id)).where(Session.launch_spec_id == spec.id))
        if references > 1:
            row.launch_spec = LaunchSpec(provider=spec.provider, inputs=dict(spec.inputs), files=files, digest=new_digest)
        else:
            spec.files, spec.digest = files, new_digest
        db.flush()
    return changes


def transfer_owner(db, sessions, owner, actor, *, projects=()):
    """Owner-only transfer; preserve stale fences and update valid proofs atomically."""
    from .models import AuditLog
    user = db.scalar(select(User).where(User.username == actor))
    if actor != API_PRINCIPAL and (user is None or user.role != 'owner'):
        raise Refused('Only an owner can transfer session chains')
    if owner is None:
        raise Refused('A transfer requires a recorded owner')
    rows = list(sessions)
    rehash = proof_refresh_plan(db, rows)
    valid = set(rehash)
    for project in projects:
        project.owner = owner
    db.info['transfer_session_owner'] = owner
    try:
        for row in rows:
            row.owner = row.root_owner = owner
        db.flush()
    finally:
        db.info.pop('transfer_session_owner', None)
    changes = refresh_proofs(db, rows, rehash)
    db.add(AuditLog(actor=actor, action='session.owner.transfer', target=owner,
                    detail={'sessions': [row.id for row in rows], 'root_owner': owner,
                            'refreshed_proofs': sorted(valid),
                            'preserved_stale_proofs': [row.id for row in rows if row.parent_session_id and row.id not in valid],
                            'launch_digest_changes': changes, 'consent_review_required': bool(rows)}))
