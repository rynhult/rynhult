"""One scoped security resolver. OS/owner authority is never a configurable switch."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from .models import (
    AuditLog,
    Node,
    Project,
    SecurityChange,
    SecurityFolderPolicy,
    SecurityPolicy,
    SecurityRevision,
    Session,
    Setting,
)

REGISTRY_VERSION = 1
REGISTRY = {
    'force_push': 'Block force pushes', 'push_default': 'Block pushes to main or master',
    'merge_pr': 'Block agent pull-request merges', 'rm_outside': 'Block recursive deletes outside the workspace',
    'secrets': 'Block credential-file and environment reads', 'custom_rules': 'Apply custom command blocks',
    'allowed_folders': 'Limit account-service operations to allowed folders',
    'tool_install_check': 'Ask before account-wide tool changes',
    'provider_prompts': 'Ask before provider tool actions', 'budget_checks': 'Enforce saved usage budgets',
    'risk_labels': 'Show advisory risk labels',
}
BUILTINS = ('force_push', 'push_default', 'merge_pr', 'rm_outside', 'secrets')


class Conflict(ValueError):
    def __init__(self, message, code='policy_conflict'):
        super().__init__(message)
        self.code = code


def revision(db):
    row = db.get(SecurityRevision, 1)
    return row.revision if row else 0


def lock_revision(db):
    """Serialize short policy/launch-publication transactions on PostgreSQL too.

    Never keep this lock across broker awaits. SQLite serializes writes through
    its existing write-intent transaction; SELECT FOR UPDATE is ignored there.
    """
    db.scalar(select(SecurityRevision).where(SecurityRevision.id == 1).with_for_update()
              .execution_options(populate_existing=True))


def scope_key(scope):
    return json.dumps(scope, sort_keys=True, separators=(',', ':'))


def validate_scope(db, scope, *, default_account=None):
    if not isinstance(scope, dict):
        raise ValueError('Choose a security scope.')
    kind = scope.get('kind')
    fields = {'instance': {'kind'}, 'account': {'kind', 'machine', 'account'}, 'project': {'kind', 'project_id'}}
    if not isinstance(kind, str) or kind not in fields or set(scope) - {'machine_id'} != fields[kind] or ('machine_id' in scope and kind != 'account'):
        raise ValueError('Choose an instance, machine/account or project scope.')
    if kind == 'project':
        if type(scope['project_id']) is not int or db.get(Project, scope['project_id']) is None:
            raise ValueError('No such project.')
    if kind == 'account':
        machine, account = scope['machine'], scope['account']
        if not isinstance(machine, str) or not isinstance(account, str):
            raise ValueError('Choose a provisioned machine/account.')
        from .models import User
        known = db.scalar(select(Project.id).where(Project.host == machine, Project.account == account)) is not None
        if machine == 'local':
            known = known or account == default_account or db.scalar(select(User.id).where(User.account == account)) is not None
        else:
            node = db.scalar(select(Node).where(Node.name == machine, Node.status == 'active'))
            known = bool(node and account in ((node.info or {}).get('accounts') or []))
        if not known or account in ('root', 'acc', 'acc-node'):
            raise ValueError('Choose a provisioned agent account on this machine.')
    result = dict(scope)
    if kind == 'account':
        canonical = account_scope(db, machine, account)
        if 'machine_id' in scope and scope['machine_id'] != canonical['machine_id']:
            raise Conflict('The machine identity changed. Review a fresh preview.', 'identity_changed')
        result = canonical
    return result


def account_scope(db, machine, account):
    node_id = db.scalar(select(Node.id).where(Node.name == machine, Node.status == 'active')) if machine != 'local' else 'local'
    return {'kind': 'account', 'machine': machine, 'machine_id': node_id or 'unavailable', 'account': account}


def resolve(db, *, project_id=None, owner=None, machine=None, account=None, policy_override=None):
    project = db.get(Project, project_id) if project_id is not None else None
    if project:
        machine, account = machine or project.host or 'local', account or project.account
        owner = owner or project.owner
    switches = dict.fromkeys(REGISTRY, True)
    sources = {key: {'kind': 'default'} for key in REGISTRY}
    legacy = db.get(Setting, 'guardrails')
    for key, value in ((legacy.value.get('rules') or {}).items() if legacy else ()):
        if key in BUILTINS:
            switches[key] = bool(value)
            sources[key] = {'kind': 'legacy', 'key': 'guardrails'}
    for key in (f'tools.person.{owner}', f'tools.project.{project_id}'):
        row = db.get(Setting, key)
        if row and row.value.get('required') is False:
            switches['tool_install_check'] = False
            sources['tool_install_check'] = {'kind': 'legacy', 'key': key}
    scopes = [{'kind': 'instance'}]
    if machine and account:
        scopes.append(account_scope(db, machine, account))
    if project:
        scopes.append({'kind': 'project', 'project_id': project.id})
    for scope in scopes:
        row = db.get(SecurityPolicy, scope_key(scope))
        overrides = policy_override[1] if policy_override and scope_key(scope) == scope_key(policy_override[0]) else row.overrides if row else {}
        if overrides:
            for key, value in overrides.items():
                if key in REGISTRY:
                    switches[key], sources[key] = value, scope
    result = {'registry_version': REGISTRY_VERSION, 'revision': revision(db), 'switches': switches,
              'sources': sources, 'scope': scopes[-1]}
    result['mode'] = 'unrestricted' if not any(switches.values()) else 'custom' if not all(switches.values()) else 'default'
    result['policy_mode'] = result['mode']
    folders = db.get(SecurityFolderPolicy, scope_key(account_scope(db, machine, account))) if machine and account else None
    acknowledged = bool(folders and folders.state == 'applied' and folders.revision == folders.applied_revision
                        and folders.machine_id == account_scope(db, machine, account)['machine_id']
                        and folders.account == account
                        and folders.detail.get('allowed_folders') is switches['allowed_folders'])
    folder_pending = bool(folders and not acknowledged) or (not switches['allowed_folders'] and not acknowledged)
    folder_pending = folder_pending or bool(machine and account and access_pending(db, account_scope(db, machine, account)))
    result['enforcement'] = {'allowed_folders': 'pending' if folder_pending else 'applied'}
    if folder_pending:
        result['mode'] = 'custom'
    result['digest'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def validate_overrides(scope, overrides):
    if not isinstance(overrides, dict) or set(overrides) - REGISTRY.keys() or any(type(v) is not bool for v in overrides.values()):
        raise ValueError('Security overrides must be known switches with boolean values.')
    if 'allowed_folders' in overrides and scope['kind'] != 'account':
        raise ValueError('Allowed folders is a machine/account setting.')
    return dict(overrides)


def planned(db, scope, overrides, preset, *, default_account=None):
    scope = validate_scope(db, scope, default_account=default_account)
    overrides = validate_overrides(scope, overrides)
    row = db.get(SecurityPolicy, scope_key(scope))
    before = dict(row.overrides) if row else {}
    if preset == 'unrestricted':
        overrides = dict.fromkeys(REGISTRY, False)
        if scope['kind'] != 'account':
            overrides.pop('allowed_folders')
    elif preset == 'restore':
        if not row or row.previous is None:
            raise ValueError('No previous settings snapshot to restore.')
        overrides = dict(row.previous)
    elif preset != 'custom':
        raise ValueError('Choose custom, unrestricted or restore.')
    return scope, before, overrides


def save(db, scope, overrides, *, actor, expected_revision, preset='custom', default_account=None):
    with db.begin_nested():
        return _save(db, scope, overrides, actor=actor, expected_revision=expected_revision, preset=preset, default_account=default_account)


def _save(db, scope, overrides, *, actor, expected_revision, preset='custom', default_account=None):
    lock_revision(db)
    scope, before, after = planned(db, scope, overrides, preset, default_account=default_account)
    if revision(db) != expected_revision:
        raise Conflict('Security settings changed. Review a fresh preview.', 'stale_revision')
    old_folders = resolve(db, machine=scope.get('machine'), account=scope.get('account'))['switches']['allowed_folders'] if scope['kind'] == 'account' else None
    changed = db.execute(update(SecurityRevision).where(SecurityRevision.id == 1,
                         SecurityRevision.revision == expected_revision).values(revision=expected_revision + 1))
    if changed.rowcount != 1:
        raise Conflict('Security settings changed. Review a fresh preview.', 'stale_revision')
    db.expire_all()
    key = scope_key(scope)
    row = db.get(SecurityPolicy, key)
    previous = before if preset == 'unrestricted' else (row.previous if row else None)
    # Reapplying a preset must not erase the last real pre-preset snapshot.
    if preset == 'unrestricted' and row and row.previous is not None and before == after:
        previous = row.previous
    db.merge(SecurityPolicy(key=key, scope=scope, overrides=after, registry_version=REGISTRY_VERSION,
                            previous=previous))
    db.flush()
    if scope['kind'] == 'account':
        desired = resolve(db, machine=scope['machine'], account=scope['account'])['switches']['allowed_folders']
        folders = db.get(SecurityFolderPolicy, key)
        if desired != old_folders or (folders and folders.state == 'pending_policy'):
            if folders and folders.state in ('pending', 'recovering'):
                raise Conflict('A folder operation is pending. Confirm its outcome before changing Security.', 'target_pending')
            acknowledged = folders.detail.get('allowed_folders', True) if folders else True
            if desired is acknowledged:
                # Imported legacy Off may have no row at all. Do not add a
                # transient row just to delete it while restoring installer On.
                if folders is not None:
                    if folders.applied_revision == 0 and not folders.detail.get('operation_id'):
                        db.delete(folders)
                    else:
                        folders.state = 'applied'
                db.add(AuditLog(actor=actor, action='security.folders.unpublished_cancelled', target=key,
                    detail={'allowed_folders': desired, 'revision': expected_revision + 1}))
            else:
                if folders is None:
                    folders = SecurityFolderPolicy(key=key, machine_id=scope['machine_id'], account=scope['account'],
                        roots=[], revision=0, applied_revision=0, detail={})
                    db.add(folders)
                folders.state = 'pending_policy'
                folders.detail = {**folders.detail, 'policy_revision': expected_revision + 1}
                folders.updated_by = actor
            db.flush()
    older = older_sessions(db, scope)
    db.add(AuditLog(actor=actor, action='security.policy.applied', target=key,
                    detail={'before': before, 'after': after, 'revision': expected_revision + 1,
                            'registry_version': REGISTRY_VERSION, 'preset': preset,
                            'older_sessions': older, 'older_session_count': len(older)}))
    return expected_revision + 1


def validate_launch_inputs(inputs, effective):
    """Validate a permission ceiling without selecting a new provider mode."""
    if effective.get('enforcement', {}).get('allowed_folders') == 'pending':
        raise ValueError('Folder policy is pending target acknowledgement. Review Allowed folders in Security.')
    # Raw CLI/config overrides cannot be proved narrower than owner policy,
    # including mandatory read-only constraints when provider prompts are Off.
    if inputs.extra_args:
        raise ValueError('Security does not permit raw provider arguments; use validated launch options.')
    if effective['switches']['provider_prompts']:
        from acc import providers
        provider = providers.get(inputs.provider)
        if provider.dangerous(inputs):
            raise ValueError('Security requires provider prompts. An owner must review a scoped exception before using bypass options.')


def launch_inputs(inputs, effective):
    validate_launch_inputs(inputs, effective)
    if effective['switches']['provider_prompts']:
        return inputs
    from .agent_contract import read_only
    if read_only(inputs):
        return inputs  # Mandatory Prepare/reviewer/user read-only choice always wins.
    if inputs.provider == 'claude':
        return replace(inputs, permission_mode='bypassPermissions')
    if inputs.provider == 'codex':
        return replace(inputs, sandbox='danger-full-access', approval='never')
    raise ValueError('This provider does not support the Security bypass mapping.')


def frozen_protections(session):
    frozen = json.loads(session.launch_spec.files.get('guardrails.json', '{}'))
    old = frozen.get('security', {}).get('switches')
    if old is None:
        # Legacy sessions only had five hooks and account tool checks; provider
        # permissions remain their frozen inputs. Treat missing protections safely.
        old = {**dict.fromkeys(REGISTRY, True), **frozen.get('rules', {}),
               'tool_install_check': frozen.get('tool_approval', True)}
    return frozen, old


def on_older_settings(db, session, *, policy_override=None):
    """Compare the immutable launch ceiling with this execution binding, not its inspector."""
    if getattr(session, 'management', 'acc') != 'acc':
        return False
    frozen, old = frozen_protections(session)
    current = resolve(db, project_id=session.project_id, owner=session.owner,
                      machine=session.host, account=session.run_as, policy_override=policy_override)
    legacy = db.get(Setting, 'guardrails')
    patterns = (legacy.value.get('custom') or []) if legacy else []
    return weaker(session, frozen, old, current, patterns)


def weaker(session, frozen, old, current, patterns):
    return (saved_input_problem(session, old, current) is not None
            or any(current['switches'][key] and not old.get(key, False) for key in REGISTRY)
            or bool(current['switches']['custom_rules'] and frozen.get('custom', []) != patterns))


def older_sessions(db, scope, *, overrides=None):
    overlay = (scope, overrides) if overrides is not None else None
    legacy = db.get(Setting, 'guardrails')
    patterns = (legacy.value.get('custom') or []) if legacy else []
    bindings, older = {}, []
    for session in affected(db, scope, with_spec=True):
        if session.management != 'acc':
            continue
        binding = (session.project_id, session.owner, session.host, session.run_as)
        if binding not in bindings:
            bindings[binding] = resolve(db, project_id=session.project_id, owner=session.owner,
                machine=session.host, account=session.run_as, policy_override=overlay)
        frozen, old = frozen_protections(session)
        if weaker(session, frozen, old, bindings[binding], patterns):
            older.append(session.id)
    return sorted(older)


def saved_input_problem(session, old, current):
    from .compiler import LaunchInputs
    try:
        saved_inputs = dict(getattr(session.launch_spec, 'inputs', {}) or {})
        # Older persisted specs may omit the provider; the session still records
        # it. Validate that provider's defaults/options without mutating the spec.
        saved_inputs.setdefault('provider', getattr(session, 'provider', 'claude'))
        inputs = LaunchInputs.from_dict(saved_inputs)
        # Resuming keeps the saved mapping. Validate against both current and
        # frozen protections, without demanding today's weaker bypass mapping.
        # Pending target acknowledgement is a separate admission fence, not a
        # weaker frozen generation. Compare saved inputs against static ceilings.
        ceiling = {**current, 'enforcement': {}, 'switches': {**current['switches'],
                   'provider_prompts': current['switches']['provider_prompts'] or old.get('provider_prompts', True)}}
        validate_launch_inputs(inputs, ceiling)
    except (ValueError, TypeError):
        return 'Security refuses this saved provider generation. Start a new session with reviewed settings.'
    return None


def check_generation(db, session):
    """Never reuse a weaker frozen launch after tightening; never rewrite it on loosening."""
    frozen, old = frozen_protections(session)
    current = resolve(db, project_id=session.project_id, owner=session.owner,
                      machine=session.host, account=session.run_as)
    if current.get('enforcement', {}).get('allowed_folders') == 'pending':
        raise Conflict('Security refuses this saved provider generation while folder policy awaits target acknowledgement.')
    if problem := saved_input_problem(session, old, current):
        raise Conflict(problem)
    legacy = db.get(Setting, 'guardrails')
    patterns = (legacy.value.get('custom') or []) if legacy else []
    if current['switches']['custom_rules'] and frozen.get('custom', []) != patterns:
        raise Conflict('Security custom blocks changed. Start a new session with the reviewed settings.')
    if any(current['switches'][key] and not old.get(key, False) for key in REGISTRY):
        raise Conflict('Security protection increased. Start a new session with the reviewed settings instead of resuming this generation.')


def affected(db, scope, *, with_spec=False):
    query = select(Session).where((Session.desired_state == 'running') | Session.team_stop_pending
                                  | Session.multirun_stop_pending | Session.last_error.startswith('stop not confirmed:')
                                  | Session.observed_state.not_in(('stopped', 'exited', 'failed', 'archived')))
    if with_spec:
        query = query.options(selectinload(Session.launch_spec))
    if scope['kind'] == 'project':
        query = query.where(Session.project_id == scope['project_id'])
    elif scope['kind'] == 'account':
        query = query.where(Session.run_as == scope['account'])
    rows = list(db.scalars(query.options(selectinload(Session.placement), selectinload(Session.project))))
    return [s for s in rows if s.host == scope['machine']] if scope['kind'] == 'account' else rows


def inspect_policy(db, *, project_id=None, machine=None, account=None, default_account=None):
    """Read the execution binding, never the inspector's person exemption."""
    if project_id is not None or not (machine and account):
        return resolve(db, project_id=project_id, machine=machine, account=account)
    from .models import User
    owners = set(db.scalars(select(Project.owner).where(Project.host == machine, Project.account == account)))
    # A person's agent-account binding applies on nodes too; it does not
    # require a project to have been created on that machine yet.
    users = select(User.username).where((User.account == account) | (User.account.is_(None) if account == default_account else False))
    owners.update(db.scalars(users))
    # None is a real legacy ownerless subject, with no person's exception.
    # Keep it as None (not the username 'None') and sort deterministically.
    variants = [resolve(db, owner=owner, machine=machine, account=account)
                for owner in sorted(owners, key=lambda owner: (owner is not None, owner or ''))]
    result = variants[0] if variants else resolve(db, machine=machine, account=account)
    if len({r['switches']['tool_install_check'] for r in variants}) > 1:
        result['switches']['tool_install_check'] = None
        result['sources']['tool_install_check'] = {'kind': 'varies'}
        result['varying_switches'] = ['tool_install_check']
        result['mode'] = result['policy_mode'] = 'custom'
        result['digest'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def folder_revision(db, machine, account):
    """Admission fence; the target separately enforces the acknowledged revision."""
    from .broker_client import BrokerError
    coordinates = account_scope(db, machine, account)
    if access_pending(db, coordinates):
        raise BrokerError('Folder access is pending or needs repair. Ask an owner to check the grant in Security.')
    key = scope_key(coordinates)
    row = db.get(SecurityFolderPolicy, key)
    saved = db.get(SecurityPolicy, key)
    allowed = saved.overrides.get('allowed_folders', True) if saved else True
    if row is None:
        if not allowed:
            raise BrokerError('Folder policy is pending target application. Ask an owner to apply it in Security.')
        return None  # unchanged legacy configured roots, not a claim of new-helper qualification
    if (row.state != 'applied' or row.revision != row.applied_revision
            or row.detail.get('allowed_folders') is not allowed):
        raise BrokerError('Folder policy is pending target application. Ask an owner to review it in Security.')
    return row.applied_revision


def access_pending(db, coordinates):
    return db.scalar(select(SecurityChange.id).where(
        SecurityChange.applied.is_(True),
        SecurityChange.payload['lane'].as_string() == 'access',
        SecurityChange.payload['access_key'].as_string() == scope_key(coordinates),
        SecurityChange.payload['access_state'].as_string().in_(('pending', 'partial', 'repair'))).limit(1)) is not None


def clear_stop_marker(db, session, *, actor, reason):
    if ((session.last_error or '').startswith('stop not confirmed:')
            or session.team_stop_pending or session.multirun_stop_pending):
        db.add(AuditLog(actor=actor, action='security.session.stop_marker_cleared', target=session.id,
                        detail={'reason': reason, 'machine': session.host}))
        session.last_error = None
        session.team_stop_pending = session.multirun_stop_pending = False
